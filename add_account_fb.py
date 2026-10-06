"""Automated Facebook OAuth login for Dola account setup with 2FA support and Vietnamese locale.

Usage:
    python add_account_fb.py <account_name> "tk|pass|2fa"
    or
    python add_account_fb.py "tk|pass|2fa"
    or
    python add_account_fb.py --file accounts_fb.txt
"""
import sys

# Force UTF-8 for Windows console
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import asyncio
import base64
import hashlib
import hmac
import os
import re
import struct
import time
from pathlib import Path

from patchright.async_api import async_playwright

import json
from browser import LAUNCH_ARGS, configure_clean_profile
import config


def totp(secret: str, period: int = 30, digits: int = 6) -> str:
    """Standard TOTP (RFC 6238), Google Authenticator / FB 2FA compatible."""
    clean_secret = secret.replace(" ", "").strip().upper()
    missing_padding = (8 - len(clean_secret) % 8) % 8
    key = base64.b32decode(clean_secret + "=" * missing_padding)
    counter = int(time.time() // period)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[19] & 15
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7fffffff) % (10 ** digits)
    return str(code).zfill(digits)


def sanitize_account_name(name: str) -> str:
    """Ensure safe filesystem name for accounts/<name>."""
    clean = re.sub(r"[^a-zA-Z0-9_-]", "_", name.strip())
    return clean[:40] or f"fb_{int(time.time())}"


