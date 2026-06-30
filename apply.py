"""
Completes LinkedIn Easy Apply submissions using the saved session.
"""
import argparse
import os
import random
import re
import subprocess
import time
import tomllib
from pathlib import Path
from playwright.sync_api import sync_playwright
from lib.auth import auth_state
from lib.browser import human_click, open_linkedin_context, wait_for_page_full_load

_MODULE_DIR = Path(__file__).parent
CONFIG_FILE = _MODULE_DIR / "config.toml"


def _load_config() -> dict:
    with open(CONFIG_FILE, "rb") as f:
        return tomllib.load(f)


def _fill_known_fields(page, personal: dict):
    field_map = [
        (["firstName", "first-name", "first_name"],   personal.get("firstName", "")),
        (["lastName",  "last-name",  "last_name"],    personal.get("lastName", "")),
        (["email", "emailAddress"],                   personal.get("email", "")),
        (["phoneNumber", "phone", "mobilePhone"],     personal.get("phone", "")),
        (["city", "location"],                        personal.get("city", "")),
    ]
    for ids, value in field_map:
        if not value:
            continue
        for hint in ids:
            for sel in [
                f'input[id*="{hint}" i]',
                f'input[name*="{hint}" i]',
                f'input[autocomplete*="{hint}" i]',
            ]:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=400) and not el.input_value():
                        el.fill(value)
                        break
                except Exception:
                    continue


def _get_modal_handle(page):
    for sel in ['div.jobs-easy-apply-modal', '[role="dialog"]']:
        try:
            handle = page.query_selector(sel)
            if handle:
                return handle
        except Exception:
            continue
    return None


def _fill_radio_buttons(page) -> int:
    filled = 0
    modal  = _get_modal_handle(page)
    if not modal:
        return 0
    try:
        groups = modal.query_selector_all('fieldset, div[role="radiogroup"]')
        for group in groups:
            try:
                radios = group.query_selector_all('input[type="radio"]')
                if not radios:
                    continue
                if any(r.is_checked() for r in radios):
                    continue

                target = None
                for r in radios:
                    val = (r.get_attribute("value") or "").lower().strip()
                    if val in ("yes", "true", "sí", "si"):
                        target = r
                        break
                if not target:
                    target = radios[0]

                label_text = ""
                r_id = target.get_attribute("id") or ""
                if r_id:
                    lbl = modal.query_selector(f'label[for="{r_id}"]')
                    if lbl:
                        label_text = lbl.inner_text().strip()

                try:
                    target.click()
                except Exception:
                    target.click(force=True)
                filled += 1
                print(f"  ✓ Radio: '{label_text or r_id}'")
            except Exception:
                continue
    except Exception:
        pass
    return filled


def _has_unhandled_required(page) -> bool:
    try:
        empties = page.evaluate("""
            () => {
                const inputs = document.querySelectorAll(
                    'input[required]:not([type="hidden"]):not([type="file"]):not([type="checkbox"]):not([type="radio"])'
                );
                return Array.from(inputs)
                    .filter(i => !i.value && i.offsetParent !== null)
                    .map(i => i.name || i.id || 'unknown');
            }
        """)
        if empties:
            print(f"  WARN: empty required fields: {empties}")
            return True

        textareas = page.evaluate("""
            () => Array.from(document.querySelectorAll('textarea[required]'))
                .filter(t => !t.value && t.offsetParent !== null)
                .map(t => t.name || t.id || 'textarea')
        """)
        if textareas:
            print(f"  WARN: empty required textareas: {textareas}")
            return True

        bad_selects = page.evaluate("""
            () => {
                const placeholders = ['select an option', 'selecciona una opción',
                    'seleciona una opción', 'select', 'choose an option',
                    'seleccione una opción', ''];
                return Array.from(document.querySelectorAll('select[required]'))
                    .filter(s => {
                        const v = (s.value || '').trim().toLowerCase();
                        return placeholders.includes(v) && s.offsetParent !== null;
                    })
                    .map(s => s.id || s.name || 'select');
            }
        """)
        if bad_selects:
            print(f"  WARN: required selects without answer: {bad_selects}")
            return True

        return False
    except Exception:
        return False


