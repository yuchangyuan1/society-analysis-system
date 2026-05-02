#!/usr/bin/env python3
"""
Society Analysis - v2 main entry point (redesign-2026-05).

Phase 5 retired the v1 pipeline along with its capability + intervention
arms. The CLI now exclusively drives the v2 PrecomputePipeline.

Usage:
    # Reddit ingestion + v2 precompute (5 stages + schema_propose + persist)
    python main.py --subreddit conspiracy --days 3

    # Telegram channel
    python main.py --channel SomeChannel --days 7

    # Reproducible run from a JSONL fixture
    python main.py --jsonl tests/fixtures/sample_posts.jsonl

    # Pull official sources (BBC / NYT / Reuters / AP / Xinhua) once
    python -m agents.official_ingestion_pipeline --once
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

# ── Windows UTF-8 console fix ──────────────────────────────────────────────────
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                    errors="replace")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                                    errors="replace")

import structlog

from agents.entity_extractor import EntityExtractor
from agents.ingestion import IngestionAgent
from agents.knowledge import KnowledgeAgent
from agents.multimodal_agent import MultimodalAgent
from agents.post_dedup import PostDeduper
from agents.precompute_pipeline_v2 import PrecomputePipelineV2
from agents.schema_agent import SchemaAgent
from agents.topic_clusterer import TopicClusterer
from services import ManifestService
from services.claude_vision_service import ClaudeVisionService
from services.kuzu_service import KuzuService
from services.postgres_service import PostgresService
from services.reddit_service import RedditService
from services.schema_sync import SchemaSync
from services.telegram_service import TelegramService

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.BoundLogger,
    logger_factory=structlog.PrintLoggerFactory(),
)
log = structlog.get_logger(__name__)


def _build_ingestion() -> IngestionAgent:
    pg = PostgresService()
    kuzu = KuzuService()
    vision = ClaudeVisionService()
    telegram = None
    try:
        telegram = TelegramService()
    except Exception as exc:
        log.warning("main.telegram_unavailable", error=str(exc)[:120])
    reddit = None
    try:
        reddit = RedditService()
    except Exception as exc:
        log.warning("main.reddit_unavailable", error=str(exc)[:120])
    return IngestionAgent(
        pg=pg, kuzu=kuzu, vision=vision,
        telegram=telegram, x_api=None, reddit=reddit,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Society Analysis v2 - precompute pipeline driver",
    )
    parser.add_argument("--subreddit", default=None,
                        help="Subreddit (comma-separate for multi).")
    parser.add_argument("--reddit-query", default=None,
                        help="Full-text search query on Reddit.")
    parser.add_argument("--channel", default=None,
                        help="Telegram channel handle.")
    parser.add_argument("--jsonl", default=None,
                        help="JSONL file of pre-collected posts.")
    parser.add_argument("--days", type=int, default=7,
                        help="Days back to fetch (default 7).")
    args = parser.parse_args()

    if not (args.subreddit or args.reddit_query or args.channel
            or args.jsonl):
        parser.error(
            "Specify at least one source: --subreddit, --reddit-query, "
            "--channel, or --jsonl."
        )

    ingestion = _build_ingestion()
    knowledge = KnowledgeAgent()
    pg = ingestion._pg  # type: ignore[attr-defined]
    kuzu = ingestion._kuzu  # type: ignore[attr-defined]

    schema_sync = None
    try:
        schema_sync = SchemaSync(pg=pg)
    except Exception as exc:
        log.warning("main.schema_sync_unavailable", error=str(exc)[:120])

    pipeline = PrecomputePipelineV2(
        ingestion=ingestion,
        knowledge=knowledge,
        multimodal=MultimodalAgent(),
        entity_extractor=EntityExtractor(),
        topic_clusterer=TopicClusterer(),
        post_deduper=PostDeduper(),
        schema_agent=SchemaAgent(),
        schema_sync=schema_sync,
        pg=pg,
        kuzu=kuzu,
    )

    # New run dir
    ms = ManifestService()
    manifest = ms.new_run(
        query_text=(args.reddit_query or args.channel or args.subreddit or ""),
        subreddits=[s.strip() for s in (args.subreddit or "").split(",")
                     if s.strip()] or None,
        reddit_query=args.reddit_query,
        reddit_sort="hot",
        channel=args.channel,
        jsonl_path=args.jsonl,
        image_url=None,
        image_path=None,
        days_back=args.days,
    )
    run_dir = ms.run_dir(manifest.run_id)
    log.info("main.run_start", run_id=manifest.run_id, run_dir=str(run_dir))

    subreddits = (
        [s.strip() for s in args.subreddit.split(",") if s.strip()]
        if args.subreddit else None
    )
    result = pipeline.run(
        run_dir=run_dir,
        subreddits=subreddits,
        reddit_query=args.reddit_query,
        reddit_days_back=args.days,
        jsonl_path=args.jsonl,
        channel=args.channel,
        channel_days_back=args.days,
    )

    log.info("main.run_done",
             run_id=result.run_id,
             posts=len(result.posts),
             topics=len(result.topics),
             stages=[(s.name, s.status) for s in result.stages])
    print(f"OK - run_id={result.run_id}  "
          f"posts={len(result.posts)}  topics={len(result.topics)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