async def solve_fb_2fa(fb_page, secret: str) -> bool:
    """Detects and fills 2FA / checkpoint screens on Facebook.

    Handles:
    - Screen waiting for device approval -> clicks 'Thử cách khác' (Try another way)
    - Modal 'Chọn một cách để xác nhận đó là bạn' -> selects 'Ứng dụng xác thực' & clicks 'Tiếp tục'
    - 6-digit TOTP code entry -> generates RFC 6238 code, fills and submits
    - Checkpoint intermediate screens -> 'Lưu trình duyệt', 'Đây là tôi', etc.
    - Consent screen -> 'Tiếp tục dưới tên [Name]'
    """
    for attempt in range(40):
        await asyncio.sleep(2)
        try:
            if fb_page.is_closed():
                print("[fb] Facebook popup closed (login completed or redirected)", flush=True)
                return True
        except Exception:
            return True

        try:
            url = fb_page.url.lower()
        except Exception:
            return True

        # Check if already redirected back to Dola
        if "facebook.com" not in url:
            print(f"[fb] Redirected out of Facebook (URL: {url[:60]})", flush=True)
            return True

        # Check for immediate 2FA error / notice
        try:
            err_msg = await fb_page.evaluate("""() => {
                const texts = ['mã không chính xác', 'mã không hợp lệ', 'invalid code', 'incorrect code', 'thử lại sau', 'vui lòng chờ'];
                for (const el of document.querySelectorAll('*')) {
                    if (el.children.length === 0) {
                        const t = (el.innerText || '').trim();
                        if (texts.some(x => t.toLowerCase().includes(x))) {
                            return t;
                        }
                    }
                }
                return null;
            }""")
            if err_msg:
                print(f"[fb] ✗ 2FA notice/error: {err_msg}", flush=True)
        except Exception:
            pass

        # -------------------------------------------------------------
        # STEP 1: Check for OAuth Consent Screen FIRST ("Tiếp tục dưới tên [Name]" / "Continue as [Name]")
        # -------------------------------------------------------------
        consent_btn = None
        for c_sel in [
            "[role='button']:has-text('Tiếp tục dưới tên')",
            "button:has-text('Tiếp tục dưới tên')",
            "[role='button']:has-text('Continue as')",
            "button:has-text('Continue as')",
            "[aria-label*='Tiếp tục dưới tên' i]",
            "[aria-label*='Continue as' i]",
        ]:
            try:
                if fb_page.is_closed():
                    return True
                cand = fb_page.locator(c_sel).first
                if await cand.count() and await cand.is_visible():
                    consent_btn = cand
                    break
            except Exception:
                if fb_page.is_closed():
                    return True

        if consent_btn:
            print("[fb] OAuth Consent screen -> clicking 'Tiếp tục dưới tên'...", flush=True)
            try:
                await consent_btn.click(timeout=4000)
                await asyncio.sleep(2)
                if fb_page.is_closed():
                    print("[fb] Facebook popup closed after consent!", flush=True)
                    return True
            except Exception:
                if fb_page.is_closed():
                    return True
            continue

        # -------------------------------------------------------------
        # STEP 2: Check for "Tin cậy thiết bị này" (Remember browser / Trust device)
        # Screen: "Bạn đã đăng nhập. Tin cậy thiết bị này?" (two_factor/remember_browser)
        # -------------------------------------------------------------
        trust_btn = None
        for cand in [
            fb_page.get_by_role("button", name=re.compile(r"^Tin cậy thiết bị( này)?$", re.I)),
            fb_page.locator("button, [role='button']").filter(has_text=re.compile(r"^Tin cậy thiết bị( này)?$", re.I)),
            fb_page.locator("[role='button']:has-text('Tin cậy thiết bị này')"),
            fb_page.locator("button:has-text('Tin cậy thiết bị này')"),
            fb_page.locator("[role='button']:has-text('Tin cậy thiết bị')"),
            fb_page.locator("button:has-text('Tin cậy thiết bị')"),
            fb_page.locator("[role='button']:has-text('Tin cậy')"),
            fb_page.locator("button:has-text('Tin cậy')"),
            fb_page.get_by_text("Tin cậy thiết bị này", exact=True),
            fb_page.get_by_role("button", name=re.compile(r"^Trust this device$", re.I)),
            fb_page.locator("button, [role='button']").filter(has_text=re.compile(r"^Trust this device$", re.I)),
            fb_page.locator("[role='button']:has-text('Trust this device')"),
            fb_page.locator("button:has-text('Trust this device')"),
            fb_page.get_by_text("Trust this device", exact=True),
            fb_page.locator("[role='button']:has-text('Lưu trình duyệt')"),
            fb_page.locator("button:has-text('Lưu trình duyệt')"),
            fb_page.locator("[role='button']:has-text('Save browser')"),
            fb_page.locator("button:has-text('Save browser')"),
        ]:
            try:
                if fb_page.is_closed():
                    return True
                if await cand.count() and await cand.first.is_visible():
                    tag = await cand.first.evaluate("el => el.tagName.toLowerCase()")
                    if tag in ["h1", "h2", "h3", "h4", "p"]:
                        continue
                    trust_btn = cand.first
                    break
            except Exception:
                if fb_page.is_closed():
                    return True

        # Fallback on remember_browser URL
        if not trust_btn and "remember_browser" in url:
            try:
                all_b = fb_page.locator("[role='button'], button")
                b_cnt = await all_b.count()
                for i in range(b_cnt):
                    b = all_b.nth(i)
                    if await b.is_visible():
                        b_txt = (await b.inner_text()).strip()
                        if any(w in b_txt.lower() for w in ["tin cậy", "trust", "lưu trình duyệt", "save browser"]):
                            trust_btn = b
                            break
            except Exception:
                pass

        if trust_btn:
            print("[fb] Screen 'Tin cậy thiết bị này' detected -> clicking 'Tin cậy thiết bị này'...", flush=True)
            try:
                btn_anc = trust_btn.locator("xpath=ancestor-or-self::*[@role='button' or self::button][1]")
                target = btn_anc.first if (await btn_anc.count() and await btn_anc.first.is_visible()) else trust_btn
                await target.scroll_into_view_if_needed()
                await target.click(timeout=4000)
                await asyncio.sleep(2.5)
                if fb_page.is_closed():
                    print("[fb] Facebook popup closed after clicking 'Tin cậy thiết bị'!", flush=True)
                    return True
            except Exception as e:
                print(f"[fb] Warning clicking 'Tin cậy thiết bị': {e}", flush=True)
            continue

        # -------------------------------------------------------------
        # STEP 3: Check for Method Selection Modal ("Chọn một cách để xác nhận đó là bạn")
        # -------------------------------------------------------------
        auth_app_option = None
        for cand in [
            fb_page.get_by_text("Ứng dụng xác thực", exact=True),
            fb_page.get_by_text("Authentication app", exact=True),
            fb_page.locator("[aria-label*='Ứng dụng xác thực' i]"),
            fb_page.locator("[aria-label*='Authentication app' i]"),
            fb_page.get_by_text("Nhận mã từ ứng dụng xác thực"),
        ]:
            try:
                if fb_page.is_closed():
                    return True
                if await cand.count() and await cand.first.is_visible():
                    auth_app_option = cand.first
                    break
            except Exception:
                if fb_page.is_closed():
                    return True

        if auth_app_option:
            print("[fb] Modal detected -> selecting 'Ứng dụng xác thực' (Authentication app)...", flush=True)
            try:
                opt_parent = auth_app_option.locator("xpath=ancestor-or-self::*[@role='radio' or @role='button' or self::label or contains(@class, 'radio')][1]")
                if await opt_parent.count():
                    await opt_parent.first.click()
                else:
                    await auth_app_option.click()
                await asyncio.sleep(0.8)
            except Exception as e:
                print(f"[fb] Warning selecting auth option: {e}", flush=True)

            # Click "Tiếp tục" in the modal
            modal_continue = None
            for cand in [
                fb_page.locator("[role='dialog']").get_by_role("button", name="Tiếp tục"),
                fb_page.locator("[role='dialog']").get_by_role("button", name="Continue"),
                fb_page.get_by_role("button", name="Tiếp tục"),
                fb_page.get_by_role("button", name="Continue"),
                fb_page.locator("[role='dialog'] [role='button']:has-text('Tiếp tục')"),
                fb_page.locator("[role='dialog'] button:has-text('Tiếp tục')"),
            ]:
                try:
                    if await cand.count() and await cand.first.is_visible():
                        modal_continue = cand.first
                        break
                except Exception:
                    pass

            if modal_continue:
                print("[fb] Clicking 'Tiếp tục' after choosing Authentication App...", flush=True)
                try:
                    await modal_continue.click()
                    await asyncio.sleep(2.5)
                except Exception as e:
                    print(f"[fb] Warning clicking modal continue: {e}", flush=True)
            continue

        # -------------------------------------------------------------
        # STEP 3: Check for 2FA Code Input Field (BEFORE "Thử cách khác")
        # -------------------------------------------------------------
        has_code_input = False
        code_input = None
        try:
            if fb_page.is_closed():
                return True
            code_input = fb_page.locator(
                "input[name='approvals_code'], input#approvals_code, "
                "input[autocomplete='one-time-code'], input[inputmode='numeric'], "
                "input[placeholder*='mã' i], input[placeholder*='code' i], "
                "input[aria-label*='mã' i], input[aria-label*='code' i]"
            ).first
            if await code_input.count() and await code_input.is_visible():
                has_code_input = True
            elif "two_step" in url or "two_factor" in url or "checkpoint" in url:
                visible_inputs = fb_page.locator("input:not([type='hidden']):not([type='submit']):not([type='checkbox']):not([type='radio'])")
                if await visible_inputs.count() and await visible_inputs.first.is_visible():
                    code_input = visible_inputs.first
                    has_code_input = True
        except Exception:
            if fb_page.is_closed():
                return True

        if has_code_input and code_input:
            if not secret:
                raise RuntimeError("Facebook requested 2FA, but no 2FA secret key was provided in tk|pass|2fa")
            code = totp(secret)
            print(f"[fb] Found 2FA code input -> generated TOTP code: {code}", flush=True)

            try:
                all_digit_inputs = fb_page.locator("input:not([type='hidden']):not([type='submit']):not([type='checkbox']):not([type='radio'])")
                d_count = await all_digit_inputs.count()
                if d_count >= 6:
                    print(f"[fb] Found {d_count} separate digit inputs -> filling each digit...", flush=True)
                    for idx, digit in enumerate(code[:d_count]):
                        try:
                            inp = all_digit_inputs.nth(idx)
                            await inp.fill(digit)
                            await asyncio.sleep(0.08)
                        except Exception:
                            pass
                    try:
                        await all_digit_inputs.last.press("Enter")
                    except Exception:
                        pass
                else:
                    await code_input.fill("")
                    await code_input.fill(code)
                    await asyncio.sleep(0.4)
                    print("[fb] Submitting 2FA code via Enter...", flush=True)
                    await code_input.press("Enter")
            except Exception as e:
                print(f"[fb] Warning filling 2FA code: {e}", flush=True)

            # Fallback submit button
            await asyncio.sleep(1.2)
            if not fb_page.is_closed():
                for sel in [
                    "[role='button']:has-text('Tiếp tục')",
                    "button:has-text('Tiếp tục')",
                    "[role='button']:has-text('Gửi mã')",
                    "button:has-text('Gửi mã')",
                    "[role='button']:has-text('Continue')",
                    "button:has-text('Continue')",
                    "[role='button']:has-text('Submit')",
                    "button:has-text('Submit')",
                    "#checkpointSubmitButton",
                ]:
                    try:
                        sub_btn = fb_page.locator(sel).first
                        if await sub_btn.count() and await sub_btn.is_visible():
                            await sub_btn.click(timeout=3000)
                            print(f"[fb] Clicked 2FA submit ({sel})", flush=True)
                            break
                    except Exception:
                        pass

            await asyncio.sleep(3)
            continue

        # -------------------------------------------------------------
        # STEP 4: Check for "Thử cách khác" (Try another way)
        # -------------------------------------------------------------
        try_another_btn = None
        for cand in [
            fb_page.get_by_role("button", name="Thử cách khác"),
            fb_page.get_by_role("button", name="Try another way"),
            fb_page.get_by_text("Thử cách khác", exact=True),
            fb_page.get_by_text("Try another way", exact=True),
            fb_page.locator("[role='button']:has-text('Thử cách khác')"),
            fb_page.locator("button:has-text('Thử cách khác')"),
        ]:
            try:
                if fb_page.is_closed():
                    return True
                if await cand.count() and await cand.first.is_visible():
                    try_another_btn = cand.first
                    break
            except Exception:
                if fb_page.is_closed():
                    return True

        if try_another_btn:
            print("[fb] Waiting for approval screen detected -> clicking 'Thử cách khác' (Try another way)...", flush=True)
            try:
                btn_ancestor = try_another_btn.locator("xpath=ancestor-or-self::*[@role='button' or self::button][1]")
                if await btn_ancestor.count():
                    await btn_ancestor.first.click()
                else:
                    await try_another_btn.click()
                await asyncio.sleep(2.5)
            except Exception as e:
                print(f"[fb] Warning clicking 'Thử cách khác': {e}", flush=True)
            continue

        # -------------------------------------------------------------
        # STEP 5: Check for Checkpoint intermediate screens ("Lưu trình duyệt", "Đây là tôi")
        # -------------------------------------------------------------
        checkpoint_clicked = False
        for sel in [
            "#checkpointSubmitButton",
            "[role='button']:has-text('Tin cậy thiết bị này')",
            "button:has-text('Tin cậy thiết bị này')",
            "[role='button']:has-text('Tin cậy thiết bị')",
            "button:has-text('Tin cậy thiết bị')",
            "[role='button']:has-text('Tin cậy')",
            "button:has-text('Tin cậy')",
            "[role='button']:has-text('Trust this device')",
            "button:has-text('Trust this device')",
            "[role='button']:has-text('Trust device')",
            "button:has-text('Trust device')",
            "[role='button']:has-text('Lưu trình duyệt')",
            "button:has-text('Lưu trình duyệt')",
            "[role='button']:has-text('Save browser')",
            "button:has-text('Save browser')",
            "[role='button']:has-text('Không lưu')",
            "button:has-text('Không lưu')",
            "[role='button']:has-text('Don\\'t Save')",
            "[role='button']:has-text('Đây là tôi')",
            "[role='button']:has-text('This was me')",
            "button#checkpointSubmitButton",
        ]:
            try:
                if fb_page.is_closed():
                    return True
                candidate = fb_page.locator(sel).first
                if await candidate.count() and await candidate.is_visible():
                    print(f"[fb] Checkpoint intermediate step -> clicking {sel}", flush=True)
                    await candidate.scroll_into_view_if_needed()
                    await candidate.click(timeout=3000)
                    await asyncio.sleep(2)
                    checkpoint_clicked = True
                    break
            except Exception:
                pass

        if checkpoint_clicked:
            cont = fb_page.locator(
                "[role='button']:has-text('Tiếp tục'), button:has-text('Tiếp tục'), "
                "[role='button']:has-text('Continue'), button:has-text('Continue')"
            ).first
            try:
                if await cont.count() and await cont.is_visible():
                    await cont.click(timeout=3000)
                    await asyncio.sleep(2.5)
            except Exception:
                pass
            continue

        # -------------------------------------------------------------
        # STEP 6: Generic "Tiếp tục" / "Continue" fallback on checkpoint pages
        # -------------------------------------------------------------
        if "checkpoint" in url or "two_step" in url or "two_factor" in url:
            gen_btn = fb_page.locator(
                "[role='button']:has-text('Tiếp tục'), button:has-text('Tiếp tục'), "
                "[role='button']:has-text('Continue'), button:has-text('Continue')"
            ).first
            try:
                if await gen_btn.count() and await gen_btn.is_visible():
                    print("[fb] Clicking generic 'Tiếp tục' / 'Continue' on checkpoint page...", flush=True)
                    await gen_btn.click(timeout=3000)
                    await asyncio.sleep(2.5)
            except Exception:
                pass
            continue

    return False