def _normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _cv_match_terms(cv_path: str) -> list[str]:
    name       = os.path.basename(cv_path)
    stem       = os.path.splitext(name)[0]
    normalized = _normalize_text(stem)
    parts      = [p.strip() for p in normalized.split() if p.strip()]
    terms      = [normalized]
    if len(parts) >= 3:
        terms.append(" ".join(parts[:-1]))
    if len(parts) >= 2:
        terms.append(" ".join(parts[:2]))
    return list(dict.fromkeys(t for t in terms if t))


def _modal_text(page) -> str:
    try:
        text = page.locator(".jobs-easy-apply-modal").first.inner_text(timeout=700)
    except Exception:
        try:
            text = page.locator('[role="dialog"]').first.inner_text(timeout=700)
        except Exception:
            text = ""
    return _normalize_text(text)


def _has_expected_cv(page, cv_path: str) -> bool:
    text = _modal_text(page)
    return any(term in text for term in _cv_match_terms(cv_path))


def _expand_cv_list(page):
    for text in ["Mostrar", "Show more", "Ver más"]:
        try:
            btn = page.locator(f'button:has-text("{text}"), span:has-text("{text}")').first
            if btn.is_visible(timeout=500):
                btn.click()
                time.sleep(0.5)
                break
        except Exception:
            continue


def _select_existing_cv_radio(page, cv_path: str) -> bool:
    terms = _cv_match_terms(cv_path)
    _expand_cv_list(page)
    modal = _get_modal_handle(page)
    scope = modal or page
    try:
        radios = scope.query_selector_all('input[type="radio"]')
        for radio in radios:
            try:
                r_id       = radio.get_attribute("id") or ""
                label_text = ""
                if r_id:
                    lbl = scope.query_selector(f'label[for="{r_id}"]')
                    if lbl:
                        label_text = _normalize_text(lbl.inner_text())
                if any(term in label_text for term in terms):
                    if not radio.is_checked():
                        try:
                            radio.click()
                        except Exception:
                            radio.click(force=True)
                        time.sleep(0.5)
                    print(f"  ✓ CV selected (radio): {os.path.basename(cv_path)}")
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _upload_cv_if_possible(page, cv_path: str) -> bool:
    if not cv_path or not os.path.isfile(cv_path):
        print(f"  WARN: CV not found: {cv_path}")
        return False

    modal = _get_modal_handle(page)
    scope = modal or page

    for btn_text in [
        "Subir currículum", "Subir curriculum",
        "Upload resume", "Cargar currículum", "Cargar curriculum",
        "Reemplazar", "Replace", "Cambiar", "Change",
    ]:
        try:
            btn = scope.query_selector(f'button:text("{btn_text}")')
            if btn and btn.is_visible():
                btn.click()
                time.sleep(0.5)
                break
        except Exception:
            continue

    uploaded = False
    try:
        file_input = scope.query_selector('input[type="file"]')
        if file_input:
            file_input.set_input_files(cv_path)
            uploaded = True
            time.sleep(1.5)
            print(f"  ✓ CV uploaded: {os.path.basename(cv_path)}")
    except Exception:
        pass

    if uploaded:
        time.sleep(1)
        if _select_existing_cv_radio(page, cv_path):
            return True
        return _has_expected_cv(page, cv_path)
    return False


def _resume_controls_present(page) -> bool:
    try:
        if page.locator('input[type="file"]').count() > 0:
            return True
    except Exception:
        pass
    text = _modal_text(page)
    return any(term in text for term in ["resume", "curriculum", "curriculum vitae", "curr culum"])


def _ensure_expected_cv(page, cv_path: str) -> bool:
    if _has_expected_cv(page, cv_path):
        print(f"  ✓ CV selected: {os.path.basename(cv_path)}")
        return True
    if _select_existing_cv_radio(page, cv_path):
        return True
    if _upload_cv_if_possible(page, cv_path):
        return True
    print(f"  → Could not verify/upload expected CV: {os.path.basename(cv_path)}")
    return False


