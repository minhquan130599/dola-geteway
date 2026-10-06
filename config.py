"""Dola Pool Configuration: Environment variable based with smart defaults."""
import os
from pathlib import Path


def _load_local_env():
    """Load ignored .env.local for local/tunnel runs; real environment wins."""
    path = Path(__file__).with_name(".env.local")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()
HOST = os.getenv("DOLA_HOST", "0.0.0.0")
PORT = int(os.getenv("DOLA_PORT", "8000"))

# Service API keys (comma-separated; empty = no auth, local debug only)
API_KEYS = [k.strip() for k in os.getenv("DOLA_API_KEYS", "").split(",") if k.strip()]

# Account pool cookie file (one profile|cookie-header entry per line)
COOKIES_FILE = os.getenv("DOLA_COOKIES_FILE", "cookies.txt")

# Max concurrent video generation tasks
MAX_CONCURRENCY = int(os.getenv("DOLA_MAX_CONCURRENCY", "3"))

# Global pending task queue limit (queued + processing), 0 = unlimited
MAX_PENDING_TASKS = int(os.getenv("DOLA_MAX_PENDING_TASKS", "100"))

# Video generation timeout in seconds
VIDEO_TIMEOUT = int(os.getenv("DOLA_VIDEO_TIMEOUT", "300"))

# SQLite database path
DB_PATH = os.getenv("DOLA_DB_PATH", "tasks.db")

# Video download storage directory (served statically by FastAPI)
DOWNLOAD_DIR = os.getenv("DOLA_DOWNLOAD_DIR", "downloads")

# Explicit browser proxy (must point to JP/KR egress; empty = system proxy)
PROXY = os.getenv("DOLA_PROXY", "")

# Run browser in headless mode (login always runs with head)
HEADLESS = os.getenv("DOLA_HEADLESS", "1") == "1"

# Base public URL for returning static video links
PUBLIC_BASE = os.getenv("DOLA_PUBLIC_BASE", f"http://127.0.0.1:{PORT}")

# Admin web dashboard password (empty = no auth)
ADMIN_KEY = os.getenv("DOLA_ADMIN_KEY", "")

# Dola 30s / Watermark Removal Chromium extension path
EXTENSION_DIR = os.getenv("DOLA_EXTENSION_DIR", "extensions/dola30")
EXTENSION_ENABLED = os.getenv("DOLA_EXTENSION_ENABLED", "1") == "1"

# Daily quota reset timezone (Japan midnight by default)
LOCALE = os.getenv("DOLA_LOCALE", "vi-VN")
TIMEZONE = os.getenv("DOLA_TIMEZONE", "Asia/Ho_Chi_Minh")
LIMIT_RESET_TZ = os.getenv("DOLA_LIMIT_RESET_TZ", "Asia/Tokyo")

# Conservative credit check before video generation (default 2 points)
VIDEO_REQUIRED_POINTS = int(os.getenv("DOLA_VIDEO_REQUIRED_POINTS", "2"))
DEFAULT_DAILY_CREDITS = int(os.getenv("DOLA_DEFAULT_DAILY_CREDITS", "6"))


def estimate_required_credits(model: str = "seedance-2.0", ratio: str = "16:9", duration: int = 10) -> int:
    """Calculates required Dola credit points based on generation mode.
    - Seedance 2.0, 9:16, 30s: 1 point
    - Seedance 2.0 (standard 10s/15s or other ratios): 2 points
    - Seedance 2.5 (<= 10s): 4 points
    - Seedance 2.5 (> 10s): 6 points
    """
    m = (model or "").lower().replace("-", "_").replace(".", "")
    dur = duration or 10
    rat = (ratio or "").strip()

    if "25" in m or "2_5" in m:
        return 4 if dur <= 10 else 6

    # Seedance 2.0 (standard or fast)
    if rat == "9:16" and dur == 30:
        return 1

    return VIDEO_REQUIRED_POINTS

# Public reference image download limits
REFERENCE_IMAGE_MAX_BYTES = int(os.getenv("DOLA_REFERENCE_IMAGE_MAX_BYTES", str(15 * 1024 * 1024)))
REFERENCE_DOWNLOAD_TIMEOUT = int(os.getenv("DOLA_REFERENCE_DOWNLOAD_TIMEOUT", "60"))
REFERENCE_IMAGE_MAX_COUNT = int(os.getenv("DOLA_REFERENCE_IMAGE_MAX_COUNT", "30"))

# Extended generation window for reference image tasks (seconds)
REFERENCE_VIDEO_TIMEOUT = int(os.getenv("DOLA_REFERENCE_VIDEO_TIMEOUT", "900"))
