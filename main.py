"""
Telegram Stream-on-Demand Server
ארכיטקטורה מפושטת: בוט אחד, מחובר ב-MTProto (לא Bot API HTTP), שמזרים
ישירות מהצ'אט המקורי שבו הוא קיבל את הקובץ. אין userbot, אין
SESSION_STRING, אין copy/forward ל-Saved Messages.

למה זה עובד בלי מגבלת 20MB?
ה-20MB הוא מגבלה של שכבת ה-HTTP Bot API (api.telegram.org/bot.../getFile)
בלבד. Pyrogram מדבר ישירות עם שרתי MTProto של טלגרם — אותו פרוטוקול
שאפליקציית טלגרם הרגילה משתמשת בו — ולכן לא כפוף למגבלה הזו. בוט
שמחובר עם bot_token דרך Pyrogram יכול להוריד/להזרים קבצים גדולים בלי
שום תחבולה.

זה מבטל לגמרי את הבאג "'NoneType' object has no attribute 'id'" כי
אין יותר שום קופי/פורוורד — המסר נשלף ישירות מהמיקום המקורי שלו.
"""

import os
import re
import sys
import time
import asyncio
import hashlib
import json
import logging
from difflib import SequenceMatcher
import httpx
from pathlib import Path
from urllib.parse import urlparse
from dotenv import load_dotenv
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, AsyncIterator, Optional, cast
from datetime import datetime, timezone

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from pyrogram import filters
from pyrogram.client import Client
from pyrogram.types import Message, ReplyKeyboardMarkup
from pyrogram.errors import FloodWait
import uvicorn

from catalog import Catalog
from cloudinary_storage import CloudinaryStorage
from memory_cache import ChunkMemoryCache
from stream_utils import RangeNotSatisfiable, content_disposition_filename, parse_range

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logging.getLogger("pyrogram").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

load_dotenv()


def parse_cloudinary_config(cloudinary_url: str) -> dict[str, str]:
    url = (cloudinary_url or "").strip()
    if not url:
        raise ValueError("CLOUDINARY_URL is empty")

    parsed = urlparse(url)
    if parsed.scheme != "cloudinary":
        raise ValueError("CLOUDINARY_URL must use the cloudinary:// scheme")
    if not parsed.username or not parsed.password or not parsed.hostname:
        raise ValueError(
            "CLOUDINARY_URL must be in the format cloudinary://<api_key>:<api_secret>@<cloud_name>"
        )

    return {
        "api_key": parsed.username,
        "api_secret": parsed.password,
        "cloud_name": parsed.hostname,
    }


# ── בדיקת משתני סביבה ──────────────────────────────────────────────────────
# SESSION_STRING, API_ID ו-API_HASH לא נדרשים מהמשתמש.
# הערכים האופציונליים מאפשרים להחליף את פרטי האפליקציה בלי לשנות קוד.
DEFAULT_API_ID = 6
DEFAULT_API_HASH = "eb06d4abfb49dc3e"
REQUIRED_ENV_VARS = ["BOT_TOKEN"]
_missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
if _missing:
    sys.exit(
        f"❌ חסרים משתני סביבה: {', '.join(_missing)}\n"
        f"   הגדר אותם ב-Render → Environment ונסה שוב."
    )

API_ID    = int(os.environ.get("API_ID", DEFAULT_API_ID))
API_HASH  = os.environ.get("API_HASH", DEFAULT_API_HASH)
BOT_TOKEN = os.environ["BOT_TOKEN"]
configured_cloudinary_url = os.environ.get("CLOUDINARY_URL", "").strip()
if not configured_cloudinary_url:
    cloudinary_key = os.environ.get("CLOUDINARY_API_KEY", "").strip()
    cloudinary_secret = os.environ.get("CLOUDINARY_API_SECRET", "").strip()
    cloudinary_name = os.environ.get("CLOUDINARY_CLOUD_NAME", "").strip()
    if cloudinary_key and cloudinary_secret and cloudinary_name:
        configured_cloudinary_url = f"cloudinary://{cloudinary_key}:{cloudinary_secret}@{cloudinary_name}"
CLOUDINARY = parse_cloudinary_config(configured_cloudinary_url) if configured_cloudinary_url else None
CLOUDINARY_STORAGE = CloudinaryStorage(configured_cloudinary_url)
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
PORT      = int(os.environ.get("PORT", 8000))
KEEP_ALIVE_INTERVAL = int(os.environ.get("KEEP_ALIVE_INTERVAL", 300))
LEAVE_UNAPPROVED_CHATS = os.environ.get("LEAVE_UNAPPROVED_CHATS", "1").strip().lower() in {
    "1", "true", "yes", "on",
}

# Render מגדיר את זה אוטומטית לכתובת הציבורית האמיתית של השירות.
BASE_URL = (
    os.environ.get("BASE_URL")
    or os.environ.get("RENDER_EXTERNAL_URL")
    or f"http://localhost:{PORT}"
).rstrip("/")

stats: dict[str, Any] = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "files_processed": 0,
    "links_generated": 0,
    "last_file": None,
    "last_ping": None,
}
STATE_TTL_SECONDS = max(60, int(os.environ.get("STATE_TTL_SECONDS", "600")))
STATE_CLEANUP_INTERVAL = max(30, int(os.environ.get("STATE_CLEANUP_INTERVAL", "120")))


def cleanup_expired_state() -> None:
    now = time.time()
    expired_user_ids = [
        user_id
        for user_id, state in user_states.items()
        if now - float(state.get("updated_at", now)) > STATE_TTL_SECONDS
    ]
    for user_id in expired_user_ids:
        user_states.pop(user_id, None)

    expired_batch_users = [
        user_id
        for user_id, state in auto_batch_states.items()
        if now - float(state.get("updated_at", now)) > STATE_TTL_SECONDS
    ]
    for user_id in expired_batch_users:
        auto_batch_states.pop(user_id, None)
        task = auto_batch_tasks.pop(user_id, None)
        if task is not None and not task.done():
            task.cancel()


async def cleanup_state_loop() -> None:
    while True:
        cleanup_expired_state()
        await asyncio.sleep(STATE_CLEANUP_INTERVAL)

def _catalog_database_path() -> str:
    configured_raw = os.environ.get("CATALOG_DB", "").strip()
    configured_path = Path(configured_raw) if configured_raw else Path("catalog.db")

    candidate_paths = [configured_path]
    candidate_roots = [
        Path("data"),
        Path.cwd() / "data",
        Path("/var/data"),
        Path("/tmp"),
    ]

    for root in candidate_roots:
        candidate_paths.append(root / "catalog.db")

    seen_paths: set[Path] = set()
    for candidate in candidate_paths:
        candidate = candidate.expanduser()
        if candidate in seen_paths:
            continue
        seen_paths.add(candidate)

        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            if os.access(candidate.parent, os.W_OK):
                if candidate != configured_path:
                    log.info("Using catalog storage at %s", candidate)
                return str(candidate)
        except OSError as error:
            log.info("Catalog storage path %s is unavailable (%s); trying next candidate", candidate, error)

    fallback_path = Path("/tmp/catalog.db")
    try:
        fallback_path.parent.mkdir(parents=True, exist_ok=True)
        if os.access(fallback_path.parent, os.W_OK):
            log.info("Using fallback catalog storage at %s", fallback_path)
            return str(fallback_path)
    except OSError as error:
        log.warning("Could not create fallback catalog storage at %s (%s)", fallback_path, error)

    return str(configured_path)


CATALOG_DB_PATH = _catalog_database_path()
if not DATABASE_URL and CLOUDINARY_STORAGE.restore_catalog(CATALOG_DB_PATH):
    log.info("Using restored Cloudinary catalog at %s", CATALOG_DB_PATH)


def _pyrogram_workdir() -> str:
    configured = (os.environ.get("SESSION_DIR") or os.environ.get("DATA_DIR") or "").strip()
    candidates: list[Path] = []

    if configured:
        candidates.append(Path(configured).expanduser())

    # Prefer a writable directory under the app workspace first, then fall back
    # to the familiar container locations when needed.
    candidates.extend(
        [
            Path.cwd() / "data",
            Path("/app/data"),
            Path("/var/data"),
            Path("/tmp"),
        ]
    )

    seen_paths: set[Path] = set()
    for candidate in candidates:
        absolute_candidate = candidate.expanduser()
        if not absolute_candidate.is_absolute():
            absolute_candidate = (Path.cwd() / absolute_candidate).resolve()

        if absolute_candidate in seen_paths:
            continue
        seen_paths.add(absolute_candidate)

        try:
            absolute_candidate.mkdir(parents=True, exist_ok=True)
            test_file = absolute_candidate / ".pyrogram-write-test"
            test_file.touch(exist_ok=True)
            test_file.unlink(missing_ok=True)
            log.info("Using Pyrogram session workdir %s", absolute_candidate)
            return str(absolute_candidate)
        except OSError as error:
            log.info("Pyrogram workdir %s is unavailable (%s); trying next candidate", absolute_candidate, error)

    fallback_path = (Path.cwd() / "data").resolve()
    fallback_path.mkdir(parents=True, exist_ok=True)
    log.warning("Falling back to Pyrogram session workdir %s", fallback_path)
    return str(fallback_path)


PYROGRAM_WORKDIR = _pyrogram_workdir()
log.info("Using Pyrogram session workdir %s", PYROGRAM_WORKDIR)

CATALOG = Catalog(CATALOG_DB_PATH, database_url=DATABASE_URL)
if CATALOG.is_postgres:
    log.info("Using PostgreSQL catalog from DATABASE_URL")
OWNER_USER_ID = 5699704187
CONFIGURED_ADMIN_USER_IDS = {
    int(value.strip())
    for value in os.environ.get("ADMIN_USER_IDS", "").split(",")
    if value.strip().isdigit()
}
for admin_id in CONFIGURED_ADMIN_USER_IDS:
    CATALOG.add_admin(admin_id)
    CATALOG.add_user(admin_id, OWNER_USER_ID)
CATALOG.add_admin(OWNER_USER_ID)
CATALOG.add_user(OWNER_USER_ID, OWNER_USER_ID)
ADMIN_USER_IDS = set(CATALOG.list_admins()) | {OWNER_USER_ID}
user_states: dict[int, dict[str, Any]] = {}
pending_metadata_by_user: dict[int, dict[str, Any]] = {}
auto_batch_states: dict[int, dict[str, Any]] = {}
auto_batch_tasks: dict[int, asyncio.Task[None]] = {}
auto_batch_locks: dict[int, asyncio.Lock] = {}
batch_locks: dict[int, asyncio.Lock] = {}
MAX_CONCURRENT_UPLOADS = min(4, max(1, int(os.environ.get("MAX_CONCURRENT_UPLOADS", "2"))))
upload_slots = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)
MESSAGE_CACHE_TTL = 60.0
message_cache: dict[tuple[int, int], tuple[float, Message]] = {}
message_cache_lock = asyncio.Lock()
MAX_ACTIVE_STREAMS = min(3, max(1, int(os.environ.get("MAX_ACTIVE_STREAMS", "2"))))
stream_slots = asyncio.Semaphore(MAX_ACTIVE_STREAMS)
active_streams = 0
STREAM_CACHE_MB = min(64, max(16, int(os.environ.get("STREAM_CACHE_MB", "32"))))
STREAM_CACHE_TTL = max(30, int(os.environ.get("STREAM_CACHE_TTL", "90")))
STREAM_READ_AHEAD = max(0, min(1, int(os.environ.get("STREAM_READ_AHEAD", "0"))))
chunk_cache = ChunkMemoryCache(STREAM_CACHE_MB * 1024 * 1024, STREAM_CACHE_TTL)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🎬 סרטים", "📺 סדרות"],
        ["📚 רשימה"],
        ["📊 דוח", "📖 מדריך", "ℹ️ אודות"],
        ["🛠️ ניהול"],
        ["⬅️ אחורה"],
    ],
    resize_keyboard=True,
)
MOVIES_KEYBOARD = ReplyKeyboardMarkup(
    [["🎬 צפה בסרט", "➕ הוסף סרט", "✏️ ערוך סרט"], ["🗑️ הסר סרט"], ["⬅️ חזרה"]],
    resize_keyboard=True,
)
SERIES_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📺 צפה בסדרה", "➕ הוסף סדרה", "✏️ ערוך סדרה"],
        ["🗑️ הסר סדרה", "➕ הוסף פרק"],
        ["🗑️ הסר עונה", "🗑️ הסר פרק"],
        ["⬅️ חזרה"],
    ],
    resize_keyboard=True,
)
UPLOAD_SEASON_KEYBOARD = ReplyKeyboardMarkup(
    [["✅ סיום"], ["🔄 רענן", "⬅️ אחורה"]],
    resize_keyboard=True,
)
OPTIONAL_FIELD_KEYBOARD = ReplyKeyboardMarkup(
    [["⏭️ דלג"], ["⬅️ אחורה"]],
    resize_keyboard=True,
)
WATCH_KEYBOARD = ReplyKeyboardMarkup(
    [["▶️ צפה עכשיו"], ["⬅️ אחורה"]],
    resize_keyboard=True,
)
EDIT_FIELD_KEYBOARD = ReplyKeyboardMarkup(
    [["✏️ שם", "📝 תקציר"], ["📅 שנת יציאה", "🖼️ פוסטר"], ["🎭 זאנר", "⬅️ אחורה"]],
    resize_keyboard=True,
)
MANAGEMENT_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["➕ הוסף מנהל", "➖ הסר מנהל"],
        ["✅ אשר משתמש", "🗑️ הסר משתמש"],
        ["✅ אשר קבוצה", "✅ אשר ערוץ"],
        ["🗑️ הסר צ׳אט", "📋 רשימות ניהול"],
        ["⬅️ חזרה"],
    ],
    resize_keyboard=True,
)
SKIP_WORDS = {"-", "דלג", "skip", "סבבה", "ok", "okay"}

