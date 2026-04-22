"""
Vision service — OCR + image captioning + claim extraction via GPT-4o.
Single API call handles OCR and captioning.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Optional

import openai
import structlog

from config import OPENAI_API_KEY, OPENAI_MODEL

log = structlog.get_logger(__name__)


_VISION_SYSTEM = """You are a multimodal analysis assistant specialized in social media image posts.
Given an image, produce a JSON object with these exact keys:
  ocr_text       : string  — all text visible in the image, verbatim
  image_caption  : string  — a concise natural-language description of the image
  image_type     : string  — one of: screenshot | chart | meme | photo | other
  candidate_claims : list[string] — factual claims that can be verified (max 5)
Return ONLY the JSON object, no markdown fences."""


class ClaudeVisionService:
    def __init__(self) -> None:
        self._client = openai.OpenAI(api_key=OPENAI_API_KEY)
        self._model = OPENAI_MODEL

    def analyze_image(
        self,
        image_path: Optional[str] = None,
        image_url: Optional[str] = None,
        post_text: str = "",
    ) -> dict:
        """
        Analyze an image and return OCR text, caption, type, and candidate claims.
        Pass either image_path (local file) or image_url (public URL).
        """
        if image_path is None and image_url is None:
            return self._empty_result(reason="no image provided")

        try:
            content = self._build_content(image_path=image_path,
                                          image_url=image_url,
                                          post_text=post_text)
        except Exception as exc:
            log.error("vision.build_content_error", error=str(exc))
            return self._empty_result(reason=str(exc))

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                max_tokens=1024,
                messages=[
                    {"role": "system", "content": _VISION_SYSTEM},
                    {"role": "user", "content": content},
                ],
            )
            raw = response.choices[0].message.content or "{}"
            data = json.loads(raw)
            data.setdefault("ocr_text", "")
            data.setdefault("image_caption", "")
            data.setdefault("image_type", "other")
            data.setdefault("candidate_claims", [])
            data["image_text_unavailable"] = False
            return data
        except json.JSONDecodeError as exc:
            log.error("vision.json_parse_error", error=str(exc))
            return self._empty_result(reason="json parse error")
        except Exception as exc:
            log.error("vision.api_error", error=str(exc))
            return self._empty_result(reason=str(exc))

    # ── Internal ───────────────────────────────────────────────────────────────

    def _build_content(
        self,
        image_path: Optional[str],
        image_url: Optional[str],
        post_text: str,
    ) -> list:
        """Build OpenAI vision content blocks."""
        blocks: list = []

        if image_url:
            blocks.append({
                "type": "image_url",
                "image_url": {"url": image_url},
            })
        elif image_path:
            img_bytes = Path(image_path).read_bytes()
            b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
            suffix = Path(image_path).suffix.lower().lstrip(".")
            media_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                         "png": "image/png", "gif": "image/gif",
                         "webp": "image/webp"}
            media_type = media_map.get(suffix, "image/png")
            blocks.append({
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{b64}"},
            })

        user_text = "Analyze this social media image post."
        if post_text:
            user_text += f"\n\nPost text: {post_text}"
        blocks.append({"type": "text", "text": user_text})
        return blocks

    @staticmethod
    def _empty_result(reason: str = "") -> dict:
        return {
            "ocr_text": "",
            "image_caption": "",
            "image_type": "other",
            "candidate_claims": [],
            "image_text_unavailable": True,
            "error": reason,
        }
