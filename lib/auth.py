"""
Session detection helpers for LinkedIn.
"""

import json


SAMESITE_MAP = {
    "no_restriction": "None",
    "unspecified": "Lax",
    "lax": "Lax",
    "strict": "Strict",
    "none": "None",
}


def load_session_cookies(path: str) -> list[dict]:
    with open(path) as f:
        raw_cookies = json.load(f)

    cookies = []
    for raw in raw_cookies:
        domain = raw.get("domain")
        cookie = {
            "name": raw.get("name"),
            "value": raw.get("value", ""),
            "path": raw.get("path", "/"),
            "httpOnly": bool(raw.get("httpOnly", False)),
            "secure": bool(raw.get("secure", False)),
        }
        cookie["domain"] = domain

        expires = raw.get("expires", raw.get("expirationDate"))
        if expires is not None:
            cookie["expires"] = float(expires)

        same_site = raw.get("sameSite")
        if same_site:
            cookie["sameSite"] = SAMESITE_MAP.get(str(same_site).lower(), "Lax")

        if cookie["name"] and (cookie.get("domain") or cookie.get("url")):
            cookies.append(cookie)

    return cookies


def auth_state(page) -> str:
    """
    Returns:
      - "ok":       page accessible with valid session
      - "login":    login required
      - "authwall": LinkedIn public authwall
    """
    url = (page.url or "").lower()
    if "authwall" in url:
        return "authwall"
    if "/login" in url or "/uas/login" in url or "session_redirect=" in url:
        return "login"

    try:
        login_signals = page.locator(
            'input[name="session_key"], '
            'input[name="session_password"], '
            'form[action*="/login"], '
            'button:has-text("Iniciar sesión"), '
            'button:has-text("Sign in")'
        )
        for i in range(min(login_signals.count(), 5)):
            try:
                if login_signals.nth(i).is_visible(timeout=300):
                    return "login"
            except Exception:
                continue
    except Exception:
        pass

    try:
        authwall_signals = page.locator(
            '.authwall, '
            '[class*="authwall"], '
            'text=/Sign in to view|Inicia sesión para ver|Join LinkedIn/i'
        )
        for i in range(min(authwall_signals.count(), 5)):
            try:
                if authwall_signals.nth(i).is_visible(timeout=300):
                    return "authwall"
            except Exception:
                continue
    except Exception:
        pass

    return "ok"


def is_authenticated(page) -> bool:
    return auth_state(page) == "ok"