_llm_session_cache: dict[str, str] = {}


def _llm_answer(label: str, job_title: str, cv_path: str, profile: dict,
                options: list[str] | None = None) -> str:
    """
    Calls Claude CLI (claude -p) to answer form questions not covered by keyword matching.
    Requires Claude Code to be installed. Uses your Claude Code subscription — no API key needed.
    Disable with llm_fallback = false in config.toml.
    """
    cache_key = label.strip().lower()
    if cache_key in _llm_session_cache:
        return _llm_session_cache[cache_key]
    try:
        cv_name   = os.path.basename(cv_path)
        opts_line = f"\nOptions: {', '.join(options)}" if options else ""
        prompt = (
            "You are filling out a LinkedIn Easy Apply form.\n"
            f"Candidate: {profile.get('name', 'the candidate')}\n"
            f"Experience: {profile.get('years_of_experience', '')} years\n"
            f"Stack: {profile.get('stack', '')}\n"
            f"Location: {profile.get('location', '')}\n"
            f"Languages: {profile.get('languages', '')}\n"
            f"Job: {job_title}\nCV: {cv_name}\n\n"
            f"Question: {label}{opts_line}\n\n"
            "Reply ONLY with the exact value (integer, or the exact option from the list). "
            "No explanation, no punctuation."
        )
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            answer = result.stdout.strip()
            _llm_session_cache[cache_key] = answer
            print(f"  ✓ LLM '{label[:50]}': {answer}")
            return answer
    except Exception as e:
        print(f"  WARN: LLM fallback failed: {e}")
    return ""


_KEYWORD_RULES: list[tuple[list[str], str]] = [
    (["years of experience", "años de experiencia", "how many years", "cuántos años",
      "years experience", "experience do you have", "engineering experience"],
     "years_of_experience"),
    (["work authorization", "authorized to work", "legally authorized",
      "right to work", "autorizado para trabajar", "eligible to work"],
     "work_authorization"),
    (["visa sponsorship", "require sponsorship", "need sponsorship", "sponsorship"],
     "sponsorship_required"),
    (["willing to relocate", "open to relocate", "able to relocate", "reubicarse"],
     "willing_to_relocate"),
    (["commut", "comfortable working onsite", "travel to the office", "presencial",
      "comfortable to this job"],
     "willing_to_commute"),
    (["notice period", "when can you start", "availability to start",
      "how soon can you", "periodo de aviso", "disponibilidad para empezar"],
     "notice_period_days"),
    (["english proficiency", "level of english", "proficiency in english",
      "english level", "nivel de inglés", "nivel inglés"],
     "english_proficiency"),
    (["education", "degree", "highest level of education",
      "nivel de educación", "nivel educativo"],
     "education_level"),
    (["currently employed", "are you currently", "actualmente empleado"],
     "currently_employed"),
    (["previously worked", "worked for this company", "former employee",
      "trabajado anteriormente"],
     "previously_worked_here"),
]


def _answer_question(question_text: str, form_answers: dict) -> str:
    q = question_text.lower()
    for keywords, key in _KEYWORD_RULES:
        if any(kw in q for kw in keywords):
            val = form_answers.get(key, "")
            if val:
                return str(val)
    return ""


def _get_element_label(modal, element) -> str:
    try:
        el_id = element.get_attribute("id") or ""
        if el_id:
            lbl = modal.query_selector(f'label[for="{el_id}"]')
            if lbl:
                return lbl.inner_text().strip()
        aria = element.get_attribute("aria-label") or ""
        if aria:
            return aria.strip()
        placeholder = element.get_attribute("placeholder") or ""
        if placeholder:
            return placeholder.strip()
        legend_text = element.evaluate(
            "el => { const fs = el.closest('fieldset'); "
            "return fs && fs.querySelector('legend') ? fs.querySelector('legend').innerText.trim() : ''; }"
        )
        return legend_text or ""
    except Exception:
        return ""


