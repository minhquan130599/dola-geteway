import json
"""Patchright persistent context launcher and per-account cookie importer."""
import re
from pathlib import Path

import config

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",
    "--disable-features=PasswordSaveConfirmation,PasswordLeakDetection,Translate,OptimizationGuideModelDownloading",
    "--disable-save-password-bubble",
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



def configure_clean_profile(profile_dir: Path):
    """Configures Chrome profile Preferences to disable password prompts, translate, etc."""
    default_dir = Path(profile_dir) / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    prefs_file = default_dir / "Preferences"
    prefs = {}
    if prefs_file.exists():
        try:
            prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        except Exception:
            prefs = {}
    prefs["credentials_enable_service"] = False
    profile_dict = prefs.setdefault("profile", {})
    profile_dict["password_manager_enabled"] = False
    profile_dict["password_manager_leak_detection"] = False
    profile_dict.setdefault("default_content_setting_values", {})["notifications"] = 2
    autofill_dict = prefs.setdefault("autofill", {})
    autofill_dict["profile_enabled"] = False
    autofill_dict["credit_card_enabled"] = False
    translate_dict = prefs.setdefault("translate", {})
    translate_dict["enabled"] = False

    # Route all manual and extension downloads directly to project downloads directory
    project_download_dir = str(Path(config.DOWNLOAD_DIR).resolve())
    Path(project_download_dir).mkdir(parents=True, exist_ok=True)
    prefs["download"] = {
        "default_directory": project_download_dir,
        "prompt_for_download": False,
        "directory_upgrade": True,
    }
    prefs["savefile"] = {
        "default_directory": project_download_dir,
    }
    prefs_file.write_text(json.dumps(prefs, indent=2), encoding="utf-8")

def parse_proxy(raw: str | None) -> dict | None:
    """Parses various proxy formats into Playwright proxy dict.
    
    Supported formats:
    - host:port
    - http://host:port
    - socks5://host:port
    - host:port:user:pass
    - user:pass@host:port
    - http://user:pass@host:port
    - socks5://user:pass@host:port
    """
    if not raw or not raw.strip():
        return None
    raw = raw.strip()
    scheme = "http"
    if "://" in raw:
        scheme, remainder = raw.split("://", 1)
        scheme = scheme.lower()
    else:
        remainder = raw

    if "@" in remainder:
        auth_part, host_part = remainder.split("@", 1)
        if ":" in auth_part:
            u, p = auth_part.split(":", 1)
        else:
            u, p = auth_part, ""
        return {"server": f"{scheme}://{host_part}", "username": u, "password": p}

    parts = remainder.split(":")
    if len(parts) == 4:
        host, port, u, p = parts
        return {"server": f"{scheme}://{host}:{port}", "username": u, "password": p}
    elif len(parts) == 2:
        host, port = parts
        return {"server": f"{scheme}://{host}:{port}"}

    return {"server": f"{scheme}://{remainder}"}


async def launch_account_context(p, account: str, headless: bool = None, use_extension: bool = False, proxy: str | None = None):
    """Launches accounts/<account> profile, returns BrowserContext. Caller must close.

    p: async_playwright() instance
    headless: None = uses config.HEADLESS
    proxy: None = uses account proxy from DB, then config.PROXY
    """
    profile_dir = Path("accounts") / account
    configure_clean_profile(profile_dir)
    if not profile_dir.exists():
        raise FileNotFoundError(
            f"Account profile does not exist: {profile_dir} (run python add_account.py {account} first)"
        )
    launch_headless = config.HEADLESS if headless is None else headless
    args = list(LAUNCH_ARGS)
    pw_headless = False
    if launch_headless:
        # Chrome's new headless mode supports extensions and background debugger
        args.append("--headless=new")
        pw_headless = False
    else:
        pw_headless = False

    if use_extension:
        if not config.EXTENSION_ENABLED:
            raise RuntimeError("Dola extension is disabled (DOLA_EXTENSION_ENABLED=0)")
        extension_dir = Path(config.EXTENSION_DIR).resolve()
        if not extension_dir.exists():
            raise FileNotFoundError(f"Dola extension directory does not exist: {extension_dir}")
        args.extend([
            f"--disable-extensions-except={extension_dir}",
            f"--load-extension={extension_dir}",
        ])
    project_download_dir = str(Path(config.DOWNLOAD_DIR).resolve())
    Path(project_download_dir).mkdir(parents=True, exist_ok=True)
    kwargs = {
        "headless": pw_headless,
        "args": args,
        "locale": config.LOCALE,
        "timezone_id": config.TIMEZONE,
        "accept_downloads": True,
        "downloads_path": project_download_dir,
    }

    # Proxy selection: explicit arg > account proxy in DB > global config.PROXY
    proxy_to_use = proxy
    if not proxy_to_use:
        try:
            import sqlite3
            with sqlite3.connect("pool_usage.db") as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT proxy FROM accounts_meta WHERE name=?", (account,)).fetchone()
                if row and "proxy" in row.keys() and row["proxy"]:
                    proxy_to_use = row["proxy"].strip()
        except Exception:
            pass
    if not proxy_to_use and config.PROXY:
        proxy_to_use = config.PROXY.strip()

    if proxy_to_use:
        parsed = parse_proxy(proxy_to_use)
        if parsed:
            kwargs["proxy"] = parsed

    context = await p.chromium.launch_persistent_context(str(profile_dir), **kwargs)
    try:
        await context.add_cookies([
            {"name": "tt_locale", "value": "vi", "domain": ".dola.com", "path": "/", "secure": True},
            {"name": "i18n_redirect", "value": "vi", "domain": ".dola.com", "path": "/", "secure": True},
            {"name": "language", "value": "vi", "domain": ".dola.com", "path": "/", "secure": True},
        ])
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
