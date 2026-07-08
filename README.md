# YouTube Chat Exporter — Backend

FastAPI + chat-downloader backend that streams **real** YouTube live chat messages to the frontend.

## Quick Start (Local)

```bash
# 1. Install Python deps
pip install -r requirements.txt

# 2. Run the dev server
uvicorn main:app --reload --port 8000
```

The API is now at `http://localhost:8000`.

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/info/{video_id}` | Stream title, channel, status, viewer count |
| GET | `/api/chat/{video_id}` | Server-Sent Events stream of all chat messages |
| GET | `/health` | Health check |

### `/api/chat/{video_id}` query params

| Param | Default | Description |
|-------|---------|-------------|
| `max_messages` | `0` (no limit) | Stop after N messages |

### SSE message format

Each `data:` line is a JSON object:
```json
{
  "id": "abc123",
  "username": "viewer42",
  "message": "LET'S GO!!!",
  "timestamp": "1:23:45",
  "epoch": 1718000000000,
  "avatarColor": "#6366f1",
  "isMod": false,
  "isMember": true,
  "isSuperChat": true,
  "isSuperSticker": false,
  "scAmount": 50.00,
  "scCurrency": "USD",
  "memberBadge": "6 months"
}
```

The stream ends with `data: [DONE]`.

Error events look like:
```json
{"error": "No chat replay available for this video"}
```

---


## Notes

- **chat-downloader** works for both live streams and ended VODs with chat replay enabled.
- No API key required.
- Very long streams (millions of messages) can take many minutes to download fully.
  The frontend shows real-time progress as messages stream in.
- YouTube occasionally rate-limits heavy scrapers. If you get 429 errors, wait a few minutes.