# בוט אחד בלבד, מחובר ב-MTProto (לא Bot API HTTP) — גם מקבל הודעות וגם מזרים מהן.
bot_client = Client(
    name="stream_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=PYROGRAM_WORKDIR,
    in_memory=False,
)


@bot_client.on_message(filters.group | filters.channel, group=-1)  # type: ignore[reportUnknownMemberType, reportUntypedFunctionDecorator]
async def enforce_chat_allowlist(client: Client, message: Message) -> None:
    if not await reject_unauthorized(message):
        return
    chat_id = int(getattr(message.chat, "id", 0) or 0)
    if chat_id:
        if not LEAVE_UNAPPROVED_CHATS:
            return
        try:
            await client.leave_chat(chat_id)
            log.info("Left unapproved chat %s", chat_id)
        except Exception as error:
            log.warning("Could not leave unapproved chat %s: %s", chat_id, error)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    await bot_client.start()
    keep_alive_task = asyncio.create_task(keep_alive())
    catalog_sync_task = asyncio.create_task(catalog_sync_loop())
    cleanup_task = asyncio.create_task(cleanup_state_loop())
    log.info("All systems ready ✅ BASE_URL=%s", BASE_URL)
    try:
        yield
    finally:
        catalog_sync_task.cancel()
        cleanup_task.cancel()
        await sync_catalog_snapshot()
        keep_alive_task.cancel()
        await send_heartbeat("shutdown")
        await bot_client.stop()


async def sync_catalog_snapshot() -> None:
    if CATALOG.is_postgres or not CLOUDINARY_STORAGE.enabled:
        return
    try:
        CATALOG.checkpoint()
        synced = await asyncio.to_thread(CLOUDINARY_STORAGE.sync_catalog, CATALOG_DB_PATH)
        if synced:
            log.info("Catalog snapshot synchronized to Cloudinary")
    except Exception:
        log.exception("Catalog snapshot synchronization failed")


async def catalog_sync_loop() -> None:
    interval = max(30, int(os.environ.get("CATALOG_SYNC_INTERVAL", "60")))
    while True:
        await asyncio.sleep(interval)
        await sync_catalog_snapshot()


api = FastAPI(title="Telegram Stream Server", lifespan=lifespan)
cors_origins = [
    origin.strip()
    for origin in os.environ.get("CORS_ORIGINS", "*").split(",")
    if origin.strip()
]
api.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ── Stream helpers ────────────────────────────────────────────────────────────

async def fetch_message(chat_id: int, message_id: int) -> Message:
    cache_key = (chat_id, message_id)
    async with message_cache_lock:
        cached = message_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < MESSAGE_CACHE_TTL:
            return cached[1]
    for _ in range(5):
        try:
            msg = await bot_client.get_messages(chat_id, message_id)
            if isinstance(msg, list):
                raise HTTPException(status_code=404, detail="Message not found")
            async with message_cache_lock:
                message_cache[cache_key] = (time.monotonic(), msg)
            return msg
        except FloodWait as e:
            delay = e.value if isinstance(e.value, (int, float)) else 1.0
            log.warning("FloodWait %ss", delay)
            await asyncio.sleep(float(delay))
    raise HTTPException(status_code=429, detail="Rate limit")


PYROGRAM_CHUNK_SIZE = 1024 * 1024  # Pyrogram's chunk size is fixed at 1 MiB — not configurable


async def stream_chunks(
    msg: Message,
    start: int = 0,
    end: Optional[int] = None,
    file_size: Optional[int] = None,
) -> AsyncGenerator[bytes, None]:
    global active_streams
    if not msg or not msg.media:
        raise HTTPException(status_code=404, detail="No media in message")

    media = msg.audio or msg.video or msg.document or msg.video_note
    if not media:
        raise HTTPException(status_code=415, detail="Unsupported media type")

    first_chunk = start // PYROGRAM_CHUNK_SIZE
    skip = start % PYROGRAM_CHUNK_SIZE
    last_chunk = end // PYROGRAM_CHUNK_SIZE if end is not None else (
        (file_size - 1) // PYROGRAM_CHUNK_SIZE if file_size else None
    )
    to_send = (end - start + 1) if end is not None else None
    sent = 0
    prefetch_task: Optional[asyncio.Task[bytes]] = None

    async def load_chunk(chunk_index: int) -> bytes:
        chunks = cast(AsyncIterator[bytes], bot_client.stream_media(msg, offset=chunk_index, limit=1))
        parts: list[bytes] = []
        async for part in chunks:
            parts.append(part)
        return b"".join(parts)

    try:
        await asyncio.wait_for(stream_slots.acquire(), timeout=10.0)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=503, detail="Too many active streams") from exc
    active_streams += 1
    try:
        chunk_index = first_chunk
        while last_chunk is None or chunk_index <= last_chunk:
            if prefetch_task is not None:
                chunk = await prefetch_task
                prefetch_task = None
            else:
                chunk = await chunk_cache.get_or_load(
                    (int(msg.chat.id), int(msg.id), chunk_index),
                    lambda index=chunk_index: load_chunk(index),
                )
            if not chunk:
                break
            if skip > 0:
                if skip >= len(chunk):
                    skip -= len(chunk)
                    chunk_index += 1
                    continue
                chunk = chunk[skip:]
                skip = 0

            if to_send is not None:
                remaining = to_send - sent
                if remaining <= 0:
                    break
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]

            yield chunk
            sent += len(chunk)
            if to_send is not None and sent >= to_send:
                break
            if last_chunk is None and len(chunk) < PYROGRAM_CHUNK_SIZE:
                break
            next_chunk = chunk_index + 1
            if STREAM_READ_AHEAD and (last_chunk is None or next_chunk <= last_chunk):
                prefetch_task = asyncio.create_task(
                    chunk_cache.get_or_load(
                        (int(msg.chat.id), int(msg.id), next_chunk),
                        lambda index=next_chunk: load_chunk(index),
                    )
                )
            chunk_index += 1
    except asyncio.CancelledError:
        log.info("Stream cancelled after %s bytes", sent)
        raise
    finally:
        if prefetch_task is not None:
            if not prefetch_task.done():
                prefetch_task.cancel()
            else:
                prefetch_task.exception()
        active_streams -= 1
        stream_slots.release()

# ── Routes ────────────────────────────────────────────────────────────────────

@api.get("/stream/{chat_id}/{message_id}")
async def stream(chat_id: int, message_id: int, request: Request):
    msg = await fetch_message(chat_id, message_id)
    if not msg or not msg.media:
        raise HTTPException(status_code=404, detail="No media found")

    media     = msg.audio or msg.video or msg.document or msg.video_note
    if not media:
        raise HTTPException(status_code=415, detail="Unsupported media type")

    file_size = int(getattr(media, "file_size", 0) or 0)
    if file_size <= 0:
        raise HTTPException(status_code=503, detail="File size is unavailable")
    mime_type = getattr(media, "mime_type", "application/octet-stream")
    file_name = getattr(media, "file_name", f"file_{message_id}")

    range_header = request.headers.get("Range")
    if range_header:
        try:
            start, end = parse_range(range_header, file_size)
        except RangeNotSatisfiable as exc:
            raise HTTPException(
                status_code=416,
                detail="Range Not Satisfiable",
                headers={"Content-Range": f"bytes */{file_size}"},
            ) from exc
        headers = {
            "Content-Range":       f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges":       "bytes",
            "Content-Length":      str(end - start + 1),
            "Content-Disposition": content_disposition_filename(file_name),
            "Cache-Control": "no-store",
        }
        return StreamingResponse(
            stream_chunks(msg, start, end, file_size),
            status_code=206, media_type=mime_type, headers=headers,
        )

    headers = {
        "Accept-Ranges":       "bytes",
        "Content-Length":      str(file_size),
        "Content-Disposition": content_disposition_filename(file_name),
        "Cache-Control": "no-store",
    }
    return StreamingResponse(
        stream_chunks(msg, file_size=file_size),
        status_code=200, media_type=mime_type, headers=headers,
    )


@api.get("/api/v1/playback/{chat_id}/{message_id}")
async def playback_info(chat_id: int, message_id: int, mode: str = "auto"):
    """Return the lightweight direct Range stream without server-side downloads."""
    if mode not in {"auto", "direct"}:
        raise HTTPException(status_code=400, detail="mode must be auto or direct on this hosting plan")
    msg = await fetch_message(chat_id, message_id)
    if not msg or not msg.media:
        raise HTTPException(status_code=404, detail="Media not found")
    media = msg.audio or msg.video or msg.document or msg.video_note
    if not media:
        raise HTTPException(status_code=415, detail="Unsupported media type")
    file_size = int(getattr(media, "file_size", 0) or 0)
    mime_type = getattr(media, "mime_type", "application/octet-stream")
    direct_url = f"{BASE_URL}/stream/{chat_id}/{message_id}"
    payload: dict[str, Any] = {
        "api_version": 1,
        "source": {"chat_id": chat_id, "message_id": message_id, "size": file_size, "mime_type": mime_type},
        "direct": {"url": direct_url, "supports_range": True, "supports_seek": True},
        "selected": {"mode": "direct", "url": direct_url},
    }
    return JSONResponse(payload)


@api.get("/ping")
async def ping():
    stats["last_ping"] = datetime.now(timezone.utc).isoformat()
    return JSONResponse({"status": "ok", "active_streams": active_streams, "cache": await chunk_cache.stats()})


@api.get("/health")
async def health():
    """Dependency-aware health endpoint for monitoring and deployment checks."""
    database_ok = True
    database_error = None
    try:
        CATALOG.summary()
    except Exception as error:
        database_ok = False
        database_error = type(error).__name__
    bot_ok = bool(getattr(bot_client, "is_connected", False))
    status = "ok" if database_ok and bot_ok else "degraded"
    return JSONResponse(
        {
            "status": status,
            "database": "ok" if database_ok else "error",
            "database_error": database_error,
            "telegram": "connected" if bot_ok else "starting",
            "active_streams": active_streams,
            "stream_capacity": MAX_ACTIVE_STREAMS,
            "cache": await chunk_cache.stats(),
        },
        status_code=200 if database_ok else 503,
    )


@api.get("/api/catalog")
async def catalog_api():
    return JSONResponse({
        "items": CATALOG.list_items(),
        "summary": CATALOG.summary(),
    })


@api.get("/api/catalog/{item_id}")
async def catalog_item_api(item_id: int):
    item = CATALOG.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Catalog item not found")
    if item["kind"] == "series":
        item["seasons"] = CATALOG.list_seasons(item_id)
        item["episodes"] = CATALOG.list_episodes(item_id)
    return JSONResponse(item)


@api.get("/api/uploads")
async def uploads_api():
    return JSONResponse({"uploads": CATALOG.list_uploads()})


# ── Versioned API for the Android catalogue app ─────────────────────────────

def _public_item(item: dict[str, Any], include_stream: bool = False) -> dict[str, Any]:
    """Return a stable app-facing representation without Telegram internals."""
    allowed = {
        "id", "kind", "title", "summary", "release_year", "poster_url",
        "backdrop_url", "quality", "genre", "rating", "tmdb_id", "created_at", "updated_at",
    }
    result = {key: item.get(key) for key in allowed if key in item}
    if include_stream:
        result["stream_url"] = item.get("stream_url", "")
    return result


