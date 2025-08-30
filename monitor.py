import os, sys, json, asyncio, logging, subprocess, re
from datetime import datetime
from typing import Optional
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, MessageMediaGeo
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

# ========= Env =========
API_ID            = os.getenv("API_ID")
API_HASH          = os.getenv("API_HASH")
TG_SESSION_STRING = os.getenv("TG_SESSION_STRING")
CHANNEL           = os.getenv("CHANNEL") or os.getenv("CHAT_ID")  # accepts @name / -100id / t.me/...
DOWNLOAD_DIR      = os.getenv("DOWNLOAD_DIR", "/data/downloads")
STATE_FILE        = os.getenv("STATE_FILE", "/data/state.json")
FAILED_FILE       = os.getenv("FAILED_FILE", "/data/failed_messages.json")
LOG_FILE          = os.getenv("LOG_FILE", "/data/logs/telegram_downloader.log")

RCLONE_DEST       = os.getenv("RCLONE_DEST")
RCLONE_ARGS       = os.getenv("RCLONE_ARGS", "--transfers=4 --checkers=8 --contimeout=30s --low-level-retries=5 --retries=3 --stats-one-line")
UPLOAD_WORKERS    = int(os.getenv("UPLOAD_WORKERS", "2"))
BATCH_SIZE        = int(os.getenv("BATCH_SIZE", "50"))
RATE_DELAY_BASE   = float(os.getenv("RATE_DELAY_BASE", "0.5"))
HISTORY_LIMIT     = int(os.getenv("HISTORY_LIMIT", "0"))  # <=0: no backfill

# ========= Checks & Dirs =========
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

missing = [k for k,v in [("API_ID",API_ID),("API_HASH",API_HASH),("TG_SESSION_STRING",TG_SESSION_STRING),("CHANNEL",CHANNEL),("RCLONE_DEST",RCLONE_DEST)] if not v]
if missing:
    print(f"Error: Missing env: {', '.join(missing)}", file=sys.stderr)
    sys.exit(2)

# ========= Logging =========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("tg-downloader")

# ========= State =========
class State:
    def __init__(self):
        self.last_processed_id: int = 0
        self.processed_ids: set[int] = set()
        self.statistics = {
            "total_processed": 0,
            "downloaded_photos": 0,
            "downloaded_videos": 0,
            "downloaded_files": 0,
            "text_messages": 0,
            "failed_downloads": 0,
            "start_time": datetime.now().isoformat(),
            "last_update": datetime.now().isoformat()
        }
    def to_dict(self):
        return {"last_processed_id": self.last_processed_id, "processed_ids": list(self.processed_ids), "statistics": self.statistics}
    @classmethod
    def from_dict(cls, data: dict):
        s = cls()
        s.last_processed_id = data.get("last_processed_id", 0)
        s.processed_ids = set(data.get("processed_ids", []))
        s.statistics = data.get("statistics", s.statistics)
        return s
    def update_stats(self, kind: str):
        self.statistics["total_processed"] += 1
        self.statistics["last_update"] = datetime.now().isoformat()
        if kind == "photo": self.statistics["downloaded_photos"] += 1
        elif kind == "video": self.statistics["downloaded_videos"] += 1
        elif kind == "file": self.statistics["downloaded_files"] += 1
        elif kind == "text": self.statistics["text_messages"] += 1
        elif kind == "failed": self.statistics["failed_downloads"] += 1
    def mark_seen(self, mid: int):
        self.processed_ids.add(mid)
        if len(self.processed_ids) > 100000:
            cutoff = sorted(self.processed_ids)[len(self.processed_ids)//4]
            self.processed_ids = set(i for i in self.processed_ids if i >= cutoff)
        if mid > self.last_processed_id:
            self.last_processed_id = mid

state = State()

async def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"save_state error: {e}")

async def load_state():
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = State.from_dict(json.load(f))
            logger.info(f"Loaded state: last_id={state.last_processed_id} seen={len(state.processed_ids)}")
        except Exception as e:
            logger.error(f"load_state error, using fresh: {e}")
    else:
        logger.info("No previous state, fresh start.")

# ========= Utils =========
def gen_path(message_id: int, group_id: Optional[int]=None, ext: Optional[str]=None) -> str:
    base = f"{message_id}-{group_id}" if group_id else f"{message_id}"
    if ext: base = f"{base}.{ext}"
    return os.path.join(DOWNLOAD_DIR, base)

