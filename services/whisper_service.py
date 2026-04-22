"""
Whisper transcription service — converts video/audio files to text.

Uses faster-whisper (CTranslate2 backend) which is 4× lighter than the
original OpenAI Whisper at equivalent quality.

Install:
    pip install faster-whisper

Optional (needed only if the downloaded media file is a raw video container
and your Python environment cannot find ffmpeg on PATH automatically):
    pip install ffmpeg-python
    # or install the ffmpeg binary: https://ffmpeg.org/download.html

Model sizes and approximate CPU RAM usage:
    tiny    ~400 MB    small   ~1.0 GB    large   ~5 GB
    base    ~500 MB    medium  ~2.5 GB
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

import structlog

from config import WHISPER_MODEL_SIZE, WHISPER_TEMP_DIR

log = structlog.get_logger(__name__)

_safe_log = lambda s: str(s).encode("ascii", "replace").decode("ascii")

# Soft-import check — faster-whisper is optional
_WHISPER_AVAILABLE = False
try:
    from faster_whisper import WhisperModel as _WhisperModel  # noqa: F401
    _WHISPER_AVAILABLE = True
except ImportError:
    log.warning(
        "whisper.not_installed",
        note="pip install faster-whisper",
    )


class WhisperService:
    """
    Lazy-loading wrapper around faster-whisper.

    The model is downloaded on first use (~150 MB for 'base') and cached
    by Hugging Face in ~/.cache/huggingface.  Subsequent starts are instant.
    """

    def __init__(self, model_size: str = WHISPER_MODEL_SIZE) -> None:
        self._model_size = model_size
        self._model = None
        self._tmp_dir = Path(WHISPER_TEMP_DIR)
        self._tmp_dir.mkdir(parents=True, exist_ok=True)

    # ── Public interface ───────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        return _WHISPER_AVAILABLE

    def transcribe(
        self,
        media_path: str,
        language: Optional[str] = None,
    ) -> str:
        """
        Transcribe a video or audio file and return the full transcript as a
        single string.  Returns "" on failure.

        Parameters
        ----------
        media_path : str
            Local path to a video (.mp4, .mkv, …) or audio (.mp3, .wav, …)
            file.  faster-whisper uses ffmpeg internally, so video containers
            are handled transparently when ffmpeg is on PATH.
        language : str, optional
            BCP-47 language code (e.g. "en", "zh").  When None, faster-whisper
            auto-detects the language from the first 30 seconds.
        """
        if not _WHISPER_AVAILABLE:
            log.warning("whisper.not_available", path=media_path)
            return ""

        model = self._load_model()
        if model is None:
            return ""

        try:
            segments, info = model.transcribe(
                media_path,
                language=language,
                beam_size=5,
                vad_filter=True,           # skip silence, speeds up CPU runs
                vad_parameters={"min_silence_duration_ms": 500},
            )
            text = " ".join(seg.text.strip() for seg in segments)
            log.info(
                "whisper.transcribed",
                path=_safe_log(media_path),
                detected_lang=info.language,
                duration_s=round(info.duration, 1),
                chars=len(text),
            )
            return text.strip()
        except Exception as exc:
            log.error("whisper.transcribe_error",
                      path=_safe_log(media_path), error=_safe_log(exc))
            return ""

    def make_temp_path(self, suffix: str = ".mp4") -> str:
        """Return a unique temp file path inside WHISPER_TEMP_DIR."""
        fd, path = tempfile.mkstemp(suffix=suffix, dir=str(self._tmp_dir))
        os.close(fd)
        return path

    @staticmethod
    def cleanup(path: str) -> None:
        """Delete a temp file; silently ignore errors."""
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass

    # ── Internal ───────────────────────────────────────────────────────────────

    def _load_model(self):
        if self._model is not None:
            return self._model
        if not _WHISPER_AVAILABLE:
            return None
        try:
            from faster_whisper import WhisperModel
            log.info("whisper.loading_model", size=self._model_size)
            # int8 quantisation: ~4× less RAM, negligible quality loss on base/small
            self._model = WhisperModel(
                self._model_size,
                device="cpu",
                compute_type="int8",
            )
            log.info("whisper.model_ready", size=self._model_size)
            return self._model
        except Exception as exc:
            log.error("whisper.load_error", error=_safe_log(exc))
            return None
