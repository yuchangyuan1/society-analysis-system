"""
Stable Diffusion service — visual clarification card generation.
Output: PNG image 1200×675 px (X post format).
Text overlay rendered via Pillow to ensure English typography quality.
"""
from __future__ import annotations

import platform
import textwrap
import time
from pathlib import Path
from typing import Optional

import structlog
from PIL import Image, ImageDraw, ImageFont

from config import (
    SD_MODEL_ID, SD_DEVICE,
    COUNTER_VISUALS_DIR,
    VISUAL_WIDTH, VISUAL_HEIGHT,
)

log = structlog.get_logger(__name__)

# Soft-load SD dependencies — they're optional (large); degrade gracefully.
_SD_AVAILABLE = False
try:
    from diffusers import StableDiffusionPipeline
    import torch
    _SD_AVAILABLE = True
except ImportError:
    log.warning("stable_diffusion.not_installed",
                note="pip install diffusers torch transformers accelerate")


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """
    Load a TrueType font at the given size, trying common system locations
    across macOS, Windows, and Linux before falling back to PIL default.
    """
    candidates: list[str] = []
    system = platform.system()
    if system == "Darwin":
        candidates = [
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
        ]
    elif system == "Windows":
        candidates = [
            "C:\\Windows\\Fonts\\arial.ttf",
            "C:\\Windows\\Fonts\\calibri.ttf",
        ]
    else:  # Linux / other
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    # PIL bitmap default (no size parameter)
    return ImageFont.load_default()


class StableDiffusionService:
    def __init__(self) -> None:
        self._pipe = None
        self._output_dir = Path(COUNTER_VISUALS_DIR)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def _load_pipeline(self) -> bool:
        if self._pipe is not None:
            return True
        if not _SD_AVAILABLE:
            return False
        try:
            log.info("stable_diffusion.loading_pipeline", model=SD_MODEL_ID,
                     device=SD_DEVICE)
            self._pipe = StableDiffusionPipeline.from_pretrained(
                SD_MODEL_ID,
                torch_dtype=torch.float16 if SD_DEVICE == "cuda" else torch.float32,
            )
            self._pipe = self._pipe.to(SD_DEVICE)
            if SD_DEVICE == "cuda":
                # Reduce peak VRAM usage; safe on any CUDA device
                self._pipe.enable_attention_slicing()
                # Enable xformers if installed (further ~20% VRAM reduction)
                try:
                    self._pipe.enable_xformers_memory_efficient_attention()
                    log.info("stable_diffusion.xformers_enabled")
                except Exception:
                    pass  # xformers not installed; attention slicing is sufficient
            log.info("stable_diffusion.pipeline_ready")
            return True
        except Exception as exc:
            log.error("stable_diffusion.load_error", error=str(exc))
            return False

    def generate_card(
        self,
        counter_text: str,
        background_prompt: str = "",
        claim_summary: str = "",
        report_id: str = "",
    ) -> Optional[str]:
        """
        Generate a 1200×675 px clarification card.

        1. Generate background via Stable Diffusion (or solid colour fallback).
        2. Overlay counter_text and claim_summary using Pillow.
        3. Save as PNG; return the file path.

        Returns None if generation fails (caller logs visual_card_unavailable).
        """
        timestamp = int(time.time())
        filename = f"card_{report_id or 'unknown'}_{timestamp}.png"
        out_path = self._output_dir / filename

        try:
            bg = self._generate_background(background_prompt)
            img = self._overlay_text(bg, counter_text, claim_summary)
            img.save(str(out_path), "PNG")
            log.info("stable_diffusion.card_saved", path=str(out_path))
            return str(out_path)
        except Exception as exc:
            log.error("stable_diffusion.generate_error", error=str(exc))
            return None

    def generate_background_only(self, prompt: str) -> Image.Image:
        """Public: generate (or fallback) a background image for topic cards."""
        return self._generate_background(prompt)

    # ── Internal ───────────────────────────────────────────────────────────────

    def _generate_background(self, prompt: str) -> Image.Image:
        if self._load_pipeline() and prompt:
            try:
                full_prompt = (
                    f"{prompt}, clean factual infographic, blue and white palette, "
                    "professional design, no text"
                )
                # SD 1.4 works at 512px multiples. Generate 512×512 (CUDA) or
                # 512×512 (CPU) then upscale to target with Pillow.
                gen_w, gen_h = (512, 512)
                result = self._pipe(
                    full_prompt,
                    width=gen_w,
                    height=gen_h,
                    num_inference_steps=20,
                    guidance_scale=7.5,
                )
                img = result.images[0]
                if (gen_w, gen_h) != (VISUAL_WIDTH, VISUAL_HEIGHT):
                    img = img.resize((VISUAL_WIDTH, VISUAL_HEIGHT), Image.LANCZOS)
                return img
            except Exception as exc:
                log.warning("stable_diffusion.inference_failed", error=str(exc))
        # Fallback: a clean gradient background
        return self._make_fallback_bg()

    @staticmethod
    def _make_fallback_bg() -> Image.Image:
        img = Image.new("RGB", (VISUAL_WIDTH, VISUAL_HEIGHT), color=(20, 80, 160))
        draw = ImageDraw.Draw(img)
        # Simple gradient-like bands
        for y in range(VISUAL_HEIGHT):
            r = 20 + int(30 * y / VISUAL_HEIGHT)
            g = 80 + int(60 * y / VISUAL_HEIGHT)
            b = 160 + int(40 * y / VISUAL_HEIGHT)
            draw.line([(0, y), (VISUAL_WIDTH, y)], fill=(r, g, b))
        return img

    @staticmethod
    def _overlay_text(
        bg: Image.Image,
        counter_text: str,
        claim_summary: str,
    ) -> Image.Image:
        img = bg.copy().resize((VISUAL_WIDTH, VISUAL_HEIGHT))
        draw = ImageDraw.Draw(img)

        # Semi-transparent dark overlay for readability
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ov_draw = ImageDraw.Draw(overlay)
        ov_draw.rectangle(
            [40, 40, VISUAL_WIDTH - 40, VISUAL_HEIGHT - 40],
            fill=(0, 0, 0, 140),
        )
        img = img.convert("RGBA")
        img = Image.alpha_composite(img, overlay).convert("RGB")
        draw = ImageDraw.Draw(img)

        # Try system fonts; gracefully fall back to PIL default (cross-platform)
        font_title = _load_font(48)
        font_body = _load_font(28)
        font_small = _load_font(22)

        # "FACT CHECK" header
        draw.text((60, 60), "FACT CHECK", fill=(255, 220, 50), font=font_title)
        draw.line([(60, 120), (VISUAL_WIDTH - 60, 120)], fill=(255, 220, 50), width=2)

        # Claim summary (truncated)
        if claim_summary:
            wrapped_claim = textwrap.fill(f'Claim: "{claim_summary}"', width=70)
            draw.text((60, 140), wrapped_claim, fill=(200, 200, 200), font=font_small)

        # Counter-message text
        wrapped = textwrap.fill(counter_text, width=55)
        draw.text((60, 240), wrapped, fill=(255, 255, 255), font=font_body)

        # Footer
        draw.text(
            (60, VISUAL_HEIGHT - 50),
            "Source-backed analysis | Verify claims before sharing",
            fill=(180, 180, 180),
            font=font_small,
        )
        return img
