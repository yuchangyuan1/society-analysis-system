"""
Reddit data collection service — public JSON API (no credentials required).

Reddit exposes a public JSON API for all public subreddits:
  https://www.reddit.com/r/{sub}/hot.json
  https://www.reddit.com/r/{sub}/search.json?q=...

No API key or registration needed.  Rate limit: ~1 req/sec with a
descriptive User-Agent.  PRAW is NOT required.

Fetch modes
-----------
1. get_subreddit_posts(subreddit, sort, days_back)
       Posts + top-level comments from one subreddit.
2. search_posts(query, subreddits, days_back)
       Full-text search, optionally scoped to a list of subreddits.
3. get_multi_subreddit(subreddits, sort, days_back)
       Aggregate from multiple subreddits.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urlencode

import httpx
import structlog

from config import REDDIT_DEFAULT_SUBREDDITS, REDDIT_PROXY
from models.post import Post, ImageAsset

log = structlog.get_logger(__name__)

_safe = lambda s: str(s).encode("ascii", "replace").decode("ascii")

_BASE = "https://www.reddit.com"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}
_TIMEOUT        = 15    # seconds per request
_REQ_DELAY      = 1.2   # seconds between requests (polite; stays under 60/min)
_MAX_COMMENTS   = 100   # max comments fetched per post (across all depths)
_MAX_DEPTH      = 3     # cap comment-tree depth (3 = post -> comment -> reply -> reply)
_MIN_SCORE      = 1     # ignore comments below this score


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ts(utc: float) -> datetime:
    return datetime.fromtimestamp(utc, tz=timezone.utc)


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _extract_image_url(data: dict) -> Optional[str]:
    """
    Try to extract a direct image URL from a Reddit post's API data.
    Returns None if the post has no image.
    """
    import os

    # Direct image link (post_hint == "image" or url ending in image ext)
    url = data.get("url", "") or ""
    hint = data.get("post_hint", "") or ""
    if hint == "image" or os.path.splitext(url.split("?")[0])[-1].lower() in _IMAGE_EXTS:
        return url or None

    # Gallery posts — take the first image
    media_metadata = data.get("media_metadata") or {}
    if media_metadata:
        for item in media_metadata.values():
            s = item.get("s") or {}
            u = s.get("u") or s.get("gif") or ""
            if u:
                return u.replace("&amp;", "&")

    # Preview image (thumbnail-quality fallback)
    preview = data.get("preview") or {}
    images = preview.get("images") or []
    if images:
        source = images[0].get("source") or {}
        u = source.get("url", "")
        if u:
            return u.replace("&amp;", "&")

    return None


def _parse_post(data: dict, subreddit: str) -> Optional[Post]:
    """Convert a Reddit listing child's 'data' dict to a Post."""
    import uuid

    title   = data.get("title", "") or ""
    body    = data.get("selftext", "") or ""
    if body in ("[removed]", "[deleted]"):
        body = ""
    if not (title.strip() or body.strip()):
        return None

    text = f"{title}\n\n{body}".strip() if body.strip() else title.strip()

    # Link posts — prepend domain tag
    is_self = data.get("is_self", True)
    url     = data.get("url", "") or ""
    if not is_self and url:
        try:
            from urllib.parse import urlparse
            domain = urlparse(url).netloc.lstrip("www.") or url[:40]
        except Exception:
            domain = url[:40]
        text = f"[{domain}] {text}"

    author = data.get("author") or "deleted"
    sub    = data.get("subreddit") or subreddit
    post_id = f"reddit_{data['id']}"

    # Extract image if present
    images = []
    image_url = _extract_image_url(data)
    if image_url:
        images.append(ImageAsset(
            id=f"img_{data['id']}",
            post_id=post_id,
            url=image_url,
        ))

    return Post(
        id           = post_id,
        account_id   = author,
        channel_name = f"r/{sub}",
        text         = text,
        lang         = "en",
        like_count   = max(0, data.get("score", 0)),
        reply_count  = data.get("num_comments", 0),
        retweet_count= data.get("num_crossposts", 0),
        posted_at    = _ts(float(data.get("created_utc", 0))),
        images       = images,
    )


