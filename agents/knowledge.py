"""
KnowledgeAgent (slim v2) - redesign-2026-05 Phase 5.5.

What's left after the v1 cleanup is just the per-post emotion baseline,
which the v2 PrecomputePipeline still calls in `emotion_baseline`. The
former responsibilities (claim extraction / clustering / persuasion /
entity extraction) moved to dedicated v2 modules:

    claim extraction      -> deleted (v2 uses RAG + NL2SQL instead)
    topic clustering      -> agents/topic_clusterer.py
    entity extraction     -> agents/entity_extractor.py
    persuasion / risk     -> deleted

The class name is kept so existing call sites keep working.
"""
from __future__ import annotations

import json
from typing import Optional

import openai
import structlog

from config import OPENAI_API_KEY, OPENAI_MODEL
from models.post import Post

log = structlog.get_logger(__name__)


_EMOTION_SYSTEM = """You classify the primary emotion of a single social-media post.
Output STRICT JSON:
  {"emotion": one of [fear, anger, hope, disgust, neutral],
   "score": float in [0, 1] indicating intensity}

Rules:
- Choose the dominant emotion expressed by the AUTHOR, not the topic.
- "neutral" when the post is informational with no strong emotion.
- score=0.5 default; raise toward 1.0 when the language is clearly intense.
"""


class KnowledgeAgent:
    """Slim v2 KnowledgeAgent: emotion-only."""

    def __init__(
        self,
        client: Optional[openai.OpenAI] = None,
        model: str = OPENAI_MODEL,
    ) -> None:
        self._claude = client or openai.OpenAI(api_key=OPENAI_API_KEY)
        self._model = model

    # ── Public ─────────────────────────────────────────────────────────────────

    def classify_post_emotions(self, posts: list) -> None:
        """Set `post.emotion` and `post.emotion_score` in place.

        Skips posts already classified. Caps to 50 posts per call to bound
        OpenAI cost.
        """
        for post in posts[:50]:
            if not isinstance(post, Post) or post.emotion:
                continue
            try:
                resp = self._claude.chat.completions.create(
                    model=self._model,
                    max_tokens=64,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": _EMOTION_SYSTEM},
                        {"role": "user", "content": (post.text or "")[:800]},
                    ],
                )
                raw = (resp.choices[0].message.content or "{}").strip()
                data = json.loads(raw) if raw else {}
                emotion = data.get("emotion", "neutral")
                if emotion not in ("fear", "anger", "hope", "disgust", "neutral"):
                    emotion = "neutral"
                post.emotion = emotion
                post.emotion_score = min(
                    1.0, max(0.0, float(data.get("score", 0.5))),
                )
            except Exception as exc:
                log.warning("knowledge.emotion_error",
                            post_id=post.id, error=str(exc)[:80])
                post.emotion = "neutral"
                post.emotion_score = 0.0
