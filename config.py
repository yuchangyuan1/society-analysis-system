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
RUNS_DIR: str = os.getenv("RUNS_DIR", str(DATA_DIR / "runs"))

# ── API keys ───────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")  # optional, kept for reference

OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
if not OPENAI_API_KEY:
    raise EnvironmentError(
        "OPENAI_API_KEY is not set.\n"
        "Add OPENAI_API_KEY=sk-... to your .env file."
    )

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

# ── Embedding model ────────────────────────────────────────────────────────────
EMBEDDING_MODEL: str = "text-embedding-3-small"
EMBEDDING_DIM: int = 1536

# ── Chroma collection names ────────────────────────────────────────────────────
# redesign-2026-05 Phase 2: three-collection split (PROJECT_REDESIGN_V2.md 5b)
CHROMA_OFFICIAL_COLLECTION: str = os.getenv(
    "CHROMA_OFFICIAL_COLLECTION", "chroma_official"
)
CHROMA_NL2SQL_COLLECTION: str = os.getenv(
    "CHROMA_NL2SQL_COLLECTION", "chroma_nl2sql"
)
CHROMA_PLANNER_COLLECTION: str = os.getenv(
    "CHROMA_PLANNER_COLLECTION", "chroma_planner"
)

# Conflict-replacement thresholds (PROJECT_REDESIGN_V2.md 7c-H, Q11=B)
# Three-tier policy:
#   < SIM_TIER_LOW          -> append (no conflict check)
#   [SIM_TIER_LOW, SIM_TIER_HIGH) -> direct overwrite (no LLM)
#   >= SIM_TIER_HIGH        -> LLM-arbitrated pairwise comparison
NL2SQL_CONFLICT_SIM_LOW: float = float(os.getenv("NL2SQL_CONFLICT_SIM_LOW", "0.92"))
NL2SQL_CONFLICT_SIM_HIGH: float = float(os.getenv("NL2SQL_CONFLICT_SIM_HIGH", "0.95"))

# redesign-2026-05 Phase 3: NL2SQL safety + repair limits
# Read-only DSN takes precedence over POSTGRES_DSN when set.
POSTGRES_READONLY_DSN: str = os.getenv("POSTGRES_READONLY_DSN", "")
NL2SQL_MAX_REPAIR_ROUNDS: int = int(os.getenv("NL2SQL_MAX_REPAIR_ROUNDS", "3"))
NL2SQL_RESULT_ROW_LIMIT: int = int(os.getenv("NL2SQL_RESULT_ROW_LIMIT", "1000"))
NL2SQL_STATEMENT_TIMEOUT_MS: int = int(os.getenv("NL2SQL_STATEMENT_TIMEOUT_MS", "5000"))

# redesign-2026-05 Phase 5: experience decay
# Records below this confidence are considered stale and decayed away.
EXPERIENCE_MIN_CONFIDENCE: float = float(
    os.getenv("EXPERIENCE_MIN_CONFIDENCE", "0.2")
)
# Records last used more than this many days ago are decayed (skipped for
# kind=schema and kind=module_card which are intentionally permanent).
EXPERIENCE_TTL_DAYS: int = int(os.getenv("EXPERIENCE_TTL_DAYS", "30"))

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

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# ── redesign-2026-05: multimodal sampling + budget ─────────────────────────────
# Daily USD budget for multimodal (image-understanding) calls. The pipeline
# skips remaining calls once the daily budget is consumed.
# Estimate: ~$0.01-0.03 per Claude Vision call.
MULTIMODAL_DAILY_BUDGET_USD: float = float(
    os.getenv("MULTIMODAL_DAILY_BUDGET_USD", "5.0")
)
# Per-call cost estimate for budget accounting (USD).
MULTIMODAL_COST_PER_CALL_USD: float = float(
    os.getenv("MULTIMODAL_COST_PER_CALL_USD", "0.02")
)
# Sampling thresholds: only run multimodal on posts that exceed either.
MULTIMODAL_MIN_LIKES: int = int(os.getenv("MULTIMODAL_MIN_LIKES", "50"))
MULTIMODAL_MIN_REPLIES: int = int(os.getenv("MULTIMODAL_MIN_REPLIES", "20"))