async def fb_login(fb_page, username: str, password: str, secret: str):
    """Executes Facebook OAuth login flow including credentials and 2FA."""
    print(f"[fb] Handling Facebook login page (URL: {fb_page.url[:80]})...", flush=True)
    try:
        await fb_page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    await asyncio.sleep(1.5)

    # Check if login form is present or if profile is already logged in / on consent screen
    has_login_form = False
    for _ in range(5):
        if fb_page.is_closed():
            return
        try:
            user_input = fb_page.locator("input[name='email'], input#email").first
            if await user_input.count() and await user_input.is_visible():
                has_login_form = True
                break
            # Check if consent screen or checkpoint is directly visible
            for cont_sel in [
                "[role='button']:has-text('Tiếp tục')",
                "button:has-text('Tiếp tục')",
                "[role='button']:has-text('Continue')",
                "button:has-text('Continue')",
                "[role='button']:has-text('Tin cậy')",
                "button:has-text('Tin cậy')",
                "[role='button']:has-text('Trust')",
                "button:has-text('Trust')",
            ]:
                cand = fb_page.locator(cont_sel).first
                if await cand.count() and await cand.is_visible():
                    print("[fb] Already logged in to Facebook -> proceeding directly to consent/2FA...", flush=True)
                    break
        except Exception:
            if fb_page.is_closed():
                return
        await asyncio.sleep(1)

    if has_login_form:
        print("[fb] Entering Facebook credentials...", flush=True)
        try:
            user_input = fb_page.locator("input[name='email'], input#email").first
            await user_input.fill("")
            await user_input.fill(username)
            await asyncio.sleep(0.3)

            pass_input = fb_page.locator("input[name='pass'], input#pass, input[type='password']").first
            if await pass_input.count() and await pass_input.is_visible():
                await pass_input.fill("")
                await pass_input.fill(password)
                await asyncio.sleep(0.5)
                print("[fb] Submitting credentials via Enter key...", flush=True)
                await pass_input.press("Enter")
        except Exception as e:
            print(f"[fb] Warning filling credentials: {e}", flush=True)

        # Fallback: also click submit button if still on login form
        await asyncio.sleep(1.5)
        if not fb_page.is_closed():
            for sel in [
                "[role='button']:has-text('Đăng nhập')",
                "[role='button']:has-text('Log In')",
                "input[type='submit']",
                "button[type='submit']",
                "button[name='login']",
                "#loginbutton",
            ]:
                try:
                    btn = fb_page.locator(sel).first
                    if await btn.count() and await btn.is_visible():
                        print(f"[fb] Clicking fallback submit button: {sel}", flush=True)
                        await btn.click(timeout=3000)
                        break
                except Exception:
                    pass

        # Check for immediate error message on Facebook
        await asyncio.sleep(2)
        if not fb_page.is_closed():
            try:
                err_msg = await fb_page.evaluate("""() => {
                    const patterns = ['không chính xác', 'không kết nối', 'incorrect', 'bị vô hiệu hóa', 'tạm thời bị khóa'];
                    for (const el of document.querySelectorAll('*')) {
                        if (el.children.length === 0) {
                            const t = (el.innerText || '').trim();
                            if (patterns.some(p => t.toLowerCase().includes(p))) {
                                return t;
                            }
                        }
                    }
                    return null;
                }""")
                if err_msg:
                    print(f"[fb] ✗ Facebook error: {err_msg}", flush=True)
                    raise RuntimeError(f"Facebook login rejected: {err_msg}")
            except RuntimeError:
                raise
            except Exception:
                pass

    # Wait and handle 2FA or Checkpoint
    await asyncio.sleep(1)
    if not fb_page.is_closed():
        await solve_fb_2fa(fb_page, secret)


