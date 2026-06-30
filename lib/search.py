"""
Searches LinkedIn for jobs and scores them according to config preferences.
"""
import json
import logging
import os
import random
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from playwright.sync_api import sync_playwright
from tqdm import tqdm

from .auth import auth_state
from .browser import open_linkedin_context, wait_for_page_full_load

_MODULE_DIR = Path(__file__).parent.parent
SEEN_FILE   = _MODULE_DIR / "linkedin_seen.json"

sys.path.insert(0, str(_MODULE_DIR))

log = logging.getLogger("linkedin")


@contextmanager
def _tqdm_logging(logger_name: str):
    """Redirects the console handler to tqdm.write() to avoid breaking progress bars."""
    logger = logging.getLogger(logger_name)
    original = next(
        (h for h in logger.handlers
         if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)),
        None,
    )
    if original is None:
        yield
        return

    class _TqdmHandler(logging.StreamHandler):
        def emit(self, record):
            try:
                tqdm.write(self.format(record), file=original.stream, end="\n")
            except Exception:
                self.handleError(record)

    tqdm_h = _TqdmHandler(original.stream)
    tqdm_h.setLevel(original.level)
    tqdm_h.setFormatter(original.formatter)

    logger.removeHandler(original)
    logger.addHandler(tqdm_h)
    try:
        yield
    finally:
        logger.removeHandler(tqdm_h)
        logger.addHandler(original)


DEFAULT_MAX_REMOTE_QUERIES      = 3
DEFAULT_SEARCH_TIME_WINDOW_SECONDS = 604800

DEFAULT_TIMING = {
    "page_load_delay":              [1.2, 2.4],
    "description_delay":            [1.0, 2.0],
    "between_queries_delay":        [2.5, 5.0],
    "feed_warmup_delay":            [2.0, 3.5],
    "scroll_step_delay":            [0.3, 0.7],
    "description_after_fetch_delay": [0.8, 1.8],
}

# LinkedIn uses an "occludable" system: li[data-occludable-job-id] contains
# ALL job IDs for the page, including those not yet rendered (empty li).
_EXTRACT_CARDS_JS = """
    () => {
        // 1. Rendered cards (visible or display:none but with content)
        const rendered = {};
        document.querySelectorAll('div.job-card-container[data-job-id]').forEach(card => {
            const jobId = card.dataset.jobId;
            if (!jobId) return;
            const linkEl = card.querySelector('a[href*="/jobs/view/"]');
            const link = linkEl ? linkEl.href.split('?')[0] : '';
            const titleEl = card.querySelector(
                '.job-card-list__title, .artdeco-entity-lockup__title, '
                + 'h3.base-search-card__title, strong'
            );
            const titleRaw = titleEl ? titleEl.innerText.trim() : '';
            const title = titleRaw.split('\\n')[0].trim();
            const compEl = card.querySelector(
                '.job-card-container__primary-description, .artdeco-entity-lockup__subtitle, '
                + '.job-card-list__company-name, h4.base-search-card__subtitle, '
                + 'a[class*="hidden-nested-link"]'
            );
            const company = compEl ? compEl.innerText.trim().split('\\n')[0] : '';
            const locEl = card.querySelector(
                '.job-card-container__metadata-item, .artdeco-entity-lockup__caption, '
                + 'span.job-search-card__location'
            );
            const location = locEl ? locEl.innerText.trim().split('\\n')[0] : '';
            const text = card.innerText || '';
            const isEasyApply = /Solicitar f.cilmente|Solicitud sencilla|Easy Apply/i.test(text);
            rendered[jobId] = { jobId, title, link, company, location, workType: '', isEasyApply, stub: false };
        });

        // 2. All IDs from the occludable system (includes not-yet-rendered ones)
        const results = [];
        const seen = new Set();
        document.querySelectorAll('li[data-occludable-job-id]').forEach(li => {
            const jobId = li.dataset.occludableJobId;
            if (!jobId || seen.has(jobId)) return;
            seen.add(jobId);
            if (rendered[jobId]) {
                results.push(rendered[jobId]);
            } else {
                // Stub: empty li, only the ID is available. Will be visited to get details.
                results.push({
                    jobId,
                    title: '',
                    link: 'https://www.linkedin.com/jobs/view/' + jobId + '/',
                    company: '',
                    location: '',
                    workType: '',
                    isEasyApply: false,
                    stub: true,
                });
            }
        });

        // Fallback for public UI (no occludable system)
        if (!results.length) {
            document.querySelectorAll('div.base-card').forEach(card => {
                const jobId = (card.dataset.entityUrn || '').split(':').pop() || '';
                if (!jobId) return;
                const linkEl = card.querySelector('a[href*="/jobs/view/"]');
                const link = linkEl ? linkEl.href.split('?')[0] : '';
                const titleEl = card.querySelector('h3.base-search-card__title, strong');
                const title = titleEl ? titleEl.innerText.trim().split('\\n')[0] : '';
                const compEl = card.querySelector('h4.base-search-card__subtitle, a[class*="hidden-nested-link"]');
                const company = compEl ? compEl.innerText.trim().split('\\n')[0] : '';
                const locEl = card.querySelector('span.job-search-card__location');
                const location = locEl ? locEl.innerText.trim().split('\\n')[0] : '';
                const isEasyApply = /Easy Apply/i.test(card.innerText || '');
                results.push({ jobId, title, link, company, location, workType: '', isEasyApply, stub: false });
            });
        }

        return results.filter(j => j.jobId);
    }
"""


