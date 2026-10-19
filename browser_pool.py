"""Browser Account Pool: Manages accounts/ profiles with concurrency control and daily limits."""
import asyncio
import shutil
import sqlite3
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from dola_client import CreditError
from video_worker_ui import (
    ACTIVE_WORKER_BROWSERS, AccountLimitedError, CreditInsufficientError,
    PromptClarificationError, RiskControlError, generate_video, resume_video,
)
import config
from browser import cookie_profile_names

DAILY_LIMIT = 2
COOLDOWN_SEC = 1800  # 30-minute cooldown on risk control


class AllAccountsLimitedError(RuntimeError):
    """All active schedulable accounts have reached daily video limit."""


class AllAccountsQuotaBlockedError(RuntimeError):
    """All active schedulable accounts are known to have insufficient credits."""


class BrowserPool:
    def __init__(self, accounts_dir: str = "accounts", db_path: str = "pool_usage.db",
                 max_concurrency: int = 1):
        self.accounts_dir = Path(accounts_dir)
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        # Cookie-backed profiles are intentionally created at startup so they
        # appear in the dashboard and can be verified like normal profiles.
        for name in cookie_profile_names():
            (self.accounts_dir / name).mkdir(parents=True, exist_ok=True)
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self._locks: dict[str, asyncio.Lock] = {}
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS usage (account TEXT, day TEXT, used INTEGER, "
            "PRIMARY KEY(account, day))"
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts_meta (
                name TEXT PRIMARY KEY,
                scheduling INTEGER DEFAULT 1,
                note TEXT DEFAULT '',
                email TEXT DEFAULT '',
                created_at REAL,
                last_used_at REAL DEFAULT 0,
                login_ok INTEGER,
                login_checked_at REAL DEFAULT 0,
                cooldown_until REAL DEFAULT 0,
                rate_limited_until REAL DEFAULT 0,
                limit_reason TEXT DEFAULT '',
                quota_blocked_until REAL DEFAULT 0,
                quota_reason TEXT DEFAULT '',
                credit_balance INTEGER,
                credit_checked_at REAL DEFAULT 0
            )
            """
        )
        self._conn.commit()
        # Legacy migration: add metadata columns
        for column, definition in (
            ("email", "TEXT DEFAULT ''"),
            ("rate_limited_until", "REAL DEFAULT 0"),
            ("limit_reason", "TEXT DEFAULT ''"),
            ("quota_blocked_until", "REAL DEFAULT 0"),
            ("quota_reason", "TEXT DEFAULT ''"),
            ("credit_balance", "INTEGER"),
            ("credit_checked_at", "REAL DEFAULT 0"),
            ("proxy", "TEXT DEFAULT ''"),
            ("password", "TEXT DEFAULT ''"),
            ("secret", "TEXT DEFAULT ''"),
            ("account_type", "TEXT DEFAULT 'facebook'"),
        ):
            try:
                self._conn.execute(f"ALTER TABLE accounts_meta ADD COLUMN {column} {definition}")
                self._conn.commit()
            except sqlite3.OperationalError:
                pass

    # ===== Account Discovery & Metadata =====

    def _ensure_meta(self, name: str):
        self._conn.execute(
            "INSERT OR IGNORE INTO accounts_meta (name, created_at) VALUES (?, ?)",
            (name, time.time()),
        )
        self._conn.commit()

    @property
    def accounts(self) -> list:
        if not self.accounts_dir.exists():
            return []
        names = sorted(d.name for d in self.accounts_dir.iterdir()
                       if d.is_dir() and not d.name.startswith(".") and not d.name.startswith("_") and not d.name.startswith("tmp"))
        for n in names:
            self._ensure_meta(n)
        return names

    def _meta(self, name: str):
        return self._conn.execute(
            "SELECT * FROM accounts_meta WHERE name=?", (name,)).fetchone()

    def used_today(self, account: str) -> int:
        row = self._conn.execute(
            "SELECT used FROM usage WHERE account=? AND day=?",
            (account, date.today().isoformat()),
        ).fetchone()
        return row[0] if row else 0

    def _claim(self, account: str):
        self._conn.execute(
            "INSERT INTO usage(account, day, used) VALUES (?,?,1) "
            "ON CONFLICT(account, day) DO UPDATE SET used=used+1",
            (account, date.today().isoformat()),
        )
        self._conn.commit()

    def _next_limit_reset(self) -> float:
        """Calculates next daily quota reset timestamp."""
        try:
            tz = ZoneInfo(config.LIMIT_RESET_TZ)
        except Exception:
            # Fallback to fixed offset if tzdata is not installed.
            offsets = {"Asia/Tokyo": 9, "Asia/Hong_Kong": 8, "UTC": 0}
            tz = timezone(timedelta(hours=offsets.get(config.LIMIT_RESET_TZ, 9)))
        now = datetime.now(tz)
        next_day = now.date() + timedelta(days=1)
        return datetime.combine(next_day, dt_time.min, tzinfo=tz).timestamp()

    def _clear_expired_rate_limits(self):
        now = time.time()
        cur = self._conn.execute(
            "UPDATE accounts_meta SET rate_limited_until=0, limit_reason='', "
            "quota_blocked_until=0, quota_reason='', credit_balance=NULL "
            "WHERE (rate_limited_until > 0 AND rate_limited_until <= ?) "
            "OR (quota_blocked_until > 0 AND quota_blocked_until <= ?)", (now, now))
        # Ensure accounts with positive credits are never blocked by stale quota blocks
        self._conn.execute(
            "UPDATE accounts_meta SET quota_blocked_until=0, quota_reason='' "
            "WHERE credit_balance > 0 AND quota_blocked_until > 0"
        )
        self._conn.commit()

    def _mark_quota_blocked(self, account: str, reason: str = ""):
        self._conn.execute(
            "UPDATE accounts_meta SET quota_blocked_until=?, quota_reason=?, last_used_at=? WHERE name=?",
            (self._next_limit_reset(), reason[:300], time.time(), account),
        )
        self._conn.commit()

    def _mark_daily_limit(self, account: str, reason: str = ""):
        """Marks account as reaching daily limit until next reset."""
        self._conn.execute(
            "INSERT INTO usage(account, day, used) VALUES (?,?,?) "
            "ON CONFLICT(account, day) DO UPDATE SET used=MAX(used, excluded.used)",
            (account, date.today().isoformat(), DAILY_LIMIT),
        )
        self._conn.execute(
            "UPDATE accounts_meta SET last_used_at=?, rate_limited_until=?, limit_reason=? WHERE name=?",
            (time.time(), self._next_limit_reset(), reason[:300], account),
        )
        self._conn.commit()

    def list_accounts(self) -> list:
        """Dashboard view: combines metadata, quota, and busy status."""
        self._clear_expired_rate_limits()
        now = time.time()
        out = []
        for a in self.accounts:
            m = self._meta(a)
            used = self.used_today(a)
            lock = self._locks.get(a)
            out.append({
                "name": a,
                "scheduling": bool(m["scheduling"]) if m else True,
                "note": m["note"] if m else "",
                "email": m["email"] if m else "",
                "created_at": m["created_at"] if m else 0,
                "last_used_at": m["last_used_at"] if m else 0,
                "login_ok": m["login_ok"] if m else None,
                "login_checked_at": m["login_checked_at"] if m else 0,
                "cooldown_until": m["cooldown_until"] if m else 0,
                "cooling": bool(m and m["cooldown_until"] > now),
                "rate_limited_until": m["rate_limited_until"] if m and m["rate_limited_until"] else 0,
                "rate_limited": bool(m and m["rate_limited_until"] > now),
                "limit_reason": m["limit_reason"] if m else "",
                "quota_blocked_until": m["quota_blocked_until"] if m and m["quota_blocked_until"] else 0,
                "quota_blocked": bool(m and m["quota_blocked_until"] > now),
                "quota_reason": m["quota_reason"] if m else "",
                "credit_balance": m["credit_balance"] if m else None,
                "credit_checked_at": m["credit_checked_at"] if m else 0,
                "proxy": (m["proxy"] or "") if m and "proxy" in m.keys() else "",
                "has_credentials": bool(m and "password" in m.keys() and m["password"]),
                "account_type": (m["account_type"] or "facebook") if m and "account_type" in m.keys() else "facebook",
                "used_today": used,
                "limit": config.DEFAULT_DAILY_CREDITS,
                "remaining": m["credit_balance"] if m and m["credit_balance"] is not None else config.DEFAULT_DAILY_CREDITS,
                "busy": bool(lock and lock.locked()),
            })
        return out

    def set_scheduling(self, name: str, on: bool):
        self._conn.execute(
            "UPDATE accounts_meta SET scheduling=? WHERE name=?", (1 if on else 0, name))
        self._conn.commit()

    def set_email(self, name: str, email: str):
        self._conn.execute(
            "UPDATE accounts_meta SET email=? WHERE name=?", (email, name))
        self._conn.commit()

    def set_proxy(self, name: str, proxy: str):
        self._conn.execute(
            "UPDATE accounts_meta SET proxy=? WHERE name=?", (proxy.strip(), name))
        self._conn.commit()

    def get_proxy(self, name: str) -> str:
        row = self._meta(name)
        return (row["proxy"] or "").strip() if row and "proxy" in row.keys() else ""

    def set_credentials(self, name: str, password: str, secret: str = "", account_type: str = "facebook"):
        self._conn.execute(
            "UPDATE accounts_meta SET password=?, secret=?, account_type=? WHERE name=?",
            (password, secret, account_type, name),
        )
        self._conn.commit()

    def get_credentials(self, name: str) -> dict:
        row = self._meta(name)
        if not row:
            return {}
        keys = row.keys()
        return {
            "email": (row["email"] or "") if "email" in keys else "",
            "password": (row["password"] or "") if "password" in keys else "",
            "secret": (row["secret"] or "") if "secret" in keys else "",
            "proxy": (row["proxy"] or "") if "proxy" in keys else "",
            "account_type": (row["account_type"] or "facebook") if "account_type" in keys else "facebook",
        }

    def set_login_status(self, name: str, ok: bool):
        self._conn.execute(
            "UPDATE accounts_meta SET login_ok=?, login_checked_at=? WHERE name=?",
            (1 if ok else 0, time.time(), name),
        )
        self._conn.commit()

    def set_note(self, name: str, note: str):
        self._conn.execute(
            "UPDATE accounts_meta SET note=? WHERE name=?", (note, name))
        self._conn.commit()

    def delete_account(self, name: str):
        lock = self._locks.get(name)
        if lock and lock.locked():
            raise RuntimeError("Account is generating video, cannot delete")
        d = self.accounts_dir / name
        if d.exists():
            shutil.rmtree(d)
        self._conn.execute("DELETE FROM accounts_meta WHERE name=?", (name,))
        self._conn.commit()

    async def verify_account(self, name: str) -> bool:
        """Verifies login state in headless mode and updates cache."""
        if name not in self.accounts:
            raise FileNotFoundError(f"Profile does not exist: {name}")
        lock = self._locks.setdefault(name, asyncio.Lock())
        if lock.locked():
            raise RuntimeError("Account is generating video, please verify later")
        from browser import check_login_state
        ok = await check_login_state(name)
        self._conn.execute(
            "UPDATE accounts_meta SET login_ok=?, login_checked_at=? WHERE name=?",
            (1 if ok else 0, time.time(), name),
        )
        self._conn.commit()
        return ok

    # ===== Scheduling =====

    def _set_credit_balance(self, account: str, balance: int, source: str = ""):
        bal = max(0, int(balance))
        self._conn.execute(
            "UPDATE accounts_meta SET credit_balance=?, credit_checked_at=? WHERE name=?",
            (bal, time.time(), account),
        )
        if bal <= 0:
            self._conn.execute(
                "UPDATE accounts_meta SET quota_blocked_until=?, quota_reason=? WHERE name=?",
                (self._next_limit_reset(), source[:300] or "Hết tín dụng (0 pts)", account),
            )
        else:
            self._conn.execute(
                "UPDATE accounts_meta SET quota_blocked_until=0, quota_reason='' WHERE name=?",
                (account,)
            )
        self._conn.commit()

    def deduct_credits(self, account: str, points: int):
        """Deducts consumed credits after a successful generation and recalculates remaining."""
        m = self._meta(account)
        if m and m["credit_balance"] is not None:
            new_balance = max(0, int(m["credit_balance"]) - points)
            self._conn.execute(
                "UPDATE accounts_meta SET credit_balance=?, credit_checked_at=? WHERE name=?",
                (new_balance, time.time(), account),
            )
            if new_balance <= 0:
                self._conn.execute(
                    "UPDATE accounts_meta SET quota_blocked_until=?, quota_reason=? WHERE name=?",
                    (self._next_limit_reset(), "Hết tín dụng trong ngày (0 pts)", account),
                )
            self._conn.commit()
            print(f"[pool] {account} deducted {points} credit pt(s), remaining balance: {new_balance} pts", flush=True)

    def _credit_available(self, account: str, required: int = 1) -> bool:
        row = self._meta(account)
        return not row or row["credit_balance"] is None or row["credit_balance"] >= required

    def _schedulable(self, a: dict, req_points: int = 1) -> bool:
        """Determines if account can be scheduled for a task requiring req_points credits.
        Only excludes account if credits is 0 or less than req_points.
        """
        if not a.get("scheduling", True):
            return False
        if a.get("cooling") or a.get("rate_limited"):
            return False
        if a.get("login_ok") is False or a.get("login_ok") == 0:
            return False
        pts = a.get("credit_balance")
        if pts is not None:
            if pts <= 0:
                return False
            if pts < req_points:
                return False
        else:
            if a.get("quota_blocked"):
                return False
        return True

    @property
    def all_accounts_limited(self) -> bool:
        """Returns True if all active accounts are rate limited by Dola."""
        candidates = [a for a in self.list_accounts() if a["scheduling"] and not a["cooling"]]
        return bool(candidates) and all(a["rate_limited"] for a in candidates)

    @property
    def all_accounts_quota_blocked(self) -> bool:
        """Returns True if all active accounts are confirmed to have 0 credits or quota blocked."""
        candidates = [a for a in self.list_accounts() if a["scheduling"] and not a["cooling"]]
        return bool(candidates) and all(
            a["quota_blocked"] or (a["credit_balance"] is not None and a["credit_balance"] <= 0)
            for a in candidates
        )

    @property
    def available(self) -> bool:
        return any(self._schedulable(a, req_points=1) for a in self.list_accounts())

    @property
    def cookie_count(self) -> int:  # /health compatibility
        return len(self.accounts)

    def account_status(self) -> list:
        return [{
            "account": a["name"], "used_today": a["used_today"], "limit": a["limit"],
            "rate_limited": a["rate_limited"], "rate_limited_until": a["rate_limited_until"],
            "quota_blocked": a["quota_blocked"], "quota_blocked_until": a["quota_blocked_until"],
        } for a in self.list_accounts()]

    async def resume_video(self, account: str, conversation_id: str, timeout: int,
                           on_poll=None, duration: int = None, check_cancelled=None) -> dict:
        """Resumes an accepted session without re-scheduling."""
        async with self.semaphore:
            lock = self._locks.setdefault(account, asyncio.Lock())
            async with lock:
                def on_balance(balance, source=""):
                    self._set_credit_balance(account, balance, source)
                try:
                    result = await resume_video(account, conversation_id, timeout,
                                                on_poll=on_poll, on_balance=on_balance,
                                                duration=duration, check_cancelled=check_cancelled)
                    self._claim(account)
                    self._conn.execute(
                        "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                        (time.time(), account))
                    self._conn.commit()
                    return result
                except TimeoutError:
                    self._claim(account)
                    self._conn.commit()
                    raise

    async def generate_video(self, prompt: str, ratio: str = None, duration: int = None,
                             model: str = "seedance_v2.0", on_conversation_id=None,
                             on_poll=None, on_balance=None,
                             reference_image_paths: list[str] | None = None,
                             check_cancelled=None,
                             on_account_assigned=None) -> dict:
        """Picks an idle schedulable account; waits if accounts are busy; rotates on quota/risk limits."""
        last_err = None
        req_points = config.estimate_required_credits(model, ratio, duration)
        print(f"[pool] Video request: model={model}, ratio={ratio}, duration={duration}s -> requires {req_points} credit point(s)", flush=True)

        while True:
            if check_cancelled and check_cancelled():
                raise asyncio.CancelledError("Task was cancelled")

            schedulable = [a for a in self.list_accounts() if self._schedulable(a, req_points=req_points)]
            if not schedulable:
                candidates = [a for a in self.list_accounts() if a["scheduling"] and not a["cooling"]]
                if candidates and all((a.get("credit_balance") is not None and a["credit_balance"] < req_points) or a.get("quota_blocked") or a.get("rate_limited") for a in candidates):
                    raise AllAccountsQuotaBlockedError(
                        f"429: Tất cả tài khoản rảnh đều không đủ tín dụng cho mode này (yêu cầu {req_points} pts). Vui lòng thử mode khác (như 1 pt) hoặc đợi reset ngày mới."
                    )
                raise RuntimeError(f"No available accounts in pool for required {req_points} pt(s): {last_err or 'No accounts'}")

            # Randomize account selection across all schedulable accounts
            import random
            random.shuffle(schedulable)

            for a in schedulable:
                account = a["name"]
                lock = self._locks.setdefault(account, asyncio.Lock())
                if lock.locked():
                    continue

                async with self.semaphore:
                    async with lock:
                        acc_obj = next((x for x in self.list_accounts() if x['name'] == account), None)
                        if not acc_obj or not self._schedulable(acc_obj, req_points=req_points):
                            continue
                        if on_account_assigned:
                            try:
                                on_account_assigned(account)
                            except Exception:
                                pass
                        try:
                            poll_balance_reported = False

                            def on_balance_cb(balance, source="", is_poll=False):
                                nonlocal poll_balance_reported
                                if is_poll:
                                    poll_balance_reported = True
                                self._set_credit_balance(account, balance, source)

                            result = await generate_video(
                                account, prompt, ratio, duration, model=model,
                                on_conversation_id=on_conversation_id, on_poll=on_poll,
                                on_balance=on_balance_cb, reference_image_paths=reference_image_paths,
                                check_cancelled=check_cancelled)
                            self._claim(account)
                            if not poll_balance_reported:
                                self.deduct_credits(account, req_points)
                            self._conn.execute(
                                "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                                (time.time(), account))
                            self._conn.commit()
                            return result
                        except CreditInsufficientError as e:
                            bal = getattr(e, "balance", None)
                            if bal is not None:
                                self._set_credit_balance(account, bal, str(e))
                            else:
                                self._set_credit_balance(account, max(0, req_points - 1), str(e))
                            print(f"[pool] {account} insufficient points for {req_points} pt(s) task: {e}", flush=True)
                            last_err = e
                            continue
                        except AccountLimitedError as e:
                            print(f"[pool] {account} reached daily limit, rotating: {e}", flush=True)
                            self._mark_daily_limit(account, str(e))
                            last_err = e
                            continue
                        except CreditError as e:
                            print(f"[pool] {account} out of quota, rotating: {e}", flush=True)
                            self._set_credit_balance(account, 0, str(e))
                            last_err = e
                            continue
                        except RiskControlError as e:
                            print(f"[pool] {account} risk control triggered (30m cooldown), rotating: {e}", flush=True)
                            self._conn.execute(
                                "UPDATE accounts_meta SET cooldown_until=? WHERE name=?",
                                (time.time() + COOLDOWN_SEC, account))
                            self._conn.commit()
                            last_err = e
                            continue
                        except PromptClarificationError as e:
                            self._claim(account)
                            self._conn.execute(
                                "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                                (time.time(), account))
                            self._conn.commit()
                            print(f"[pool] {account} prompt clarification error (rejecting task immediately): {e}", flush=True)
                            raise
                        except TimeoutError as e:
                            self._claim(account)
                            self._conn.execute(
                                "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                                (time.time(), account))
                            self._conn.commit()
                            raise
                        except FileNotFoundError as e:
                            print(f"[pool] {account} profile missing, skipping: {e}", flush=True)
                            last_err = e
                            continue

            # All schedulable accounts are currently occupied, wait and retry
            await asyncio.sleep(3)