def append_to_cookies_file(account: str, cookie_header: str, file_path: str = "cookies.txt"):
    """Appends or updates account entry in cookies.txt."""
    path = Path(file_path)
    lines = []
    found = False
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line_str = line.strip()
            if line_str and not line_str.startswith("#") and "|" in line_str:
                acc_name, _ = line_str.split("|", 1)
                if acc_name.strip() == account:
                    lines.append(f"{account}|{cookie_header}")
                    found = True
                    continue
            lines.append(line)
    if not found:
        lines.append(f"{account}|{cookie_header}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[{account}] ✓ Saved cookie entry to {file_path}", flush=True)


async def add_fb_account_flow(
    account: str,
    username: str,
    password: str,
    secret: str = "",
    cookies_file: str = "cookies.txt",
    proxy: str = ""
) -> dict:
    """Full automated login flow via Facebook OAuth.

    Returns dict with {ok: bool, account: str, cookies: dict, cookie_header: str, error: str}
    """
    account = sanitize_account_name(account)
    profile_dir = Path("accounts") / account
    profile_dir.mkdir(parents=True, exist_ok=True)
    configure_clean_profile(profile_dir)

    print(f"\n==========================================")
    print(f"Starting Facebook login for [{account}]")
    print(f"Username: {username}")
    print(f"2FA Secret: {'Present' if secret else 'None'}")
    if proxy:
        print(f"Proxy: {proxy}")
    print(f"Profile: {profile_dir}")
    print(f"==========================================\n", flush=True)

    async with async_playwright() as p:
        kwargs = {
            "headless": False,
            "args": list(LAUNCH_ARGS),
            "locale": "vi-VN",
            "timezone_id": "Asia/Ho_Chi_Minh",
        }
        acc_proxy = proxy or config.PROXY
        if acc_proxy:
            from browser import parse_proxy
            parsed = parse_proxy(acc_proxy)
            if parsed:
                kwargs["proxy"] = parsed

        context = await p.chromium.launch_persistent_context(str(profile_dir), **kwargs)
        try:
            page = context.pages[0] if context.pages else await context.new_page()

            # Set Vietnamese cookies on dola.com domain in advance
            await context.add_cookies([
                {"name": "tt_locale", "value": "vi", "domain": ".dola.com", "path": "/", "secure": True},
                {"name": "i18n_redirect", "value": "vi", "domain": ".dola.com", "path": "/", "secure": True},
                {"name": "language", "value": "vi", "domain": ".dola.com", "path": "/", "secure": True},
            ])

            print(f"[{account}] Navigating to Dola chat...", flush=True)
            await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3500)

            # Check if active session already exists
            cookies = await context.cookies("https://www.dola.com")
            if any(c["name"] == "sessionid" and c["value"] for c in cookies):
                print(f"[{account}] Active session already exists in profile!", flush=True)
                cookie_map = {c["name"]: c["value"] for c in cookies if c.get("value")}
                header = "; ".join(f"{k}={v}" for k, v in cookie_map.items())
                append_to_cookies_file(account, header, cookies_file)
                return {"ok": True, "account": account, "cookies": cookie_map, "cookie_header": header}

            # Dismiss cookie banner
            try:
                ok_btn = page.locator("button:has-text('OK'), div:has-text('OK')").first
                if await ok_btn.count() and await ok_btn.is_visible():
                    await ok_btn.click()
                    await page.wait_for_timeout(500)
            except Exception:
                pass

            # Open Login Modal if not already open
            modal = page.locator(".semi-modal-wrap, [role='dialog']").first
            if not await modal.count() or not await modal.is_visible():
                login_btn = page.locator(
                    "button:has-text('Đăng nhập'), button:has-text('Log in'), button:has-text('ログイン')"
                ).first
                if await login_btn.count() and await login_btn.is_visible():
                    print(f"[{account}] Clicking header 'Đăng nhập' button...", flush=True)
                    await login_btn.click()
                    await page.wait_for_timeout(2000)

            # Find Facebook button in modal
            fb_btn = None

            # Check 1: Find by Facebook SVG or color #1877F2
            svg_candidates = page.locator(".semi-modal-wrap svg, [role='dialog'] svg")
            svg_count = await svg_candidates.count()
            for i in range(svg_count):
                svg_el = svg_candidates.nth(i)
                html = await svg_el.evaluate("el => el.outerHTML")
                if "#1877f2" in html.lower() or "facebook" in html.lower() or "M14" in html:
                    # Target the button parent
                    fb_btn = svg_el.locator("xpath=ancestor::*[contains(@class, 'button') or @role='button'][1]")
                    if await fb_btn.count():
                        print(f"[{account}] Found Facebook button by SVG signature!", flush=True)
                        break

            # Check 2: If modal is in Phone Mode (only Apple + Google visible), click Google to switch to Google Mode
            if not fb_btn:
                circ_buttons = page.locator(
                    ".semi-modal-wrap [class*='button-PgvIWh'], [role='dialog'] [class*='button-PgvIWh']"
                )
                count = await circ_buttons.count()
                if count >= 3:
                    # In 3-icon row (Phone, Facebook, Apple)
                    fb_btn = circ_buttons.nth(1)
                    print(f"[{account}] Selected middle circular button as Facebook.", flush=True)
                elif count == 2:
                    # In 2-icon row (Apple, Google) - click Google to switch view to Google mode
                    print(f"[{account}] Modal in Phone mode. Switching to Google mode to expose Facebook...", flush=True)
                    google_icon = circ_buttons.nth(1)
                    await google_icon.click()
                    await page.wait_for_timeout(1500)
                    # Now 3 circular buttons should appear under Google button
                    new_circ = page.locator(".semi-modal-wrap [class*='button-PgvIWh'], [role='dialog'] [class*='button-PgvIWh']")
                    if await new_circ.count() >= 3:
                        fb_btn = new_circ.nth(1)

            if not fb_btn:
                # Fallback: find any element containing Facebook text or aria-label
                alt = page.locator("[aria-label*='Facebook' i], [title*='Facebook' i], button:has-text('Facebook')").first
                if await alt.count() and await alt.is_visible():
                    fb_btn = alt

            # Listen for Facebook popup window
            popup_future = asyncio.get_event_loop().create_future()
            def on_page_event(new_pg):
                if not popup_future.done():
                    popup_future.set_result(new_pg)
            context.on("page", on_page_event)

            print(f"[{account}] Clicking Facebook login option...", flush=True)
            await fb_btn.click()

            fb_page = None
            try:
                fb_page = await asyncio.wait_for(popup_future, timeout=15.0)
                print(f"[{account}] Facebook OAuth popup opened!", flush=True)
            except asyncio.TimeoutError:
                # Popup might have redirected current tab instead of popup
                if "facebook.com" in page.url:
                    fb_page = page
                else:
                    await page.screenshot(path=f"dbg_fb_popup_fail_{account}.png")
                    raise RuntimeError("Failed to open Facebook login popup (saved screenshot)")

            # Perform FB login
            await fb_login(fb_page, username, password, secret)

            # Wait for OAuth callback to complete and Dola sessionid to appear
            print(f"[{account}] Waiting for Dola session authorization...", flush=True)
            session_found = False
            for step in range(35):
                await asyncio.sleep(2)

                # Check if 18+ Age verification or Onboarding terms popup appeared
                try:
                    for ok_sel in [
                        "button:has-text('Xác nhận')",
                        "[role='dialog'] button:has-text('Xác nhận')",
                        ".semi-modal button:has-text('Xác nhận')",
                        "button:has-text('OK')",
                        "button:has-text('Đồng ý')",
                    ]:
                        confirm = page.locator(ok_sel).first
                        if await confirm.count() and await confirm.is_visible():
                            print(f"[{account}] Found and clicked confirmation popup: {ok_sel}", flush=True)
                            await confirm.click()
                            await asyncio.sleep(2)
                            break
                except Exception:
                    pass

                cookies = await context.cookies("https://www.dola.com")
                session_cookie = next((c for c in cookies if c["name"] == "sessionid" and c["value"]), None)
                if session_cookie:
                    session_found = True
                    break

            if not session_found:
                await page.screenshot(path=f"dbg_session_fail_{account}.png")
                raise RuntimeError("Login completed but 'sessionid' cookie was not acquired within 70s")

            # Extract full cookie header
            cookie_map = {c["name"]: c["value"] for c in cookies if c.get("value")}
            header = "; ".join(f"{k}={v}" for k, v in cookie_map.items())

            # Save to cookies.txt
            append_to_cookies_file(account, header, cookies_file)

            # Save backup cookies.json into profile directory
            import json
            (profile_dir / "cookies.json").write_text(json.dumps(cookies, indent=2), encoding="utf-8")

            print(f"[{account}] ✓ Login successful! Profile ready with Vietnamese interface.")
            return {
                "ok": True,
                "account": account,
                "cookies": cookie_map,
                "cookie_header": header,
            }

        except Exception as exc:
            print(f"[{account}] ✗ Login failed: {exc}", flush=True)
            return {"ok": False, "account": account, "error": str(exc)}
        finally:
            await context.close()


