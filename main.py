"""
YouTube Live Chat Exporter — FastAPI Backend
Uses yt-dlp to fetch stream info + chat messages (works for live & ended streams).
"""

import asyncio
import json
import re
import ssl
import subprocess
import sys
import tempfile
import os
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import httpx
import yt_dlp

app = FastAPI(title="YT Chat Exporter API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# ─── Helpers ──────────────────────────────────────────────────────────────────

def extract_video_id(url_or_id: str) -> str | None:
    url_or_id = url_or_id.strip()
    patterns = [
        r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/live/)([a-zA-Z0-9_-]{11})",
        r"^([a-zA-Z0-9_-]{11})$",
    ]
    for p in patterns:
        m = re.search(p, url_or_id)
        if m:
            return m.group(1)
    return None


# Shared yt-dlp options — disable SSL verify to handle Windows cert issues
YDL_OPTS_BASE = {
    "quiet": True,
    "no_warnings": True,
    "nocheckcertificate": True,   # fixes SSL errors on Windows
}

# YouTube frequently blocks requests from cloud/datacenter IPs with a
# "Sign in to confirm you're not a bot" error. Supplying cookies from a
# real logged-in browser session works around this. If a cookies file is
# present (mounted as a Render "Secret File", or via the COOKIES_FILE env
# var), use it automatically.
COOKIES_FILE = os.environ.get("COOKIES_FILE", "/etc/secrets/cookies.txt")
if os.path.exists(COOKIES_FILE):
    YDL_OPTS_BASE["cookiefile"] = COOKIES_FILE


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/api/info/{video_id}")
async def stream_info(video_id: str):
    vid = extract_video_id(video_id)
    if not vid:
        raise HTTPException(400, "Invalid video ID")

    url = f"https://www.youtube.com/watch?v={vid}"
    loop = asyncio.get_event_loop()

    def _fetch():
        opts = {**YDL_OPTS_BASE, "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await loop.run_in_executor(None, _fetch)
    except Exception as exc:
        raise HTTPException(500, f"Could not fetch stream info: {exc}")

    is_live = info.get("is_live") or info.get("was_live") or False
    status  = "LIVE" if info.get("is_live") else "ENDED"

    return {
        "videoId":     vid,
        "title":       info.get("title", "YouTube Stream"),
        "channel":     info.get("uploader") or info.get("channel", "Unknown"),
        "thumbnail":   f"https://img.youtube.com/vi/{vid}/mqdefault.jpg",
        "status":      status,
        "viewerCount": info.get("concurrent_view_count"),
        "url":         url,
    }


@app.get("/api/chat/{video_id}")
async def stream_chat(video_id: str, max_messages: int = 0):
    """
    Stream real YouTube chat via Server-Sent Events.
    Downloads the live_chat.json subtitles via yt-dlp, then streams
    each message back to the frontend as it's parsed.
    """
    vid = extract_video_id(video_id)
    if not vid:
        raise HTTPException(400, "Invalid video ID")

    url = f"https://www.youtube.com/watch?v={vid}"

    AVATAR_COLORS = [
        "#6366f1","#8b5cf6","#ec4899","#f43f5e","#ef4444",
        "#f97316","#eab308","#22c55e","#14b8a6","#06b6d4",
        "#3b82f6","#a855f7","#d946ef","#fb923c","#84cc16",
    ]

    def color_for(author_id: str) -> str:
        return AVATAR_COLORS[sum(ord(c) for c in (author_id or "x")) % len(AVATAR_COLORS)]

    # Map of currency symbols/prefixes -> ISO 4217 code
    CURRENCY_MAP = {
        "$": "USD", "US$": "USD", "CA$": "CAD", "A$": "AUD", "NZ$": "NZD",
        "HK$": "HKD", "MX$": "MXN", "CLP$": "CLP", "ARS$": "ARS",
        "£": "GBP", "€": "EUR", "¥": "JPY", "CN¥": "CNY", "₩": "KRW",
        "₹": "INR", "₱": "PHP", "₪": "ILS", "₺": "TRY", "₴": "UAH",
        "₫": "VND", "₦": "NGN", "₲": "PYG", "฿": "THB", "R$": "BRL",
        "kr": "SEK", "NOK": "NOK", "DKK": "DKK", "zł": "PLN",
        "Kč": "CZK", "Ft": "HUF", "₽": "RUB",
        "CHF": "CHF", "SGD": "SGD", "MYR": "MYR",
        "R": "ZAR",
    }

    def detect_currency(amount_str: str) -> str:
        """Extract ISO currency code from a YouTube Super Chat amount string like ₹500 or CA$10.00."""
        if not amount_str:
            return "USD"
        s = amount_str.strip()
        # Try longest prefix/suffix match first so CA$ beats $
        for symbol in sorted(CURRENCY_MAP, key=len, reverse=True):
            if s.startswith(symbol) or s.endswith(symbol):
                return CURRENCY_MAP[symbol]
        leftover = re.sub(r"[\d.,\s]", "", s).strip()
        return CURRENCY_MAP.get(leftover, leftover or "USD")

    def parse_amount(amount_str: str | None) -> float | None:
        if not amount_str:
            return None
        cleaned = re.sub(r"[^\d.]", "", amount_str)
        try:
            return float(cleaned)
        except ValueError:
            return None

    async def event_generator() -> AsyncGenerator[str, None]:
        tmp_dir = tempfile.mkdtemp()
        count = 0

        try:
            loop = asyncio.get_event_loop()

            # Download the live-chat JSON subtitle file
            def _download_chat():
                opts = {
                    **YDL_OPTS_BASE,
                    "skip_download": True,
                    "writesubtitles": True,
                    "writeautomaticsub": True,
                    "subtitleslangs": ["live_chat"],
                    "outtmpl": os.path.join(tmp_dir, "%(id)s.%(ext)s"),
                }
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])

            await loop.run_in_executor(None, _download_chat)

            # Find the downloaded .live_chat.json file
            chat_file = None
            for fname in os.listdir(tmp_dir):
                if "live_chat" in fname and fname.endswith(".json"):
                    chat_file = os.path.join(tmp_dir, fname)
                    break

            if not chat_file:
                yield f"data: {json.dumps({'error': 'No chat replay found for this video. The streamer may have disabled chat replay.'})}\n\n"
                yield "data: [DONE]\n\n"
                return

            # Stream messages from the JSON file line by line
            with open(chat_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # yt-dlp live_chat.json format: each line is one action
                    actions = raw.get("replayChatItemAction", {}).get("actions", [])
                    if not actions:
                        # also handle direct liveChatRenderer format
                        actions = [raw]

                    for action in actions:
                        item = action.get("addChatItemAction", {}).get("item", {})

                        # Regular message
                        renderer = item.get("liveChatTextMessageRenderer", {})
                        msg_type = "text"
                        sc_amount = None
                        sc_currency = None
                        is_super_chat = False

                        # Super Chat
                        if not renderer:
                            renderer = item.get("liveChatPaidMessageRenderer", {})
                            if renderer:
                                msg_type = "super_chat"
                                is_super_chat = True
                                amount_str = renderer.get("purchaseAmountText", {}).get("simpleText", "")
                                sc_amount = parse_amount(amount_str)
                                # extract currency symbol
                                sc_currency = detect_currency(amount_str)

                        if not renderer:
                            continue

                        author = renderer.get("authorName", {}).get("simpleText", "Anonymous")
                        author_id = renderer.get("authorExternalChannelId", author)
                        badges = renderer.get("authorBadges", [])

                        is_mod = any(
                            b.get("liveChatAuthorBadgeRenderer", {}).get("icon", {}).get("iconType") == "MODERATOR"
                            for b in badges
                        )
                        is_member = any(
                            "liveChatAuthorBadgeRenderer" in b and
                            b["liveChatAuthorBadgeRenderer"].get("customThumbnail")
                            for b in badges
                        )
                        member_badge = None
                        if is_member:
                            for b in badges:
                                tooltip = b.get("liveChatAuthorBadgeRenderer", {}).get("tooltip", "")
                                if tooltip:
                                    member_badge = tooltip
                                    break

                        # Message text
                        runs = renderer.get("message", {}).get("runs", [])
                        message_text = "".join(
                            r.get("text", "") or r.get("emoji", {}).get("shortcuts", [""])[0]
                            for r in runs
                        )

                        # Timestamp
                        timestamp_usec = int(renderer.get("timestampUsec", 0))
                        epoch_ms = timestamp_usec // 1000
                        time_text = renderer.get("timestampText", {}).get("simpleText", "")

                        msg = {
                            "id":           renderer.get("id", ""),
                            "username":     author,
                            "message":      message_text,
                            "timestamp":    time_text,
                            "epoch":        epoch_ms,
                            "avatarColor":  color_for(author_id),
                            "isMod":        is_mod,
                            "isMember":     is_member,
                            "isSuperChat":  is_super_chat,
                            "isSuperSticker": False,
                            "scAmount":     sc_amount,
                            "scCurrency":   sc_currency,
                            "memberBadge":  member_badge,
                        }

                        yield f"data: {json.dumps(msg)}\n\n"
                        count += 1

                        if max_messages and count >= max_messages:
                            return

                        if count % 100 == 0:
                            await asyncio.sleep(0)

        except Exception as exc:
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        finally:
            # Cleanup temp files
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class SummarizeRequest(BaseModel):
    prompt: str


@app.post("/api/summarize")
async def summarize(req: SummarizeRequest):
    """
    Proxies a chat-analysis prompt to the Claude API using a server-side
    API key, so the key is never exposed to the browser.
    """
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY is not configured on the server")

    async with httpx.AsyncClient(timeout=60) as client:
        try:
            res = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-5",
                    "max_tokens": 1000,
                    "messages": [{"role": "user", "content": req.prompt}],
                },
            )
            res.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(exc.response.status_code, f"Anthropic API error: {exc.response.text}")
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Could not reach Anthropic API: {exc}")

    return res.json()


@app.get("/health")
async def health():
    return {"status": "ok"}