def _fill_text_questions(page, form_answers: dict, job_title: str = "", cv_path: str = "",
                         profile: dict | None = None, llm_fallback: bool = True):
    modal  = _get_modal_handle(page)
    scope  = modal or page
    try:
        inputs = scope.query_selector_all('input[type="text"], input[type="number"], textarea')
        for inp in inputs:
            try:
                if inp.input_value():
                    continue
                label = _get_element_label(modal or page, inp) if modal else _get_element_label(page, inp)
                if not label:
                    continue
                answer = _answer_question(label, form_answers)
                if not answer and llm_fallback and (job_title or cv_path):
                    answer = _llm_answer(label, job_title, cv_path, profile or {})
                if answer:
                    inp.click()
                    inp.type(str(answer), delay=random.randint(40, 120))
                    print(f"  ✓ Field '{label[:45]}': {answer}")
            except Exception:
                continue
    except Exception:
        pass


_SELECT_PLACEHOLDERS = {
    "select an option", "selecciona una opción", "seleciona una opción",
    "select", "none", "", "choose an option", "seleccione una opción",
}


def _fill_dropdowns(page, form_answers: dict, job_title: str = "", cv_path: str = "",
                    profile: dict | None = None, llm_fallback: bool = True):
    modal = _get_modal_handle(page)
    scope = modal or page
    try:
        selects = scope.query_selector_all("select")
        for sel_el in selects:
            try:
                current = (sel_el.input_value() or "").strip().lower()
                if current and current not in _SELECT_PLACEHOLDERS:
                    continue

                label = _get_element_label(modal or page, sel_el) if modal else _get_element_label(page, sel_el)

                options = sel_el.query_selector_all("option")
                valid_opts = []
                for opt in options:
                    v = (opt.get_attribute("value") or opt.inner_text().strip())
                    if v.lower() not in _SELECT_PLACEHOLDERS:
                        valid_opts.append((v, opt.inner_text().strip()))

                if not valid_opts:
                    continue

                answer  = _answer_question(label, form_answers) if label else ""
                matched = False
                if answer:
                    for opt_val, opt_text in valid_opts:
                        if answer.lower() in opt_text.lower() or opt_text.lower() in answer.lower():
                            sel_el.select_option(value=opt_val)
                            print(f"  ✓ Dropdown '{(label or '?')[:45]}': {opt_text}")
                            matched = True
                            break

                if matched:
                    continue

                is_yes_no = any(v.lower() in ("yes", "no", "sí", "si") for v, _ in valid_opts)

                if is_yes_no:
                    for opt_val, opt_text in valid_opts:
                        if opt_val.lower() in ("yes", "sí", "si"):
                            sel_el.select_option(value=opt_val)
                            print(f"  ✓ Dropdown '{(label or '?')[:45]}': {opt_text} (default Yes)")
                            break
                    continue

                if label and llm_fallback and (job_title or cv_path):
                    opt_texts  = [t for _, t in valid_opts]
                    llm_answer = _llm_answer(label, job_title, cv_path, profile or {}, options=opt_texts)
                    if llm_answer:
                        for opt_val, opt_text in valid_opts:
                            if llm_answer.lower() in opt_text.lower() or opt_text.lower() in llm_answer.lower():
                                sel_el.select_option(value=opt_val)
                                print(f"  ✓ Dropdown LLM '{(label or '?')[:45]}': {opt_text}")
                                break
            except Exception:
                continue
    except Exception:
        pass