def safe_write(path: str, text: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

# def save_metadata(message, file_path: str) -> str:
#     meta = {
#         "message_id": getattr(message, "id", None),
#         "group_id": getattr(message, "grouped_id", None),
#         "date": message.date.isoformat() if getattr(message, "date", None) else None,
#         "file_path": file_path,
#         "download_time": datetime.now().isoformat(),
#         "chat_id": getattr(message, "chat_id", None),
#         "text": getattr(message, "text", None),
#     }
#     try:
#         if isinstance(message.media, MessageMediaDocument):
#             doc = message.media.document
#             meta["mime_type"] = getattr(doc, "mime_type", None)
#             if getattr(doc, "attributes", None):
#                 meta["doc_attributes"] = [a.__class__.__name__ for a in doc.attributes]
#     except Exception as e:
#         meta["meta_error"] = str(e)
#     mpath = os.path.splitext(file_path)[0] + "_metadata.json"
#     safe_write(mpath, json.dumps(meta, ensure_ascii=False, indent=2))
#     return mpath

def append_failed(message, reason: str):
    try:
        arr = []
        if os.path.exists(FAILED_FILE):
            try:
                with open(FAILED_FILE, "r", encoding="utf-8") as f:
                    arr = json.load(f)
            except Exception:
                arr = []
        arr.append({
            "message_id": getattr(message, "id", None),
            "chat_id": getattr(message, "chat_id", None),
            "date": message.date.isoformat() if getattr(message, "date", None) else None,
            "error": reason,
            "error_time": datetime.now().isoformat()
        })
        with open(FAILED_FILE, "w", encoding="utf-8") as f:
            json.dump(arr, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"append_failed error: {e}")

# ========= rclone upload =========
upload_q: asyncio.Queue[str] = asyncio.Queue()
stop_upload_workers = asyncio.Event()

async def rclone_copyto(src_path: str, dest_path: str) -> bool:
    cmd = ["rclone", "copyto", src_path, dest_path] + RCLONE_ARGS.split()
    proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        logger.error(f"rclone failed: {proc.stderr.strip()}")
        return False
    return True

async def uploader_worker(worker_id: int):
    while not (stop_upload_workers.is_set() and upload_q.empty()):
        try:
            path = await asyncio.wait_for(upload_q.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        try:
            dest = RCLONE_DEST.rstrip("/") + "/" + os.path.basename(path)
            ok = await rclone_copyto(path, dest)
            if ok:
                try: os.remove(path)
                except FileNotFoundError: pass
                logger.info(f"[uploader-{worker_id}] uploaded & cleaned: {os.path.basename(path)}")
            else:
                logger.warning(f"[uploader-{worker_id}] upload failed, kept local: {os.path.basename(path)}")
        finally:
            upload_q.task_done()

async def start_upload_workers(n: int):
    return [asyncio.create_task(uploader_worker(i)) for i in range(n)]

async def enqueue_upload(path: str):
    await upload_q.put(path)

async def scan_and_enqueue_local_files():
    for name in os.listdir(DOWNLOAD_DIR):
        path = os.path.join(DOWNLOAD_DIR, name)
        if os.path.isfile(path):
            await enqueue_upload(path)

# ========= Download handlers =========
client = TelegramClient(StringSession(TG_SESSION_STRING), int(API_ID), API_HASH)
TARGET_CHAT_ID: Optional[int] = None

async def handle_expired_and_download(message, dst_path: str) -> Optional[str]:
    try:
        return await client.download_media(message.media, file=dst_path)
    except Exception as e:
        if "file reference has expired" in str(e).lower():
            try:
                fresh = await client.get_messages(message.chat_id, ids=message.id)
                if not fresh: return None
                return await client.download_media(fresh.media, file=dst_path)
            except Exception as e2:
                logger.error(f"refresh failed: {e2}")
                return None
        logger.error(f"download error: {e}")
        return None

async def save_text_sidecar(base_noext: str, text: Optional[str]):
    if text:
        p = base_noext + ".txt"
        safe_write(p, text)
        await enqueue_upload(p)

async def handle_text(message):
    p = gen_path(message.id, ext="txt")
    safe_write(p, message.text or "")
    state.update_stats("text")
    await enqueue_upload(p); 

async def handle_photo(message):
    p = gen_path(message.id, message.grouped_id)
    got = await handle_expired_and_download(message, p)
    if got:
        await save_text_sidecar(os.path.splitext(got)[0], message.text)
        state.update_stats("photo")
        await enqueue_upload(got); 
    else:
        state.update_stats("failed"); append_failed(message, "photo download failed")

async def handle_document_or_video(message):
    p = gen_path(message.id, message.grouped_id)
    got = await handle_expired_and_download(message, p)
    if got:
        await save_text_sidecar(os.path.splitext(got)[0], message.text)
        try:
            mime = message.media.document.mime_type or ""
            kind = "video" if mime.startswith("video/") else "file"
        except Exception:
            kind = "file"
        state.update_stats(kind)
        await enqueue_upload(got); 
    else:
        state.update_stats("failed"); append_failed(message, "document/video download failed")

async def handle_geo(message):
    p = gen_path(message.id, ext="txt")
    try:
        lat = message.media.geo.lat; lon = message.media.geo.long
        content = f"lat: {lat}\\nlon: {lon}"
        if message.text: content += f"\\n\\n{message.text}"
        safe_write(p, content)
        state.update_stats("text")
        await enqueue_upload(p); 
    except Exception as e:
        state.update_stats("failed"); append_failed(message, f"geo parse failed: {e}")

async def process_message(msg):
    mid = msg.id
    if mid in state.processed_ids:
        return
    if msg.media is None:
        if msg.text: await handle_text(msg)
    elif isinstance(msg.media, MessageMediaPhoto):
        await handle_photo(msg)
    elif isinstance(msg.media, MessageMediaDocument):
        await handle_document_or_video(msg)
    elif isinstance(msg.media, MessageMediaGeo):
        await handle_geo(msg)
    else:
        await handle_document_or_video(msg)
    state.mark_seen(mid)
    await save_state()

# --- Event handler: no 'chats=...' filter; filter by chat_id at runtime ---
@client.on(events.NewMessage())
async def on_new_message(event):
    try:
        if TARGET_CHAT_ID is not None and event.chat_id != TARGET_CHAT_ID:
            return
        await process_message(event.message)
    except FloodWaitError as e:
        logger.warning(f"FloodWait {e.seconds}s; backing off"); await asyncio.sleep(e.seconds)
    except Exception as e:
        logger.exception(f"on_new_message error: {e}")

# ========= Target resolve =========
async def resolve_target_id(spec: str):
    """Resolve CHANNEL env into numeric TARGET_CHAT_ID when possible.
    Accepts: -100id / @username / t.me/ links. If not resolvable, keep None
    and rely on runtime filtering (still fine if account receives events)."""
    global TARGET_CHAT_ID
    s = (spec or '').strip()
    if not s:
        TARGET_CHAT_ID = None; return

    # numeric -100...
    if s.lstrip('-').isdigit():
        TARGET_CHAT_ID = int(s)
        return

    # @username
    if s.startswith('@'):
        try:
            ent = await client.get_entity(s)
        except Exception:
            # try join public channel (no effect if already in)
            try:
                await client(JoinChannelRequest(s))
                ent = await client.get_entity(s)
            except Exception as e:
                logger.warning(f"join/resolve {s} failed: {e}")
                TARGET_CHAT_ID = None
                return
        TARGET_CHAT_ID = ent.id
        return

    # t.me/+invite or t.me/joinchat/...
    m = re.search(r't\\.me/(?:\\+|joinchat/)([A-Za-z0-9_-]+)', s)
    if m:
        invite = m.group(1)
        try:
            await client(ImportChatInviteRequest(invite))
        except Exception:
            pass
        TARGET_CHAT_ID = None
        return

    # t.me/c/<internalId>/...
    m2 = re.search(r't\\.me/c/(\\d+)', s)
    if m2:
        internal = m2.group(1)
        TARGET_CHAT_ID = int(f"-100{internal}")
        return

    TARGET_CHAT_ID = None

# ========= Backfill / Stats =========
async def backfill_history():
    if HISTORY_LIMIT <= 0:
        logger.info("Skip backfill; listen-only mode.")
        return
    processed = 0
    offset = state.last_processed_id if state.last_processed_id > 0 else 0
    delay = RATE_DELAY_BASE
    while True:
        cur = min(BATCH_SIZE, max(0, HISTORY_LIMIT - processed))
        if cur == 0: break
        messages = await client.get_messages(CHANNEL, limit=cur, offset_id=offset, reverse=True)
        if not messages:
            logger.info("Backfill done"); break
        offset = messages[-1].id
        for m in messages:
            if m.id in state.processed_ids: continue
            try:
                await process_message(m); processed += 1
                await asyncio.sleep(delay)
            except FloodWaitError as e:
                logger.warning(f"Backfill FloodWait {e.seconds}s"); await asyncio.sleep(e.seconds)
            except Exception as e:
                state.update_stats("failed"); append_failed(m, f"backfill error: {e}")
        await asyncio.sleep(0.5)

async def periodic_stats():
    while True:
        await asyncio.sleep(3600)
        s = state.statistics
        logger.info(f"Stats total:{s['total_processed']} photo:{s['downloaded_photos']} video:{s['downloaded_videos']} file:{s['downloaded_files']} text:{s['text_messages']} fail:{s['failed_downloads']}")
        await save_state()

# ========= Main =========
async def main():
    logger.info("Starting tg-rclone (event-driven; per-file upload & delete)")
    await load_state()
    await client.start()  # StringSession: headless
    await resolve_target_id(CHANNEL)
    logger.info(f"TARGET_CHAT_ID => {TARGET_CHAT_ID} (CHANNEL={CHANNEL})")

    workers = await start_upload_workers(UPLOAD_WORKERS)
    await scan_and_enqueue_local_files()
    await backfill_history()
    asyncio.create_task(periodic_stats())

    logger.info("Listening for new messages...")
    try:
        await client.run_until_disconnected()
    finally:
        stop_upload_workers.set()
        await asyncio.gather(*workers, return_exceptions=True)

if __name__ == "__main__":
    asyncio.run(main())