def parse_account_string(raw: str) -> tuple[str, str, str, str, str]:
    """Parses user input strings.

    Supported formats:
    1. account_name|tk|pass|2fa|proxy
    2. tk|pass|2fa|proxy
    3. account_name|tk|pass|2fa
    4. tk|pass|2fa
    """
    raw = raw.strip()
    delimiter = "----" if "----" in raw else "|"
    parts = [p.strip() for p in raw.split(delimiter)]

    proxy = ""
    if len(parts) >= 5:
        account, tk, password, secret, proxy = parts[0], parts[1], parts[2], parts[3], parts[4]
    elif len(parts) == 4:
        if ":" in parts[3] or "://" in parts[3]:
            tk, password, secret, proxy = parts[0], parts[1], parts[2], parts[3]
            account = f"fb_{re.sub(r'[^a-zA-Z0-9]', '', tk)[:12] or int(time.time())}"
        else:
            account, tk, password, secret = parts[0], parts[1], parts[2], parts[3]
    elif len(parts) == 3:
        tk, password, secret = parts[0], parts[1], parts[2]
        account = f"fb_{re.sub(r'[^a-zA-Z0-9]', '', tk)[:12] or int(time.time())}"
    elif len(parts) == 2:
        tk, password = parts[0], parts[1]
        secret = ""
        account = f"fb_{re.sub(r'[^a-zA-Z0-9]', '', tk)[:12] or int(time.time())}"
    else:
        raise ValueError(f"Invalid account format. Expected: tk|pass|2fa or name|tk|pass|2fa. Got: {raw}")

    return account, tk, password, secret, proxy


