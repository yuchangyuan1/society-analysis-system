"""
X (Twitter/X) API v2 service — post ingestion via tweepy.
Supports filtered stream and recent search endpoints.
Falls back gracefully when credentials are missing (dev / offline mode).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterator, Optional

import structlog

from config import (
    X_BEARER_TOKEN, X_API_KEY, X_API_SECRET,
    X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET,
)
from models.post import Post, ImageAsset

log = structlog.get_logger(__name__)


def _make_image_asset(media: dict, post_id: str) -> ImageAsset:
    return ImageAsset(
        id=media.get("media_key", f"{post_id}-img"),
        post_id=post_id,
        url=media.get("url") or media.get("preview_image_url"),
    )


class XApiService:
    """
    Thin wrapper around tweepy v4 for X API v2.
    If credentials are not set, all methods return empty results so the
    rest of the pipeline can still run with pre-collected data.
    """

    def __init__(self) -> None:
        self._available = bool(X_BEARER_TOKEN)
        if self._available:
            try:
                import tweepy
                self._client = tweepy.Client(
                    bearer_token=X_BEARER_TOKEN,
                    consumer_key=X_API_KEY,
                    consumer_secret=X_API_SECRET,
                    access_token=X_ACCESS_TOKEN,
                    access_token_secret=X_ACCESS_TOKEN_SECRET,
                    wait_on_rate_limit=True,
                )
                log.info("x_api.initialized")
            except Exception as exc:
                log.warning("x_api.init_failed", error=str(exc))
                self._available = False
        else:
            log.warning("x_api.no_credentials", note="Using offline mode")

    def search_recent(
        self,
        query: str,
        max_results: int = 50,
        lang: str = "en",
    ) -> list[Post]:
        """Search recent tweets matching query (last 7 days)."""
        if not self._available:
            return []
        try:
            import tweepy
            full_query = f"{query} lang:{lang} -is:retweet"
            response = self._client.search_recent_tweets(
                query=full_query,
                max_results=min(max_results, 100),
                tweet_fields=["created_at", "public_metrics", "attachments"],
                expansions=["attachments.media_keys", "author_id"],
                media_fields=["url", "preview_image_url", "media_key", "type"],
                user_fields=["username", "public_metrics", "verified"],
            )
            return self._parse_response(response)
        except Exception as exc:
            log.error("x_api.search_error", query=query, error=str(exc))
            return []

    def search_multi_keywords(
        self,
        keywords: list[str],
        max_per_query: int = 20,
    ) -> list[Post]:
        """
        Search multiple keyword queries and merge/deduplicate results.
        Returns up to len(keywords) * max_per_query unique posts.
        """
        seen_ids: set[str] = set()
        all_posts: list[Post] = []
        for kw in keywords:
            posts = self.search_recent(kw, max_results=max_per_query)
            for p in posts:
                if p.id not in seen_ids:
                    seen_ids.add(p.id)
                    all_posts.append(p)
        log.info("x_api.multi_search_complete",
                 keywords=keywords, total=len(all_posts))
        return all_posts

    def load_from_jsonl(self, filepath: str) -> list[Post]:
        """
        Load pre-collected posts from a JSONL file (one tweet JSON per line).
        Use this when X API quota is exhausted or for offline development.

        Expected JSON shape (matches X API v2 tweet object):
          {"id": "...", "text": "...", "author_id": "...",
           "public_metrics": {...}, "created_at": "..."}
        """
        posts = []
        path = Path(filepath)
        if not path.exists():
            log.warning("x_api.jsonl_not_found", path=filepath)
            return []
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    post = Post(
                        id=str(data["id"]),
                        account_id=str(data.get("author_id", "unknown")),
                        text=data.get("text", ""),
                        lang=data.get("lang", "en"),
                        retweet_count=data.get("public_metrics", {}).get("retweet_count", 0),
                        like_count=data.get("public_metrics", {}).get("like_count", 0),
                        reply_count=data.get("public_metrics", {}).get("reply_count", 0),
                    )
                    posts.append(post)
                except Exception as exc:
                    log.warning("x_api.jsonl_parse_error", error=str(exc))
        log.info("x_api.loaded_jsonl", count=len(posts), path=filepath)
        return posts

    # ── Internal ───────────────────────────────────────────────────────────────

    def _parse_response(self, response) -> list[Post]:
        posts = []
        if response.data is None:
            return posts
        media_map: dict[str, dict] = {}
        if response.includes and "media" in response.includes:
            for m in response.includes["media"]:
                media_map[m.media_key] = m.data
        user_map: dict[str, dict] = {}
        if response.includes and "users" in response.includes:
            for u in response.includes["users"]:
                user_map[str(u.id)] = u.data

        for tweet in response.data:
            tid = str(tweet.id)
            images: list[ImageAsset] = []
            if tweet.attachments and tweet.attachments.get("media_keys"):
                for mk in tweet.attachments["media_keys"]:
                    if mk in media_map:
                        images.append(_make_image_asset(media_map[mk], tid))
            post = Post(
                id=tid,
                account_id=str(tweet.author_id),
                text=tweet.text,
                retweet_count=tweet.public_metrics.get("retweet_count", 0)
                if tweet.public_metrics else 0,
                like_count=tweet.public_metrics.get("like_count", 0)
                if tweet.public_metrics else 0,
                reply_count=tweet.public_metrics.get("reply_count", 0)
                if tweet.public_metrics else 0,
                posted_at=tweet.created_at,
                images=images,
            )
            posts.append(post)
        return posts
