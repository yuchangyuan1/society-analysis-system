"""
Central configuration — loads from environment / .env file.
All other modules import from here; never import os.environ directly.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Explicitly load .env from the project directory so that the correct
# credentials are used regardless of the current working directory.
load_dotenv(Path(__file__).parent / ".env", override=True)

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"

CHROMA_PERSIST_DIR: str = os.getenv("CHROMA_PERSIST_DIR", str(DATA_DIR / "chroma"))
KUZU_DB_DIR: str = os.getenv("KUZU_DB_DIR", str(DATA_DIR / "kuzu_graph"))
RAW_MEDIA_DIR: str = os.getenv("RAW_MEDIA_DIR", str(DATA_DIR / "raw_media"))
COUNTER_VISUALS_DIR: str = os.getenv("COUNTER_VISUALS_DIR", str(DATA_DIR / "counter_visuals"))
COUNTER_EFFECTS_DB: str = os.getenv("COUNTER_EFFECTS_DB", str(DATA_DIR / "counter_effects.db"))
RUNS_DIR: str = os.getenv("RUNS_DIR", str(DATA_DIR / "runs"))

# ── API keys ───────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")  # optional, kept for reference

OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
if not OPENAI_API_KEY:
    raise EnvironmentError(
        "OPENAI_API_KEY is not set.\n"
        "Add OPENAI_API_KEY=sk-... to your .env file."
    )

X_BEARER_TOKEN: str = os.getenv("X_BEARER_TOKEN", "")
X_API_KEY: str = os.getenv("X_API_KEY", "")
X_API_SECRET: str = os.getenv("X_API_SECRET", "")
X_ACCESS_TOKEN: str = os.getenv("X_ACCESS_TOKEN", "")
X_ACCESS_TOKEN_SECRET: str = os.getenv("X_ACCESS_TOKEN_SECRET", "")

# ── Telegram (MTProto — data collection) ───────────────────────────────────────
TELEGRAM_API_ID: str = os.getenv("TELEGRAM_API_ID", "")
TELEGRAM_API_HASH: str = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_SESSION_PATH: str = os.getenv(
    "TELEGRAM_SESSION_PATH",
    str(BASE_DIR / "data" / "telegram_session"),
)

POSTGRES_DSN: str = os.getenv(
    "POSTGRES_DSN", "postgresql://society:society_pass@localhost:5432/society_db"
)

# ── LLM model ─────────────────────────────────────────────────────────────────
# Using OpenAI GPT-4o as the primary model.
# Switch to "gpt-4o-mini" for lower cost at the expense of quality.
OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o")

# ── Stable Diffusion ───────────────────────────────────────────────────────────
SD_MODEL_ID: str = os.getenv("SD_MODEL_ID", "stabilityai/stable-diffusion-2-1")
SD_DEVICE: str = os.getenv("SD_DEVICE", "cpu")

# ── Embedding model ────────────────────────────────────────────────────────────
EMBEDDING_MODEL: str = "text-embedding-3-small"
EMBEDDING_DIM: int = 1536

# ── Chroma collection names ────────────────────────────────────────────────────
CHROMA_CLAIMS_COLLECTION: str = "claims"
CHROMA_ARTICLES_COLLECTION: str = "articles"

# ── Claim deduplication thresholds ────────────────────────────────────────────
CLAIM_EMBED_SIM_HIGH: float = 0.92   # → candidate SAME; check with LLM
CLAIM_EMBED_SIM_LOW: float = 0.85    # → new claim (skip LLM check)

# ── Critic retry limit ─────────────────────────────────────────────────────────
CRITIC_MAX_RETRIES: int = 2

# ── Visual card dimensions (X post format) ─────────────────────────────────────
VISUAL_WIDTH: int = 1200
VISUAL_HEIGHT: int = 675

# ── Reddit API ────────────────────────────────────────────────────────────────
# No credentials needed — uses Reddit's public JSON API directly.
# REDDIT_PROXY: optional HTTP/HTTPS proxy (e.g. http://127.0.0.1:7890)
# Comma-separated default subreddits used when none is specified on CLI
# HTTP/HTTPS proxy for Reddit (needed in regions where Reddit is blocked)
# Example: http://127.0.0.1:7890  or  socks5://127.0.0.1:1080
REDDIT_PROXY: str = os.getenv("REDDIT_PROXY", "")

REDDIT_DEFAULT_SUBREDDITS: list[str] = [
    s.strip()
    for s in os.getenv(
        "REDDIT_DEFAULT_SUBREDDITS",
        "conspiracy,worldnews,politics,health,news",
    ).split(",")
    if s.strip()
]

# ── Whisper (video transcription) ─────────────────────────────────────────────
# Model sizes: tiny / base / small / medium / large
# "base" (≈500 MB RAM) is recommended for most machines.
WHISPER_MODEL_SIZE: str = os.getenv("WHISPER_MODEL_SIZE", "base")
WHISPER_TEMP_DIR: str = os.getenv("WHISPER_TEMP_DIR", str(DATA_DIR / "whisper_tmp"))
# Skip transcription for videos larger than this (MB) to avoid long waits
WHISPER_MAX_VIDEO_MB: int = int(os.getenv("WHISPER_MAX_VIDEO_MB", "100"))

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