def _parse_comment(
    data: dict, subreddit: str, parent_post_id: Optional[str] = None,
) -> Optional[Post]:
    """Parse a Reddit comment into a Post.

    `parent_post_id` is the id of the immediate parent (either the
    submission or another comment). Setting it lets the v2 ingestion
    write a Kuzu Replied edge so propagation queries have data.
    """
    body = data.get("body", "") or ""
    if body in ("[removed]", "[deleted]", ""):
        return None
    author = data.get("author") or "deleted"
    sub    = data.get("subreddit") or subreddit
    return Post(
        id           = f"reddit_c_{data['id']}",
        account_id   = author,
        channel_name = f"r/{sub}",
        text         = body,
        lang         = "en",
        like_count   = max(0, data.get("score", 0)),
        reply_count  = 0,
        retweet_count= 0,
        posted_at    = _ts(float(data.get("created_utc", 0))),
        parent_post_id=parent_post_id,
    )


class RedditService:
    """
    Read-only Reddit data collection via public JSON API.
    No credentials or PRAW required.
    """

    def __init__(self) -> None:
        transport = (
            httpx.HTTPTransport(proxy=REDDIT_PROXY)
            if REDDIT_PROXY else None
        )
        self._http = httpx.Client(
            headers=_HEADERS,
            timeout=_TIMEOUT,
            follow_redirects=True,
            **({"transport": transport} if transport else {}),
        )
        self._available = True
        log.info("reddit.client_ready",
                 proxy=REDDIT_PROXY or "none")

    # ── Public interface ───────────────────────────────────────────────────────

    def get_subreddit_posts(
        self,
        subreddit: str,
        sort: str = "hot",
        limit: int = 100,
        days_back: int = 7,
        include_comments: bool = True,
    ) -> list[Post]:
        """Fetch posts (+ top comments) from one subreddit."""
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days_back)
        posts: list[Post] = []
        seen: set[str]    = set()
        after: str        = ""
        fetched           = 0

        while fetched < limit:
            batch = min(100, limit - fetched)
            params: dict = {"limit": batch, "raw_json": 1}
            if after:
                params["after"] = after

            url  = f"{_BASE}/r/{subreddit}/{sort}.json?{urlencode(params)}"
            data = self._get_json(url)
            if not data:
                break

            children = data.get("data", {}).get("children", [])
            if not children:
                break

            for child in children:
                d = child.get("data", {})
                posted_at = _ts(float(d.get("created_utc", 0)))
                if posted_at < cutoff:
                    if sort == "new":
                        # new is chronological → safe to stop early
                        return posts
                    continue

                post = _parse_post(d, subreddit)
                if post and post.id not in seen:
                    seen.add(post.id)
                    posts.append(post)
                    fetched += 1

                    if include_comments:
                        for cp in self._fetch_comments(d["id"], subreddit):
                            if cp.id not in seen:
                                seen.add(cp.id)
                                posts.append(cp)

            after = data.get("data", {}).get("after") or ""
            if not after:
                break
            time.sleep(_REQ_DELAY)

        log.info("reddit.subreddit_fetched",
                 subreddit=subreddit, sort=sort,
                 days_back=days_back, count=len(posts))
        return posts

    def search_posts(
        self,
        query: str,
        subreddits: Optional[list[str]] = None,
        sort: str = "relevance",
        limit: int = 100,
        days_back: int = 7,
        include_comments: bool = True,
    ) -> list[Post]:
        """Full-text Reddit search, optionally within specific subreddits."""
        cutoff    = datetime.now(tz=timezone.utc) - timedelta(days=days_back)
        t_filter  = "week" if days_back <= 7 else "month"
        target    = "+".join(subreddits) if subreddits else "all"
        posts: list[Post] = []
        seen: set[str]    = set()
        after: str        = ""
        fetched           = 0

        while fetched < limit:
            batch = min(100, limit - fetched)
            params = {
                "q": query, "sort": sort, "t": t_filter,
                "limit": batch, "raw_json": 1,
            }
            if after:
                params["after"] = after

            url  = f"{_BASE}/r/{target}/search.json?{urlencode(params)}"
            data = self._get_json(url)
            if not data:
                break

            children = data.get("data", {}).get("children", [])
            if not children:
                break

            for child in children:
                d = child.get("data", {})
                posted_at = _ts(float(d.get("created_utc", 0)))
                if posted_at < cutoff:
                    continue
                sub  = d.get("subreddit", target)
                post = _parse_post(d, sub)
                if post and post.id not in seen:
                    seen.add(post.id)
                    posts.append(post)
                    fetched += 1

                    if include_comments:
                        for cp in self._fetch_comments(d["id"], sub):
                            if cp.id not in seen:
                                seen.add(cp.id)
                                posts.append(cp)

            after = data.get("data", {}).get("after") or ""
            if not after:
                break
            time.sleep(_REQ_DELAY)

        log.info("reddit.search_fetched",
                 query=query[:60], subreddits=target[:60],
                 days_back=days_back, count=len(posts))
        return posts

    def get_multi_subreddit(
        self,
        subreddits: Optional[list[str]] = None,
        sort: str = "hot",
        limit_per_sub: int = 50,
        days_back: int = 7,
    ) -> list[Post]:
        """Aggregate posts from multiple subreddits."""
        targets   = subreddits or REDDIT_DEFAULT_SUBREDDITS
        all_posts: list[Post] = []
        seen: set[str]        = set()

        for sub in targets:
            for post in self.get_subreddit_posts(
                sub, sort=sort, limit=limit_per_sub, days_back=days_back
            ):
                if post.id not in seen:
                    seen.add(post.id)
                    all_posts.append(post)
            time.sleep(_REQ_DELAY)

        log.info("reddit.multi_fetched",
                 subreddits=targets, total=len(all_posts))
        return all_posts

    def close(self) -> None:
        self._http.close()

    # ── Internal ───────────────────────────────────────────────────────────────

    def _get_json(self, url: str) -> Optional[dict]:
        """GET a Reddit JSON URL; handle rate limiting with backoff."""
        for attempt in range(3):
            try:
                resp = self._http.get(url)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 429:
                    wait = 10 * (attempt + 1)
                    log.warning("reddit.rate_limited",
                                attempt=attempt + 1, wait=wait)
                    time.sleep(wait)
                    continue
                log.warning("reddit.http_error",
                            status=resp.status_code, url=_safe(url[:80]))
                return None
            except Exception as exc:
                log.error("reddit.request_error",
                          url=_safe(url[:80]), error=_safe(exc))
                if attempt < 2:
                    time.sleep(3)
        return None

    def _fetch_comments(
        self, post_id: str, subreddit: str,
    ) -> list[Post]:
        """Fetch the comment tree for a post (BFS, depth-limited).

        Each returned Post has `parent_post_id` set to the immediate parent
        (either the submission post or another comment). Combined with
        `agents/ingestion._store_post`'s Kuzu Replied edge writer, this
        produces a real reply graph for KG propagation queries.

        Caps:
          - Total comments per post: `_MAX_COMMENTS` (default 100)
          - Max depth: `_MAX_DEPTH` (default 3)
          - Min score: `_MIN_SCORE` (default 1)
        """
        url = (f"{_BASE}/r/{subreddit}/comments/{post_id}.json"
               f"?limit={_MAX_COMMENTS}&depth={_MAX_DEPTH}&raw_json=1")
        data = self._get_json(url)
        if not data or not isinstance(data, list) or len(data) < 2:
            return []
        time.sleep(_REQ_DELAY)

        # The submission's stable Reddit post_id is "reddit_<post_id>" in
        # our system (see _parse_post). Comments link back to it via t3_*.
        submission_post_id = f"reddit_{post_id}"

        out: list[Post] = []
        children = data[1].get("data", {}).get("children", [])

        def _walk(node: dict, parent_id: str, depth: int) -> None:
            if len(out) >= _MAX_COMMENTS:
                return
            if depth > _MAX_DEPTH:
                return
            if node.get("kind") != "t1":
                return
            d = node.get("data", {})
            if d.get("score", 0) < _MIN_SCORE:
                return
            cp = _parse_comment(d, subreddit, parent_post_id=parent_id)
            if cp is None:
                return
            out.append(cp)
            replies = (d.get("replies") or {})
            if isinstance(replies, dict):
                for sub in (replies.get("data", {}).get("children") or []):
                    _walk(sub, parent_id=cp.id, depth=depth + 1)

        for child in children:
            _walk(child, parent_id=submission_post_id, depth=1)
            if len(out) >= _MAX_COMMENTS:
                break
        return out
