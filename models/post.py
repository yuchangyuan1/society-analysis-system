from __future__ import annotations
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class ImageAsset(BaseModel):
    id: str
    post_id: str
    url: Optional[str] = None
    local_path: Optional[str] = None
    ocr_text: Optional[str] = None
    image_caption: Optional[str] = None
    image_type: Optional[str] = None   # screenshot | chart | meme | photo
    embedding_id: Optional[str] = None
    candidate_claims: list[str] = Field(default_factory=list)


class Post(BaseModel):
    """A normalized social-media post.

    Reddit-only convention (the project's only supported source today):
    `account_id` is the Reddit username verbatim, and `posts_v2.author`
    stores the same string. The two columns are intentionally identical
    on Reddit because the username is itself a stable identifier; we do
    not crawl Reddit's internal `t2_xxx` user IDs.

    If/when the project gains a second platform that distinguishes
    display name from handle (e.g. Twitter/X), introduce an `accounts_v2`
    table and let `account_id` become a synthetic key — `author` will
    then carry the display handle.
    """

    id: str
    account_id: str
    channel_name: str = ""     # human-readable source label, e.g. r/worldnews
    text: str
    lang: str = "en"
    retweet_count: int = 0
    like_count: int = 0
    reply_count: int = 0
    posted_at: Optional[datetime] = None
    source: str = ""           # reddit
    subreddit: Optional[str] = None
    images: list[ImageAsset] = Field(default_factory=list)
    # Phase 0: Emotional State
    emotion: str = ""          # fear | anger | hope | disgust | neutral
    emotion_score: float = 0.0 # 0.0-1.0 intensity
    # redesign-2026-05 Phase 1.4: post-level entities (replaces v1 NamedEntity-on-claim)
    entities: list = Field(default_factory=list)  # list[EntitySpan]
    # redesign-2026-05 Phase 2: topic_id assigned by post-level clustering
    topic_id: Optional[str] = None
    # redesign-2026-05 Phase 2.8: 64-bit simhash for near-duplicate detection
    simhash: Optional[int] = None
    # redesign-2026-05-kg Phase A: Reddit comment/reply chains.
    # When set, ingestion writes a Kuzu (this Post) -[:Replied]-> (parent Post)
    # edge so propagation queries actually have multi-hop data.
    parent_post_id: Optional[str] = None

    @property
    def has_image(self) -> bool:
        return len(self.images) > 0

    def merged_text(self) -> str:
        """Combine post text with OCR and captions from all images."""
        parts = [self.text]
        for img in self.images:
            if img.ocr_text:
                parts.append(f"[OCR] {img.ocr_text}")
            if img.image_caption:
                parts.append(f"[Caption] {img.image_caption}")
        return " ".join(parts)