def apply_easy_apply(job_url: str, cv_path: str, job_title: str = "", dry_run: bool = False) -> str:
    """
    Attempts to complete an Easy Apply on LinkedIn.

    Returns:
        "applied"  — submitted successfully
        "ready"    — dry-run: ready to submit but not sent
        "manual"   — requires manual intervention
        "error"    — unexpected failure
    """
    config       = _load_config()
    personal     = config.get("personal_data", {})
    form_answers = config.get("form_answers", {})
    profile      = config.get("profile", {})
    llm_fallback = config.get("llm_fallback", True)

    screenshots_dir = config.get("screenshots_dir", str(_MODULE_DIR))

    with sync_playwright() as p:
        context, browser = open_linkedin_context(p, headless=True, config=config)
        page = context.new_page()

        try:
            page.goto(job_url, wait_until="domcontentloaded", timeout=20000)
            wait_for_page_full_load(page, timeout=20000)
            time.sleep(random.uniform(2, 3))

            state = auth_state(page)
            if state != "ok":
                print(f"  ERROR: Session expired ({state})")
                browser.close()
                return "error"

            apply_btn = None
            for sel in [
                '#jobs-apply-button-id',
                '[aria-label="Solicitud sencilla"]',
                '[aria-label="Easy Apply"]',
                '[aria-label*="Solicitar fácilmente"]',
                'button:has-text("Solicitud sencilla")',
                'button:has-text("Solicitar fácilmente")',
                'button:has-text("Easy Apply")',
                'a:has-text("Solicitud sencilla")',
                'a:has-text("Easy Apply")',
            ]:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=2000):
                        apply_btn = btn
                        break
                except Exception:
                    continue

            if not apply_btn:
                print("  → Not Easy Apply")
                browser.close()
                return "manual"

            human_click(apply_btn)
            time.sleep(random.uniform(1.5, 2.5))

            state = auth_state(page)
            if state != "ok":
                print(f"  ERROR: Session expired ({state})")
                browser.close()
                return "error"

            try:
                page.wait_for_selector(
                    '.jobs-easy-apply-modal, [aria-label*="Solicitar"], [aria-label*="Apply"]',
                    timeout=5000,
                )
            except Exception:
                print("  → Modal did not appear")
                browser.close()
                return "manual"

            last_nav_btn = None
            for step in range(25):
                print(f"  Step {step + 1}...")
                if last_nav_btn in ("Revisar", "Review"):
                    time.sleep(random.uniform(3.0, 4.5))
                else:
                    time.sleep(random.uniform(0.8, 1.4))

                modal_gone = not page.locator('.jobs-easy-apply-modal').is_visible(timeout=500)
                if modal_gone:
                    if last_nav_btn in ("Revisar", "Review"):
                        print(f"  ✓ Submitted (modal closed after Review): {job_title or job_url}")
                        page.screenshot(path=os.path.join(screenshots_dir, "linkedin_after_submit.png"))
                        browser.close()
                        return "applied"
                    success_text = page.evaluate("() => document.body.innerText.toLowerCase()")
                    if any(kw in success_text for kw in [
                        "solicitud enviada", "application submitted", "you applied",
                        "te has postulado", "aplicaste", "gracias por postularte",
                        "thank you for applying", "successfully applied",
                    ]):
                        print(f"  ✓ Submitted (modal closed): {job_title or job_url}")
                        page.screenshot(path=os.path.join(screenshots_dir, "linkedin_after_submit.png"))
                        browser.close()
                        return "applied"
                    page.screenshot(path=os.path.join(screenshots_dir, "linkedin_modal_gone.png"))
                    print("  DEBUG: modal closed, no success confirmation")
                    browser.close()
                    return "manual"

                _fill_known_fields(page, personal)
                _fill_text_questions(page, form_answers, job_title=job_title, cv_path=cv_path,
                                     profile=profile, llm_fallback=llm_fallback)
                _fill_dropdowns(page, form_answers, job_title=job_title, cv_path=cv_path,
                                profile=profile, llm_fallback=llm_fallback)
                _fill_radio_buttons(page)
                time.sleep(0.5)

                if _has_unhandled_required(page):
                    print("  → Form requires manual intervention")
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
                    browser.close()
                    return "manual"

                submitted     = False
                submit_labels = ["Enviar solicitud", "Submit application", "Enviar", "Submit"]

                if last_nav_btn in ("Revisar", "Review"):
                    page.screenshot(path=os.path.join(screenshots_dir, "linkedin_review_page.png"))
                    if not dry_run:
                        js_clicked = page.evaluate("""
                            () => {
                                const targets = ['enviar solicitud', 'submit application', 'enviar', 'submit'];
                                for (const el of document.querySelectorAll('button, [role="button"]')) {
                                    const txt = (el.innerText || el.textContent || '').trim().toLowerCase();
                                    if (targets.some(t => txt.includes(t))) {
                                        el.scrollIntoView({ block: 'center' });
                                        el.click();
                                        return true;
                                    }
                                }
                                return false;
                            }
                        """)
                        if js_clicked:
                            time.sleep(2)
                            if not page.locator('.jobs-easy-apply-modal').is_visible(timeout=2000):
                                print(f"  ✓ Submitted (review JS): {job_title or job_url}")
                                page.screenshot(path=os.path.join(screenshots_dir, "linkedin_after_submit.png"))
                                browser.close()
                                return "applied"

                for btn_text in submit_labels:
                    try:
                        btn = page.locator(f'button:has-text("{btn_text}")').last
                        if last_nav_btn in ("Revisar", "Review") and btn.count() > 0:
                            try:
                                btn.scroll_into_view_if_needed(timeout=1500)
                                time.sleep(0.2)
                            except Exception:
                                pass
                        if btn.is_visible(timeout=500):
                            if not _ensure_expected_cv(page, cv_path):
                                print("  → Requires manual CV review")
                                try:
                                    page.keyboard.press("Escape")
                                except Exception:
                                    pass
                                browser.close()
                                return "manual"
                            page.screenshot(path=os.path.join(screenshots_dir, "linkedin_before_submit.png"))
                            if dry_run:
                                print(f"  ✓ Dry-run ready to submit: {job_title or job_url}")
                                try:
                                    page.keyboard.press("Escape")
                                except Exception:
                                    pass
                                browser.close()
                                return "ready"
                            btn.click()
                            time.sleep(2)
                            print(f"  ✓ Submitted: {job_title or job_url}")
                            page.screenshot(path=os.path.join(screenshots_dir, "linkedin_after_submit.png"))
                            submitted = True
                            break
                    except Exception:
                        continue

                if submitted:
                    browser.close()
                    return "applied"

                nav_buttons = ["Siguiente", "Next", "Continuar", "Continue", "Revisar", "Review"]
                if last_nav_btn in ("Revisar", "Review"):
                    still_has_form = page.locator("input[required], select[required]").count() > 0
                    if not still_has_form:
                        nav_buttons = ["Siguiente", "Next", "Continuar", "Continue"]

                advanced = False
                for btn_text in nav_buttons:
                    try:
                        btn = page.locator(f'button:has-text("{btn_text}")').last
                        if btn.is_visible(timeout=500):
                            if _resume_controls_present(page) and not _ensure_expected_cv(page, cv_path):
                                print("  → Requires manual CV review")
                                try:
                                    page.keyboard.press("Escape")
                                except Exception:
                                    pass
                                browser.close()
                                return "manual"
                            print(f"    → Advancing with '{btn_text}'")
                            btn.click()
                            last_nav_btn = btn_text
                            advanced = True
                            break
                    except Exception:
                        continue

                if not advanced:
                    print("  → Could not advance")
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
                    browser.close()
                    return "manual"

            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            browser.close()
            return "manual"

        except KeyboardInterrupt:
            try:
                browser.close()
            except Exception:
                pass
            raise
        except Exception as e:
            print(f"  Unexpected error: {e}")
            try:
                browser.close()
            except Exception:
                pass
            return "error"


def parse_args():
    parser = argparse.ArgumentParser(description="LinkedIn Easy Apply a single job")
    parser.add_argument("job_url")
    parser.add_argument("cv_path")
    parser.add_argument("--title", default="")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args   = parse_args()
    result = apply_easy_apply(
        job_url=args.job_url,
        cv_path=args.cv_path,
        job_title=args.title,
        dry_run=args.dry_run,
    )
    print(result)