def _timing(config: dict, key: str) -> tuple[float, float]:
    value = config.get("timing", {}).get(key, DEFAULT_TIMING[key])
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        value = DEFAULT_TIMING[key]
    low, high = float(value[0]), float(value[1])
    return (low, high) if low <= high else (high, low)


def _sleep(config: dict, key: str):
    low, high = _timing(config, key)
    time.sleep(random.uniform(low, high))


def _posted_within_seconds(config: dict) -> int:
    value = config.get("posted_within_seconds", DEFAULT_SEARCH_TIME_WINDOW_SECONDS)
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = DEFAULT_SEARCH_TIME_WINDOW_SECONDS
    return max(3600, value)


def load_seen() -> set:
    if SEEN_FILE.exists():
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen_ids: set):
    with tempfile.NamedTemporaryFile("w", dir=str(_MODULE_DIR), delete=False, suffix=".tmp") as f:
        json.dump(sorted(seen_ids), f, indent=2)
        tmp = f.name
    os.replace(tmp, str(SEEN_FILE))


def select_cv(config: dict, job_text: str = "", lang: str = "en") -> str:
    """
    Returns the path to the most appropriate CV file.

    Config supports:
      [cv]
      path = "resume.pdf"          # single CV — always use this
      dir  = "resumes/"            # directory — rule-based selection
    """
    cv_cfg = config.get("cv", {})

    cv_path = cv_cfg.get("path", "")
    if cv_path:
        p = Path(cv_path)
        if not p.is_absolute():
            p = _MODULE_DIR / p
        return str(p) if p.is_file() else ""

    cv_dir = cv_cfg.get("dir", "")
    if not cv_dir:
        return ""

    cv_dir_path = Path(cv_dir) if Path(cv_dir).is_absolute() else _MODULE_DIR / cv_dir
    if not cv_dir_path.is_dir():
        return ""

    rules = config.get("cv_rules", [])
    normalized = job_text.lower()
    lang_suffix = lang.lower()

    for rule in rules:
        filename = rule.get("file", "")
        keywords = [str(k).lower() for k in rule.get("keywords", [])]
        if not filename or not keywords:
            continue
        if any(re.search(r"\b" + re.escape(kw) + r"\b", normalized) for kw in keywords):
            candidates = [
                cv_dir_path / filename,
                cv_dir_path / f"{Path(filename).stem}_{lang_suffix}{Path(filename).suffix}",
                cv_dir_path / f"{Path(filename).stem}-{lang_suffix}{Path(filename).suffix}",
            ]
            for c in candidates:
                if c.is_file():
                    return str(c)

    default_cv = cv_cfg.get("default", "")
    if default_cv:
        p = cv_dir_path / default_cv
        if p.is_file():
            return str(p)

    # Last resort: first PDF in the directory
    pdfs = sorted(cv_dir_path.glob("*.pdf"))
    return str(pdfs[0]) if pdfs else ""


