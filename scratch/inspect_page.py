import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import asyncio
from browser import launch_account_context
from patchright.async_api import async_playwright

async def inspect():
    from browser_pool import BrowserPool
    pool = BrowserPool()
    conn = pool._conn
    conn.row_factory = __import__('sqlite3').Row
    accs = [r['name'] for r in conn.execute('SELECT name FROM accounts_meta').fetchall()]
    print(f'Checking {len(accs)} accounts...')
    async with async_playwright() as p:
        for a in accs:
            try:
                ctx = await launch_account_context(p, a, headless=True)
                page = await ctx.new_page()
                await page.goto('https://www.dola.com/chat', wait_until='domcontentloaded')
                await page.wait_for_timeout(3500)
                buttons = await page.evaluate("() => Array.from(document.querySelectorAll('button, a, [role=\"button\"]')).map(el => (el.innerText || '').trim())")
                has_login = any(t in ['Đăng nhập', 'Sign in', 'Log in', 'Tiếp tục bằng Google'] for t in buttons)
                has_avatar = await page.evaluate("() => !!(document.querySelector(\"[class*='avatar'], [class*='user-avatar'], img[src*='avatar']\"))")
                print(f'[{a}] has_login_btn={has_login}, has_avatar={has_avatar}')
                pool.set_login_status(a, not has_login and has_avatar)
                await ctx.close()
            except Exception as e:
                print(f'[{a}] error: {e}')

if __name__ == '__main__':
    asyncio.run(inspect())
