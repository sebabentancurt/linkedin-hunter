"""
Quickly verifies whether your LinkedIn session is still valid.

Usage:
    python3 check_session.py
"""
import sys
import tomllib
from pathlib import Path
from playwright.sync_api import sync_playwright
from lib.auth import auth_state
from lib.browser import open_linkedin_context

_MODULE_DIR = Path(__file__).parent
CONFIG_FILE = _MODULE_DIR / "config.toml"


def main():
    if not CONFIG_FILE.exists():
        print("ERROR: config.toml not found.")
        sys.exit(1)

    with open(CONFIG_FILE, "rb") as f:
        config = tomllib.load(f)

    session_mode = config.get("session_mode", "persistent")

    if session_mode == "public":
        print("session_mode = 'public' — no session to check.")
        sys.exit(0)

    if session_mode == "cookies":
        session_file = _MODULE_DIR / "linkedin_session.json"
        if not session_file.exists():
            print(f"ERROR: {session_file} not found. Run: python3 save_session.py --cookies")
            sys.exit(1)
    elif session_mode == "persistent":
        user_data_dir = _MODULE_DIR / "linkedin_user_data"
        if not user_data_dir.exists():
            print(f"ERROR: {user_data_dir} not found. Run: python3 save_session.py")
            sys.exit(1)

    print(f"Checking session (mode: {session_mode})...")

    with sync_playwright() as p:
        context, browser = open_linkedin_context(p, headless=True, config=config)
        page = context.new_page()
        try:
            page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=20000)
            state = auth_state(page)
        except Exception as e:
            print(f"ERROR: Could not load LinkedIn: {e}")
            sys.exit(1)
        finally:
            try:
                browser.close()
            except Exception:
                pass

    if state == "ok":
        print("Session OK — ready to run daily.py")
        sys.exit(0)
    else:
        print(f"Session expired ({state}). Run: python3 save_session.py")
        sys.exit(1)


if __name__ == "__main__":
    main()