def score_job(title: str, description: str, location: str, config: dict) -> int:
    title_l = title.lower()
    desc_l  = description.lower()
    score   = 0
    for kw, pts in config.get("stack_keywords", {}).items():
        pattern = re.compile(r"\b" + re.escape(kw.lower()) + r"\b")
        if pattern.search(title_l):
            score += pts * 2  # title counts double
        elif pattern.search(desc_l):
            score += pts

    local_locations = [loc.lower() for loc in config.get("local_location_names", [])]
    if local_locations and any(loc in location.lower() for loc in local_locations):
        score += int(config.get("local_bonus", 0))

    return score


def _normalize_company(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _radar_company_match(company: str, config: dict) -> str:
    company_norm = _normalize_company(company)
    if not company_norm:
        return ""
    for radar in config.get("radar_companies", []):
        radar_norm = _normalize_company(str(radar))
        if radar_norm and (radar_norm in company_norm or company_norm in radar_norm):
            return str(radar)
    return ""


def _company_match(company: str, companies: list) -> str:
    company_norm = _normalize_company(company)
    if not company_norm:
        return ""
    for item in companies:
        item_norm = _normalize_company(str(item))
        if item_norm and (item_norm in company_norm or company_norm in item_norm):
            return str(item)
    return ""


def _keyword_matches(title: str, description: str, keywords: list) -> list[str]:
    text = f"{title} {description}".lower()
    return [str(k) for k in keywords if str(k).strip().lower() and str(k).strip().lower() in text]


def detect_lang(location: str, title: str) -> str:
    if any(c in title for c in "áéíóúñüÁÉÍÓÚÑÜ"):
        return "es"
    _SPANISH_TITLE_WORDS = frozenset({
        "lider", "tecnico", "desarrollador", "arquitecto", "gerente", "jefe",
        "analista", "responsable", "ingeniero", "ingenieria", "soluciones",
        "tecnologia", "direccion", "coordinador", "especialista",
    })
    words = set(re.findall(r"\b\w+\b", title.lower()))
    if words & _SPANISH_TITLE_WORDS:
        return "es"
    return "en"


def _extract_page_jobs(page, config: dict) -> list:
    try:
        page.wait_for_selector(
            "li[data-occludable-job-id], div.job-card-container, div.base-card",
            timeout=10000,
        )
    except Exception:
        return []
    return page.evaluate(_EXTRACT_CARDS_JS)


def _get_description(page, job_url: str, config: dict) -> str:
    if not job_url:
        return ""
    try:
        page.goto(job_url, wait_until="domcontentloaded", timeout=15000)
        _sleep(config, "description_delay")
        if auth_state(page) != "ok" and config.get("session_mode", "persistent") == "public":
            return ""
        desc = page.evaluate("""
            () => {
                const selectors = [
                    '#job-details',
                    '.jobs-description__content',
                    '.jobs-box__html-content',
                    'div[class*="jobs-description"]',
                    'div.show-more-less-html__markup',
                    '[class*="description__text"]',
                    'div[class*="description"] section',
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.innerText.trim()) return el.innerText.trim();
                }
                return '';
            }
        """)
        return desc[:3000]
    except Exception:
        return ""


def _populate_stub(page, job: dict):
    """For stubs (empty li), extracts title/company/location/isEasyApply from the detail page."""
    try:
        details = page.evaluate("""
            () => {
                const titleEl = document.querySelector(
                    'h1.t-24, h1.jobs-unified-top-card__job-title, '
                    + 'h1[class*="job-title"], .job-details-jobs-unified-top-card__job-title h1'
                );
                const title = titleEl ? titleEl.innerText.trim().split('\\n')[0] : '';
                const compEl = document.querySelector(
                    '.jobs-unified-top-card__company-name a, '
                    + '.job-details-jobs-unified-top-card__company-name a, '
                    + '[class*="top-card"] a[href*="/company/"]'
                );
                const company = compEl ? compEl.innerText.trim() : '';
                const locEl = document.querySelector(
                    '.jobs-unified-top-card__bullet, '
                    + '.job-details-jobs-unified-top-card__primary-description-without-tagline span'
                );
                const location = locEl ? locEl.innerText.trim().split('\\n')[0] : '';
                const isEasyApply = /Solicitar f.cilmente|Easy Apply/i.test(document.body.innerText || '');
                return { title, company, location, isEasyApply };
            }
        """)
        if details.get("title"):
            job["title"] = details["title"]
        if details.get("company"):
            job["company"] = details["company"]
        if details.get("location"):
            job["location"] = details["location"]
        job["isEasyApply"] = details.get("isEasyApply", False)
        job["stub"] = False
    except Exception:
        pass


def _fetch_descriptions(page, new_jobs: list, config: dict):
    threshold = config.get("score_threshold", 5)
    for job in new_jobs:
        is_stub = job.get("stub", False)
        pre = score_job(job.get("title", ""), "", job.get("location", ""), config)
        if is_stub or pre >= threshold - 3:
            job["description"] = _get_description(page, job.get("link", ""), config)
            if is_stub:
                _populate_stub(page, job)
            _sleep(config, "description_after_fetch_delay")
        else:
            job["description"] = ""


def _scrape_recommended(page, seen: set, all_jobs: dict, config: dict):
    log.info("Scraping LinkedIn Recommended feed")
    base_url   = "https://www.linkedin.com/jobs/collections/recommended/"
    REC_PAGE_SIZE = 24
    max_rec    = int(config.get("max_recommended_jobs", 60))
    max_pages  = (max_rec + REC_PAGE_SIZE - 1) // REC_PAGE_SIZE
    collected: dict = {}
    pages_loaded = 0

    try:
        pbar = tqdm(range(max_pages), desc="  recommended", unit="page", leave=False, ncols=90, position=1)
        for page_num in pbar:
            url = base_url if page_num == 0 else f"{base_url}?start={page_num * REC_PAGE_SIZE}"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                break
            _sleep(config, "page_load_delay")

            try:
                page.wait_for_selector("li[data-occludable-job-id], div.job-card-container", timeout=10000)
            except Exception:
                if page_num == 0:
                    log.warning("No recommended jobs loaded")
                break

            page_jobs = page.evaluate(_EXTRACT_CARDS_JS)
            if not page_jobs:
                break
            for job in page_jobs:
                collected[job["jobId"]] = job
            pages_loaded += 1
            pbar.set_postfix(found=len(collected))

            if len(collected) >= max_rec:
                break

            _sleep(config, "page_load_delay")

        raw      = list(collected.values())[:max_rec]
        new_jobs = [j for j in raw if j["jobId"] not in seen and j["jobId"] not in all_jobs]
        for job in new_jobs:
            all_jobs[job["jobId"]] = job
        stubs = sum(1 for j in raw if j.get("stub"))
        log.info(f"  recommended: {len(raw)} found ({len(raw)-stubs} rendered + {stubs} stubs, {pages_loaded}p/{max_pages}), {len(new_jobs)} new")
        _fetch_descriptions(page, new_jobs, config)
    except Exception as e:
        log.warning(f"Could not scrape recommended: {e}")


def _run_search(page, query: str, location: str | None, remote: bool,
                seen: set, all_jobs: dict, config: dict, max_jobs: int | None = None) -> bool:
    posted_within = _posted_within_seconds(config)
    url = (
        f"https://www.linkedin.com/jobs/search/"
        f"?keywords={query.replace(' ', '%20')}"
        f"&f_TPR=r{posted_within}&sortBy=DD"
    )
    if location:
        geo_id   = config.get("geo_id")
        distance = config.get("distance_km")
        if geo_id:
            url += f"&geoId={geo_id}"
        else:
            url += f"&location={location.replace(' ', '%20')}"
        if distance:
            url += f"&distance={distance}"
        url += "&f_WT=1%2C2%2C3"
    if remote:
        url += "&f_WT=2"
    if config.get("easy_apply_only"):
        url += "&f_AL=true"

    tag = f"{query}" + (f" in {location}" if location else " (remote)")
    log.info(tag)
    log.debug(f"  URL p1: {url}")

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
    except Exception:
        log.warning(f"Timeout loading page: {tag}")
        return False

    _sleep(config, "page_load_delay")

    if config.get("session_mode", "persistent") != "public":
        state = auth_state(page)
        if state != "ok":
            raise RuntimeError(f"LinkedIn session expired ({state}). Run save_session.py.")

    is_public = config.get("session_mode", "persistent") == "public"
    max_pages  = int(config.get("max_pages_per_query", 3))
    seen_this_query: set = set()
    raw_all: list = []

    pages_loaded = 0
    next_start   = 0
    pbar = tqdm(range(max_pages), desc=f"  {tag}", unit="page", leave=False, ncols=90, position=1)
    for page_num in pbar:
        if page_num > 0:
            if is_public:
                prev_count = len(seen_this_query)
                for _ in range(8):
                    page.mouse.wheel(0, 600)
                    time.sleep(random.uniform(0.15, 0.30))
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(random.uniform(0.4, 0.8))
                try:
                    page.wait_for_function(
                        f"() => document.querySelectorAll('div.base-card, li[data-occludable-job-id]').length > {prev_count}",
                        timeout=8000,
                    )
                except Exception:
                    _sleep(config, "page_load_delay")
            else:
                paged_url = url + f"&start={next_start}"
                log.debug(f"  URL p{page_num+1}: {paged_url}")
                try:
                    page.goto(paged_url, wait_until="domcontentloaded", timeout=25000)
                except Exception:
                    log.warning(f"Timeout on page {page_num + 1}")
                    break
                _sleep(config, "page_load_delay")

        page_raw = _extract_page_jobs(page, config)
        if not is_public:
            next_start += len(page_raw)
        pages_loaded += 1
        new_from_page = [j for j in page_raw if j["jobId"] not in seen_this_query]
        for j in new_from_page:
            seen_this_query.add(j["jobId"])
        raw_all.extend(new_from_page)

        stubs_p       = sum(1 for j in page_raw if j.get("stub"))
        globally_new_p = sum(1 for j in page_raw if j["jobId"] not in seen)
        log.debug(f"  p{page_num+1}/{max_pages}: {len(page_raw)} jobs ({len(page_raw)-stubs_p} rendered + {stubs_p} stubs) — {globally_new_p} globally new")
        pbar.set_postfix(found=len(raw_all), new=globally_new_p)

        if not new_from_page:
            log.debug(f"  p{page_num+1}: no new jobs in this query, stopping")
            break

    new_jobs = []
    for job in raw_all:
        if max_jobs and len(all_jobs) >= max_jobs:
            break
        if job["jobId"] not in seen and job["jobId"] not in all_jobs:
            all_jobs[job["jobId"]] = job
            new_jobs.append(job)

    stubs    = sum(1 for j in raw_all if j.get("stub"))
    rendered = len(raw_all) - stubs
    log.info(f"  {len(raw_all)} found ({rendered} rendered + {stubs} stubs, {pages_loaded}p/{max_pages}), {len(new_jobs)} new")

    _fetch_descriptions(page, new_jobs, config)

    return bool(max_jobs and len(all_jobs) >= max_jobs)


def search_linkedin(config: dict, max_jobs: int | None = None) -> list:
    """Searches LinkedIn. Returns list sorted by score descending."""
    queries_local  = config.get("search_queries_local", [])
    queries_remote = config.get("search_queries_remote", [])
    local_location = config.get("local_location")

    session_file = config.get("session_file", str(_MODULE_DIR / "linkedin_session.json"))
    if config.get("session_mode", "persistent") == "cookies" and not Path(session_file).exists():
        print("ERROR: linkedin_session.json not found.")
        return []

    seen     = set() if config.get("include_seen") else load_seen()
    all_jobs: dict = {}

    max_local_queries = config.get("max_local_queries")
    if max_local_queries is not None:
        queries_local = queries_local[:int(max_local_queries)]

    max_remote_queries = config.get("max_remote_queries", DEFAULT_MAX_REMOTE_QUERIES)
    remote_limited     = queries_remote[:max_remote_queries]

    queries_local  = random.sample(queries_local, len(queries_local))
    remote_limited = random.sample(remote_limited, len(remote_limited))

    is_public = config.get("session_mode", "persistent") == "public"

    with sync_playwright() as p:
        context, browser = open_linkedin_context(p, headless=True, config=config)
        page = context.new_page()

        _BLOCK_TYPES = {"image", "media", "font", "stylesheet"}
        page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in _BLOCK_TYPES
            else route.continue_(),
        )

        if not is_public:
            try:
                page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=15000)
                wait_for_page_full_load(page, timeout=15000)
                _sleep(config, "feed_warmup_delay")
            except Exception as exc:
                raise RuntimeError(f"Could not validate LinkedIn session: {exc}")

            state = auth_state(page)
            if state != "ok":
                raise RuntimeError(f"LinkedIn session expired ({state}). Run save_session.py.")

        def _flush_seen():
            if not config.get("include_seen"):
                seen.update(all_jobs.keys())
                save_seen(seen)

        total_queries = len(queries_local) + len(remote_limited)
        gbar = tqdm(total=total_queries, desc="Queries", unit="q", position=0, ncols=90,
                    bar_format="{desc}: {percentage:3.0f}%|{bar}| {n}/{total} [{elapsed}, {postfix}]")
        _search_start = time.time()

        def _gbar_update(query: str):
            gbar.update(1)
            done = gbar.n
            if done > 0:
                secs_left = (time.time() - _search_start) / done * (total_queries - done)
                if secs_left >= 3600:
                    eta = f"~{int(secs_left // 3600)}h{int((secs_left % 3600) // 60)}m"
                elif secs_left >= 60:
                    eta = f"~{int(secs_left // 60)}m"
                else:
                    eta = f"~{int(secs_left)}s"
            else:
                eta = "?"
            gbar.set_postfix_str(f"eta {eta}  jobs={len(all_jobs)}  {query[:28]}")

        try:
            with _tqdm_logging("linkedin"):
                if not is_public and config.get("include_recommended", True):
                    _scrape_recommended(page, seen, all_jobs, config)
                    _flush_seen()
                    _sleep(config, "between_queries_delay")

                for query in queries_local:
                    if max_jobs and len(all_jobs) >= max_jobs:
                        break
                    reached_limit = _run_search(
                        page, query, local_location, False, seen, all_jobs, config, max_jobs
                    )
                    _flush_seen()
                    _gbar_update(query)
                    if reached_limit:
                        break
                    _sleep(config, "between_queries_delay")

                if not max_jobs or len(all_jobs) < max_jobs:
                    for query in remote_limited:
                        reached_limit = _run_search(page, query, None, True, seen, all_jobs, config, max_jobs)
                        _flush_seen()
                        _gbar_update(query)
                        if reached_limit:
                            break
                        _sleep(config, "between_queries_delay")

            gbar.close()

        except RuntimeError as e:
            gbar.close()
            print(f"ERROR: {e}")
            browser.close()
            return []

        browser.close()

    results = []
    for job in all_jobs.values():
        if not job.get("title"):
            continue
        score = score_job(
            job.get("title", ""),
            job.get("description", ""),
            job.get("location", ""),
            config,
        )
        radar_match          = _radar_company_match(job.get("company", ""), config)
        avoid_company_match  = _company_match(job.get("company", ""), config.get("avoid_companies", []))
        if avoid_company_match:
            continue
        preferred_matches    = _keyword_matches(
            job.get("title", ""), job.get("description", ""), config.get("preferred_keywords", []),
        )
        avoid_keyword_matches = _keyword_matches(
            job.get("title", ""), job.get("description", ""), config.get("avoid_keywords", []),
        )
        if radar_match:
            score += int(config.get("radar_company_bonus", 0))
        if preferred_matches:
            score += int(config.get("preferred_keyword_bonus", 0)) * len(preferred_matches)
        if avoid_keyword_matches:
            score -= int(config.get("avoid_keyword_penalty", 0)) * len(avoid_keyword_matches)

        lang    = detect_lang(job.get("location", ""), job.get("title", ""))
        cv_text = f"{job.get('title', '')} {job.get('description', '')}"
        cv_path = select_cv(config, cv_text, lang)

        results.append({
            **job,
            "score":                    score,
            "lang":                     lang,
            "cv_path":                  cv_path,
            "radar_company":            bool(radar_match),
            "radar_match":              radar_match,
            "preferred_keyword_matches": preferred_matches,
            "avoid_keyword_matches":    avoid_keyword_matches,
        })

    results.sort(key=lambda j: j["score"], reverse=True)
    return results
