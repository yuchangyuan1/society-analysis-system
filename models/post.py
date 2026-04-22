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
    id: str
    account_id: str
    channel_name: str = ""     # human-readable channel / username (Telegram: @handle)
    text: str
    lang: str = "en"
    retweet_count: int = 0
    like_count: int = 0
    reply_count: int = 0
    posted_at: Optional[datetime] = None
    images: list[ImageAsset] = Field(default_factory=list)
    # Phase 0: Emotional State (图2 — Emotional State)
    emotion: str = ""          # fear | anger | hope | disgust | neutral
    emotion_score: float = 0.0 # 0.0–1.0 intensity

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
