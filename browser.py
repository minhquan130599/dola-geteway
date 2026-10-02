"""Patchright persistent context launcher and per-account cookie importer."""
import re
from pathlib import Path

import config

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
]

_COOKIE_ATTRIBUTES = {
    "domain", "path", "expires", "max-age", "samesite", "secure",
    "httponly", "priority",
}
_ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _cookie_file_path() -> Path:
    """Returns the configured cookie file path."""
    return Path(config.COOKIES_FILE).expanduser()


def _read_cookie_map() -> dict[str, str]:
    """Reads ``profile|cookie-header`` entries from cookies.txt.

    Cookie values are deliberately not included in exceptions or log messages.
    """
    path = _cookie_file_path()
    if not path.exists():
        return {}

    result: dict[str, str] = {}
    for line_no, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            raise ValueError(
                f"{path} line {line_no}: expected profile|name=value; name=value"
            )
        profile, header = (part.strip() for part in line.split("|", 1))
        if not _ACCOUNT_NAME_RE.fullmatch(profile):
            raise ValueError(
                f"{path} line {line_no}: invalid profile name {profile!r}"
            )
        if not header:
            raise ValueError(f"{path} line {line_no}: cookie header is empty")
        if profile in result:
            raise ValueError(f"{path} line {line_no}: duplicate profile {profile!r}")
        result[profile] = header
    return result


def cookie_profile_names() -> list[str]:
    """Returns profile names declared in the cookie file."""
    return list(_read_cookie_map())


def _parse_cookie_header(header: str) -> list[dict]:
    """Converts a browser Cookie header to Playwright cookie objects."""
    cookies = []
    for part in header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = (item.strip() for item in part.split("=", 1))
        if not name or name.startswith("$") or name.lower() in _COOKIE_ATTRIBUTES:
            continue
        cookies.append({
            "name": name,
            "value": value,
            "domain": ".dola.com",
            "path": "/",
            "secure": True,
        })
    return cookies


async def apply_account_cookies(context, account: str) -> bool:
    """Imports the cookie-file entry for ``account`` into a browser profile.

    Returns True when an entry was found. Imported cookies are persisted by the
    persistent browser context and are available to all later page requests.
    """
    header = _read_cookie_map().get(account)
    if header is None:
        return False
    cookies = _parse_cookie_header(header)
    names = {cookie["name"] for cookie in cookies}
    if "sessionid" not in names:
        raise ValueError(
            f"Cookie entry for profile {account!r} must include sessionid"
        )
    await context.add_cookies(cookies)
    return True


async def launch_account_context(p, account: str, headless: bool = None, use_extension: bool = False):
    """Launches accounts/<account> profile, returns BrowserContext. Caller must close.

    p: async_playwright() instance
    headless: None = uses config.HEADLESS
    """
    profile_dir = Path("accounts") / account
    if not profile_dir.exists():
        raise FileNotFoundError(
            f"Account profile does not exist: {profile_dir} (run python add_account.py {account} first)"
        )
    launch_headless = config.HEADLESS if headless is None else headless
    args = list(LAUNCH_ARGS)
    if use_extension:
        if not config.EXTENSION_ENABLED:
            raise RuntimeError("Dola extension is disabled (DOLA_EXTENSION_ENABLED=0)")
        extension_dir = Path(config.EXTENSION_DIR).resolve()
        if not extension_dir.exists():
            raise FileNotFoundError(f"Dola extension directory does not exist: {extension_dir}")
        # Chromium debugger extension requires headed window to intercept skill/action-bar responses
        launch_headless = False
        args.extend([
            f"--disable-extensions-except={extension_dir}",
            f"--load-extension={extension_dir}",
        ])
    kwargs = {
        "headless": launch_headless,
        "args": args,
        "locale": "ja-JP",
        "timezone_id": "Asia/Tokyo",
    }
    if config.PROXY:
        kwargs["proxy"] = {"server": config.PROXY}
    context = await p.chromium.launch_persistent_context(str(profile_dir), **kwargs)
    try:
        await apply_account_cookies(context, account)
    except Exception:
        await context.close()
        raise
    return context


def cookie_value(cookies: list, name: str) -> str:
    """Extracts cookie value from context.cookies() result."""
    return next((c["value"] for c in cookies if c["name"] == name and c["value"]), "")


async def check_login_state(account: str) -> bool:
    """Opens Dola in headless mode and checks whether session is active."""
    from patchright.async_api import async_playwright
    async with async_playwright() as p:
        context = await launch_account_context(p, account)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            cookies = await context.cookies("https://www.dola.com")
            if not cookie_value(cookies, "sessionid"):
                return False
            return bool(await page.evaluate(
                """() => !!(document.querySelector('textarea')
                        || document.querySelector('[contenteditable="true"]')
                        || document.querySelector('input[type="text"]'))"""
            ))
        finally:
            await context.close()