async def main():
    parser = argparse.ArgumentParser(description="Automated Dola Facebook login with 2FA")
    parser.add_argument("account", nargs="?", help="Account profile name (optional if given in format)")
    parser.add_argument("credentials", nargs="?", help="'tk|pass|2fa' or 'name|tk|pass|2fa'")
    parser.add_argument("--file", "-f", help="Text file containing list of accounts (one per line)")
    parser.add_argument("--cookies-file", default="cookies.txt", help="Path to save cookies (default: cookies.txt)")

    args = parser.parse_args()

    entries = []
    if args.file:
        file_path = Path(args.file)
        if not file_path.exists():
            print(f"File not found: {file_path}", file=sys.stderr)
            sys.exit(1)
        for line in file_path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                entries.append(line)
    elif args.credentials:
        if args.account:
            entries.append(f"{args.account}|{args.credentials}")
        else:
            entries.append(args.credentials)
    elif args.account:
        entries.append(args.account)
    else:
        print("Usage: python add_account_fb.py <account_name> 'tk|pass|2fa'")
        print("   or: python add_account_fb.py 'tk|pass|2fa'")
        print("   or: python add_account_fb.py --file accounts_fb.txt")
        sys.exit(1)

    print(f"Loaded {len(entries)} account(s) to process.")
    for idx, raw in enumerate(entries, 1):
        try:
            account, tk, password, secret = parse_account_string(raw)
            print(f"\n--- [{idx}/{len(entries)}] Processing: {account} ---")
            res = await add_fb_account_flow(account, tk, password, secret, cookies_file=args.cookies_file)
            if not res["ok"]:
                print(f"Failed to add {account}: {res.get('error')}", file=sys.stderr)
        except Exception as e:
            print(f"Error parsing line {raw}: {e}", file=sys.stderr)

    print("\nAll done!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nProcess interrupted by user.")
        sys.exit(0)
    except Exception as e:
        print(f"\nFatal error: {e}", file=sys.stderr)
        sys.exit(1)
