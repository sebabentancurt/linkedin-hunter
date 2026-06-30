"""
Opens a visible Chromium window so you can log in to LinkedIn manually.
Saves the session as a persistent profile (default) or as a cookie file.

Usage:
    python3 save_session.py           # persistent profile (recommended)
    python3 save_session.py --cookies # save linkedin_session.json instead
"""
import argparse
import json
import time
import tomllib
from pathlib import Path
from playwright.sync_api import sync_playwright

_MODULE_DIR   = Path(__file__).parent
CONFIG_FILE   = _MODULE_DIR / "config.toml"
SESSION_FILE  = _MODULE_DIR / "linkedin_session.json"
USER_DATA_DIR = _MODULE_DIR / "linkedin_user_data"


def _load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "rb") as f:
            return tomllib.load(f)
    return {}


def save_persistent(headless: bool = False):
    """Launch persistent context and wait for the user to log in."""
    config     = _load_config()
    proxy_url  = config.get("proxy")
    proxy_cfg  = {"server": proxy_url} if proxy_url else None

    USER_DATA_DIR.mkdir(exist_ok=True)
    print("Opening LinkedIn in a visible browser window...")
    print("Log in manually, then press Enter here to save the session.\n")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=headless,
            viewport={"width": 1280, "height": 800},
            locale="es-419",
            args=["--disable-blink-features=AutomationControlled"],
            proxy=proxy_cfg,
        )
        page = context.new_page()
        page.goto("https://www.linkedin.com/login")

        input("Press Enter once you are logged in...")
        context.close()

    print(f"Session saved to: {USER_DATA_DIR}")
    print("You can now run: python3 daily.py")


def save_cookies(headless: bool = False):
    """Launch a temporary browser and save cookies to linkedin_session.json."""
    config    = _load_config()
    proxy_url = config.get("proxy")
    proxy_cfg = {"server": proxy_url} if proxy_url else None

    print("Opening LinkedIn in a visible browser window...")
    print("Log in manually, then press Enter here to save the cookies.\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, proxy=proxy_cfg)
        context = browser.new_context(viewport={"width": 1280, "height": 800}, proxy=proxy_cfg)
        page    = context.new_page()
        page.goto("https://www.linkedin.com/login")

        input("Press Enter once you are logged in...")

        cookies = context.cookies()
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies, f, indent=2, ensure_ascii=False)

        browser.close()

    print(f"Session saved to: {SESSION_FILE}")
    print('Set session_mode = "cookies" in config.toml, then run: python3 daily.py')


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cookies", action="store_true",
                        help="Save as cookie file instead of persistent profile")
    args = parser.parse_args()

    if args.cookies:
        save_cookies()
    else:
        save_persistent()
