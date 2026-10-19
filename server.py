"""Dola Pool: OpenAI-compatible Video API (FastAPI) and Admin Dashboard.

Endpoints (Asynchronous 2-stage):
POST /v1/videos/generations -> Create task (status=queued)
GET  /v1/videos/<id>         -> Query task status (queued/processing/completed/failed)
GET  /videos/<file>          -> Static video download server

Admin Dashboard: GET / -> web/index.html; Admin API /api/admin/*
"""
import sys

# Force UTF-8 for stdout/stderr on Windows (prevents 'charmap' codec errors with Vietnamese/CJK text)
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import asyncio
import base64
import hashlib
import json
import jwt
import re
import shutil
import time
import uuid
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
from add_account import add_account_flow
from browser_pool import AllAccountsLimitedError, AllAccountsQuotaBlockedError, BrowserPool
from media import download_reference_images, validate_reference_urls
from store import PendingTaskLimitExceeded, TaskQuotaExceeded, TaskStore
from video_worker_ui import ACTIVE_WORKER_BROWSERS, PromptClarificationError

Path(config.DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
Path("web").mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="dola-pool",
    version="0.4.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.middleware("http")
async def admin_auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/admin/") and path != "/api/admin/login":
        raw_token = request.headers.get("authorization") or request.headers.get("x-admin-key")
        if not raw_token:
            return JSONResponse(status_code=401, content={"detail": "Yêu cầu đăng nhập quản trị viên (Thiếu JWT token)"})
        if raw_token.startswith("Bearer "):
            raw_token = raw_token[7:].strip()
        try:
            payload = jwt.decode(raw_token, config.JWT_SECRET, algorithms=["HS256"])
            if payload.get("sub") != config.ADMIN_USERNAME:
                return JSONResponse(status_code=401, content={"detail": "JWT token không hợp lệ"})
        except jwt.ExpiredSignatureError:
            return JSONResponse(status_code=401, content={"detail": "Phiên đăng nhập đã hết hạn, vui lòng đăng nhập lại"})
        except Exception:
            if not (config.ADMIN_PASSWORD and raw_token == config.ADMIN_PASSWORD):
                return JSONResponse(status_code=401, content={"detail": "JWT token không hợp lệ hoặc đã hết hạn"})
    return await call_next(request)


store = TaskStore(config.DB_PATH)
pool = BrowserPool(max_concurrency=config.MAX_CONCURRENCY)

app.mount("/videos", StaticFiles(directory=config.DOWNLOAD_DIR), name="videos")


@app.get("/api/download_video/{filename}")
async def download_video_endpoint(filename: str):
    """Serves video file with Content-Disposition attachment to trigger local download on client machine."""
    safe_name = Path(filename).name
    dest = Path(config.DOWNLOAD_DIR) / safe_name
    if not dest.exists() or not dest.is_file():
        raise HTTPException(404, "Video file not found")
    return FileResponse(
        str(dest),
        media_type="application/octet-stream",
        filename=safe_name,
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'}
    )

# Background jobs (add/verify), in-memory
JOBS: dict[str, dict] = {}

SIZE_TO_RATIO = {
    "1280x720": "16:9", "1920x1080": "16:9",
    "720x1280": "9:16", "1080x1920": "9:16",
    "1024x1024": "1:1", "1440x1080": "4:3", "1080x1440": "3:4",
}
SUPPORTED_DURATIONS = (10, 15, 30)
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class KeyConcurrencyLimiter:
    """Concurrency limits per API Key; 0 = unlimited."""

    def __init__(self):
        self._condition = asyncio.Condition()
        self._active: defaultdict[str, int] = defaultdict(int)

    async def acquire(self, api_key_hash: str | None, limit: int):
        if not api_key_hash or limit <= 0:
            return
        async with self._condition:
            while self._active[api_key_hash] >= limit:
                await self._condition.wait()
            self._active[api_key_hash] += 1

    async def release(self, api_key_hash: str | None):
        if not api_key_hash:
            return
        async with self._condition:
            if self._active[api_key_hash] > 0:
                self._active[api_key_hash] -= 1
            if self._active[api_key_hash] == 0:
                self._active.pop(api_key_hash, None)
            self._condition.notify_all()



key_limiter = KeyConcurrencyLimiter()
RUNNING_TASKS: dict[str, asyncio.Task] = {}


# ===== Authentication =====


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _anonymous_client() -> dict:
    return {
        "api_key_hash": None,
        "api_key_name": "Anonymous",
        "daily_limit": 0,
        "concurrency_limit": 0,
        "allowed_durations": list(SUPPORTED_DURATIONS),
    }


def _env_client(key: str) -> dict:
    return {
        "api_key_hash": _hash_key(key),
        "api_key_name": f"Env Key ({key[:8]}…)",
        "daily_limit": 0,
        "concurrency_limit": 0,
        "allowed_durations": list(SUPPORTED_DURATIONS),
    }


def _auth(authorization):
    """Requires all client APIs to be authenticated with a valid API key created in the dashboard."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Authentication failed: API key required in Authorization header (Bearer <api_key>)")
    key = authorization[7:].strip()
    if not key:
        raise HTTPException(401, "Authentication failed: Missing API key in bearer token")

    record = store.get_key(key)
    if not record:
        if config.API_KEYS and key in config.API_KEYS:
            return _env_client(key)
        raise HTTPException(401, "Authentication failed: Invalid API key. Please use an API key generated in Dashboard.")

    if not store.is_key_valid(key):
        raise HTTPException(401, "Authentication failed: API key is disabled or expired.")

    store.touch_key(key)
    return {
        "api_key_hash": _hash_key(key),
        "api_key_name": record["name"] or "Dashboard Client",
        "daily_limit": record["daily_limit"],
        "concurrency_limit": record["concurrency_limit"],
        "allowed_durations": record["allowed_durations"],
    }


def _admin_auth(x_admin_key: str | None = None):
    if not x_admin_key:
        return {"sub": config.ADMIN_USERNAME, "role": "admin"}
    token = x_admin_key
    if token.startswith("Bearer "):
        token = token[7:].strip()

    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
        if payload.get("sub") != config.ADMIN_USERNAME:
            raise HTTPException(401, "JWT token không hợp lệ")
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Phiên đăng nhập đã hết hạn, vui lòng đăng nhập lại")
    except Exception:
        # Fallback for configured admin password
        if config.ADMIN_PASSWORD and token == config.ADMIN_PASSWORD:
            return {"sub": config.ADMIN_USERNAME, "role": "admin"}
        raise HTTPException(401, "JWT token không hợp lệ hoặc đã hết hạn")


def _normalize_allowed_durations(values) -> list[int]:
    if values is None:
        return list(SUPPORTED_DURATIONS)
    try:
        normalized = sorted({int(value) for value in values})
    except (TypeError, ValueError):
        raise HTTPException(422, "allowed_durations must be an array of 10, 15, or 30")
    if not normalized or any(value not in SUPPORTED_DURATIONS for value in normalized):
        raise HTTPException(422, "allowed_durations must contain at least one of 10, 15, 30")
    return normalized


# ===== Client API =====


class VideoGenRequest(BaseModel):
    model: str = "seedance-2.0"
    prompt: str = Field(..., min_length=1)
    size: str | None = None
    ratio: str | None = None
    duration: int | None = Field(None, ge=10, le=30)
    # Accepts durations: 10, 15, 30 seconds.
    reference_images: list[str] = Field(default_factory=list)


class TaskResponse(BaseModel):
    id: str
    status: str
    model: str | None = None
    prompt: str | None = None
    video_url: str | None = None
    error: str | None = None


def _resolve_ratio(size, ratio):
    if size and size in SIZE_TO_RATIO:
        return SIZE_TO_RATIO[size]
    return ratio


def resolve_video_url(url: str | None, request: Request | None = None) -> str | None:
    """Dynamically converts video URL to current public base URL or active request domain."""
    if not url:
        return None
    if "/videos/" in url:
        filename = url.rsplit("/videos/", 1)[-1]
        base = config.get_public_base(request)
        return f"{base}/videos/{filename}"
    return url


async def _run_task(task_id, model, prompt, ratio, duration, reference_images, client):
    RUNNING_TASKS[task_id] = asyncio.current_task()
    api_key_hash = client.get("api_key_hash")
    acquired = False
    reference_root = None
    try:
        await key_limiter.acquire(api_key_hash, client.get("concurrency_limit", 0))
        acquired = True

        def on_account_assigned(account):
            store.update(task_id, status="processing", account=account, started_at=time.time())

        def on_conversation_id(account, conversation_id, deadline_at):
            store.update(task_id, status="processing", account=account,
                         conversation_id=conversation_id, deadline_at=deadline_at,
                         last_poll_at=time.time())

        def on_poll(now):
            store.update(task_id, last_poll_at=now)

        def check_cancelled():
            t = store.get(task_id)
            return not t or t.get("status") in ("failed", "cancelled")

        reference_root, reference_paths = await download_reference_images(
            reference_images or [], task_id)
        result = await pool.generate_video(
            prompt, ratio, duration, model,
            on_conversation_id=on_conversation_id, on_poll=on_poll,
            reference_image_paths=reference_paths,
            check_cancelled=check_cancelled,
            on_account_assigned=on_account_assigned)
        public_url = f"{config.get_public_base()}/videos/{Path(result['local_path']).name}"
        store.update(task_id, status="completed", video_url=public_url,
                     account=result.get("account"), last_poll_at=time.time(),
                     finished_at=time.time())
    except (AllAccountsLimitedError, AllAccountsQuotaBlockedError) as e:
        store.update(task_id, status="failed", error=str(e)[:500],
                     failure_code="429", finished_at=time.time())
    except PromptClarificationError as e:
        curr = store.get(task_id)
        if not curr or curr.get("status") not in ("completed", "failed"):
            store.update(task_id, status="failed", error=str(e)[:500],
                         finished_at=time.time())
    except asyncio.CancelledError:
        curr = store.get(task_id)
        if not curr or curr.get("status") not in ("completed", "failed"):
            store.update(task_id, status="failed", error="Cancelled by user",
                         finished_at=time.time())
    except Exception as e:
        curr = store.get(task_id)
        if not curr or curr.get("status") not in ("completed", "failed"):
            store.update(task_id, status="failed", error=str(e)[:500],
                         finished_at=time.time())
    finally:
        RUNNING_TASKS.pop(task_id, None)
        if reference_root:
            shutil.rmtree(reference_root, ignore_errors=True)
        if acquired:
            await key_limiter.release(api_key_hash)


async def _queue_video(req: VideoGenRequest, client: dict) -> TaskResponse:
    """Validates and queues a video request for API clients and the dashboard."""
    duration = req.duration or 10
    if duration not in SUPPORTED_DURATIONS:
        raise HTTPException(422, "Currently supports durations of 10s, 15s, and 30s")
    if duration not in client["allowed_durations"]:
        raise HTTPException(422, f"Current API Key is not allowed to generate {duration}s videos")
    model_key = req.model.lower().replace("-", "_")
    if model_key not in (
        "seedance_2.0", "seedance_2.5", "seedance_v2.0", "seedance_v2.5",
        "seedance_20", "seedance_25", "seedance_v20", "seedance_v25",
    ):
        raise HTTPException(422, "Supported models are seedance-2.0 and seedance-2.5")
    try:
        reference_images = await validate_reference_urls(req.reference_images)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # Queue task when accounts are busy; reject only when the pool is fully exhausted.
    if not pool.available and pool.all_accounts_limited:
        raise HTTPException(429, "Rate limited: All accounts reached Dola daily video limit, please try again tomorrow")
    if not pool.available and pool.all_accounts_quota_blocked:
        raise HTTPException(429, "Insufficient credits: All accounts lack points, waiting for refresh")
    if not pool.accounts:
        raise HTTPException(503, "no account in pool")
    task_id = "video_" + uuid.uuid4().hex
    ratio = _resolve_ratio(req.size, req.ratio)
    try:
        store.create(
            task_id,
            req.model,
            req.prompt,
            ratio or "default",
            duration,
            reference_images=json.dumps(reference_images, ensure_ascii=False),
            api_key_hash=client["api_key_hash"],
            api_key_name=client["api_key_name"],
            daily_limit=client["daily_limit"],
            concurrency_limit=client["concurrency_limit"],
            max_pending=config.MAX_PENDING_TASKS,
        )
    except TaskQuotaExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    except PendingTaskLimitExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    asyncio.create_task(_run_task(
        task_id, req.model, req.prompt, ratio, duration, reference_images, client
    ))
    return TaskResponse(id=task_id, status="queued", model=req.model, prompt=req.prompt)


async def _resume_task(row: dict):
    task_id = row["id"]
    deadline = row.get("deadline_at") or (
        time.time() + (1800 if row.get("duration") == 30 else config.VIDEO_TIMEOUT)
    )
    remaining = max(1, int(deadline - time.time()))
    api_key_hash = row.get("api_key_hash")
    acquired = False
    try:
        await key_limiter.acquire(
            api_key_hash, int(row.get("client_concurrency_limit") or 0)
        )
        acquired = True
        store.update(task_id, status="processing", last_poll_at=time.time(),
                     started_at=row.get("started_at") or time.time())

        def on_poll(now):
            store.update(task_id, last_poll_at=now)

        def check_cancelled():
            t = store.get(task_id)
            return not t or t.get("status") in ("failed", "cancelled")

        result = await pool.resume_video(
            row["account"], row["conversation_id"], remaining, on_poll=on_poll,
            duration=row.get("duration"), check_cancelled=check_cancelled)
        public_url = f"{config.get_public_base()}/videos/{Path(result['local_path']).name}"
        store.update(task_id, status="completed", video_url=public_url,
                     account=result.get("account"), last_poll_at=time.time(),
                     finished_at=time.time())
    except Exception as e:
        store.update(task_id, status="failed", error=str(e)[:500],
                     finished_at=time.time())
    finally:
        if acquired:
            await key_limiter.release(api_key_hash)


def _task_client(row: dict) -> dict:
    """Restores client context from task snapshot."""
    return {
        "api_key_hash": row.get("api_key_hash"),
        "api_key_name": row.get("api_key_name") or "Historical Task",
        "daily_limit": 0,
        "concurrency_limit": int(row.get("client_concurrency_limit") or 0),
        "allowed_durations": list(SUPPORTED_DURATIONS),
    }


def _task_reference_images(raw) -> list[str]:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return values if isinstance(values, list) else []


@app.on_event("startup")
async def resume_incomplete_tasks():
    """Recovers accepted sessions on startup and requeues pending tasks."""
    for row in store.recoverable_tasks():
        asyncio.create_task(_resume_task(row))
    for row in store.recoverable_queued_tasks():
        ratio = row.get("ratio")
        if ratio == "default":
            ratio = None
        asyncio.create_task(_run_task(
            row["id"], row["model"], row["prompt"], ratio, row["duration"],
            _task_reference_images(row.get("reference_images")), _task_client(row),
        ))


@app.post("/v1/videos/generations", response_model=TaskResponse)
async def create_video(req: VideoGenRequest, authorization: str | None = Header(default=None)):
    return await _queue_video(req, _auth(authorization))


@app.get("/v1/videos/{task_id}", response_model=TaskResponse)
async def get_video(task_id: str, request: Request, authorization: str | None = Header(default=None)):
    client = _auth(authorization)
    row = store.get_for_client(task_id, client["api_key_hash"])
    if not row:
        raise HTTPException(404, "task not found")
    return TaskResponse(
        id=row["id"], status=row["status"], model=row["model"],
        prompt=row["prompt"], video_url=resolve_video_url(row["video_url"], request), error=row["error"],
    )


@app.post("/api/admin/videos/generations", response_model=TaskResponse)
async def admin_create_video(req: VideoGenRequest, x_admin_key: str | None = Header(default=None)):
    """Queues a video directly from the admin dashboard."""
    _admin_auth(x_admin_key)
    return await _queue_video(req, _anonymous_client())


@app.get("/api/admin/videos/{task_id}", response_model=TaskResponse)
async def admin_get_video(task_id: str, request: Request, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    row = store.get(task_id)
    if not row:
        raise HTTPException(404, "task not found")
    return TaskResponse(
        id=row["id"], status=row["status"], model=row["model"],
        prompt=row["prompt"], video_url=resolve_video_url(row["video_url"], request), error=row["error"],
    )


@app.get("/health")
async def health():
    return {
        "ok": True,
        "accounts": pool.account_status(),
        "available": pool.available,
        "pending_tasks": store.pending_task_count(),
        "max_pending_tasks": config.MAX_PENDING_TASKS,
    }


# ===== Admin Dashboard API =====


class AdminLogin(BaseModel):
    username: str | None = None
    password: str | None = None
    key: str | None = None


class AccountPatch(BaseModel):
    scheduling: bool | None = None
    note: str | None = None
    email: str | None = None
    proxy: str | None = None


class AccountAdd(BaseModel):
    name: str
    email: str
    password: str
    totp: str
    proxy: str | None = None


class FacebookAccountAdd(BaseModel):
    lines: str


class AccountReloginRequest(BaseModel):
    password: str | None = None
    secret: str | None = None
    proxy: str | None = None


class KeyCreate(BaseModel):
    name: str = ""
    daily_limit: int = Field(0, ge=0, le=1_000_000)
    concurrency_limit: int = Field(0, ge=0, le=1_000)
    allowed_durations: list[int] = Field(default_factory=lambda: list(SUPPORTED_DURATIONS))
    expires_at: float | None = Field(None, ge=0)


class KeyPatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    daily_limit: int | None = Field(None, ge=0, le=1_000_000)
    concurrency_limit: int | None = Field(None, ge=0, le=1_000)
    allowed_durations: list[int] | None = None
    expires_at: float | None = Field(None, ge=0)


@app.post("/api/admin/login")
async def admin_login(body: AdminLogin):
    is_valid = False
    if body.username and body.password:
        if body.username == config.ADMIN_USERNAME and body.password == config.ADMIN_PASSWORD:
            is_valid = True
    elif body.key:
        if body.key == config.ADMIN_PASSWORD or (config.ADMIN_KEY and body.key == config.ADMIN_KEY):
            is_valid = True

    if not is_valid:
        raise HTTPException(401, "Tên đăng nhập hoặc mật khẩu quản trị viên không chính xác")

    payload = {
        "sub": config.ADMIN_USERNAME,
        "role": "admin",
        "iat": int(time.time()),
        "exp": int(time.time()) + config.JWT_EXPIRE_SECONDS,
    }
    token = jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")
    return {
        "ok": True,
        "token": token,
        "username": config.ADMIN_USERNAME,
        "expires_in": config.JWT_EXPIRE_SECONDS,
    }


@app.get("/api/admin/me")
async def admin_me(x_admin_key: str | None = Header(default=None)):
    payload = _admin_auth(x_admin_key)
    return {"ok": True, "username": payload.get("sub", config.ADMIN_USERNAME)}


ACTIVE_USER_BROWSERS: dict[str, dict] = {}
ACTIVE_RELOGIN_BROWSERS: dict[str, dict] = {}


async def _run_user_browser(account: str):
    profile_dir = Path("accounts") / account
    if not profile_dir.exists():
        return
    lock = pool._locks.setdefault(account, asyncio.Lock())
    if lock.locked():
        raise RuntimeError("Tài khoản đang bận (đang tạo video hoặc đã mở trình duyệt)")

    async with lock:
        from patchright.async_api import async_playwright
        from browser import launch_account_context
        async with async_playwright() as p:
            ctx = await launch_account_context(p, account, headless=False, use_extension=True)
            ACTIVE_USER_BROWSERS[account] = {"context": ctx}
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto("https://www.dola.com/chat", wait_until="domcontentloaded")
                while True:
                    await asyncio.sleep(1)
                    if not ctx.pages or account not in ACTIVE_USER_BROWSERS:
                        break
            except Exception as e:
                print(f"[open_browser] {account} session closed: {e}", flush=True)
            finally:
                ACTIVE_USER_BROWSERS.pop(account, None)
                try:
                    await ctx.close()
                except Exception:
                    pass
                try:
                    await pool.verify_account(account)
                except Exception:
                    pass


async def _serve_browser_stream(ws: WebSocket, name: str, ctx, is_worker: bool = False, is_relogin: bool = False):
    """Handles CDP screencast and user interaction for standalone, worker, or relogin browser."""
    cur_task = asyncio.current_task()
    ACTIVE_USER_BROWSERS[name] = {"context": ctx, "ws": ws, "is_worker": is_worker, "is_relogin": is_relogin, "task": cur_task}

    cdp = None
    current_page = None
    notified_downloads = set()
    watcher_task = None

    try:
        # Pick the active page or first open page
        for pg in ctx.pages:
            if not pg.is_closed():
                current_page = pg
                break
        if not current_page:
            current_page = await ctx.new_page()

        if not is_worker and not is_relogin:
            try:
                await current_page.set_viewport_size({"width": 1280, "height": 950})
            except Exception:
                pass

        cdp = await ctx.new_cdp_session(current_page)
        try:
            win_info = await cdp.send("Browser.getWindowForTarget")
            if win_info and "windowId" in win_info:
                await cdp.send("Browser.setWindowBounds", {
                    "windowId": win_info["windowId"],
                    "bounds": {"width": 1296, "height": 1080, "windowState": "normal"}
                })
        except Exception:
            pass
        try:
            await cdp.send("Page.bringToFront")
        except Exception:
            pass

        if not is_worker and not is_relogin:
            if not current_page.url or current_page.url == "about:blank":
                try:
                    await current_page.goto("https://www.dola.com/chat", wait_until="domcontentloaded", timeout=45000)
                except Exception:
                    pass

        async def on_screencast_frame(params):
            session_id = params.get("sessionId")
            if not session_id or not cdp:
                return
            try:
                raw_b64 = params.get("data", "")
                if raw_b64:
                    jpeg_bytes = base64.b64decode(raw_b64)
                    await ws.send_bytes(jpeg_bytes)
            except Exception:
                pass
            finally:
                try:
                    if cdp:
                        await cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
                except Exception:
                    pass

        cdp.on("Page.screencastFrame", on_screencast_frame)
        await cdp.send("Page.startScreencast", {
            "format": "jpeg",
            "quality": 65,
            "maxWidth": 1280,
            "maxHeight": 1050,
            "everyNthFrame": 1
        })

        async def send_tabs_update():
            try:
                tabs = []
                for idx, pg in enumerate(ctx.pages):
                    if not pg.is_closed():
                        try:
                            t = await pg.title()
                        except Exception:
                            t = "Tab"
                        tabs.append({"id": idx, "url": pg.url, "title": t or "Tab"})
                active_idx = 0
                for idx, pg in enumerate(ctx.pages):
                    if pg == current_page:
                        active_idx = idx
                        break
                await ws.send_text(json.dumps({
                    "type": "tabs",
                    "tabs": tabs,
                    "active": active_idx,
                    "url": current_page.url,
                    "title": await current_page.title()
                }))
            except Exception:
                pass

        async def switch_to_page(target_page):
            nonlocal current_page, cdp
            if target_page == current_page or target_page.is_closed():
                return
            try:
                if cdp:
                    await cdp.send("Page.stopScreencast")
            except Exception:
                pass
            current_page = target_page
            if not is_worker:
                try:
                    await current_page.set_viewport_size({"width": 1280, "height": 950})
                except Exception:
                    pass
            cdp = await ctx.new_cdp_session(current_page)
            try:
                win_info = await cdp.send("Browser.getWindowForTarget")
                if win_info and "windowId" in win_info:
                    await cdp.send("Browser.setWindowBounds", {
                        "windowId": win_info["windowId"],
                        "bounds": {"width": 1296, "height": 1080, "windowState": "normal"}
                    })
            except Exception:
                pass
            try:
                await cdp.send("Page.bringToFront")
            except Exception:
                pass
            cdp.on("Page.screencastFrame", on_screencast_frame)
            await cdp.send("Page.startScreencast", {
                "format": "jpeg",
                "quality": 65,
                "maxWidth": 1280,
                "maxHeight": 1050,
                "everyNthFrame": 1
            })
            await send_tabs_update()

        async def notify_client_download(filename: str, f_path: Path):
            if filename in notified_downloads:
                return
            notified_downloads.add(filename)
            size = f_path.stat().st_size if f_path.exists() else 0
            print(f"[LiveStream Download Relay] -> Client: {filename} ({size} bytes)", flush=True)
            try:
                await ws.send_text(json.dumps({
                    "type": "download",
                    "filename": filename,
                    "url": f"/api/download_video/{filename}",
                    "size": size
                }))
            except Exception as ex:
                print(f"[LiveStream WS send error]: {ex}", flush=True)

        async def handle_download(dl):
            try:
                suggested = dl.suggested_filename or f"video_{int(time.time())}.mp4"
                if not re.search(r"\.(mp4|webm|mkv|mov|avi|jpg|jpeg|png)$", suggested, re.I):
                    suggested += ".mp4"
                safe_name = f"{name}_{int(time.time())}_{suggested}"
                dest_path = Path(config.DOWNLOAD_DIR) / safe_name
                await dl.save_as(str(dest_path))
                await notify_client_download(safe_name, dest_path)
            except Exception as ex:
                print(f"[LiveStream dl error]: {ex}", flush=True)

        def setup_page_listeners(pg):
            pg.on("framenavigated", lambda frame: asyncio.create_task(send_tabs_update()) if frame == pg.main_frame else None)
            pg.on("download", lambda dl: asyncio.create_task(handle_download(dl)))

        setup_page_listeners(current_page)

        async def on_new_page(new_pg):
            setup_page_listeners(new_pg)
            await asyncio.sleep(0.5)
            await switch_to_page(new_pg)

        ctx.on("page", lambda p: asyncio.create_task(on_new_page(p)))

        # Watch downloads folder for files written by Chrome extension or direct downloads
        initial_files = {f.name for f in Path(config.DOWNLOAD_DIR).iterdir() if f.is_file()}

        async def watch_downloads_loop():
            known = set(initial_files)
            while True:
                try:
                    await asyncio.sleep(1.0)
                    if not Path(config.DOWNLOAD_DIR).exists():
                        continue
                    for f in Path(config.DOWNLOAD_DIR).iterdir():
                        if not f.is_file():
                            continue
                        fname = f.name
                        if fname in known or fname.endswith(".crdownload") or fname.startswith("."):
                            continue
                        if f.suffix.lower() in [".mp4", ".webm", ".mkv", ".mov"]:
                            try:
                                s1 = f.stat().st_size
                                await asyncio.sleep(0.5)
                                s2 = f.stat().st_size
                                if s1 == s2 and s1 > 1000:
                                    known.add(fname)
                                    await notify_client_download(fname, f)
                            except Exception:
                                pass
                except asyncio.CancelledError:
                    break
                except Exception:
                    await asyncio.sleep(1.0)

        watcher_task = asyncio.create_task(watch_downloads_loop())

        vp = current_page.viewport_size or {"width": 1280, "height": 900}
        await ws.send_text(json.dumps({
            "type": "connected",
            "account": name,
            "viewport": vp,
            "url": current_page.url,
            "title": await current_page.title() if not current_page.is_closed() else "Dola",
            "is_worker": is_worker,
            "is_relogin": is_relogin
        }))
        await send_tabs_update()

        while True:
            msg_text = await ws.receive_text()
            try:
                cmd = json.loads(msg_text)
            except Exception:
                continue

            ctype = cmd.get("type")
            if ctype == "mouse":
                mevent = cmd.get("event")
                x = int(cmd.get("x", 0))
                y = int(cmd.get("y", 0))
                btn = cmd.get("button", "left")
                try:
                    if mevent == "move":
                        await current_page.mouse.move(x, y)
                    elif mevent == "down":
                        await current_page.mouse.move(x, y)
                        await current_page.mouse.down(button=btn)
                    elif mevent == "up":
                        await current_page.mouse.move(x, y)
                        await current_page.mouse.up(button=btn)
                    elif mevent == "click":
                        count = cmd.get("clickCount", 1)
                        await current_page.mouse.click(x, y, button=btn, click_count=count)
                    elif mevent == "wheel":
                        dx = int(cmd.get("deltaX", 0))
                        dy = int(cmd.get("deltaY", 0))
                        await current_page.mouse.move(x, y)
                        await current_page.mouse.wheel(dx, dy)
                except Exception:
                    pass
            elif ctype == "text":
                text_val = cmd.get("text", "")
                if text_val:
                    try:
                        await current_page.keyboard.insert_text(text_val)
                    except Exception:
                        pass
            elif ctype == "key_press" or ctype == "keyboard":
                key = cmd.get("key", "")
                ctrl = cmd.get("ctrl", False)
                meta = cmd.get("meta", False)
                shift = cmd.get("shift", False)
                alt = cmd.get("alt", False)
                kevent = cmd.get("event")
                if key:
                    try:
                        modifiers = []
                        if ctrl: modifiers.append("Control")
                        if meta: modifiers.append("Meta")
                        if alt: modifiers.append("Alt")
                        if shift: modifiers.append("Shift")
                        if modifiers and len(key) == 1:
                            combo = "+".join(modifiers + [key.lower()])
                            await current_page.keyboard.press(combo)
                        elif kevent == "down":
                            await current_page.keyboard.down(key)
                        elif kevent == "up":
                            await current_page.keyboard.up(key)
                        else:
                            await current_page.keyboard.press(key)
                    except Exception:
                        pass
            elif ctype == "navigate":
                nav_url = (cmd.get("url") or "").strip()
                if nav_url:
                    if not (nav_url.startswith("http://") or nav_url.startswith("https://")):
                        nav_url = "https://" + nav_url
                    try:
                        await current_page.goto(nav_url, wait_until="domcontentloaded", timeout=45000)
                        await send_tabs_update()
                    except Exception:
                        pass
            elif ctype == "back":
                try:
                    await current_page.go_back()
                    await send_tabs_update()
                except Exception:
                    pass
            elif ctype == "forward":
                try:
                    await current_page.go_forward()
                    await send_tabs_update()
                except Exception:
                    pass
            elif ctype == "reload":
                try:
                    await current_page.reload()
                    await send_tabs_update()
                except Exception:
                    pass
            elif ctype == "switch_tab":
                tab_id = int(cmd.get("index", 0))
                open_pgs = [p for p in ctx.pages if not p.is_closed()]
                if 0 <= tab_id < len(open_pgs):
                    await switch_to_page(open_pgs[tab_id])
            elif ctype == "new_tab":
                new_tab_url = cmd.get("url") or "https://www.dola.com/chat"
                n_page = await ctx.new_page()
                setup_page_listeners(n_page)
                try:
                    await n_page.goto(new_tab_url, wait_until="domcontentloaded")
                except Exception:
                    pass
                await switch_to_page(n_page)
            elif ctype == "close_tab":
                tab_id = int(cmd.get("index", 0))
                open_pgs = [p for p in ctx.pages if not p.is_closed()]
                if len(open_pgs) > 1 and 0 <= tab_id < len(open_pgs):
                    target = open_pgs[tab_id]
                    await target.close()
                    rem = [p for p in ctx.pages if not p.is_closed()]
                    if rem:
                        await switch_to_page(rem[0])
            elif ctype == "capture_video":
                try:
                    urls = await current_page.evaluate("""() => {
                        const list = [];
                        document.querySelectorAll('video').forEach(v => {
                            if (v.src && v.src.startsWith('http')) list.push(v.src);
                            v.querySelectorAll('source').forEach(s => {
                                if (s.src && s.src.startsWith('http')) list.push(s.src);
                            });
                        });
                        document.querySelectorAll('a[href*=".mp4"], a[download]').forEach(a => {
                            if (a.href && a.href.startsWith('http')) list.push(a.href);
                        });
                        return Array.from(new Set(list));
                    }""")
                    if urls:
                        from video_worker import _download
                        dl_path = await _download(urls[-1], name)
                        await notify_client_download(dl_path.name, dl_path)
                    else:
                        await ws.send_text(json.dumps({
                            "type": "status",
                            "text": "Không tìm thấy video nào trên trang hiện tại để tải."
                        }))
                except Exception as ex:
                    await ws.send_text(json.dumps({
                        "type": "status",
                        "text": f"Lỗi quét video: {ex}"
                    }))
            elif ctype == "close":
                break
    except WebSocketDisconnect:
        print(f"[ws_browser] Client disconnected from {name}", flush=True)
    except Exception as e:
        print(f"[ws_browser] Error in session {name}: {e}", flush=True)
    finally:
        if watcher_task and not watcher_task.done():
            watcher_task.cancel()
        ACTIVE_USER_BROWSERS.pop(name, None)
        try:
            if cdp:
                await cdp.send("Page.stopScreencast")
        except Exception:
            pass
        if not is_worker and not is_relogin:
            try:
                await ctx.close()
            except Exception:
                pass
        else:
            print(f"[ws_browser] Live stream detached from active session {name}", flush=True)


@app.websocket("/ws/browser/{name}")
async def ws_browser_stream(ws: WebSocket, name: str, token: str | None = Query(default=None), admin_key: str | None = Query(default=None)):
    """Method 3: Live Browser Stream with CDP Screencast, full remote interaction, and automatic client downloads."""
    await ws.accept()

    # Admin JWT authentication
    auth_token = token or admin_key
    try:
        _admin_auth(auth_token)
    except HTTPException as e:
        await ws.send_text(json.dumps({"type": "error", "message": e.detail}))
        await ws.close(code=1008)
        return

    if name not in pool.accounts:
        await ws.send_text(json.dumps({"type": "error", "message": f"Không tìm thấy tài khoản '{name}'"}))
        await ws.close(code=1008)
        return

    profile_dir = Path("accounts") / name
    if not profile_dir.exists():
        await ws.send_text(json.dumps({"type": "error", "message": f"Chưa có profile cho '{name}'"}))
        await ws.close(code=1008)
        return

    # 1. Check if account is actively running in a generation worker
    worker_browser = ACTIVE_WORKER_BROWSERS.get(name)
    is_worker = bool(worker_browser and worker_browser.get("context") and not worker_browser["context"].is_closed())
    if is_worker:
        ctx = worker_browser["context"]
        await ws.send_text(json.dumps({"type": "status", "text": f"Đang liên kết vào trình duyệt đang tạo video của '{name}'..."}))
        await _serve_browser_stream(ws, name, ctx, is_worker=True)
        return

    # 2. Check if account is actively running in a relogin flow
    relogin_browser = ACTIVE_RELOGIN_BROWSERS.get(name)
    is_relogin = bool(relogin_browser and relogin_browser.get("context") and not relogin_browser["context"].is_closed())
    if is_relogin:
        ctx = relogin_browser["context"]
        await ws.send_text(json.dumps({"type": "status", "text": f"Đang liên kết vào trình duyệt đang đăng nhập của '{name}'..."}))
        await _serve_browser_stream(ws, name, ctx, is_relogin=True)
        return

    lock = pool._locks.setdefault(name, asyncio.Lock())
    if lock.locked():
        # Account is locked (relogin or worker might be starting now) - wait up to 10 seconds
        for _ in range(20):
            await ws.send_text(json.dumps({"type": "status", "text": f"Đang chờ trình duyệt của '{name}' sẵn sàng..."}))
            await asyncio.sleep(0.5)
            # Recheck relogin
            rb = ACTIVE_RELOGIN_BROWSERS.get(name)
            if rb and rb.get("context") and not rb["context"].is_closed():
                ctx = rb["context"]
                await ws.send_text(json.dumps({"type": "status", "text": f"Đã kết nối vào trình duyệt đăng nhập của '{name}'!"}))
                await _serve_browser_stream(ws, name, ctx, is_relogin=True)
                return
            # Recheck worker
            wb = ACTIVE_WORKER_BROWSERS.get(name)
            if wb and wb.get("context") and not wb["context"].is_closed():
                ctx = wb["context"]
                await ws.send_text(json.dumps({"type": "status", "text": f"Đã kết nối vào trình duyệt tạo video của '{name}'!"}))
                await _serve_browser_stream(ws, name, ctx, is_worker=True)
                return
            if not lock.locked():
                break

    if lock.locked():
        await ws.send_text(json.dumps({
            "type": "error",
            "message": f"Tài khoản '{name}' đang bận (đang tạo video hoặc đang có phiên mở khác)"
        }))
        await ws.close(code=1008)
        return

    async with lock:
        from patchright.async_api import async_playwright
        from browser import launch_account_context

        cur_task = asyncio.current_task()
        ACTIVE_USER_BROWSERS[name] = {"task": cur_task, "is_worker": False}

        await ws.send_text(json.dumps({"type": "status", "text": f"Đang khởi động trình duyệt cho '{name}'..."}))

        async with async_playwright() as p:
            try:
                # Chromium persistent context with extension
                ctx = await launch_account_context(p, name, headless=config.HEADLESS, use_extension=True)
            except Exception as e:
                ACTIVE_USER_BROWSERS.pop(name, None)
                await ws.send_text(json.dumps({"type": "error", "message": f"Lỗi khởi động trình duyệt: {e}"}))
                await ws.close()
                return

            await _serve_browser_stream(ws, name, ctx, is_worker=False)

    try:
        asyncio.create_task(pool.verify_account(name))
    except Exception:
        pass


@app.get("/api/admin/accounts")
async def admin_accounts(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    accs = pool.list_accounts()
    for a in accs:
        is_worker = bool(a["name"] in ACTIVE_WORKER_BROWSERS)
        is_relogin = bool(a["name"] in ACTIVE_RELOGIN_BROWSERS)
        is_user = bool(a["name"] in ACTIVE_USER_BROWSERS)
        a["browser_open"] = bool(is_user or is_worker or is_relogin)
        a["worker_running"] = is_worker
        a["relogin_running"] = is_relogin
        lock = pool._locks.get(a["name"])
        a["busy"] = bool(lock and lock.locked())
    return {"accounts": accs}


@app.patch("/api/admin/accounts/{name}")
async def admin_account_patch(name: str, body: AccountPatch,
                              x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    if body.scheduling is not None:
        pool.set_scheduling(name, body.scheduling)
    if body.note is not None:
        pool.set_note(name, body.note)
    if body.email is not None:
        pool.set_email(name, body.email)
    if body.proxy is not None:
        pool.set_proxy(name, body.proxy)
    return {"ok": True}


@app.delete("/api/admin/accounts/{name}")
async def admin_account_delete(name: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    try:
        pool.delete_account(name)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/admin/accounts/{name}/verify")
async def admin_account_verify(name: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        ok = await pool.verify_account(name)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    return {"ok": ok}


@app.post("/api/admin/accounts/{name}/open_browser")
async def admin_account_open_browser(name: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    if name in ACTIVE_USER_BROWSERS or name in ACTIVE_WORKER_BROWSERS:
        return {"ok": True, "message": "Browser is already open"}
    lock = pool._locks.setdefault(name, asyncio.Lock())
    if lock.locked():
        raise HTTPException(409, "Tài khoản đang bận tạo video hoặc đã mở trình duyệt")
    asyncio.create_task(_run_user_browser(name))
    return {"ok": True, "message": f"Đã mở trình duyệt cho {name}"}


@app.post("/api/admin/accounts/{name}/close_browser")
async def admin_account_close_browser(name: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if name in ACTIVE_WORKER_BROWSERS:
        info = ACTIVE_USER_BROWSERS.pop(name, None)
        if info:
            ws = info.get("ws")
            if ws:
                try:
                    await ws.close()
                except Exception:
                    pass
            task = info.get("task")
            if task and not task.done():
                task.cancel()
        return {"ok": True, "message": f"Đã đóng live stream (worker của {name} vẫn tiếp tục chạy)"}

    if name in ACTIVE_RELOGIN_BROWSERS:
        info = ACTIVE_USER_BROWSERS.pop(name, None)
        if info:
            ws = info.get("ws")
            if ws:
                try:
                    await ws.close()
                except Exception:
                    pass
            task = info.get("task")
            if task and not task.done():
                task.cancel()
        relogin_info = ACTIVE_RELOGIN_BROWSERS.pop(name, None)
        if relogin_info and relogin_info.get("context"):
            try:
                await relogin_info["context"].close()
            except Exception:
                pass
        pool._locks[name] = asyncio.Lock()
        return {"ok": True, "message": f"Đã dừng phiên đăng nhập của {name}"}

    info = ACTIVE_USER_BROWSERS.pop(name, None)
    if info:
        ws = info.get("ws")
        if ws:
            try:
                await ws.close()
            except Exception:
                pass
        ctx = info.get("context")
        if ctx:
            try:
                await ctx.close()
            except Exception:
                pass
        task = info.get("task")
        if task and not task.done():
            task.cancel()

    # Reset lock if no worker is actively running
    if name not in ACTIVE_WORKER_BROWSERS and name not in ACTIVE_RELOGIN_BROWSERS:
        pool._locks[name] = asyncio.Lock()

    return {"ok": True, "message": f"Đã đóng và giải phóng trình duyệt của {name}"}


class ConfigPatch(BaseModel):
    headless: bool | None = None


@app.get("/api/admin/config")
async def admin_get_config(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {
        "headless": config.HEADLESS,
        "proxy": config.PROXY,
        "max_concurrency": config.MAX_CONCURRENCY,
        "locale": config.LOCALE,
        "extension_enabled": config.EXTENSION_ENABLED,
    }


@app.patch("/api/admin/config")
async def admin_patch_config(body: ConfigPatch, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if body.headless is not None:
        config.HEADLESS = body.headless
    return {"ok": True, "headless": config.HEADLESS}


async def _run_add_job(name: str, email: str, password: str, totp: str, proxy: str = ""):
    JOBS[name] = {"kind": "add", "status": "running", "error": "", "started_at": time.time()}
    try:
        await add_account_flow(name, email, password, totp, proxy=proxy or "")
        pool.set_email(name, email)
        pool.set_credentials(name, password, totp, account_type="google")
        pool.set_login_status(name, True)
        if proxy:
            pool.set_proxy(name, proxy)
        JOBS[name] = {**JOBS[name], "status": "success"}
    except Exception as e:
        JOBS[name] = {**JOBS[name], "status": "failed", "error": str(e)[:300]}


@app.post("/api/admin/accounts", status_code=202)
async def admin_account_add(body: AccountAdd, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if not NAME_RE.match(body.name):
        raise HTTPException(400, "invalid account name")
    if body.name in pool.accounts:
        raise HTTPException(409, "account exists")
    if JOBS.get(body.name, {}).get("status") == "running":
        raise HTTPException(409, "add job running")
    asyncio.create_task(_run_add_job(body.name, body.email, body.password, body.totp, body.proxy or ""))
    return {"ok": True, "job": "running"}


async def _run_fb_job(job_id: str, raw_lines: str):
    JOBS[job_id] = {"kind": "facebook_add", "status": "running", "error": "", "started_at": time.time(), "summary": ""}
    import importlib
    import add_account_fb
    importlib.reload(add_account_fb)
    from add_account_fb import add_fb_account_flow, parse_account_string
    lines = [l.strip() for l in raw_lines.splitlines() if l.strip() and not l.strip().startswith("#")]
    success_count = 0
    errors = []
    for line in lines:
        try:
            account, tk, password, secret, proxy = parse_account_string(line)
            res = await add_fb_account_flow(account, tk, password, secret, proxy=proxy or "")
            if res.get("ok"):
                pool._ensure_meta(account)
                pool.set_email(account, tk)
                pool.set_credentials(account, password, secret, account_type="facebook")
                pool.set_login_status(account, True)
                pool.set_note(account, "Facebook Login (vi-VN)")
                if proxy:
                    pool.set_proxy(account, proxy)
                success_count += 1
            else:
                errors.append(f"{account}: {res.get('error')}")
        except Exception as e:
            errors.append(f"{line[:25]}: {str(e)}")

    if errors and success_count == 0:
        JOBS[job_id] = {**JOBS[job_id], "status": "failed", "error": "; ".join(errors)[:300]}
    else:
        err_msg = f" (Warnings: {'; '.join(errors)[:120]})" if errors else ""
        JOBS[job_id] = {**JOBS[job_id], "status": "success", "summary": f"Success: {success_count}/{len(lines)}{err_msg}"}


@app.post("/api/admin/accounts/facebook", status_code=202)
async def admin_account_add_facebook(body: FacebookAccountAdd, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    raw = body.lines.strip()
    if not raw:
        raise HTTPException(400, "empty input")
    job_id = f"fb_{int(time.time())}"
    asyncio.create_task(_run_fb_job(job_id, raw))
    return {"ok": True, "job": job_id}


async def _run_relogin_job(job_id: str, account: str, email: str, password: str, secret: str = "", proxy: str = "", account_type: str = "facebook"):
    JOBS[job_id] = {"kind": "relogin", "account": account, "status": "running", "error": "", "started_at": time.time(), "summary": f"Đang đăng nhập lại {account}..."}
    lock = pool._locks.setdefault(account, asyncio.Lock())
    if lock.locked():
        JOBS[job_id] = {**JOBS[job_id], "status": "failed", "error": f"Tài khoản '{account}' đang bận (đang tạo video hoặc có phiên khác)"}
        return

    async with lock:
        try:
            async def on_browser_ready(ctx, pg):
                ACTIVE_RELOGIN_BROWSERS[account] = {"context": ctx, "page": pg, "started_at": time.time()}

            if account_type == "google":
                import add_account
                import importlib
                importlib.reload(add_account)
                from add_account import add_account_flow
                await add_account_flow(account, email, password, secret, proxy=proxy or "", on_browser_launched=on_browser_ready)
                res = {"ok": True}
            else:
                import importlib
                import add_account_fb
                importlib.reload(add_account_fb)
                from add_account_fb import add_fb_account_flow
                res = await add_fb_account_flow(account, email, password, secret, proxy=proxy or "", on_browser_launched=on_browser_ready)

            if res.get("ok"):
                pool._ensure_meta(account)
                pool.set_login_status(account, True)
                pool.set_credentials(account, password, secret, account_type=account_type)
                if proxy:
                    pool.set_proxy(account, proxy)
                JOBS[job_id] = {**JOBS[job_id], "status": "success", "summary": f"Đăng nhập lại thành công cho '{account}'!"}
            else:
                err = res.get("error") or "Đăng nhập thất bại"
                pool.set_login_status(account, False)
                JOBS[job_id] = {**JOBS[job_id], "status": "failed", "error": f"{account}: {err}"}
        except Exception as e:
            pool.set_login_status(account, False)
            JOBS[job_id] = {**JOBS[job_id], "status": "failed", "error": f"{account}: {str(e)}"}
        finally:
            ACTIVE_RELOGIN_BROWSERS.pop(account, None)


@app.post("/api/admin/accounts/{name}/relogin", status_code=202)
async def admin_account_relogin(name: str, body: AccountReloginRequest | None = None, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, f"Không tìm thấy tài khoản '{name}'")

    saved = pool.get_credentials(name)
    email = (saved.get("email") or name).strip()
    password = (body.password.strip() if body and body.password else "") or saved.get("password") or ""
    secret = (body.secret.strip() if body and body.secret else "") or saved.get("secret") or ""
    proxy = (body.proxy.strip() if body and body.proxy is not None else "") or saved.get("proxy") or ""
    acc_type = saved.get("account_type") or ("facebook" if name.startswith("fb_") else "google")

    if not password:
        raise HTTPException(400, "Tài khoản chưa lưu mật khẩu. Vui lòng nhập mật khẩu để đăng nhập lại.")

    # Save credentials if provided in request
    if body and (body.password or body.secret):
        pool.set_credentials(name, password, secret, account_type=acc_type)
    if body and body.proxy is not None:
        pool.set_proxy(name, proxy)

    job_id = f"relogin_{name}_{int(time.time())}"
    asyncio.create_task(_run_relogin_job(job_id, name, email, password, secret, proxy, acc_type))
    return {"ok": True, "job": job_id, "message": f"Đang tiến hành đăng nhập lại cho '{name}'..."}


@app.get("/api/admin/jobs")
async def admin_jobs(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {"jobs": JOBS}


@app.get("/api/admin/tasks")
async def admin_tasks(request: Request, limit: int = 50, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    tasks = store.recent_tasks(min(max(limit, 1), 200))
    for t in tasks:
        if t.get("video_url"):
            t["video_url"] = resolve_video_url(t["video_url"], request)
    return {"tasks": tasks}


@app.get("/api/admin/tasks/{task_id}")
async def admin_get_task(request: Request, task_id: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    task = store.get(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    task.pop("reference_images", None)
    if task.get("video_url"):
        task["video_url"] = resolve_video_url(task["video_url"], request)
    return {"task": task}



@app.post("/api/admin/tasks/{task_id}/cancel")
async def admin_task_cancel(task_id: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    task = store.get(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    store.update(task_id, status="failed", error="Cancelled by user", finished_at=time.time())
    running = RUNNING_TASKS.get(task_id)
    if running and not running.done():
        running.cancel()
    return {"ok": True, "task_id": task_id}


@app.get("/api/admin/stats")
async def admin_stats(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    st = store.stats()
    accs = pool.list_accounts()
    sched = [a for a in accs if a["scheduling"] and not a["cooling"]]
    st["total_accounts"] = len(accs)
    st["available_accounts"] = sum(1 for a in sched if a["remaining"] > 0)
    st["total_remaining"] = sum(a["remaining"] for a in sched)
    totals = st.pop("per_account_total", {})
    st["per_account"] = [{**a, "completed_total": totals.get(a["name"], 0)} for a in accs]
    return st


@app.get("/api/admin/keys")
async def admin_keys(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    keys = []
    for key in store.list_keys():
        usage = store.key_usage(store.hash_api_key(key["key"]))
        keys.append({**key, **{
            "today_total": usage["total"],
            "today_completed": usage["completed"],
            "today_failed": usage["failed"],
            "today_active": usage["active"],
            "today_queued": usage["queued"],
        }})
    return {"keys": keys, "env_keys": len(config.API_KEYS)}


@app.post("/api/admin/keys")
async def admin_key_create(body: KeyCreate, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    allowed = _normalize_allowed_durations(body.allowed_durations)
    return {"created": store.create_key(
        body.name,
        daily_limit=body.daily_limit,
        concurrency_limit=body.concurrency_limit,
        allowed_durations=allowed,
        expires_at=body.expires_at,
    )}


@app.patch("/api/admin/keys/{key}")
async def admin_key_patch(key: str, body: KeyPatch,
                          x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if not store.get_key(key):
        raise HTTPException(404, "api key not found")
    fields = {}
    if body.name is not None:
        fields["name"] = body.name
    if body.enabled is not None:
        fields["enabled"] = 1 if body.enabled else 0
    if body.daily_limit is not None:
        fields["daily_limit"] = body.daily_limit
    if body.concurrency_limit is not None:
        fields["concurrency_limit"] = body.concurrency_limit
    if body.allowed_durations is not None:
        fields["allowed_durations"] = _normalize_allowed_durations(body.allowed_durations)
    if body.expires_at is not None:
        fields["expires_at"] = body.expires_at
    store.update_key(key, **fields)
    return {"ok": True}


@app.delete("/api/admin/keys/{key}")
async def admin_key_delete(key: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if not store.get_key(key):
        raise HTTPException(404, "api key not found")
    store.delete_key(key)
    return {"ok": True}


# Dashboard single-file frontend
app.mount("/", StaticFiles(directory="web", html=True), name="web")

