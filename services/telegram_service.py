"""
Telegram data collection service — MTProto API via Telethon.

Two modes:
  1. search_messages(query)          — full-text search across Telegram
  2. get_channel_messages(channel)   — all recent posts from a specific channel
  3. search_multi_keywords(keywords) — parallel keyword search + dedup

Requires a real Telegram account (api_id + api_hash from my.telegram.org).
On first run, will prompt for phone number + OTP to create a session file.
Session is cached at TELEGRAM_SESSION_PATH so subsequent runs are instant.

NOTE: Only reads publicly accessible channels and groups.
      Never reads private chats or DMs.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog

from config import (
    TELEGRAM_API_ID,
    TELEGRAM_API_HASH,
    TELEGRAM_SESSION_PATH,
)

# redesign-2026-05 Phase 5: whisper service was removed; the video-post path
# below is a no-op when whisper=None (default).
WHISPER_MAX_VIDEO_MB = 100
from models.post import Post

log = structlog.get_logger(__name__)

_safe_log = lambda s: str(s).encode("ascii", "replace").decode("ascii")

# Regex to find the first URL in a string
_URL_RE = re.compile(r"https?://[^\s\)\]>\"']+")


def _extract_url(text: str) -> Optional[str]:
    """Return the first URL found in text, or None."""
    m = _URL_RE.search(text)
    return m.group(0) if m else None


def _fetch_url_title(url: str) -> str:
    """
    Synchronously fetch the <title> of a web page (best-effort).
    Returns empty string on any failure.
    """
    try:
        import httpx
        resp = httpx.get(url, timeout=5, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200:
            m = re.search(r"<title[^>]*>([^<]+)</title>", resp.text, re.I)
            if m:
                return m.group(1).strip()
    except Exception:
        pass
    return ""

# Channels to search, loaded from TELEGRAM_CHANNELS in .env.
# Use scripts/telegram_find_channels.py to discover channel usernames.
_env_channels = os.getenv("TELEGRAM_CHANNELS", "")
DEFAULT_MISINFO_CHANNELS: list[str] = (
    [c.strip().lstrip("@") for c in _env_channels.split(",") if c.strip()]
)


def _tg_message_to_post(msg, channel_name: str) -> Optional[Post]:
    """
    Convert a Telethon Message object to a Post model.

    For video / document messages with no caption:
      - If there's a web-page preview with a URL, fetch its page title and
        use that as the post text so the content enters the RAG pipeline.
      - Otherwise the post is skipped (no text to analyse).
    """
    text = getattr(msg, "message", "") or ""
    media = getattr(msg, "media", None)

    # ── Detect media type ──────────────────────────────────────────────────────
    media_prefix = ""
    if media is not None:
        try:
            from telethon.tl.types import (
                MessageMediaDocument,
                MessageMediaPhoto,
                MessageMediaWebPage,
            )
            if isinstance(media, MessageMediaDocument):
                doc = getattr(media, "document", None)
                attrs = getattr(doc, "attributes", []) if doc else []
                attr_names = {type(a).__name__ for a in attrs}
                if "DocumentAttributeVideo" in attr_names:
                    media_prefix = "[VIDEO]"
                elif "DocumentAttributeAudio" in attr_names:
                    media_prefix = "[AUDIO]"
                else:
                    media_prefix = "[FILE]"
            elif isinstance(media, MessageMediaPhoto):
                media_prefix = "[IMAGE]"
            elif isinstance(media, MessageMediaWebPage):
                wp = getattr(media, "webpage", None)
                if wp and hasattr(wp, "url") and wp.url:
                    # Show only the domain, not the full URL, to keep post text clean
                    try:
                        from urllib.parse import urlparse
                        domain = urlparse(wp.url).netloc.lstrip("www.") or wp.url[:40]
                    except Exception:
                        domain = wp.url[:40]
                    media_prefix = f"[{domain}]"
                    # If there's no caption, try to use the page title/description
                    if not text.strip():
                        page_desc = getattr(wp, "description", "") or ""
                        page_title = getattr(wp, "title", "") or ""
                        if page_title or page_desc:
                            text = f"{page_title} {page_desc}".strip()
                        else:
                            # Last resort: HTTP fetch for <title>
                            text = _fetch_url_title(wp.url)
        except Exception:
            pass  # Telethon types vary across versions; best-effort

    # ── For video/audio/file posts without any caption, try URL in caption ────
    if media_prefix in ("[VIDEO]", "[AUDIO]", "[FILE]") and not text.strip():
        # Some channels paste a link in the same message; extract it
        url = _extract_url(text) if text else None
        if url:
            page_title = _fetch_url_title(url)
            if page_title:
                text = f"{url} — {page_title}"
            else:
                text = url

    # Nothing to analyse
    if not text.strip():
        return None

    # Prepend media prefix so analysts know what type of content this was
    if media_prefix:
        text = f"{media_prefix} {text}" if text.strip() else media_prefix

    sender = getattr(msg, "sender_id", None)
    account_id = str(sender) if sender else f"{channel_name}_unknown"

    posted_at: Optional[datetime] = None
    if msg.date:
        posted_at = msg.date.replace(tzinfo=timezone.utc) if msg.date.tzinfo is None else msg.date

    # redesign-2026-05-kg Phase A: Telegram reply chain.
    # `Message.reply_to.reply_to_msg_id` is set when the message is a reply;
    # we map it to our Post id scheme so v2 ingestion writes a Replied edge.
    parent_post_id: Optional[str] = None
    reply_to = getattr(msg, "reply_to", None)
    parent_msg_id = getattr(reply_to, "reply_to_msg_id", None) if reply_to else None
    if parent_msg_id:
        parent_post_id = f"tg_{channel_name}_{parent_msg_id}"

    return Post(
        id=f"tg_{channel_name}_{msg.id}",
        account_id=account_id,
        channel_name=channel_name,   # human-readable Telegram channel username
        text=text,
        lang="",           # Telegram doesn't tag language
        like_count=getattr(msg, "reactions", None) and
                   sum(r.count for r in msg.reactions.results) or 0,
        reply_count=getattr(msg.replies, "replies", 0) if msg.replies else 0,
        retweet_count=getattr(msg, "forwards", 0) or 0,
        posted_at=posted_at,
        parent_post_id=parent_post_id,
    )


def _is_video_msg(msg) -> bool:
    """Return True if the Telethon message contains a video document."""
    try:
        from telethon.tl.types import MessageMediaDocument
        media = getattr(msg, "media", None)
        if not isinstance(media, MessageMediaDocument):
            return False
        doc = getattr(media, "document", None)
        attrs = getattr(doc, "attributes", []) if doc else []
        return any(type(a).__name__ == "DocumentAttributeVideo" for a in attrs)
    except Exception:
        return False


class TelegramService:
    """
    Synchronous wrapper around Telethon for post ingestion.
    Uses a persistent session file so the OTP prompt only happens once.

    Parameters
    ----------
    whisper : WhisperService, optional
        When provided, video posts without text captions are downloaded,
        transcribed with faster-whisper, and stored as regular text posts.
    """

    def __init__(self, whisper=None) -> None:
        self._available = bool(TELEGRAM_API_ID and TELEGRAM_API_HASH)
        self._client = None
        self._whisper = whisper          # Optional[WhisperService]
        if not self._available:
            log.warning(
                "telegram.no_credentials",
                note="Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env",
            )

    # ── Public interface ───────────────────────────────────────────────────────

    def search_messages(
        self,
        query: str,
        channels: Optional[list[str]] = None,
        max_results: int = 50,
    ) -> list[Post]:
        """
        Search for messages matching query across the given channels.
        If channels is None, searches the default misinformation channel list.
        """
        if not self._available:
            return []
        targets = channels or DEFAULT_MISINFO_CHANNELS
        return asyncio.run(self._async_search(query, targets, max_results))

    def get_channel_messages(
        self,
        channel: str,
        max_results: int = 100,
    ) -> list[Post]:
        """Fetch the most recent posts from a public channel or group."""
        if not self._available:
            return []
        return asyncio.run(self._async_get_channel(channel, max_results))

    def get_channel_today(
        self,
        channel: str,
        date: Optional[datetime] = None,
        days_back: int = 1,
    ) -> list[Post]:
        """
        Fetch ALL posts from a channel within the last `days_back` days.
        Default days_back=1 means today (UTC) only.
        If the channel has 0 posts today, automatically expands up to 3 days.
        """
        if not self._available:
            return []
        target = date or datetime.now(tz=timezone.utc)
        posts = asyncio.run(self._async_get_channel_range(channel, target, days_back))
        # Auto-expand if nothing found
        if not posts and days_back == 1:
            log.info("telegram.auto_expand_range", channel=channel,
                     reason="0 posts today, expanding to 7 days")
            posts = asyncio.run(self._async_get_channel_range(channel, target, 7))
        return posts

    def search_multi_keywords(
        self,
        keywords: list[str],
        channels: Optional[list[str]] = None,
        max_per_query: int = 20,
    ) -> list[Post]:
        """
        Search multiple keywords across channels and deduplicate results.
        """
        if not self._available:
            return []
        targets = channels or DEFAULT_MISINFO_CHANNELS
        return asyncio.run(
            self._async_multi_search(keywords, targets, max_per_query)
        )

    def load_from_jsonl(self, filepath: str) -> list[Post]:
        """Load pre-collected posts from a JSONL file (offline / replay mode)."""
        import json
        posts: list[Post] = []
        path = Path(filepath)
        if not path.exists():
            log.warning("telegram.jsonl_not_found", path=filepath)
            return []
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    posts.append(Post(
                        id=str(data["id"]),
                        account_id=str(data.get("author_id", "unknown")),
                        text=data.get("text", ""),
                        lang=data.get("lang", ""),
                        retweet_count=data.get("public_metrics", {}).get("retweet_count", 0),
                        like_count=data.get("public_metrics", {}).get("like_count", 0),
                        reply_count=data.get("public_metrics", {}).get("reply_count", 0),
                    ))
                except Exception as exc:
                    log.warning("telegram.jsonl_parse_error", error=str(exc))
        log.info("telegram.loaded_jsonl", count=len(posts), path=filepath)
        return posts

    # ── Async internals ────────────────────────────────────────────────────────

    async def _get_client(self):
        """Return a connected TelegramClient, creating session on first run."""
        from telethon import TelegramClient as _TC
        if self._client is None:
            session_path = TELEGRAM_SESSION_PATH
            Path(session_path).parent.mkdir(parents=True, exist_ok=True)
            self._client = _TC(
                session_path,
                int(TELEGRAM_API_ID),
                TELEGRAM_API_HASH,
            )
        if not self._client.is_connected():
            await self._client.start()
        return self._client

    async def _async_search(
        self,
        query: str,
        channels: list[str],
        max_results: int,
    ) -> list[Post]:
        """
        Search for query across the given channels.
        Automatically joins public channels if not already a member
        so that iter_messages can search their content.
        """
        from telethon.errors import ChannelPrivateError, UsernameNotOccupiedError
        from telethon.tl.functions.channels import JoinChannelRequest

        client = await self._get_client()
        posts: list[Post] = []

        # If no channels specified, search across all joined dialogs
        if not channels:
            try:
                async for dialog in client.iter_dialogs():
                    if not dialog.is_channel:
                        continue
                    try:
                        async for msg in client.iter_messages(
                            dialog.entity, search=query, limit=max(5, max_results // 20)
                        ):
                            post = _tg_message_to_post(msg, dialog.name or str(dialog.id))
                            if post:
                                posts.append(post)
                        if len(posts) >= max_results:
                            break
                    except Exception:
                        continue
            except Exception as exc:
                log.warning("telegram.dialog_search_error", error=str(exc))
            log.info("telegram.search_complete",
                     query=query, mode="all_dialogs", count=len(posts))
            return posts

        per_channel = max(1, max_results // len(channels))

        for channel in channels:
            try:
                entity = await client.get_entity(channel)
                # Auto-join if it's a public channel and we haven't joined yet
                try:
                    await client(JoinChannelRequest(entity))
                except Exception:
                    pass  # already joined or not a joinable channel

                async for msg in client.iter_messages(
                    entity, search=query, limit=per_channel,
                ):
                    post = _tg_message_to_post(msg, channel)
                    if post:
                        posts.append(post)
            except (ChannelPrivateError, UsernameNotOccupiedError) as exc:
                log.warning("telegram.channel_not_found",
                            channel=channel, error=str(exc))
            except Exception as exc:
                log.warning("telegram.search_error",
                            channel=channel, query=query, error=str(exc))

        log.info("telegram.search_complete",
                 query=query, channels=channels, count=len(posts))
        return posts

    async def _async_get_channel_range(
        self,
        channel: str,
        end_date: datetime,
        days_back: int,
    ) -> list[Post]:
        """Fetch posts from `channel` within the last `days_back` days ending at end_date."""
        from telethon.errors import ChannelPrivateError, UsernameNotOccupiedError
        from telethon.tl.functions.channels import JoinChannelRequest
        from datetime import timedelta

        client = await self._get_client()
        posts: list[Post] = []

        end_dt   = end_date.replace(tzinfo=timezone.utc)
        start_dt = (end_dt - timedelta(days=days_back)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        try:
            entity = await client.get_entity(channel)
            try:
                await client(JoinChannelRequest(entity))
            except Exception:
                pass

            async for msg in client.iter_messages(entity, offset_date=end_dt, limit=None):
                if msg.date is None:
                    continue
                msg_dt = msg.date.replace(tzinfo=timezone.utc)
                if msg_dt < start_dt:
                    break
                post = _tg_message_to_post(msg, channel)
                # If no text could be extracted but it's a video, try Whisper
                if post is None and _is_video_msg(msg) and self._whisper and self._whisper.available:
                    post = await self._async_transcribe_video_post(client, msg, channel)
                if post:
                    posts.append(post)

        except (ChannelPrivateError, UsernameNotOccupiedError) as exc:
            log.error("telegram.channel_not_found", channel=channel, error=str(exc))
        except Exception as exc:
            log.error("telegram.channel_range_error", channel=channel, error=str(exc))

        log.info("telegram.channel_range_fetched",
                 channel=channel,
                 start=start_dt.strftime("%Y-%m-%d"),
                 end=end_dt.strftime("%Y-%m-%d"),
                 count=len(posts))
        return posts

    async def _async_get_channel(
        self,
        channel: str,
        max_results: int,
    ) -> list[Post]:
        client = await self._get_client()
        posts: list[Post] = []
        try:
            entity = await client.get_entity(channel)
            async for msg in client.iter_messages(entity, limit=max_results):
                post = _tg_message_to_post(msg, channel)
                if post:
                    posts.append(post)
        except Exception as exc:
            log.error("telegram.channel_error", channel=channel, error=str(exc))
        log.info("telegram.channel_fetched", channel=channel, count=len(posts))
        return posts

    async def _async_multi_search(
        self,
        keywords: list[str],
        channels: list[str],
        max_per_query: int,
    ) -> list[Post]:
        seen_ids: set[str] = set()
        all_posts: list[Post] = []
        for kw in keywords:
            posts = await self._async_search(kw, channels, max_per_query)
            for post in posts:
                if post.id not in seen_ids:
                    seen_ids.add(post.id)
                    all_posts.append(post)
        log.info("telegram.multi_search_complete",
                 keywords=keywords, total=len(all_posts))
        return all_posts

    async def _async_transcribe_video_post(
        self,
        client,
        msg,
        channel_name: str,
    ) -> Optional[Post]:
        """
        Download a video message to a temp file, transcribe with Whisper,
        and return a Post with the transcript as its text.

        Returns None if:
          - The file is larger than WHISPER_MAX_VIDEO_MB
          - Whisper is unavailable or transcription yields empty text
          - Any download / IO error occurs
        """
        import asyncio as _asyncio

        tmp_path: Optional[str] = None
        try:
            # ── Size guard ─────────────────────────────────────────────────
            media = getattr(msg, "media", None)
            doc = getattr(media, "document", None)
            if doc:
                size_mb = getattr(doc, "size", 0) / (1024 * 1024)
                if size_mb > WHISPER_MAX_VIDEO_MB:
                    log.info(
                        "whisper.video_too_large",
                        msg_id=msg.id,
                        size_mb=round(size_mb, 1),
                        limit_mb=WHISPER_MAX_VIDEO_MB,
                    )
                    return None

            # ── Download ───────────────────────────────────────────────────
            tmp_path = self._whisper.make_temp_path(suffix=".mp4")
            log.info("whisper.downloading_video", msg_id=msg.id,
                     channel=_safe_log(channel_name))

            # download_media is sync in older Telethon versions; run in thread
            downloaded = await _asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.loop.run_until_complete(
                    client.download_media(msg, file=tmp_path)
                ) if not client.loop.is_running() else None
            )
            # Fallback: call directly (works when already inside running loop)
            if downloaded is None:
                downloaded = await client.download_media(msg, file=tmp_path)

            if not downloaded or not Path(tmp_path).exists():
                log.warning("whisper.download_failed", msg_id=msg.id)
                return None

            # ── Transcribe ─────────────────────────────────────────────────
            transcript = await _asyncio.get_event_loop().run_in_executor(
                None, lambda: self._whisper.transcribe(tmp_path)
            )
            if not transcript:
                return None

            # ── Build Post ─────────────────────────────────────────────────
            sender = getattr(msg, "sender_id", None)
            account_id = str(sender) if sender else f"{channel_name}_unknown"
            posted_at: Optional[datetime] = None
            if msg.date:
                posted_at = (
                    msg.date.replace(tzinfo=timezone.utc)
                    if msg.date.tzinfo is None else msg.date
                )

            caption = (getattr(msg, "message", "") or "").strip()
            text = f"[VIDEO TRANSCRIPT] {transcript}"
            if caption:
                text = f"[VIDEO] {caption}\n\n[TRANSCRIPT] {transcript}"

            log.info(
                "whisper.post_created",
                msg_id=msg.id,
                channel=_safe_log(channel_name),
                transcript_chars=len(transcript),
            )
            parent_post_id: Optional[str] = None
            reply_to = getattr(msg, "reply_to", None)
            parent_msg_id = (
                getattr(reply_to, "reply_to_msg_id", None) if reply_to else None
            )
            if parent_msg_id:
                parent_post_id = f"tg_{channel_name}_{parent_msg_id}"

            return Post(
                id=f"tg_{channel_name}_{msg.id}",
                account_id=account_id,
                channel_name=channel_name,
                text=text,
                lang="",
                like_count=(
                    sum(r.count for r in msg.reactions.results)
                    if getattr(msg, "reactions", None) else 0
                ),
                reply_count=getattr(msg.replies, "replies", 0) if msg.replies else 0,
                retweet_count=getattr(msg, "forwards", 0) or 0,
                posted_at=posted_at,
                parent_post_id=parent_post_id,
            )

        except Exception as exc:
            log.error("whisper.video_post_error",
                      msg_id=getattr(msg, "id", "?"), error=_safe_log(exc))
            return None
        finally:
            if tmp_path:
                self._whisper.cleanup(tmp_path)

    async def _disconnect(self) -> None:
        if self._client and self._client.is_connected():
            await self._client.disconnect()
            self._client = None

    def disconnect(self) -> None:
        """Cleanly disconnect the Telegram session."""
        asyncio.run(self._disconnect())
