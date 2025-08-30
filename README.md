# tg-rclone — Telegram → Cloud via rclone (Realtime, Headless)

A production-focused Dockerized service that **listens to a Telegram channel/group (user session; supports private channels)**,
**downloads every new message/media**, **uploads to your cloud using `rclone`**, and **deletes local temp files upon success**.

> Event-driven (Telethon). No cron. No polling loops. Straight-through pipeline: download → rclone copyto → delete.

---

## Features

- **Private channels supported** via Telethon `StringSession` (user account; not a bot)
- **All content types**: text, photo, video, document, audio/voice, stickers, location (geo saved as `.txt`)
- **Immediate offload**: `rclone copyto` per file → delete local file on success
- **Resilience**: auto-refresh expired file references; adaptive FloodWait backoff
- **Startup re-upload**: enqueues leftover files under `/data/downloads` on boot
- **Stateful & lean**: `state.json` (last processed id & stats), bounded in-memory set for dedup

---

## Prerequisites

1) **rclone** is configured on the host (`rclone config`) – we mount your `~/.config/rclone` into the container read-only.  
2) **Telegram user API credentials** from <https://my.telegram.org/apps>.  
3) A **Telethon StringSession** for headless login (generate once; see below).

---

## Generate `TG_SESSION_STRING` (one-time)

**One-off Docker**

```bash
docker run --rm -it \
  -e API_ID=<YOUR_API_ID> \
  -e API_HASH=<YOUR_API_HASH> \
  python:3.11-slim \
  sh -lc '
set -e
pip install --no-cache-dir telethon >/dev/null
cat > /tmp/gen_session.py << "PY"
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
from getpass import getpass
import os, sys

api_id  = int(os.environ["API_ID"])
api_hash= os.environ["API_HASH"]

# Make sure interactive prompts read from the real TTY (not the heredoc/stdin)
try:
    sys.stdin = open("/dev/tty")
except Exception:
    pass

client = TelegramClient(StringSession(), api_id, api_hash)
client.connect()
try:
    if not client.is_user_authorized():
        phone = input("Phone (e.g. +821012345678): ").strip()
        client.send_code_request(phone)
        code  = input("Login code (SMS/Telegram): ").strip()
        try:
            client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            pwd = getpass("2FA password: (Input hidden)")  # hidden input
            client.sign_in(password=pwd)
    print("\n==== COPY THIS ====")
    print("TG_SESSION_STRING=" + client.session.save())
    print("===================\n")
finally:
    client.disconnect()
PY
python /tmp/gen_session.py
'


```

Copy the printed long string and keep it **secret** (treat like a password).

---

## Build

```bash
docker build -t tg-rclone:latest .
```

## Run (single service)

```bash
docker run -d --name tg-backup       -v ~/.config/rclone:/root/.config/rclone:ro       -v $(pwd)/data:/data       -e API_ID=123456       -e API_HASH=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx       -e TG_SESSION_STRING='paste-your-string-session'       -e CHANNEL='@YourChannelOrNumericChatId'       -e RCLONE_DEST='myremote:TelegramBackup/YourChannel'       -e UPLOAD_WORKERS=3       -e HISTORY_LIMIT=0       tg-rclone:latest
```

> `CHANNEL` can be `@publicname` or a numeric id like `-1001234567890` (**always quote** to keep YAML/ENV from treating it as a number).

---

## docker-compose

See `docker-compose.yml` in this repo (also below).

```yaml
version: "3.9"
services:
  tg-backup:
    build: .               # or comment this and keep 'image:' if you've prebuilt/pushed
    image: tg-rclone:latest
    container_name: tg-backup
    restart: unless-stopped
    environment:
      API_ID: "123456"
      API_HASH: "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
      TG_SESSION_STRING: "paste-your-string-session"
      CHANNEL: "@YourChannelOrNumericChatId"
      RCLONE_DEST: "myremote:TelegramBackup/YourChannel"
      UPLOAD_WORKERS: "3"
      HISTORY_LIMIT: "0"         # >0 backfills N historical messages once at startup; 0 = listen-only
      # Optional overrides:
      # DOWNLOAD_DIR: "/data/downloads"
      # STATE_FILE: "/data/state.json"
      # FAILED_FILE: "/data/failed_messages.json"
      # LOG_FILE: "/data/logs/telegram_downloader.log"
      # RCLONE_ARGS: "--transfers=4 --checkers=8 --contimeout=30s --low-level-retries=5 --retries=3 --stats-one-line"
    volumes:
      - ~/.config/rclone:/root/.config/rclone:ro
      - ./data:/data
```

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `API_ID` | ✅ | – | Telegram user API ID (from `my.telegram.org/apps`) |
| `API_HASH` | ✅ | – | Telegram user API hash |
| `TG_SESSION_STRING` | ✅ | – | Telethon session string for headless login |
| `CHANNEL` | ✅ | – | `@username` or numeric chat id (quote it) |
| `RCLONE_DEST` | ✅ | – | rclone remote path, e.g. `myremote:TelegramBackup/YourChannel` |
| `UPLOAD_WORKERS` | – | `2` | Concurrent `rclone copyto` workers |
| `HISTORY_LIMIT` | – | `0` | `>0` backfills N historical messages once at startup |
| `DOWNLOAD_DIR` | – | `/data/downloads` | Staging dir before upload |
| `STATE_FILE` | – | `/data/state.json` | State & stats file |
| `FAILED_FILE` | – | `/data/failed_messages.json` | Failure ledger |
| `LOG_FILE` | – | `/data/logs/telegram_downloader.log` | Log path |
| `RCLONE_ARGS` | – | `--transfers=4 --checkers=8 --contimeout=30s --low-level-retries=5 --retries=3 --stats-one-line` | Extra rclone flags |

**Volumes**

- `~/.config/rclone` → `/root/.config/rclone:ro` (your remotes)
- `./data` → `/data` (state/logs/temp; safe to persist)

---

## Security Notes

- Treat `TG_SESSION_STRING` as a **secret** (persistent login). Use secret managers or `.env` files excluded from VCS.
- To revoke access: Telegram → Settings → Devices → terminate the session / sign out all sessions.
- Mount rclone config **read-only**.

---

## Troubleshooting

- **CHANNEL not found / 403**: ensure your **user account** can access the channel/group; verify `@username` vs numeric id.
- **file reference has expired**: the service re-fetches and retries automatically; if it loops, regenerate `TG_SESSION_STRING`.
- **FloodWait**: backoff is automatic during initial backfill or heavy media bursts.
- **No uploads**: check `docker logs -f tg-backup`, verify `RCLONE_DEST`, and test `rclone lsd myremote:` on the host.
- **Disk usage grows**: local files are kept only if rclone copy failed; service re-enqueues leftovers on restart.

---

## Minimal .env example

```env
API_ID=123456
API_HASH=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TG_SESSION_STRING=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
CHANNEL=@your_channel_or_numeric_id
RCLONE_DEST=myremote:TelegramBackup/YourChannel
UPLOAD_WORKERS=3
HISTORY_LIMIT=0
```

---

## License

MIT (or update to your preferred license).