def _etag(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


@api.get("/api/v1/catalog")
async def catalog_v1(kind: Optional[str] = None, q: str = "", page: int = 1, limit: int = 50):
    """Paginated catalogue endpoint intended for the Android app."""
    if kind not in {None, "movie", "series"}:
        raise HTTPException(status_code=400, detail="kind must be movie or series")
    page = max(1, page)
    limit = min(100, max(1, limit))
    items = CATALOG.search(q, kind)
    total = len(items)
    start = (page - 1) * limit
    payload: dict[str, Any] = {
        "api_version": 1,
        "page": page,
        "limit": limit,
        "total": total,
        "items": [_public_item(item) for item in items[start:start + limit]],
        "summary": CATALOG.summary(),
    }
    return JSONResponse(payload, headers={"ETag": _etag(payload), "Cache-Control": "max-age=15"})


@api.get("/api/v1/catalog/{item_id}")
async def catalog_v1_item(item_id: int):
    item = CATALOG.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Catalog item not found")
    payload: dict[str, Any] = {"api_version": 1, "item": _public_item(item)}
    if item["kind"] == "series":
        payload["seasons"] = CATALOG.list_seasons(item_id)
        payload["episodes"] = [
            {key: value for key, value in episode.items() if key != "stream_url"}
            for episode in CATALOG.list_episodes(item_id)
        ]
    return JSONResponse(payload, headers={"ETag": _etag(payload), "Cache-Control": "max-age=15"})


@api.get("/api/v1/catalog/{item_id}/play")
async def catalog_v1_movie_play(item_id: int):
    item = CATALOG.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Catalog item not found")
    if item["kind"] != "movie":
        raise HTTPException(status_code=400, detail="Use the episode play endpoint for a series")
    if not item.get("stream_url"):
        raise HTTPException(status_code=409, detail="This title has no playable stream yet")
    return JSONResponse({"api_version": 1, "type": "movie", "item_id": item_id, "stream_url": item["stream_url"]})


@api.get("/api/v1/catalog/{series_id}/episodes/{episode_id}/play")
async def catalog_v1_episode_play(series_id: int, episode_id: int):
    series = CATALOG.get_item(series_id)
    if not series or series["kind"] != "series":
        raise HTTPException(status_code=404, detail="Series not found")
    episode = next((row for row in CATALOG.list_episodes(series_id) if int(row["id"]) == episode_id), None)
    if not episode:
        raise HTTPException(status_code=404, detail="Episode not found")
    if not episode.get("stream_url"):
        raise HTTPException(status_code=409, detail="This episode has no playable stream yet")
    return JSONResponse({
        "api_version": 1,
        "type": "episode",
        "series_id": series_id,
        "episode_id": episode_id,
        "season_number": episode["season_number"],
        "episode_number": episode["episode_number"],
        "title": episode["title"],
        "stream_url": episode["stream_url"],
    })


@api.get("/", response_class=HTMLResponse)
async def dashboard():
    html = f"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Telegram Stream Dashboard</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: 'Segoe UI', sans-serif; background: #0f0f0f; color: #e0e0e0; min-height: 100vh; padding: 24px 16px; }}
    h1 {{ font-size: 1.6rem; color: #fff; margin-bottom: 6px; }}
    .subtitle {{ color: #888; font-size: 0.9rem; margin-bottom: 28px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 28px; }}
    .card {{ background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 12px; padding: 20px 16px; text-align: center; }}
    .card .num {{ font-size: 2rem; font-weight: 700; color: #4f9eff; }}
    .card .label {{ font-size: 0.8rem; color: #888; margin-top: 6px; }}
    .section {{ background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 12px; padding: 20px; margin-bottom: 20px; }}
    .section h2 {{ font-size: 1rem; color: #aaa; margin-bottom: 14px; border-bottom: 1px solid #2a2a2a; padding-bottom: 10px; }}
    .row {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #222; font-size: 0.88rem; }}
    .row:last-child {{ border-bottom: none; }}
    .row .key {{ color: #888; }}
    .row .val {{ color: #ddd; word-break: break-all; text-align: left; max-width: 65%; }}
    .status-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; background: #22c55e; margin-left: 8px; animation: pulse 2s infinite; }}
    @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.4; }} }}
    .how {{ background: #111; border: 1px solid #2a2a2a; border-radius: 8px; padding: 14px 16px; font-size: 0.82rem; color: #aaa; line-height: 1.8; }}
    .how code {{ background: #222; padding: 2px 6px; border-radius: 4px; color: #4f9eff; font-size: 0.8rem; }}
  </style>
</head>
<body>
  <h1>📡 Telegram Stream Server <span class="status-dot"></span></h1>
  <p class="subtitle">MTProto streaming — בוט יחיד, בלי 20MB limit</p>
  <div class="grid">
    <div class="card"><div class="num">{stats['files_processed']}</div><div class="label">קבצים שהתקבלו</div></div>
    <div class="card"><div class="num">{stats['links_generated']}</div><div class="label">קישורים שנוצרו</div></div>
    <div class="card"><div class="num" style="font-size:1rem;margin-top:8px">{stats['started_at'][:10]}</div><div class="label">פעיל מאז</div></div>
  </div>
  <div class="section">
    <h2>📊 מידע נוסף</h2>
    <div class="row"><span class="key">קובץ אחרון</span><span class="val">{stats['last_file'] or '—'}</span></div>
    <div class="row"><span class="key">פינג אחרון</span><span class="val">{stats['last_ping'] or '—'}</span></div>
    <div class="row"><span class="key">Base URL</span><span class="val">{BASE_URL}</span></div>
  </div>
  <div class="section">
    <h2>🎬 איך משתמשים?</h2>
    <div class="how">
      1. שלח לבוט קובץ וידאו / אודיו<br>
      2. קבל קישור סטרימינג מיידי ✅<br>
      3. עובד בכל נגן עם Seek מלא, גם מעל 20MB 🎬<br><br>
      <strong>פורמט URL:</strong><br>
      <code>{BASE_URL}/stream/CHAT_ID/MESSAGE_ID</code>
    </div>
  </div>
</body>
</html>"""
    return HTMLResponse(content=html)

# ── Bot handler ───────────────────────────────────────────────────────────────

async def _upload_to_cloudinary_in_background(
    client: Client,
    message: Message,
    *,
    upload_id: int,
    chat_id: int,
    message_id: int,
    file_name: str,
    mime_type: str,
) -> None:
    try:
        async with upload_slots:
            cloudinary_result = await CLOUDINARY_STORAGE.upload_downloaded_media(
                client,
                message,
                chat_id=chat_id,
                message_id=message_id,
                file_name=file_name,
                mime_type=mime_type,
            )
    except Exception:
        log.exception(
            "Background Cloudinary upload failed for Telegram message %s/%s",
            chat_id,
            message_id,
        )
        return

    cloudinary_stream_url = CLOUDINARY_STORAGE.storage_url(cloudinary_result)
    if not cloudinary_stream_url:
        log.warning(
            "Cloudinary background upload completed without a usable URL for %s/%s",
            chat_id,
            message_id,
        )
        return

    CATALOG.update_upload_stream_url(upload_id, cloudinary_stream_url)
    log.info(
        "Cloudinary background upload finished for %s/%s; upgraded stream URL",
        chat_id,
        message_id,
    )


@bot_client.on_message((filters.private | filters.group | filters.channel) & filters.photo, group=1)  # type: ignore[reportUnknownMemberType, reportUntypedFunctionDecorator]
async def handle_photo_metadata(client: Client, message: Message):
    if await reject_unauthorized(message):
        return
    user_id = message.from_user.id if message.from_user else 0
    metadata_text = (message.caption or "").strip()
    parsed_caption = parse_media_caption(metadata_text) if metadata_text else None

    if parsed_caption is None:
        parsed_caption = pending_metadata_by_user.get(user_id)

    poster_url = ""
    if message.photo and CLOUDINARY_STORAGE.enabled:
        try:
            cloudinary_result = await CLOUDINARY_STORAGE.upload_downloaded_media(
                client,
                message,
                chat_id=int(message.chat.id),
                message_id=int(message.id),
                file_name=f"poster-{message.id}.jpg",
                mime_type="image/jpeg",
            )
            poster_url = CLOUDINARY_STORAGE.storage_url(cloudinary_result)
        except Exception:
            log.exception(
                "Could not upload photo poster for %s/%s",
                message.chat.id,
                message.id,
            )

    if parsed_caption is None and not poster_url:
        await reply(message, "📷 קיבלתי את התמונה. שלח גם כיתוב עם שם הסדרה/הסרט כדי להשתמש בה כפוסטר.")
        return

    if parsed_caption is not None:
        if poster_url:
            parsed_caption = dict(parsed_caption)
            parsed_caption["poster_url"] = poster_url
        pending_metadata_by_user[user_id] = parsed_caption

        existing_link_message = attach_metadata_to_existing_catalog(parsed_caption)
        if existing_link_message:
            await reply(message, existing_link_message)
            return

    if poster_url and parsed_caption is None:
        pending_metadata_by_user[user_id] = {"poster_url": poster_url}
        await reply(message, "📷 הפוסטר הועלה והוכן לשימוש. שלח עכשיו את פרטי הסדרה/הסרט כדי לחבר אותו.")
        return

    await reply(message, "✅ שמרתי את פרטי הסדרה/הפוסטר. אפשר לשלוח עכשיו את הקבצים.")


@bot_client.on_message((filters.private | filters.group | filters.channel) & (filters.video | filters.audio | filters.document | filters.video_note))  # type: ignore[reportUnknownMemberType, reportUntypedFunctionDecorator]
async def handle_media(client: Client, message: Message):
    if await reject_unauthorized(message):
        return
    stats["files_processed"] += 1
    user_id = message.from_user.id if message.from_user else 0
    state = user_states.get(user_id)
    if state is not None:
        state["updated_at"] = time.time()
    is_batch_upload = is_batch_upload_state(state)
    batch_lock: Optional[asyncio.Lock] = None
    if is_batch_upload:
        batch_lock = batch_locks.setdefault(user_id, asyncio.Lock())
        # Telegram can deliver several media updates concurrently. Serialize a
        # user's explicit season upload so next_episode cannot be duplicated.
        await batch_lock.acquire()
    try:
        media     = message.video or message.audio or message.document or message.video_note
        file_name = getattr(media, "file_name", "קובץ")
        file_size = getattr(media, "file_size", 0)
        size_mb   = round(file_size / 1024 / 1024, 1)

        # Upload a durable copy when Cloudinary is configured, but do not block
        # the Telegram reply on that slower background operation. We keep the
        # direct MTProto stream URL as the immediate response and then upgrade the
        # saved record in the background when Cloudinary finishes.
        telegram_stream_url = f"{BASE_URL}/stream/{message.chat.id}/{message.id}"
        stream_url = telegram_stream_url

        upload_id = CATALOG.save_upload(
            file_name, file_size, getattr(media, "mime_type", ""), stream_url,
            message.chat.id, message.id,
        )
        if CLOUDINARY_STORAGE.enabled:
            asyncio.create_task(
                _upload_to_cloudinary_in_background(
                    client,
                    message,
                    upload_id=upload_id,
                    chat_id=int(message.chat.id),
                    message_id=int(message.id),
                    file_name=file_name,
                    mime_type=getattr(media, "mime_type", ""),
                )
            )
        if state and state.get("step") == "batch_media":
            episode_number = int(state["next_episode"])
            try:
                episode_id = CATALOG.add_episode(
                    int(state["series_id"]), int(state["season"]), episode_number,
                    f"פרק {episode_number}", stream_url,
                )
                CATALOG.attach_upload(upload_id, episode_id)
                state["uploaded"] = int(state.get("uploaded", 0)) + 1
                state["summary_sent"] = False
                state.setdefault("saved_items", []).append(
                    {
                        "kind": "episode",
                        "title": f"{state.get('series_title', 'סדרה')} — עונה {state['season']} פרק {episode_number}",
                        "series_title": state.get("series_title", "סדרה"),
                        "season": int(state["season"]),
                        "episode": episode_number,
                        "stream_url": stream_url,
                        "quality": "",
                        "genre": "",
                        "summary": "",
                        "release_year": None,
                    }
                )
                state["last_message"] = message
                auto_batch_states[user_id] = state
                queue_auto_batch_summary(user_id, message)
            except Exception:
                state["failed"] = int(state.get("failed", 0)) + 1
                state["summary_sent"] = False
                state.setdefault("failed_episodes", []).append(
                    (int(state["season"]), episode_number)
                )
                log.exception(
                    "Failed to save season %s episode %s",
                    state["season"], episode_number,
                )
                state["last_message"] = message
                auto_batch_states[user_id] = state
                queue_auto_batch_summary(user_id, message)
            state["next_episode"] = episode_number + 1
            return
        if state and state.get("step") == "media":
            data = state["data"]
            if state["flow"] == "movie":
                item_id = CATALOG.add_item(
                    "movie", data["title"], data["summary"], data["year"],
                    data["poster_url"], stream_url, data.get("backdrop_url", ""),
                    data.get("rating"), data.get("tmdb_id"),
                )
                CATALOG.attach_upload(upload_id, item_id)
                result = f"✅ הסרט **{data['title']}** נוסף לקטלוג (#{item_id})."
            else:
                series_id = int(data["series_id"])
                episode_id = CATALOG.add_episode(
                    series_id, data["season"], data["episode"], data["title"], stream_url,
                )
                CATALOG.attach_upload(upload_id, episode_id)
                result = (
                    f"✅ פרק {data['episode']} בעונה {data['season']} נוסף לסדרה "
                    f"**{data['series_title']}**."
                )
            user_states.pop(user_id, None)
            await reply(message, result)
            return

        # Captions are preferred, but common filenames such as
        # "Fauda.S01E02.1080p.mkv" are also enough to classify a file. This
        # lets users send mixed-series batches without manually captioning each
        # item, as long as the filename contains the series and SxxEyy data.
        metadata_text = message.caption or re.sub(
            r"[._]+", " ", Path(file_name or "").stem
        )
        pending_metadata = pending_metadata_by_user.pop(user_id, None)
        parsed_caption = parse_media_caption(metadata_text)
        if pending_metadata:
            parsed_caption = merge_pending_metadata(parsed_caption, pending_metadata)
        if parsed_caption:
            auto_batch_lock: Optional[asyncio.Lock] = None
            if parsed_caption.get("kind") == "episode":
                auto_batch_lock = auto_batch_locks.setdefault(user_id, asyncio.Lock())
                await auto_batch_lock.acquire()
            try:
                result_message, saved_item = await auto_catalog_media(parsed_caption, stream_url, upload_id)
            except Exception:
                if parsed_caption.get("kind") == "episode" and not is_batch_upload_state(state):
                    batch_state = auto_batch_states.setdefault(
                        user_id,
                        {
                            "uploaded": 0,
                            "failed": 0,
                            "failed_episodes": [],
                            "saved_items": [],
                            "groups": {},
                            "last_message": message,
                        },
                    )
                    batch_state["failed"] = int(batch_state.get("failed", 0)) + 1
                    batch_state["failed_episodes"].append(
                        (parsed_caption.get("season"), parsed_caption.get("episode"))
                    )
                    group = str(parsed_caption.get("title_candidates", ["לא ידוע"])[0])
                    batch_state.setdefault("groups", {}).setdefault(group, {"uploaded": 0, "failed": 0})["failed"] += 1
                    batch_state["last_message"] = message
                    queue_auto_batch_summary(user_id, message)
                    return
                raise
            finally:
                if auto_batch_lock is not None and auto_batch_lock.locked():
                    auto_batch_lock.release()
            if parsed_caption.get("kind") == "episode" and not is_batch_upload_state(state):
                batch_state = auto_batch_states.setdefault(
                    user_id,
                    {
                        "uploaded": 0,
                        "failed": 0,
                        "failed_episodes": [],
                        "saved_items": [],
                        "groups": {},
                        "last_message": message,
                    },
                )
                batch_state["uploaded"] = int(batch_state.get("uploaded", 0)) + 1
                group = str(parsed_caption.get("title_candidates", ["לא ידוע"])[0])
                batch_state.setdefault("groups", {}).setdefault(group, {"uploaded": 0, "failed": 0})["uploaded"] += 1
                if saved_item:
                    batch_state.setdefault("saved_items", []).append(saved_item)
                batch_state["last_message"] = message
                queue_auto_batch_summary(user_id, message)
                return
            if result_message:
                await reply(message, result_message)
                return

        partial = parse_partial_episode_reference(metadata_text)
        chat_type = getattr(message.chat, "type", "")
        is_channel = (
            getattr(chat_type, "value", chat_type) == "channel"
            or getattr(chat_type, "name", "").lower() == "channel"
        )
        if is_channel:
            stats["links_generated"] += 1
            stats["last_file"] = f"{file_name} ({size_mb}MB)"
            await reply(message,
                "✅ הקובץ התקבל בערוץ ונוצר קישור סטרימינג.\n\n"
                f"🔗 `{stream_url}`\n\n"
                "כדי לשייך אותו לקטלוג, צרף כיתוב לפוסט בפורמט:\n"
                "`שם הסדרה עונה 1 פרק 1`"
            )
            return
        user_states[user_id] = {
            "flow": "unlabeled_media",
            "step": "unlabeled_series",
            "data": {
                "upload_id": upload_id,
                "stream_url": stream_url,
                "file_name": file_name,
                "season": partial["season"],
                "episode": partial["episode"],
            },
        }
        await reply(message,
            "📺 קיבלתי את הקובץ.\n"
            "לאיזו סדרה הוא שייך? כתוב את שם הסדרה."
        )
        return

        stats["links_generated"] += 1
        stats["last_file"] = f"{file_name} ({size_mb}MB)"

        await reply(message,
            f"✅ **קישור סטרימינג מוכן!**\n\n"
            f"📄 קובץ: `{file_name}`\n"
            f"📦 גודל: {size_mb} MB\n\n"
            f"🔗 **קישור:**\n`{stream_url}`\n\n"
            f"_הקישור תומך ב-Seek מלא ועובד בכל נגן_ 🎬"
        )
        log.info("Stream link: %s", stream_url)

    except Exception as e:
        log.exception("Error handling media")
        if is_batch_upload and state:
            episode_number = int(state["next_episode"])
            state["failed"] = int(state.get("failed", 0)) + 1
            state["summary_sent"] = False
            state.setdefault("failed_episodes", []).append(
                (int(state["season"]), episode_number)
            )
            state["next_episode"] = episode_number + 1
            state["last_message"] = message
            auto_batch_states[user_id] = state
            queue_auto_batch_summary(user_id, message)
            log.error(
                "Failed to process season %s episode %s: %s",
                state["season"], episode_number, e,
            )
        else:
            await reply(message, f"❌ שגיאה: {str(e)}")
    finally:
        if batch_lock is not None and batch_lock.locked():
            batch_lock.release()


@bot_client.on_message((filters.private | filters.group | filters.channel) & filters.command("start"))  # type: ignore[reportUnknownMemberType, reportUntypedFunctionDecorator]
async def start_command(client: Client, message: Message):
    if await reject_unauthorized(message):
        return
    await cast(Any, message).reply_text(
        "👋 **שלום!**\n\n"
        "אני מנהל קטלוג סרטים וסדרות, יוצר קישורי סטרימינג ומדבר איתך גם בטקסט.\n\n"
        "בחר פעולה מהתפריט או כתוב /help לקבלת פקודות.\n\n"
        "שלח קובץ בכל רגע כדי לקבל קישור סטרימינג. ✅",
        reply_markup=MAIN_KEYBOARD,
    )


def is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in ADMIN_USER_IDS)


def is_owner(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == OWNER_USER_ID)


def is_user_allowed(message: Message) -> bool:
    user_id = message.from_user.id if message.from_user else 0
    return bool(
        user_id
        and (user_id == OWNER_USER_ID or user_id in ADMIN_USER_IDS
             or CATALOG.is_user_approved(user_id))
    )


def refresh_admins() -> None:
    ADMIN_USER_IDS.clear()
    ADMIN_USER_IDS.update(CATALOG.list_admins())
    ADMIN_USER_IDS.add(OWNER_USER_ID)


def chat_type_name(chat: Any) -> str:
    chat_type = getattr(chat, "type", "")
    return str(getattr(chat_type, "value", chat_type)).lower()


def is_chat_allowed(message: Message) -> bool:
    if chat_type_name(message.chat) in {"private", "bot"}:
        return True
    chat_id = int(getattr(message.chat, "id", 0) or 0)
    return bool(chat_id and CATALOG.is_chat_registered(chat_id))


async def reject_unauthorized(message: Message) -> bool:
    private_chat = chat_type_name(message.chat) in {"private", "bot"}
    allowed = is_user_allowed(message) if private_chat else is_chat_allowed(message)
    if allowed:
        return False
    scope = (
        f"user:{message.from_user.id}"
        if private_chat and message.from_user
        else f"chat:{getattr(message.chat, 'id', 0)}"
    )
    if CATALOG.claim_access_notice(scope):
        await cast(Any, message).reply_text(
            "⛔ אין לך הרשאות להפעיל את הבוט.\n"
            "יש לפנות למנהל אלון נושם."
        )
    return True


async def reply(message: Message, text: str, keyboard: Any = MAIN_KEYBOARD) -> None:
    await cast(Any, message).reply_text(text, reply_markup=keyboard)


def is_skip_word(text: str) -> bool:
    return text.strip().casefold().replace("⏭️ ", "") in SKIP_WORDS


def is_batch_upload_state(state: Optional[dict[str, Any]]) -> bool:
    return bool(state and state.get("flow") == "batch_episode" and state.get("step") == "batch_media")


def is_back_command(text: str) -> bool:
    normalized = text.strip().casefold()
    return normalized in {"⬅️ חזרה", "⬅️ אחורה", "חזרה", "אחורה", "back", "cancel"}




async def flush_auto_batch_summary(user_id: int) -> None:
    try:
        await asyncio.sleep(2.0)
        state = auto_batch_states.pop(user_id, None)
        if not state:
            return

        total = int(state.get("uploaded", 0)) + int(state.get("failed", 0))
        if total <= 0:
            return

        lines = [
            "📦 **סיכום קבצים אוטומטיים**",
            "",
            f"📥 התקבלו: {total}",
            f"✅ נשמרו בהצלחה: {state.get('uploaded', 0)}",
            f"❌ נכשלו: {state.get('failed', 0)}",
        ]

        groups = state.get("groups", {})
        if groups:
            lines.extend(["", "📚 **פירוט לפי סדרה:**"])
            for series_title, counts in sorted(groups.items(), key=lambda item: item[0].casefold()):
                lines.append(
                    f"• {series_title}: {counts.get('uploaded', 0)} נשמרו, "
                    f"{counts.get('failed', 0)} נכשלו"
                )

        if state.get("failed", 0):
            lines.extend(["", "⚠️ **קבצים שנכשלו:**"])
            lines.extend(
                f"• עונה {season} פרק {episode}"
                for season, episode in state.get("failed_episodes", [])
            )

        message = state.get("last_message")
        if message is not None:
            state["summary_sent"] = True
            await reply(message, "\n".join(lines))
    finally:
        auto_batch_tasks.pop(user_id, None)


def queue_auto_batch_summary(user_id: int, message: Message) -> None:
    state = auto_batch_states.setdefault(
        user_id,
        {
            "uploaded": 0,
            "failed": 0,
            "failed_episodes": [],
            "saved_items": [],
            "groups": {},
            "last_message": message,
        },
    )
    state["last_message"] = message

    existing_task = auto_batch_tasks.pop(user_id, None)
    if existing_task is not None:
        existing_task.cancel()

    task = asyncio.create_task(flush_auto_batch_summary(user_id))
    auto_batch_tasks[user_id] = task


def parse_episode_reference(text: str) -> tuple[str, Optional[int], Optional[int]]:
    patterns = [
        r"^(.*?)\s+(?:עונה|ע)\s*(\d+)\s+(?:פרק|פ)\s*(\d+)\s*$",
        r"^(.*?)\s+(?:season)\s*(\d+)\s+(?:episode|ep)\s*(\d+)\s*$",
        r"^(.*?)\s+s(\d+)\s*e(\d+)\s*$",
    ]
    for pattern in patterns:
        match = re.match(pattern, text.strip(), flags=re.IGNORECASE)
        if match:
            return match.group(1).strip(), int(match.group(2)), int(match.group(3))
    return text.strip(), None, None


def parse_series_season_reference(text: str) -> tuple[str, Optional[int]]:
    cleaned = re.sub(r"^\s*(?:סדרה|series)\s*:?\s*", "", text.strip(), flags=re.IGNORECASE)
    pattern = r"^(.*?)\s+(?:עונה|ע|season|s)\s*(\d+)\s*$"
    match = re.match(pattern, cleaned, flags=re.IGNORECASE)
    if not match:
        return cleaned, None
    return match.group(1).strip(), int(match.group(2))


def looks_like_metadata(text: str) -> bool:
    if not text or len(text) < 20:
        return False
    lowered = text.casefold()
    if re.search(r"(?:עונה|season)\s*\d+", lowered):
        return True
    if re.search(r"(?:פרק|episode|ep)\s*\d+", lowered):
        return True
    return bool(re.search(r"(?:ז['׳]?אנר|genre|איכות|quality|תקציר|summary|שנת\s+יציאה|release\s+year)", lowered))


def merge_pending_metadata(
    parsed_caption: Optional[dict[str, Any]],
    pending_metadata: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    if pending_metadata is None:
        return parsed_caption
    if parsed_caption is None:
        return dict(pending_metadata)

    merged = dict(pending_metadata)
    for key in ("kind", "title_candidates", "season", "episode", "quality", "genre", "summary", "year", "poster_url"):
        value = parsed_caption.get(key)
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def parse_media_caption(caption: str) -> Optional[dict[str, Any]]:
    lines = [
        re.sub(
            r"\.(?:mkv|mp4|avi|mov|m4v|webm|ts)\s*$",
            "",
            re.sub(r"[*_`>#]", "", line).strip(),
            flags=re.IGNORECASE,
        )
        for line in caption.splitlines()
    ]
    lines = [line for line in lines if line]
    if not lines:
        return None
    quality = ""
    genre = ""
    poster_url = ""
    year = None

    def sanitize_genre(value: str) -> str:
        cleaned = value.strip()
        parts: list[str] = []
        for part in re.split(r"\s*\|\s*", cleaned):
            stripped = part.strip(" -:;.,")
            if not stripped:
                continue
            lowered = stripped.casefold()
            if lowered.startswith((
                "תרגום",
                "מדובב",
                "דובב",
                "דיבוב",
                "כתוביות",
                "subtitle",
                "sub",
                "dub",
                "translated",
                "voice",
                "audio",
            )):
                continue
            parts.append(stripped)
        if parts:
            return " | ".join(parts)
        return cleaned

    for line in lines:
        quality_match = re.match(r"(?:איכות|quality)\s*:?\s*(.+)$", line, flags=re.IGNORECASE)
        if quality_match:
            quality = quality_match.group(1).strip()
            quality = re.sub(r"\s*[❤★✦☆]+\s*$", "", quality).strip()
        genre_match = re.match(r"(?:ז['׳]?אנר|genre)\s*:?\s*(.+)$", line, flags=re.IGNORECASE)
        if genre_match:
            genre = sanitize_genre(genre_match.group(1).strip())
        poster_match = re.match(r"(?:פוסטר|poster)\s*:?\s*(https?://\S+)", line, flags=re.IGNORECASE)
        if poster_match:
            poster_url = poster_match.group(1).strip()
        else:
            url_match = re.search(r"https?://\S+", line)
            if url_match and not line.casefold().startswith(("תקציר", "summary", "מקור", "source")):
                poster_url = url_match.group(0).strip()
        year_match = re.search(r"(?:שנת\s+יציאה|release\s+year|year)\s*:?\s*(\d{4})\b", line, flags=re.IGNORECASE)
        if year_match:
            year = int(year_match.group(1))

    ignored_prefixes = (
        "תרגום", "איכות", "ז'אנר", "זאנר", "תקציר", "נקרע",
        "הועלה", "עבור", "קרדיט", "מקודד",
    )

    episode_patterns = [
        r"^(?:סדרה\s*:\s*)?(.+?)\s+(?:עונה|ע)\s*(\d+)\s+(?:פרק|פ)\s*(\d+)",
        r"^(?:series\s*:\s*)?(.+?)\s+season\s*(\d+)\s+(?:episode|ep)\s*(\d+)",
        r"^(?:series\s*:\s*)?(.+?)\s+s(\d{1,2})\s*e(\d{1,2})",
    ]
    title_lines = [line for line in lines if not line.casefold().startswith(ignored_prefixes)]
    if not title_lines:
        return None
    title_candidates = [re.sub(r"\b(?:19|20)\d{2}\b", "", title_lines[0]).strip(" :-")]
    if len(title_lines) > 1 and re.search(r"[A-Za-z]", title_lines[1]):
        title_candidates.append(title_lines[1])
    if year is None:
        year_match = re.search(r"\b((?:19|20)\d{2})\b", lines[0])
        if year_match:
            year = int(year_match.group(1))
    summary = ""
    for index, line in enumerate(lines):
        if line.casefold().startswith(("תקציר", "summary")):
            summary_parts: list[str] = []
            for summary_line in lines[index + 1:]:
                if summary_line.casefold().startswith(ignored_prefixes):
                    break
                summary_parts.append(summary_line)
            summary = " ".join(summary_parts).strip()
            summary = re.split(r"\s+(?:הועלה|עבור|קרדיט|מקודד)\b", summary, maxsplit=1)[0].strip()
            break
    if not genre:
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            lowered = stripped.casefold()
            if lowered.startswith(ignored_prefixes) or lowered.startswith(("איכות", "quality", "ז'אנר", "זאנר", "genre")):
                continue
            if "|" in stripped:
                genre = stripped
                break
            if re.match(r"^(?:סדרה|סרט|series|movie)\s+.+$", stripped, flags=re.IGNORECASE):
                genre = stripped
                break

    episode_match = None
    season_number = None
    episode_number = None
    for line in lines[:3]:
        for pattern in episode_patterns:
            match = re.match(pattern, line, flags=re.IGNORECASE)
            if not match:
                continue
            episode_match = match
            # Every episode pattern captures title, season, episode in groups
            # 1, 2, 3. The previous special case treated the title as the
            # season for SxxEyy and crashed on common filenames.
            season_number = int(match.group(2))
            episode_number = int(match.group(3))
            break
        if episode_match:
            break

    if episode_match:
        title_candidates = [episode_match.group(1).strip(" :-")]
        for alternate_line in lines[1:3]:
            if (
                re.search(r"[A-Za-z]", alternate_line)
                and not alternate_line.casefold().startswith(ignored_prefixes)
            ):
                title_candidates.append(alternate_line)
        return {
            "kind": "episode",
            "title_candidates": [candidate for candidate in title_candidates if candidate],
            "season": season_number,
            "episode": episode_number,
            "quality": quality,
            "genre": genre,
            "summary": summary,
            "year": year,
            "poster_url": poster_url,
        }

    return {
        "kind": "movie",
        "title_candidates": [candidate for candidate in title_candidates if candidate],
        "year": year,
        "quality": quality,
        "genre": genre,
        "summary": summary,
        "poster_url": poster_url,
    }


def parse_partial_episode_reference(caption: str) -> dict[str, Optional[int]]:
    """Extract season/episode numbers when a media caption has no series title."""
    text = re.sub(r"[*_`>#]", "", caption or "").strip()
    season: Optional[int] = None
    episode: Optional[int] = None
    match = re.search(r"\bs(\d{1,2})\s*e(\d{1,2})\b", text, flags=re.IGNORECASE)
    if match:
        season, episode = int(match.group(1)), int(match.group(2))
    else:
        season_match = re.search(r"\b(?:עונה|season|s)\s*(\d+)\b", text, flags=re.IGNORECASE)
        episode_match = re.search(
            r"\b(?:פרק|episode|ep|e)\s*(\d+)\b", text, flags=re.IGNORECASE,
        )
        season = int(season_match.group(1)) if season_match else None
        episode = int(episode_match.group(1)) if episode_match else None
    return {"season": season, "episode": episode}


async def auto_catalog_media(
    metadata: dict[str, Any],
    stream_url: str,
    upload_id: int,
) -> tuple[str, dict[str, Any]]:
    candidates = metadata["title_candidates"]
    kind = metadata["kind"]

    if kind == "episode":
        series_title = candidates[0]
        series = find_item(series_title, "series")
        if not series:
            series = find_item(candidates[0], "series")
        if not series:
            series_id = CATALOG.add_item(
                "series",
                series_title,
                metadata.get("summary", ""),
                metadata.get("year"),
                metadata.get("poster_url", ""),
                "",
                "",
                None,
                None,
                metadata.get("quality", ""),
                metadata.get("genre", ""),
            )
        else:
            series_id = int(series["id"])
        episode_id = CATALOG.add_episode(
            series_id,
            metadata["season"],
            metadata["episode"],
            f"פרק {metadata['episode']}",
            stream_url,
            metadata.get("quality", ""),
        )
        CATALOG.attach_upload(upload_id, episode_id)
        saved_item: dict[str, Any] = {
            "kind": "episode",
            "title": f"{series_title} — עונה {metadata['season']} פרק {metadata['episode']}",
            "series_title": series_title,
            "season": metadata["season"],
            "episode": metadata["episode"],
            "stream_url": stream_url,
            "quality": metadata.get("quality", ""),
            "genre": metadata.get("genre", ""),
            "summary": metadata.get("summary", ""),
            "release_year": metadata.get("year"),
            "item_id": episode_id,
        }
        return (
            f"📺 נשמר אוטומטית: {series_title}, עונה {metadata['season']} פרק {metadata['episode']} ✅",
            saved_item,
        )

    title = candidates[0]
    summary = metadata.get("summary", "")
    release_year = metadata.get("year")
    poster_url = metadata.get("poster_url", "")
    genre = metadata.get("genre", "")

    existing = find_item(title, "movie")
    if existing:
        item_id = int(existing["id"])
        updates: dict[str, Any] = {"stream_url": stream_url}
        if summary:
            updates["summary"] = summary
        if release_year:
            updates["release_year"] = release_year
        if poster_url:
            updates["poster_url"] = poster_url
        if metadata.get("quality"):
            updates["quality"] = metadata["quality"]
        if genre:
            updates["genre"] = genre
        CATALOG.update_item(item_id, **updates)
    else:
        item_id = CATALOG.add_item(
            "movie",
            title,
            summary,
            release_year,
            poster_url,
            stream_url,
            "",
            None,
            None,
            metadata.get("quality", ""),
            genre,
        )
    CATALOG.attach_upload(upload_id, item_id)
    saved_item: dict[str, Any] = {
        "kind": "movie",
        "title": title,
        "stream_url": stream_url,
        "quality": metadata.get("quality", ""),
        "genre": genre,
        "summary": summary,
        "release_year": release_year,
        "item_id": item_id,
    }
    return f"🎬 נשמר אוטומטית: {title} ✅", saved_item


def begin_flow(user_id: int, flow: str) -> None:
    user_states[user_id] = {"flow": flow, "step": "title", "data": {}, "updated_at": time.time()}


def normalize_title_for_lookup(value: str) -> str:
    text = re.sub(r"[*_`>#]", " ", value or "")
    text = re.sub(r"\b(?:עונה|season|s)\s*\d+\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:פרק|episode|ep|e)\s*\d+\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:1080p|720p|2160p|480p|web[- ]?dl|x265|x264|hdrip|dvdrip|bluray|remux|mkv|mp4|avi|m4v|webm)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:translated|תרגום|subtitle|subtitles|written|דובב|מדובב|כתוביות)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[\[\](){}]", " ", text)
    text = re.sub(r"[-_]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -:;,. ").casefold()


def title_similarity(lhs: str, rhs: str) -> float:
    if not lhs or not rhs:
        return 0.0
    if lhs == rhs:
        return 1.0
    return SequenceMatcher(None, lhs, rhs).ratio()


def find_item(query: str, kind: Optional[str] = None) -> Optional[dict[str, Any]]:
    search_results = CATALOG.search(query, kind)
    if search_results:
        exact = [item for item in search_results if item["title"].casefold() == query.casefold()]
        if exact:
            return exact[0]

    normalized_query = normalize_title_for_lookup(query)
    all_items = CATALOG.list_items(kind)
    if not all_items:
        return search_results[0] if search_results else None

    scored_items: list[tuple[float, dict[str, Any]]] = []
    for item in all_items:
        normalized_title = normalize_title_for_lookup(item["title"])
        if not normalized_title:
            continue
        score = title_similarity(normalized_query, normalized_title)
        if score >= 0.75:
            scored_items.append((score, item))

    if scored_items:
        scored_items.sort(key=lambda entry: entry[0], reverse=True)
        best_match = scored_items[0][1]
        return best_match

    return search_results[0] if search_results else None


def find_exact_item(query: str, kind: Optional[str] = None) -> Optional[dict[str, Any]]:
    normalized_query = normalize_title_for_lookup(query)
    search_results = CATALOG.search(query, kind)
    if search_results:
        for item in search_results:
            if normalize_title_for_lookup(item["title"]) == normalized_query:
                return item

    all_items = CATALOG.list_items(kind)
    for item in all_items:
        if normalize_title_for_lookup(item["title"]) == normalized_query:
            return item
    return None


def attach_metadata_to_existing_catalog(parsed_caption: dict[str, Any]) -> Optional[str]:
    raw_candidates = cast(list[Any], parsed_caption.get("title_candidates") or [])
    if not raw_candidates:
        return None

    candidates: list[str] = []
    for candidate in raw_candidates:
        if isinstance(candidate, str) and candidate.strip():
            candidates.append(candidate.strip())

    if not candidates:
        return None

    kind_value: Any = parsed_caption.get("kind")
    kind = kind_value if isinstance(kind_value, str) else ""

    item: Optional[dict[str, Any]] = None

    if kind == "episode":
        for candidate in candidates:
            item = find_exact_item(candidate, "series")
            if item:
                break
    else:
        for candidate in candidates:
            item = find_exact_item(candidate, kind)
            if item:
                break

    if not item:
        return None

    item_id = int(item["id"])
    updates: dict[str, Any] = {}
    if parsed_caption.get("summary"):
        updates["summary"] = parsed_caption["summary"]
    if parsed_caption.get("year"):
        updates["release_year"] = parsed_caption["year"]
    if parsed_caption.get("poster_url"):
        updates["poster_url"] = parsed_caption["poster_url"]
    if parsed_caption.get("quality"):
        updates["quality"] = parsed_caption["quality"]
    if parsed_caption.get("genre"):
        updates["genre"] = parsed_caption["genre"]

    if updates:
        CATALOG.update_item(item_id, **updates)

    if kind == "episode":
        existing_episodes = CATALOG.list_episodes(item_id)
        for episode in existing_episodes:
            quality = parsed_caption.get("quality", "")
            if quality:
                CATALOG.add_episode(
                    item_id,
                    int(episode["season_number"]),
                    int(episode["episode_number"]),
                    episode.get("title", f"פרק {episode['episode_number']}"),
                    episode.get("stream_url", ""),
                    quality,
                )

    if kind == "episode":
        return f"✅ מצאתי סדרה קיימת ({item['title']}) והחברתי אליה את פרטי המידע החדש."
    return f"✅ מצאתי {kind or 'פריט'} קיים ({item['title']}) והחברתי אליו את פרטי המידע החדש."


async def show_list(message: Message) -> None:
    items = CATALOG.list_items()
    summary = CATALOG.summary()
    if not items:
        await reply(message, "📚 הקטלוג עדיין ריק.")
        return
    lines = [
        f"📚 **הקטלוג שלך** | 🎬 {summary['movies']} סרטים | 📺 {summary['series']} סדרות",
        "",
    ]
    for item in items:
        icon = "🎬" if item["kind"] == "movie" else "📺"
        year = f" ({item['release_year']})" if item["release_year"] else ""
        lines.append(f"{icon} **{item['title']}**{year} · #{item['id']}")
    await reply(message, "\n".join(lines))


async def show_report(message: Message) -> None:
    report = CATALOG.integrity_report()
    duplicates = report["duplicates"]
    missing_episodes = report["missing_episodes"]
    if not duplicates and not missing_episodes:
        await reply(message, "📊 **דוח קטלוג**\n\n✅ לא נמצאו כפילויות או פרקים חסרים.")
        return

    lines = ["📊 **דוח קטלוג**", ""]
    if duplicates:
        lines.append("⚠️ **כפילויות:**")
        for duplicate in duplicates:
            kind = "סרט" if duplicate["kind"] == "movie" else "סדרה"
            ids = ", ".join(f"#{item_id}" for item_id in duplicate["item_ids"])
            lines.append(f"• {kind}: **{duplicate['title']}** ({ids})")
        lines.append("")
    if missing_episodes:
        lines.append("⚠️ **פרקים חסרים:**")
        for gap in missing_episodes:
            episodes = ", ".join(str(number) for number in gap["missing_episodes"])
            lines.append(
                f"• **{gap['series_title']}** — עונה {gap['season_number']}: "
                f"חסר פרק {episodes}"
            )
    await reply(message, "\n".join(lines))


async def show_browse_items(message: Message, kind: Optional[str] = None) -> None:
    items = CATALOG.list_items(kind)
    if not items:
        label = "סרטים" if kind == "movie" else "סדרות" if kind == "series" else "פריטים"
        await reply(message, f"📚 עדיין אין {label} בקטלוג.")
        return
    user_id = message.from_user.id if message.from_user else 0
    user_states[user_id] = {
        "flow": "browse", "step": "browse_item", "data": {"items": items},
    }
    lines = ["🔗 בחר סרט או סדרה לפי מספר:", ""]
    for index, item in enumerate(items, start=1):
        icon = "🎬" if item["kind"] == "movie" else "📺"
        lines.append(f"{index}. {icon} {item['title']}")
    await reply(message, "\n".join(lines))


async def show_item_preview(message: Message, item: dict[str, Any]) -> None:
    poster = item.get("poster_url") or ""
    title = item["title"]
    summary = item.get("summary") or ""
    year = item.get("release_year")
    genre = item.get("genre") or "אין"

    lines = [f"📦 **{title}**", ""]
    if year:
        lines.append(f"📅 שנת יציאה: {year}")
    lines.append(f"🎭 זאנר: {genre}")
    if summary:
        lines.append("")
        lines.append(f"📝 תקציר:\n{summary}")

    if poster:
        try:
            await cast(Any, message).reply_photo(poster, caption="\n".join(lines), reply_markup=WATCH_KEYBOARD)
            return
        except Exception as error:
            log.warning("Could not send poster for %s: %s", title, error)

    await reply(message, "\n".join(lines), WATCH_KEYBOARD)


async def show_section_menu(message: Message, section: str) -> None:
    keyboard = MOVIES_KEYBOARD if section == "movies" else SERIES_KEYBOARD
    title = "🎬 **פעולות סרטים**" if section == "movies" else "📺 **פעולות סדרות**"
    await reply(message, f"{title}\nבחר פעולה:", keyboard)


async def send_stream_link(message: Message, title: str, stream_url: str) -> None:
    user_id = message.from_user.id if message.from_user else 0
    user_states.pop(user_id, None)
    if not stream_url:
        await reply(message, "⚠️ לפריט הזה עדיין אין קישור סטרימינג.")
        return
    await reply(message, f"🔗 **קישור סטרימינג מוכן**\n\n🎬 {title}\n\n{stream_url}")


async def show_guide(message: Message) -> None:
    await reply(
        message,
        "📖 **מדריך הבוט**\n\n"
        "🎬 **קטלוג**\n"
        "➕ **הוסף סרט** — שם, תקציר, שנה, פוסטר ואז קובץ וידאו.\n"
        "➕ **הוסף סדרה** — שם, תקציר, שנה ופוסטר.\n"
        "➕ **הוסף פרק** — בחר סדרה, עונה, פרק וקובץ וידאו.\n"
        "✏️ **עריכה** — בחר סרט או סדרה ושדה לעדכון.\n"
        "🗑️ **מחיקה** — תמיד נדרשת תשובת אישור.\n\n"
        "📦 **העלאת עונה**\n"
        "כתוב: `סדרה: פאודה`, והבוט ישאל איזו עונה להעלות.\n"
        "אפשר גם לכתוב ישירות `סדרה: פאודה עונה 2`.\n"
        "לאחר בחירת העונה שלח את הקבצים לפי הסדר, מהפרק הראשון ועד האחרון.\n"
        "בסיום לחץ על ✅ **סיום**.\n\n"
        "🔗 **צפייה וסטרימינג**\n"
        "כתוב `/browse` כדי לפתוח קישורי סטרימינג.\n"
        "בחר סרט, או סדרה → עונה → פרק.\n\n"
        "📊 **בדיקת קטלוג**\n"
        "לחץ על **דוח** או כתוב `/report` כדי למצוא כפילויות ופרקים חסרים.\n\n"
        "🛠️ **ניהול**\n"
        "הבעלים יכול לפתוח את **ניהול** או `/management` כדי לאשר משתמשים, מנהלים, קבוצות וערוצים.\n\n"
        "👥 **קבוצות וערוצים**\n"
        "רק צ׳אטים שאושרו על ידי הבעלים פעילים. בערוץ יש להוסיף את הבוט כמנהל ולצרף כיתוב עם סדרה, עונה ופרק.\n\n"
        "🔎 **חיפוש**\n"
        "כתוב שם של סרט או סדרה כדי לחפש בקטלוג.\n\n"
        "⌨️ **פקודות**\n"
        "`/add_movie` — הוסף סרט\n"
        "`/add_series` — הוסף סדרה\n"
        "`/add_episode` — הוסף פרק\n"
        "`/edit_movie` — ערוך סרט\n"
        "`/edit_series` — ערוך סדרה\n"
        "`/remove_movie` — מחק סרט\n"
        "`/remove_series` — מחק סדרה\n"
        "`/remove_season` — מחק עונה\n"
        "`/remove_episode` — מחק פרק\n"
        "`/list` — הצג קטלוג\n"
        "`/report` — הצג דוח\n"
        "`/about` — אודות הבוט\n"
        "`/management` — ניהול הבוט\n"
        "`/cancel` — בטל פעולה",
    )


async def show_about(message: Message) -> None:
    await reply(
        message,
        "ℹ️ **אודות הבוט**\n\n"
        "בוט לניהול קטלוג סרטים וסדרות, העלאת פרקים ויצירת קישורי סטרימינג.\n"
        "הקבצים נשארים ב־Telegram, והמערכת שומרת בקטלוג רק את פרטי ההודעה הדרושים לסטרימינג.\n"
        "תמיכה בפרטי, קבוצות וערוצים.\n\n"
        "הוכן והועלה על ידי **אלון נושם**.",
    )


async def show_management(message: Message) -> None:
    admins = CATALOG.list_admins()
    users = CATALOG.list_users()
    chats = CATALOG.list_bot_chats()
    chat_lines = [
        f"• {chat['title'] or 'ללא שם'} ({chat['chat_type']}) · `{chat['chat_id']}`"
        for chat in chats
    ] or ["• עדיין לא נרשמו קבוצות או ערוצים."]
    await reply(
        message,
        "🛠️ **ניהול הבוט**\n\n"
        f"מנהלים: **{len(admins)}**\n"
        f"משתמשים מאושרים: **{len(users)}**\n"
        f"קבוצות וערוצים פעילים: **{len(chats)}**\n\n"
        "כדי לאשר קבוצה או ערוץ, הוסף את הבוט ידנית והשתמש בכפתור המתאים עם ה־chat ID.\n"
        "צ׳אט שלא אושר יקבל הודעת הרשאה אחת והבוט יצא ממנו אוטומטית.\n\n"
        "📋 **צ׳אטים רשומים**\n" + "\n".join(chat_lines),
        MANAGEMENT_KEYBOARD,
    )


async def register_chat_by_id(message: Message, chat_id: int, expected_type: str) -> None:
    try:
        chat = await bot_client.get_chat(chat_id)
    except Exception as error:
        log.warning("Could not load chat %s: %s", chat_id, error)
        await reply(message, "❌ לא הצלחתי למצוא את הצ׳אט. ודא שהבוט נמצא בו ושה־chat ID נכון.", MANAGEMENT_KEYBOARD)
        return
    actual_type = chat_type_name(chat)
    valid_types = {"group", "supergroup"} if expected_type == "group" else {"channel"}
    if actual_type not in valid_types:
        await reply(message, f"❌ ה־chat ID הזה אינו {expected_type}.", MANAGEMENT_KEYBOARD)
        return
    resolved_chat_id = int(getattr(chat, "id", 0) or 0)
    if not resolved_chat_id:
        await reply(message, "❌ Telegram לא החזיר מזהה תקין לצ׳אט.", MANAGEMENT_KEYBOARD)
        return
    title = getattr(chat, "title", "") or str(resolved_chat_id)
    CATALOG.register_chat(
        resolved_chat_id, title,
        actual_type, OWNER_USER_ID,
    )
    await reply(message, f"✅ {title} נוסף לרשימת הצ׳אטים הפעילים.", MANAGEMENT_KEYBOARD)


async def start_action(message: Message, action: str) -> None:
    if action == "about":
        await show_about(message)
        return
    if action == "management":
        if not is_owner(message):
            await reply(message, "🔒 ניהול הרשאות וצ׳אטים זמין רק לבעלים.")
            return
        await show_management(message)
        return
    if action == "management_list":
        if is_owner(message):
            await show_management(message)
        else:
            await reply(message, "🔒 ניהול הרשאות וצ׳אטים זמין רק לבעלים.")
        return
    management_actions = {
        "add_admin", "remove_admin", "add_user", "remove_user",
        "add_group", "add_channel", "remove_chat",
    }
    if action in management_actions and not is_owner(message):
        await reply(message, "🔒 ניהול הרשאות וצ׳אטים זמין רק לבעלים.")
        return
    if action in management_actions:
        prompts = {
            "add_admin": "➕ שלח את מזהה המשתמש של המנהל החדש.",
            "remove_admin": "➖ שלח את מזהה המשתמש של המנהל להסרה.",
            "add_user": "✅ שלח את מזהה המשתמש הפרטי שברצונך לאשר.",
            "remove_user": "🗑️ שלח את מזהה המשתמש להסרה.",
            "add_group": (
                "✅ כדי למצוא את ה־chat ID, פתח את @userinfobot:\n"
                "https://t.me/userinfobot\n\n"
                "העתק את ה־ID של הקבוצה, הוסף את הבוט לקבוצה, ושלח כאן את ה־ID כדי לאשר אותה."
            ),
            "add_channel": (
                "✅ כדי למצוא את ה־chat ID, פתח את @userinfobot:\n"
                "https://t.me/userinfobot\n\n"
                "העתק את ה־ID של הערוץ, הוסף את הבוט לערוץ כמנהל, ושלח כאן את ה־ID כדי לאשר אותו."
            ),
            "remove_chat": "🗑️ שלח את ה־chat ID להסרה מהרשימה.",
        }
        user_id = message.from_user.id if message.from_user else 0
        user_states[user_id] = {"flow": "management", "step": action, "data": {}}
        await reply(message, prompts[action], MANAGEMENT_KEYBOARD)
        return
    if not is_admin(message):
        await reply(message, "🔒 הפעולה הזו זמינה למנהלים בלבד.")
        return
    user_id = message.from_user.id if message.from_user else 0
    if action == "edit_series":
        series_items = CATALOG.list_items("series")
        if not series_items:
            await reply(message, "📺 עדיין אין סדרות בקטלוג.")
            return
        user_states[user_id] = {
            "flow": "edit_series",
            "step": "series_choice",
            "data": {"series_options": series_items},
        }
        lines = ["✏️ איזו סדרה לערוך? שלח את המספר:", ""]
        lines.extend(
            f"{index}. 📺 {series['title']}"
            for index, series in enumerate(series_items, start=1)
        )
        await reply(message, "\n".join(lines))
        return
    if action in {"movie", "series", "episode", "edit", "edit_movie", "edit_series", "remove_movie", "remove_series", "remove_season", "remove_episode"}:
        begin_flow(user_id, action)
        prompts = {
            "movie": "🎬 מה שם הסרט?",
            "series": "📺 מה שם הסדרה?",
            "episode": "📺 איך קוראים לסדרה שאליה מוסיפים את הפרק?",
            "edit": "✏️ איזו סדרה לערוך?",
            "edit_series": "✏️ איזו סדרה לערוך?",
            "edit_movie": "✏️ איזה סרט לערוך?",
            "remove_movie": "🗑️ איזה סרט להסיר?",
            "remove_series": "🗑️ איזו סדרה להסיר?",
            "remove_season": "🗑️ מאיזו סדרה להסיר עונה?",
            "remove_episode": "🗑️ מאיזו סדרה להסיר פרק?",
        }
        user_states[user_id]["step"] = "lookup" if action in {"episode", "edit", "edit_movie", "edit_series", "remove_movie", "remove_series", "remove_season", "remove_episode"} else "title"
        if action in {"edit_movie", "edit_series", "remove_movie"}:
            user_states[user_id]["data"]["kind"] = "movie" if action == "edit_movie" else "series"
            if action == "remove_movie":
                user_states[user_id]["data"]["kind"] = "movie"
        await reply(message, prompts[action])
        return
    if action == "list":
        await show_list(message)
    elif action == "report":
        await show_report(message)
    elif action == "browse":
        await show_browse_items(message)
    elif action == "guide":
        await show_guide(message)


async def begin_season_upload(message: Message, text: str) -> None:
    if not is_admin(message):
        await reply(message, "🔒 הפעולה הזו זמינה למנהלים בלבד.")
        return
    series_title, season_number = parse_series_season_reference(text)
    series = find_item(series_title, "series")
    if not series:
        series_id = CATALOG.add_item("series", series_title)
        series = CATALOG.get_item(series_id)
        if not series:
            await reply(message, "❌ לא הצלחתי ליצור את הסדרה. נסה שוב.")
            return
        created_message = f"\n✅ יצרתי את הסדרה **{series_title}** בקטלוג."
    else:
        created_message = ""
    if season_number is None:
        user_id = message.from_user.id if message.from_user else 0
        user_states[user_id] = {
            "flow": "batch_episode",
            "step": "batch_season",
            "series_id": series["id"],
            "series_title": series["title"],
        }
        await reply(
            message,
            f"📺 הסדרה **{series['title']}** מוכנה.\n"
            "🔢 איזו עונה אתה מעלה? כתוב מספר, למשל `1`.",
        )
        return
    user_id = message.from_user.id if message.from_user else 0
    user_states[user_id] = {
        "flow": "batch_episode",
        "step": "batch_media",
        "series_id": series["id"],
        "series_title": series["title"],
        "season": season_number,
        "next_episode": 1,
        "uploaded": 0,
        "failed": 0,
        "failed_episodes": [],
    }
    await reply(
        message,
        f"📺 מצב העלאת עונה הופעל עבור **{series['title']}**, עונה {season_number}.\n\n"
        "שלח את קבצי הפרקים לפי הסדר. הראשון יישמר כפרק 1, השני כפרק 2 וכן הלאה."
        f"{created_message}",
        UPLOAD_SEASON_KEYBOARD,
    )


async def finish_unlabeled_episode(message: Message, state: dict[str, Any]) -> None:
    data = state["data"]
    episode_number = int(data["episode"])
    episode_id = CATALOG.add_episode(
        int(data["series_id"]), int(data["season"]), episode_number,
        f"פרק {episode_number}", data["stream_url"],
    )
    CATALOG.attach_upload(int(data["upload_id"]), episode_id)
    user_states.pop(message.from_user.id if message.from_user else 0, None)
    await reply(
        message,
        f"✅ נשמר: **{data['series_title']}** — עונה {data['season']} פרק {episode_number}.",
    )


async def handle_state(message: Message, state: dict[str, Any], text: str) -> None:
    user_id = message.from_user.id if message.from_user else 0
    flow = state["flow"]
    step = state["step"]

    if flow == "management":
        if not is_owner(message):
            user_states.pop(user_id, None)
            await reply(message, "🔒 הפעולה הזו זמינה רק לבעלים.")
            return
        if step in {"add_admin", "remove_admin", "add_user", "remove_user"}:
            if not text.isdigit() or int(text) <= 0:
                await reply(message, "❌ מזהה משתמש חייב להיות מספר חיובי.", MANAGEMENT_KEYBOARD)
                return
            target_id = int(text)
            if step == "add_admin":
                CATALOG.add_admin(target_id)
                CATALOG.add_user(target_id, OWNER_USER_ID)
                refresh_admins()
                result = f"✅ המשתמש `{target_id}` נוסף כמנהל."
            elif step == "add_user":
                CATALOG.add_user(target_id, OWNER_USER_ID)
                result = f"✅ המשתמש `{target_id}` אושר לשימוש בפרטי."
            elif target_id == OWNER_USER_ID:
                result = "❌ אי אפשר להסיר את הבעלים הראשי."
            elif step == "remove_user":
                CATALOG.remove_user(target_id)
                result = f"✅ המשתמש `{target_id}` הוסר מרשימת המשתמשים המאושרים."
            else:
                CATALOG.remove_admin(target_id)
                CATALOG.remove_user(target_id)
                refresh_admins()
                result = f"✅ המשתמש `{target_id}` הוסר מרשימת המנהלים."
            user_states.pop(user_id, None)
            await reply(message, result, MANAGEMENT_KEYBOARD)
            return
        if step in {"add_group", "add_channel", "remove_chat"}:
            try:
                chat_id = int(text)
            except ValueError:
                await reply(message, "❌ chat ID חייב להיות מספר, בדרך כלל מתחיל ב־`-100`.", MANAGEMENT_KEYBOARD)
                return
            if step == "remove_chat":
                CATALOG.unregister_chat(chat_id)
                user_states.pop(user_id, None)
                await reply(message, f"✅ הצ׳אט `{chat_id}` הוסר מהרשימה.", MANAGEMENT_KEYBOARD)
            else:
                expected_type = "group" if step == "add_group" else "channel"
                user_states.pop(user_id, None)
                await register_chat_by_id(message, chat_id, expected_type)
            return

    if step == "batch_season":
        if not text.isdigit() or int(text) < 1:
            await reply(message, "🔢 מספר עונה חייב להיות מספר חיובי. נסה שוב.")
            return
        state.update({
            "step": "batch_media",
            "season": int(text),
            "next_episode": 1,
        })
        await reply(
            message,
            f"📺 מצב העלאת עונה הופעל עבור **{state['series_title']}**, עונה {text}.\n\n"
            "שלח את קבצי הפרקים לפי הסדר. הראשון יישמר כפרק 1, השני כפרק 2 וכן הלאה.",
            UPLOAD_SEASON_KEYBOARD,
        )
        return

    data = state["data"]

    if step == "series_choice":
        options = data.get("series_options", [])
        if not text.isdigit() or not 1 <= int(text) <= len(options):
            await reply(message, "❓ שלח מספר מהרשימה כדי לבחור סדרה.")
            return
        selected = options[int(text) - 1]
        data["item_id"] = int(selected["id"])
        data["kind"] = "series"
        state["step"] = "edit_field"
        await reply(message, f"✏️ בחר מה לערוך בסדרה **{selected['title']}**:", EDIT_FIELD_KEYBOARD)
        return

    if step == "unlabeled_series":
        series = find_item(text, "series")
        if not series:
            series_id = CATALOG.add_item("series", text)
            series = CATALOG.get_item(series_id)
        if not series:
            await reply(message, "❌ לא הצלחתי ליצור את הסדרה. נסה שוב.")
            return
        data["series_id"] = int(series["id"])
        data["series_title"] = series["title"]
        if data["season"] is None:
            state["step"] = "unlabeled_season"
            await reply(message, "🔢 איזו עונה? כתוב מספר, למשל `2`.")
        elif data["episode"] is None:
            state["step"] = "unlabeled_episode"
            await reply(message, "🔢 איזה פרק? כתוב מספר, למשל `2`.")
        else:
            await finish_unlabeled_episode(message, state)
        return

    if step == "unlabeled_season":
        if not text.isdigit() or int(text) < 1:
            await reply(message, "🔢 מספר עונה חייב להיות מספר חיובי. נסה שוב.")
            return
        data["season"] = int(text)
        if data["episode"] is None:
            state["step"] = "unlabeled_episode"
            await reply(message, "🔢 איזה פרק? כתוב מספר, למשל `2`.")
        else:
            await finish_unlabeled_episode(message, state)
        return

    if step == "unlabeled_episode":
        if not text.isdigit() or int(text) < 1:
            await reply(message, "🔢 מספר פרק חייב להיות מספר חיובי. נסה שוב.")
            return
        data["episode"] = int(text)
        await finish_unlabeled_episode(message, state)
        return

    if step == "browse_item":
        items = data["items"]
        if not text.isdigit() or not 1 <= int(text) <= len(items):
            await reply(message, "❓ בחר מספר מהרשימה או לחץ אחורה.")
            return
        item = items[int(text) - 1]
        data["watch_item_id"] = item["id"]
        data["watch_kind"] = item["kind"]
        if item["kind"] == "movie":
            await show_item_preview(message, item)
            state["step"] = "browse_preview"
            return
        seasons = CATALOG.list_seasons(int(item["id"]))
        if not seasons:
            await reply(message, "⚠️ לסדרה הזו עדיין אין עונות.")
            user_states.pop(user_id, None)
            return
        data["series_id"] = item["id"]
        data["series_title"] = item["title"]
        data["seasons"] = seasons
        state["step"] = "browse_season"
        await show_item_preview(message, item)
        all_episodes = CATALOG.list_episodes(int(item["id"]))
        episode_counts = {
            int(season["season_number"]): sum(
                1 for episode in all_episodes
                if int(episode["season_number"]) == int(season["season_number"])
            )
            for season in seasons
        }
        await reply(message, "\n".join(
            [f"📺 {item['title']} — בחר עונה לפי מספר:", ""]
            + [
                f"{index}. עונה {season['season_number']} — "
                f"{episode_counts.get(int(season['season_number']), 0)} "
                f"{'פרק' if episode_counts.get(int(season['season_number']), 0) == 1 else 'פרקים'}"
                for index, season in enumerate(seasons, 1)
            ]
        ))
    elif step == "browse_preview":
        if text == "▶️ צפה עכשיו":
            item = CATALOG.get_item(int(data["watch_item_id"]))
            if not item:
                user_states.pop(user_id, None)
                await reply(message, "❌ לא נמצא פריט לצפייה.")
                return
            await send_stream_link(message, item["title"], item["stream_url"])
            return
        if text in {"⬅️ חזרה", "⬅️ אחורה"}:
            user_states.pop(user_id, None)
            await show_browse_items(message)
            return
        await reply(message, "❓ בחר ▶️ צפה עכשיו או לחץ אחורה.", WATCH_KEYBOARD)
    elif step == "browse_season":
        seasons = data["seasons"]
        if not text.isdigit() or not 1 <= int(text) <= len(seasons):
            await reply(message, "❓ בחר מספר עונה מהרשימה.")
            return
        season = seasons[int(text) - 1]
        episodes = CATALOG.list_episodes(int(data["series_id"]), int(season["season_number"]))
        if not episodes:
            await reply(message, "⚠️ לעונה הזו עדיין אין פרקים.")
            user_states.pop(user_id, None)
            return
        data["episodes"] = episodes
        state["step"] = "browse_episode"
        await reply(message, "\n".join(
            [f"📺 {data['series_title']} — עונה {season['season_number']}, בחר פרק:", ""]
            + [f"{index}. פרק {episode['episode_number']} — {episode['title']}" for index, episode in enumerate(episodes, 1)]
        ))
    elif step == "browse_episode":
        episodes = data["episodes"]
        if not text.isdigit() or not 1 <= int(text) <= len(episodes):
            await reply(message, "❓ בחר מספר פרק מהרשימה.")
            return
        episode = episodes[int(text) - 1]
        await send_stream_link(
            message,
            f"{data['series_title']} — עונה {episode['season_number']} פרק {episode['episode_number']}",
            episode["stream_url"],
        )
    elif step == "title":
        data["title"] = text
        state["step"] = "summary"
        await reply(message, "📝 כתוב תקציר קצר, או כתוב `דלג` / לחץ על ⏭️ דלג.", OPTIONAL_FIELD_KEYBOARD)
    elif step == "summary":
        data["summary"] = "" if is_skip_word(text) else text
        state["step"] = "year"
        await reply(message, "📅 מה שנת היציאה? אפשר לכתוב `דלג` או ללחוץ על הכפתור.", OPTIONAL_FIELD_KEYBOARD)
    elif step == "year":
        data["year"] = int(text) if text.isdigit() and not is_skip_word(text) else None
        state["step"] = "poster"
        await reply(message, "🖼️ שלח קישור לפוסטר, או כתוב `דלג`.", OPTIONAL_FIELD_KEYBOARD)
    elif step == "poster":
        data["poster_url"] = "" if is_skip_word(text) else text
        if flow == "series":
            item_id = CATALOG.add_item(
                "series", data["title"], data["summary"], data["year"], data["poster_url"],
                "", "", None, None,
            )
            user_states.pop(user_id, None)
            await reply(message, f"✅ הסדרה **{data['title']}** נוספה לקטלוג (#{item_id}).")
        elif flow == "movie":
            state["step"] = "media"
            await reply(message, "🎞️ מצוין. עכשיו שלח את קובץ הווידאו של הסרט.")
    elif step == "lookup":
        series_query, detected_season, detected_episode = parse_episode_reference(text)
        lookup_kind = data.get("kind", "series")
        item = find_item(series_query, lookup_kind)
        if not item:
            await reply(message, "❓ לא מצאתי סדרה כזו. נסה שוב או לחץ אחורה.")
            return
        data["series_id"] = item["id"]
        data["series_title"] = item["title"]
        if flow in {"edit", "edit_movie", "edit_series"}:
            data["item_id"] = item["id"]
            state["step"] = "edit_field"
            await reply(message, "✏️ מה תרצה לערוך?", EDIT_FIELD_KEYBOARD)
        elif flow == "remove_movie":
            data["item_id"] = item["id"]
            state["step"] = "confirm_movie"
            await reply(message, f"⚠️ למחוק את הסרט **{item['title']}**? כתוב כן או לא.")
        elif flow == "remove_series":
            state["step"] = "confirm_series"
            await reply(message, f"⚠️ למחוק את **{item['title']}** וכל הפרקים שלה? כתוב כן או לא.")
        else:
            if detected_season is not None and detected_episode is not None:
                data["season"] = detected_season
                data["episode"] = detected_episode
                state["step"] = "episode_title"
                await reply(message, "🎞️ זיהיתי את הסדרה, העונה והפרק. מה שם הפרק? אפשר לדלג.", OPTIONAL_FIELD_KEYBOARD)
            else:
                state["step"] = "season"
                await reply(message, "🔢 מה מספר העונה? אפשר גם לכתוב למשל `פאודה עונה 1 פרק 1`.")
    elif step == "edit_field":
        if is_back_command(text):
            user_states.pop(user_id, None)
            await reply(message, "🔄 חזרתי לתפריט הראשי.", MAIN_KEYBOARD)
            return
        fields = {
            "✏️ שם": "title", "שם": "title", "name": "title",
            "📝 תקציר": "summary", "תקציר": "summary", "summary": "summary",
            "📅 שנת יציאה": "release_year", "שנת יציאה": "release_year", "year": "release_year",
            "🖼️ פוסטר": "poster_url", "פוסטר": "poster_url", "poster": "poster_url",
            "🎭 זאנר": "genre", "זאנר": "genre", "genre": "genre",
        }
        field = fields.get(text.casefold())
        if not field:
            await reply(message, "❓ בחר שדה מתוך הכפתורים.", EDIT_FIELD_KEYBOARD)
            return
        data["edit_field"] = field
        state["step"] = "edit_value"
        prompts = {
            "title": "✏️ מה השם החדש?",
            "summary": "📝 מה התקציר החדש?",
            "release_year": "📅 מה שנת היציאה החדשה?",
            "poster_url": "🖼️ שלח קישור לפוסטר החדש.",
            "genre": "🎭 מה הזאנר החדש? אפשר לכתוב `דלג` כדי להישאר בלי שינוי.",
        }
        await reply(message, prompts[field], OPTIONAL_FIELD_KEYBOARD)
    elif step == "edit_value":
        if is_back_command(text):
            state["step"] = "edit_field"
            await reply(message, "✏️ בחר מה לערוך עכשיו:", EDIT_FIELD_KEYBOARD)
            return
        field = data["edit_field"]
        if is_skip_word(text):
            state["step"] = "edit_field"
            await reply(message, "✏️ בחר מה לערוך עכשיו:", EDIT_FIELD_KEYBOARD)
            return
        if field == "release_year" and not text.isdigit():
            await reply(message, "❓ השנה צריכה להיות מספר. נסה שוב.", OPTIONAL_FIELD_KEYBOARD)
            return
        value: Any = int(text) if field == "release_year" else text
        CATALOG.update_item(data["item_id"], **{field: value})
        state["step"] = "edit_field"
        item = CATALOG.get_item(int(data["item_id"]))
        title = item["title"] if item else "הפריט"
        await reply(message, f"✅ העדכון נשמר בהצלחה עבור **{title}**. בחר עוד שדה לעריכה:", EDIT_FIELD_KEYBOARD)
    elif step == "season":
        if not text.isdigit():
            await reply(message, "🔢 מספר עונה חייב להיות מספר. נסה שוב.")
            return
        data["season"] = int(text)
        if flow == "remove_season":
            state["step"] = "confirm_season"
            await reply(message, "⚠️ למחוק את העונה הזו וכל הפרקים שבה? כתוב כן או לא.")
        else:
            state["step"] = "episode"
            await reply(message, "🔢 מה מספר הפרק?")
    elif step == "episode":
        if not text.isdigit():
            await reply(message, "🔢 מספר פרק חייב להיות מספר. נסה שוב.")
            return
        data["episode"] = int(text)
        if flow == "remove_episode":
            state["step"] = "confirm_episode"
            await reply(message, "⚠️ למחוק את הפרק הזה? כתוב כן או לא.")
        else:
            state["step"] = "episode_title"
            await reply(message, "🎞️ מה שם הפרק? אפשר לכתוב `דלג`.", OPTIONAL_FIELD_KEYBOARD)
    elif step == "episode_title":
        data["title"] = f"פרק {data['episode']}" if is_skip_word(text) else text
        state["step"] = "media"
        await reply(message, "🎞️ עכשיו שלח את קובץ הווידאו של הפרק.")
    elif step == "confirm_series":
        if text.casefold() in {"כן", "כן בטוח", "yes", "y"}:
            CATALOG.delete_item(data["series_id"])
            user_states.pop(user_id, None)
            await reply(message, "✅ הסדרה וכל התוכן שלה נמחקו.")
        elif text.casefold() in {"לא", "no", "n"}:
            user_states.pop(user_id, None)
            await reply(message, "👍 המחיקה בוטלה.")
    elif step == "confirm_movie":
        if text.casefold() in {"כן", "yes", "y"}:
            CATALOG.delete_item(data["item_id"])
            user_states.pop(user_id, None)
            await reply(message, "✅ הסרט נמחק.")
        elif text.casefold() in {"לא", "no", "n"}:
            user_states.pop(user_id, None)
            await reply(message, "👍 המחיקה בוטלה.")
    elif step == "confirm_season":
        if text.casefold() in {"כן", "yes", "y"}:
            CATALOG.delete_season(data["series_id"], data["season"])
            user_states.pop(user_id, None)
            await reply(message, "✅ העונה נמחקה.")
        elif text.casefold() in {"לא", "no", "n"}:
            user_states.pop(user_id, None)
            await reply(message, "👍 המחיקה בוטלה.")
    elif step == "confirm_episode":
        if text.casefold() in {"כן", "yes", "y"}:
            CATALOG.delete_episode(data["series_id"], data["season"], data["episode"])
            user_states.pop(user_id, None)
            await reply(message, "✅ הפרק נמחק.")
        elif text.casefold() in {"לא", "no", "n"}:
            user_states.pop(user_id, None)
            await reply(message, "👍 המחיקה בוטלה.")
    else:
        await reply(message, "❓ לא הבנתי. כתוב /cancel כדי להתחיל מחדש.")


@bot_client.on_message((filters.private | filters.group | filters.channel) & filters.text, group=1)  # type: ignore[reportUnknownMemberType, reportUntypedFunctionDecorator]
async def text_router(client: Client, message: Message):
    if await reject_unauthorized(message):
        return
    text = (message.text or "").strip()
    user_id = message.from_user.id if message.from_user else 0
    state = user_states.get(user_id)
    if state is not None:
        state["updated_at"] = time.time()
    if looks_like_metadata(text):
        parsed_caption = parse_media_caption(text)
        if parsed_caption:
            existing_link_message = attach_metadata_to_existing_catalog(parsed_caption)
            if existing_link_message:
                await reply(message, existing_link_message)
                return
            pending_metadata_by_user[user_id] = parsed_caption
            await reply(message, "✅ שמרתי את פרטי הסדרה/הפוסטר. אפשר לשלוח עכשיו את הקבצים.")
            return
    if text.startswith("/"):
        command = text.split()[0].split("@")[0].lower()
        commands = {
            "/add_movie": "movie", "/add_series": "series", "/add_episode": "episode",
            "/edit_movie": "edit_movie", "/edit_series": "edit_series", "/remove_movie": "remove_movie",
            "/remove_series": "remove_series",
            "/remove_season": "remove_season", "/remove_episode": "remove_episode",
        }
        if command == "/cancel":
            user_states.pop(user_id, None)
            await reply(message, "✅ בוטל.")
        elif command in commands:
            await start_action(message, commands[command])
        elif command == "/list":
            await show_list(message)
        elif command == "/report":
            await show_report(message)
        elif command == "/browse":
            await show_browse_items(message)
        elif command in {"/guide", "/help"}:
            await show_guide(message)
        elif command == "/about":
            await show_about(message)
        elif command in {"/management", "/manage"}:
            await start_action(message, "management")
        elif command == "/refresh":
            user_states.pop(user_id, None)
            await cast(Any, message).reply_text("🔄 התפריט רוענן.", reply_markup=MAIN_KEYBOARD)
        return
    if re.match(r"^(?:סדרה|series)\s*:?.+", text, flags=re.IGNORECASE):
        await begin_season_upload(message, text)
        return
    if text in {"✅ סיום", "סיום"} and user_id in user_states:
        state = user_states[user_id]
        if is_batch_upload_state(state):
            if state.get("summary_sent"):
                user_states.pop(user_id, None)
                await reply(message, "✅ העלאת העונה הסתיימה.")
                return
            uploaded = int(state.get("uploaded", 0))
            failed = int(state.get("failed", 0))
            total = uploaded + failed
            lines = [
                "📦 **סיכום העלאת העונה**",
                "",
                f"📥 התקבלו: {total}",
                f"✅ נשמרו בהצלחה: {uploaded}",
                f"❌ נכשלו: {failed}",
            ]
            if failed:
                lines.extend(["", "⚠️ **פרקים שנכשלו:**"])
                lines.extend(
                    f"• עונה {season} פרק {episode}"
                    for season, episode in state.get("failed_episodes", [])
                )
            user_states.pop(user_id, None)
            await reply(message, "\n".join(lines))
        return
    actions = {
        "🎬 סרטים": "movies_menu", "📺 סדרות": "series_menu",
        "🎬 צפה בסרט": "browse_movies", "📺 צפה בסדרה": "browse_series",
        "➕ הוסף סרט": "movie", "➕ הוסף סדרה": "series",
        "✏️ ערוך סרט": "edit_movie", "✏️ ערוך סדרה": "edit_series",
        "🗑️ הסר סרט": "remove_movie", "🗑️ הסר סדרה": "remove_series",
        "➕ הוסף פרק": "episode", "🗑️ הסר עונה": "remove_season",
        "🗑️ הסר פרק": "remove_episode", "📚 רשימה": "list",
        "📊 דוח": "report", "📖 מדריך": "guide",
        "ℹ️ אודות": "about",
        "🛠️ ניהול": "management",
        "➕ הוסף מנהל": "add_admin", "➖ הסר מנהל": "remove_admin",
        "✅ אשר משתמש": "add_user", "🗑️ הסר משתמש": "remove_user",
        "✅ אשר קבוצה": "add_group", "✅ אשר ערוץ": "add_channel",
        "🗑️ הסר צ׳אט": "remove_chat", "📋 רשימות ניהול": "management_list",
    }
    if text in {"🔄 רענן"} or is_back_command(text):
        user_states.pop(user_id, None)
        await cast(Any, message).reply_text("🔄 התפריט רוענן.", reply_markup=MAIN_KEYBOARD)
    elif text == "❌ ביטול":
        user_states.pop(user_id, None)
        await cast(Any, message).reply_text("✅ בוטל.", reply_markup=MAIN_KEYBOARD)
    elif text in actions:
        action = actions[text]
        if action == "movies_menu":
            await show_section_menu(message, "movies")
        elif action == "series_menu":
            await show_section_menu(message, "series")
        elif action == "browse_movies":
            await show_browse_items(message, "movie")
        elif action == "browse_series":
            await show_browse_items(message, "series")
        elif action == "season_upload_help":
            await reply(message, "📦 כתוב למשל: `סדרה: פאודה עונה 1` ואז שלח את הקבצים לפי הסדר.", UPLOAD_SEASON_KEYBOARD)
        else:
            await start_action(message, action)
    elif user_id in user_states:
        await handle_state(message, user_states[user_id], text)
    else:
        results = CATALOG.search(text)
        if results:
            await reply(message, "🔎 מצאתי:\n" + "\n".join(
                f"{'🎬' if item['kind'] == 'movie' else '📺'} {item['title']} · #{item['id']}"
                for item in results[:10]
            ))
        else:
            await reply(message, "💬 לא מצאתי התאמה בקטלוג. נסה שם אחר או לחץ על 📖 מדריך.")

# ── Keep-alive ────────────────────────────────────────────────────────────────

async def send_heartbeat(reason: str) -> None:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{BASE_URL}/ping", timeout=10)
            response.raise_for_status()
        log.info("Heartbeat sent ✅ reason=%s", reason)
    except Exception as error:
        log.warning("Heartbeat failed (%s): %s", reason, error)


async def keep_alive():
    await asyncio.sleep(15)
    while True:
        await send_heartbeat("periodic")
        await asyncio.sleep(KEEP_ALIVE_INTERVAL)

if __name__ == "__main__":
    uvicorn.run("main:api", host="0.0.0.0", port=PORT, log_level="info")
