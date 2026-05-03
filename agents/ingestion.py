"""
Reddit-only ingestion helpers.

The project social data source is Reddit. This agent fetches Reddit posts or
loads JSONL fixtures, mirrors posts into Kuzu for graph queries, and leaves
Postgres persistence to PrecomputePipelineV2.
"""
from __future__ import annotations

from typing import Optional

import structlog

from models.post import ImageAsset, Post
from services.claude_vision_service import ClaudeVisionService
from services.kuzu_service import KuzuService
from services.postgres_service import PostgresService
from services.reddit_service import RedditService

log = structlog.get_logger(__name__)


class IngestionAgent:
    def __init__(
        self,
        pg: PostgresService,
        kuzu: KuzuService,
        vision: ClaudeVisionService,
        reddit: Optional[RedditService] = None,
    ) -> None:
        self._pg = pg
        self._kuzu = kuzu
        self._vision = vision
        self._reddit = reddit

    def ingest_subreddit(
        self,
        subreddit: str,
        sort: str = "hot",
        days_back: int = 7,
        limit: int = 100,
        include_comments: bool = True,
        comment_limit: int = 100,
    ) -> list[Post]:
        """Fetch posts plus comments from one subreddit."""
        if not (self._reddit and self._reddit._available):
            log.error("ingestion.reddit_not_configured")
            return []
        posts = self._reddit.get_subreddit_posts(
            subreddit,
            sort=sort,
            limit=limit,
            days_back=days_back,
            include_comments=include_comments,
            comment_limit=comment_limit,
        )
        log.info("ingestion.reddit_subreddit",
                 subreddit=subreddit, count=len(posts))
        for post in posts:
            self._store_post(post)
        return posts

    def ingest_reddit_search(
        self,
        query: str,
        subreddits: Optional[list[str]] = None,
        days_back: int = 7,
        limit: int = 100,
    ) -> list[Post]:
        """Search Reddit for `query`, optionally scoped to subreddits."""
        if not (self._reddit and self._reddit._available):
            log.error("ingestion.reddit_not_configured")
            return []
        posts = self._reddit.search_posts(
            query, subreddits=subreddits, days_back=days_back, limit=limit,
        )
        log.info("ingestion.reddit_search", query=query[:60], count=len(posts))
        for post in posts:
            self._store_post(post)
        return posts

    def ingest_multi_subreddit(
        self,
        subreddits: Optional[list[str]] = None,
        sort: str = "hot",
        days_back: int = 7,
        limit_per_sub: int = 50,
        include_comments: bool = True,
        comment_limit: int = 100,
    ) -> list[Post]:
        """Aggregate posts from multiple subreddits."""
        if not (self._reddit and self._reddit._available):
            log.error("ingestion.reddit_not_configured")
            return []
        posts = self._reddit.get_multi_subreddit(
            subreddits=subreddits,
            sort=sort,
            limit_per_sub=limit_per_sub,
            days_back=days_back,
            include_comments=include_comments,
            comment_limit=comment_limit,
        )
        log.info("ingestion.reddit_multi",
                 subreddits=subreddits or "defaults", count=len(posts))
        for post in posts:
            self._store_post(post)
        return posts

    def ingest_posts_from_jsonl(self, filepath: str) -> list[Post]:
        """Load pre-collected posts from a JSONL fixture."""
        import json
        from pathlib import Path

        posts: list[Post] = []
        for line in Path(filepath).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                posts.append(Post.model_validate(json.loads(line)))
            except Exception as exc:
                log.warning("ingestion.jsonl_skip",
                            error=str(exc)[:120], line=line[:80])
        log.info("ingestion.loaded_jsonl", count=len(posts))
        for post in posts:
            self._store_post(post)
        return posts

    def process_image_post(
        self,
        post: Post,
        image_url: Optional[str] = None,
        image_path: Optional[str] = None,
    ) -> Post:
        """Run image OCR/caption enrichment for a post with images."""
        updated_images: list[ImageAsset] = []
        for img in post.images:
            url = image_url or img.url
            path = image_path or img.local_path
            vision_result = self._vision.analyze_image(
                image_url=url, image_path=path, post_text=post.text,
            )
            img.ocr_text = vision_result.get("ocr_text", "")
            img.image_caption = vision_result.get("image_caption", "")
            img.image_type = vision_result.get("image_type", "other")
            img.candidate_claims = vision_result.get("candidate_claims", [])
            if vision_result.get("image_text_unavailable"):
                log.warning(
                    "ingestion.image_text_unavailable",
                    post_id=post.id,
                    image_id=img.id,
                    reason=vision_result.get("error", ""),
                )
            updated_images.append(img)
        post.images = updated_images
        return post

    def _store_post(self, post: Post) -> None:
        try:
            self._kuzu.upsert_account(post.account_id, post.account_id)
            self._kuzu.upsert_post(post.id, post.text)
            self._kuzu.add_posted(post.account_id, post.id)
            if post.parent_post_id:
                self._kuzu.upsert_post(post.parent_post_id, "")
                self._kuzu.add_replied(post.id, post.parent_post_id)
        except Exception as exc:
            log.warning("ingestion.kuzu_skip", post_id=post.id,
                        error=str(exc)[:120])
