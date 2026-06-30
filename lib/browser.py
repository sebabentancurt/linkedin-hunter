"""
Playwright helpers for LinkedIn.

We prefer a persistent Chromium profile over copied cookies because LinkedIn
ties part of the session to the browser profile, and cookie exports from other
browsers may not be fully equivalent in Playwright.
"""
import os
import random
import time
from pathlib import Path

from .auth import load_session_cookies

try:
    from playwright_stealth import Stealth as _Stealth
    _STEALTH = _Stealth(
        init_scripts_only=True,
        navigator_languages_override=("es-419", "es", "en-US", "en"),
        navigator_platform_override="Win32",
    )
    _STEALTH_AVAILABLE = True
except ImportError:
    _STEALTH_AVAILABLE = False

_MODULE_DIR = Path(__file__).parent.parent

SESSION_FILE  = _MODULE_DIR / "linkedin_session.json"
USER_DATA_DIR = _MODULE_DIR / "linkedin_user_data"

_VIEWPORT_POOL = [
    (1366, 768),
    (1440, 900),
    (1536, 864),
    (1600, 900),
    (1920, 1080),
]


def _random_viewport() -> dict:
    w, h = random.choice(_VIEWPORT_POOL)
    return {"width": w + random.randint(-3, 3), "height": h + random.randint(-3, 3)}


def _apply_stealth(context):
    if _STEALTH_AVAILABLE:
        try:
            _STEALTH.apply_stealth_sync(context)
        except Exception:
            pass


def random_sleep(min_s: float = 0.5, max_s: float = 1.5):
    u = random.random()
    wait = min_s + (u ** 1.4) * (max_s - min_s)
    time.sleep(wait)
    return wait


def wait_for_page_full_load(page, selector: str | None = None, timeout: int = 45000):
    page.wait_for_load_state("domcontentloaded", timeout=timeout)
    try:
        page.wait_for_load_state("load", timeout=timeout)
    except Exception:
        pass
    if selector:
        try:
            page.wait_for_selector(selector, timeout=timeout)
        except Exception:
            pass
    random_sleep(1.0, 2.5)


def human_click(locator, timeout: int = 30000):
    random_sleep(0.3, 1.0)
    locator.wait_for(state="visible", timeout=timeout)
    try:
        locator.click()
    except Exception:
        locator.click(force=True)
    random_sleep(0.3, 1.0)


def human_type(locator, text: str, timeout: int = 10000):
    """Types text character by character to simulate human typing speed."""
    locator.wait_for(state="visible", timeout=timeout)
    locator.click()
    time.sleep(random.uniform(0.1, 0.3))
    for char in text:
        locator.press(char)
        time.sleep(random.uniform(0.04, 0.14))
    random_sleep(0.2, 0.5)


def open_linkedin_context(playwright, headless: bool = True, config: dict | None = None):
    config = config or {}
    session_mode = config.get("session_mode", "persistent")
    proxy_url    = config.get("proxy")
    proxy_cfg    = {"server": proxy_url} if proxy_url else None

    if session_mode in ("public", "cookies"):
        browser = playwright.chromium.launch(headless=headless, proxy=proxy_cfg)
        context = browser.new_context(
            viewport=_random_viewport(),
            locale="es-419",
            proxy=proxy_cfg,
        )
        if session_mode == "cookies":
            session_path = config.get("session_file", str(SESSION_FILE))
            context.add_cookies(load_session_cookies(session_path))
        _apply_stealth(context)
        return context, browser

    user_data_dir = config.get("user_data_dir", str(USER_DATA_DIR))
    os.makedirs(user_data_dir, exist_ok=True)
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=user_data_dir,
        headless=headless,
        viewport=_random_viewport(),
        locale="es-419",
        args=["--disable-blink-features=AutomationControlled"],
        proxy=proxy_cfg,
    )
    _apply_stealth(context)
    return context, context
