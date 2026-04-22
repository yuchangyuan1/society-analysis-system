#!/usr/bin/env python3
"""
Multimodal Social Media Propagation Analysis and Counter-Messaging System
Main entry point — wires up all services and agents, then runs the planner.

Usage examples:
  # Analyze a claim from the command line
  python main.py --query "Claim: vaccines cause autism"

  # Analyze from a pre-collected JSONL file
  python main.py --query "vaccine misinformation" --jsonl data/sample_posts.jsonl

  # Analyze an image post with counter-message and visual card
  python main.py \\
    --query "Analyze whether this image post is misleading and generate a clarification card." \\
    --image-url "https://example.com/suspicious_post.png"

  # Real-time watch mode (Phase 3) — re-runs every 5 minutes
  python main.py --query "vaccine misinformation" --watch --interval 300

  # Watch mode with max 10 cycles and risk threshold 0.6
  python main.py --query "5G conspiracy" --watch --interval 120 --max-cycles 10
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import textwrap
from pathlib import Path

# ── Windows UTF-8 console fix ──────────────────────────────────────────────────
# On Chinese Windows (GBK console), structlog fails to print non-ASCII characters
# (e.g. Polish ł, Arabic, CJK) unless we wrap stdout/stderr in UTF-8 mode.
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import structlog

import config
from agents import (
    AnalysisAgent,
    CounterMessageAgent,
    CriticAgent,
    IngestionAgent,
    KnowledgeAgent,
    PlannerAgent,
    ReportAgent,
    RiskAgent,
    VisualAgent,
)
from agents.community import CommunityAgent
from datetime import datetime, timezone
from models.post import Post
from services import (
    ChromaService,
    ClaudeVisionService,
    EmbeddingsService,
    KuzuService,
    ManifestService,
    NewsSearchService,
    PostgresService,
    RedditService,
    StableDiffusionService,
    TelegramService,
    WhisperService,
    WikipediaService,
    XApiService,
)

# ── Logging ────────────────────────────────────────────────────────────────────
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


def load_claim_fixture(path: Path) -> tuple[list[Post], dict]:
    """
    Load a fixed claim set for reproducible A/B evaluation (P0-0, §10.6).

    Each claim becomes one synthetic Post whose id is deterministic from the
    fixture file stem + ordinal, so two runs on the same fixture produce
    identical ingested posts (up to timestamps, which are taken from the
    fixture itself). channel_name is fixed to "fixture" so downstream
    analytics can tell fixture runs from real ingestion.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    stem = path.stem
    posts: list[Post] = []
    for idx, entry in enumerate(data.get("claims", []), start=1):
        ts = entry.get("created_utc")
        posted_at = (
            datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
        )
        posts.append(Post(
            id=f"fixture:{stem}:{idx:03d}",
            account_id=entry.get("author") or f"fixture_user_{idx}",
            channel_name="fixture",
            text=entry["text"],
            posted_at=posted_at,
        ))
    metadata = {
        "run_label": data.get("run_label"),
        "fixture_path": str(path),
        "claim_count": len(posts),
    }
    return posts, metadata


def build_planner() -> PlannerAgent:
    """Instantiate all services and agents; return a wired-up PlannerAgent."""
    log.info("system.startup")

    # Services
    pg = PostgresService()
    try:
        pg.connect()
    except Exception as exc:
        log.warning("postgres.unavailable", error=str(exc),
                    note="Running without Postgres persistence")

    chroma = ChromaService()
    kuzu = KuzuService()
    embedder = EmbeddingsService()
    vision = ClaudeVisionService()
    whisper = WhisperService()
    telegram = TelegramService(whisper=whisper)
    x_api = XApiService()   # kept as fallback
    reddit = RedditService()
    sd = StableDiffusionService()
    news = NewsSearchService()
    wikipedia = WikipediaService()

    # Agents (each is an isolated workspace)
    ingestion = IngestionAgent(
        pg=pg, chroma=chroma, kuzu=kuzu,
        embedder=embedder, vision=vision,
        telegram=telegram, x_api=x_api,
        reddit=reddit,
    )
    knowledge = KnowledgeAgent(
        pg=pg, chroma=chroma, kuzu=kuzu, embedder=embedder,
        wikipedia=wikipedia, news_service=news,
    )
    analysis = AnalysisAgent(pg=pg, kuzu=kuzu)
    risk = RiskAgent()
    counter_msg = CounterMessageAgent()
    critic = CriticAgent()
    report_agent = ReportAgent(pg=pg)
    visual = VisualAgent(sd=sd, kuzu=kuzu)

    community = CommunityAgent(kuzu=kuzu)

    return PlannerAgent(
        ingestion=ingestion,
        knowledge=knowledge,
        analysis=analysis,
        risk=risk,
        counter_msg=counter_msg,
        critic=critic,
        report_agent=report_agent,
        visual=visual,
        news_service=news,
        community=community,
    )


def print_report(report) -> None:
    """Pretty-print the IncidentReport to stdout."""
    print("\n" + "=" * 70)
    print(f"  INCIDENT REPORT - {report.id}")
    print("=" * 70)
    print(f"Intent:      {report.intent_type}")
    print(f"Query:       {report.query_text or 'N/A'}")
    print(f"Risk Level:  {report.risk_level or 'N/A'}")
    print(f"Human Review Required: {report.requires_human_review}")

    if report.propagation_summary:
        ps = report.propagation_summary
        print(f"\nPropagation:")
        print(f"  Posts: {ps.post_count}, Accounts: {ps.unique_accounts}")
        print(f"  Velocity: {ps.velocity:.1f} posts/hr")
        print(f"  Anomaly: {ps.anomaly_detected}")
        if ps.anomaly_description:
            print(f"  -> {ps.anomaly_description}")
        if ps.coordinated_pairs:
            print(f"\nGraph — Coordinated Account Pairs: {ps.coordinated_pairs}")
            for pair in ps.coordination_details[:5]:
                print(f"  • {pair.account1} & {pair.account2} "
                      f"— {pair.shared_claim_count} shared claim(s)")
                for claim in pair.sample_claims[:2]:
                    print(f"      \"{claim}\"")
        else:
            print(f"  Graph Coordination: none detected")

    if report.propagation_summary and report.propagation_summary.account_role_summary:
        roles = report.propagation_summary.account_role_summary
        print(f"\nAccount Roles:")
        for role, count in sorted(roles.items()):
            bar = "█" * min(count, 30)
            print(f"  {role:<12} {count:>4}  {bar}")

    if report.topic_summaries:
        print(f"\nTopic Analysis ({len(report.topic_summaries)} topics discovered):")
        print(f"  {'Topic':<36} {'Posts':>5} {'Vel/hr':>7} {'Risk':>6}  {'Emotion':<8}  Flags")
        print(f"  {'-'*36} {'-'*5} {'-'*7} {'-'*6}  {'-'*8}  -----")
        for t in report.topic_summaries:
            flags = []
            if t.is_trending:
                flags.append("TRENDING")
            if t.is_likely_misinfo:
                flags.append("MISINFO")
            label = t.label[:34] + ".." if len(t.label) > 36 else t.label
            emotion = (t.dominant_emotion or "-")[:8]
            print(
                f"  {label:<36} {t.post_count:>5} {t.velocity:>7.1f} "
                f"{t.misinfo_risk:>6.2f}  {emotion:<8}  {', '.join(flags) or '-'}"
            )

    if report.cascade_predictions:
        print(f"\nCascade Forecast (Phase 2):")
        for cp in report.cascade_predictions[:3]:
            label = cp.topic_label[:40] if cp.topic_label else "Unknown"
            print(
                f"  {label:<40}  "
                f"~{cp.predicted_posts_24h:>5} posts/24h  "
                f"peak {cp.peak_window_hours}  "
                f"[{cp.confidence}]"
            )

    if report.persuasion_features:
        print(f"\nTop Persuasion Tactics (Phase 2):")
        for pf in report.persuasion_features[:3]:
            claim_short = (pf.claim_text or "")[:50]
            print(
                f"  virality={pf.virality_score:.2f}  "
                f"tactic={pf.top_persuasion_tactic:<18}  "
                f'"{claim_short}"'
            )

    if report.community_analysis and not report.community_analysis.skipped:
        ca = report.community_analysis
        print(f"\nCommunity Analysis (Phase 1):")
        print(f"  {ca.community_count} communities detected, "
              f"{ca.echo_chamber_count} echo chambers, "
              f"modularity={ca.modularity:.3f}")
        for comm in ca.communities[:4]:
            echo_flag = " [ECHO CHAMBER]" if comm.is_echo_chamber else ""
            print(f"  Community-{comm.community_id}: {comm.size} accounts, "
                  f"isolation={comm.isolation_score:.2f}, "
                  f"emotion={comm.dominant_emotion}{echo_flag}")
        if ca.cross_community_signals:
            print(f"  Cross-community coordination signals: {len(ca.cross_community_signals)}")

    if report.counter_target_plan and report.counter_target_plan.recommended_targets:
        ctp = report.counter_target_plan
        print(f"\nCounter-Messaging Targets (Phase 2, Task 3.3):")
        print(f"  Strategy: {ctp.strategy_summary}")
        for t in ctp.recommended_targets[:5]:
            name = t.username or t.account_id
            print(f"  #{t.priority_rank}  {name:<24}  [{t.role}]  {t.rationale[:50]}")

    if report.top_entities:
        print(f"\nKey Entities (Phase 2):")
        sorted_ents = sorted(report.top_entities,
                             key=lambda e: e.mention_count, reverse=True)
        for e in sorted_ents[:8]:
            print(f"  {e.name:<24} [{e.entity_type}]  ×{e.mention_count}")
        if report.entity_co_occurrences:
            print(f"  Co-occurrence pairs: {len(report.entity_co_occurrences)}")
            for co in report.entity_co_occurrences[:3]:
                print(f"    {co.entity_a_name} ↔ {co.entity_b_name}  (×{co.co_occurrence_count})")

    if report.immunity_strategy and not report.immunity_strategy.skipped:
        imm = report.immunity_strategy
        print(f"\nImmunity Strategy (Phase 3, Task 1.8):")
        print(f"  Coverage: {imm.immunity_coverage*100:.1f}%  "
              f"Targets: {imm.recommended_target_count}  "
              f"Strategy: {imm.strategy_used}")
        print(f"  {imm.summary}")
        for t in imm.targets[:5]:
            echo = " [ECHO-ENTRY]" if t.is_echo_chamber_entry else ""
            print(f"  #{imm.targets.index(t)+1}  {t.account_id:<24}  [{t.role}]  "
                  f"priority={t.priority_score:.3f}{echo}")

    if report.counter_effect_records:
        print(f"\nCounter-Effect Tracking (Phase 3, Task 3.2):")
        for rec in report.counter_effect_records:
            status = rec.outcome or "PENDING"
            score_str = f"{rec.effect_score:+.2f}" if rec.effect_score is not None else "N/A"
            vel_str = (f"{rec.baseline_velocity:.1f} → {rec.followup_velocity:.1f} posts/hr"
                       if rec.followup_velocity is not None
                       else f"baseline={rec.baseline_velocity:.1f} posts/hr")
            print(f"  [{status}]  topic={rec.topic_label or rec.topic_id or 'N/A'}  "
                  f"effect_score={score_str}  velocity: {vel_str}")

    if report.counter_message:
        print(f"\nCounter-Message:")
        print(textwrap.indent(
            textwrap.fill(report.counter_message, width=66), "  "
        ))

    if report.visual_card_path:
        print(f"\nVisual Card (counter-message): {report.visual_card_path}")
    else:
        has_log = any(l.stage == "visual_generation" for l in report.run_logs)
        if has_log:
            print("\nVisual Card: unavailable (see run logs)")

    if report.topic_card_paths:
        print(f"\nTopic Infographic Cards ({len(report.topic_card_paths)}):")
        for p in report.topic_card_paths:
            fname = p.replace("\\", "/").split("/")[-1]
            print(f"  {fname}")
            print(f"    {p}")

    if report.report_md:
        print("\n--- Markdown Report ---")
        safe_md = report.report_md[:2000].encode("ascii", "replace").decode("ascii")
        print(safe_md)

    print("\n--- Run Logs ---")
    for entry in report.run_logs:
        icon = {"ok": "[OK]", "degraded": "[WN]", "error": "[ER]", "blocked": "[BL]"}.get(
            entry.status.value, "[?]"
        )
        detail = f" — {entry.detail}" if entry.detail else ""
        print(f"  {icon} [{entry.status.value.upper():8s}] {entry.stage}{detail}")

    print("=" * 70 + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Society — Multimodal Propagation Analysis System"
    )
    parser.add_argument("--query", "-q", required=False, default=None,
                        help="User query or claim to analyze (optional when --jsonl is provided)")
    parser.add_argument("--image-url", default=None,
                        help="Public URL of an image post to analyze")
    parser.add_argument("--image-path", default=None,
                        help="Local path to an image file to analyze")
    parser.add_argument("--jsonl", default=None,
                        help="Path to JSONL file with pre-collected posts")
    parser.add_argument("--channel", default=None,
                        help="Telegram channel username to ingest today's posts from "
                             "(e.g. RealHealthRanger)")
    parser.add_argument("--days", type=int, default=7,
                        help="Number of days back to fetch (default: 7)")
    # ── Reddit ──────────────────────────────────────────────────────────────
    parser.add_argument("--subreddit", default=None,
                        help="Subreddit to analyse (e.g. conspiracy). "
                             "Comma-separate for multiple: conspiracy,worldnews")
    parser.add_argument("--reddit-query", default=None,
                        help="Full-text search query on Reddit "
                             "(searches all of Reddit unless --subreddit is also set)")
    parser.add_argument("--reddit-sort", default="hot",
                        choices=["hot", "new", "top", "rising", "relevance"],
                        help="Sort order for Reddit posts (default: hot)")
    parser.add_argument("--output-json", default=None,
                        help="Optional path to write JSON report output")
    parser.add_argument("--output-md", default=None,
                        help="Optional path to write Markdown report (if omitted but --output-json is set, writes alongside it)")
    # ── Phase 3: Watch mode ─────────────────────────────────────────────────
    parser.add_argument("--watch", action="store_true",
                        help="Enable real-time watch mode: re-run pipeline at --interval seconds")
    parser.add_argument("--interval", type=int, default=300,
                        help="Seconds between polling cycles in watch mode (default: 300)")
    parser.add_argument("--max-cycles", type=int, default=None,
                        help="Maximum number of watch cycles before stopping (default: unlimited)")
    parser.add_argument("--velocity-threshold", type=float, default=5.0,
                        help="posts/hr threshold that triggers HIGH_VELOCITY alert (default: 5.0)")
    parser.add_argument("--risk-threshold", type=float, default=0.70,
                        help="misinfo_risk threshold that triggers HIGH_RISK alert (default: 0.70)")
    parser.add_argument("--cascade-threshold", type=int, default=200,
                        help="predicted 24h posts threshold for CASCADE_WARNING (default: 200)")
    # ── P0-0: Fixed claim set for reproducible evaluation ──────────────────
    parser.add_argument("--claims-from", default=None, metavar="PATH",
                        help="Path to a claim-set fixture JSON (§10.6). When set, "
                             "skips Reddit/X ingestion and synthesises one pseudo-"
                             "Post per claim so the run is byte-level reproducible.")
    args = parser.parse_args()

    if not args.query and not args.jsonl and not args.image_url \
            and not args.image_path and not args.channel \
            and not args.subreddit and not args.reddit_query \
            and not args.claims_from:
        parser.error(
            "Provide at least one source: "
            "--query, --jsonl, --channel, --subreddit, --reddit-query, "
            "or --claims-from."
        )

    planner = build_planner()

    # ── P0-0: fixed claim set fixture (§10.6) ────────────────────────────────
    fixture_posts: list[Post] | None = None
    fixture_meta: dict | None = None
    if args.claims_from:
        fixture_posts, fixture_meta = load_claim_fixture(Path(args.claims_from))
        log.info(
            "fixture.loaded",
            path=args.claims_from,
            claim_count=fixture_meta["claim_count"],
            run_label=fixture_meta.get("run_label"),
        )

    # ── Auto-generate natural-language query from CLI source args ────────────
    if args.channel and not args.query:
        query = (f"Discover trending topics and identify misinformation "
                 f"in posts from @{args.channel}")
    elif args.subreddit and not args.query:
        subs = args.subreddit.replace(",", " and ")
        query = (f"Discover trending topics and identify misinformation "
                 f"in r/{subs} posts")
    elif args.reddit_query and not args.query:
        query = (f"Discover trending topics and identify misinformation "
                 f"in Reddit posts about: {args.reddit_query}")
    elif fixture_meta and not args.query:
        label = fixture_meta.get("run_label") or Path(args.claims_from).stem
        query = f"Fixture-driven misinformation analysis on claim set: {label}"
    else:
        query = args.query or "Analyze misinformation propagation in the provided posts"

    # Parse subreddits list (comma-separated)
    subreddits = (
        [s.strip() for s in args.subreddit.split(",") if s.strip()]
        if args.subreddit else None
    )

    # ── Watch mode (Phase 3) ─────────────────────────────────────────────────
    if args.watch:
        from services.monitor_service import MonitorConfig, MonitorService
        config = MonitorConfig(
            velocity_threshold=args.velocity_threshold,
            risk_threshold=args.risk_threshold,
            cascade_threshold=args.cascade_threshold,
            max_cycles=args.max_cycles,
            print_full_report=False,
        )
        monitor = MonitorService(planner, config=config)
        monitor.start(query=query, interval_seconds=args.interval)
        return 0

    # ── P0-2: Create run manifest + run_dir up-front ─────────────────────────
    ms = ManifestService()
    manifest = ms.new_run(
        query_text=query,
        subreddits=subreddits,
        reddit_query=args.reddit_query,
        reddit_sort=args.reddit_sort,
        channel=args.channel,
        jsonl_path=args.jsonl,
        image_url=args.image_url,
        image_path=args.image_path,
        days_back=args.days,
    )
    run_dir = ms.run_dir(manifest.run_id)
    log.info("run.start", run_id=manifest.run_id, run_dir=str(run_dir))

    # ── Single-run mode ──────────────────────────────────────────────────────
    log.info("planner.dispatch", query=query[:100])
    if fixture_posts is not None:
        report = planner.run(
            query=query,
            posts=fixture_posts,
            run_dir=run_dir,
        )
    else:
        report = planner.run(
            query=query,
            image_url=args.image_url,
            image_path=args.image_path,
            jsonl_path=args.jsonl,
            channel=args.channel,
            channel_days_back=args.days,
            subreddits=subreddits,
            reddit_query=args.reddit_query,
            reddit_sort=args.reddit_sort,
            reddit_days_back=args.days,
            run_dir=run_dir,
        )

    print_report(report)

    # ── Finalize manifest (P0-2) ─────────────────────────────────────────────
    try:
        ms.finalize(
            manifest,
            posts_snapshot_sha256=report.posts_snapshot_sha256,
            post_count=report.post_count,
            report_id=report.id,
        )
    except Exception as exc:
        log.error("manifest.finalize_error", error=str(exc))

    # ── Dual-write: honour --output-json / --output-md while also keeping
    #                the canonical copy in runs/{run_id}/ ─────────────────────
    if args.output_json:
        out = Path(args.output_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        log.info("report.written_to_file", path=str(out))

    md_path: Path | None = None
    if args.output_md:
        md_path = Path(args.output_md)
    elif args.output_json and report.report_md:
        md_path = Path(args.output_json).with_suffix(".md")
    if md_path and report.report_md:
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(report.report_md, encoding="utf-8")
        log.info("report.md_written", path=str(md_path))

    log.info("run.finished", run_id=manifest.run_id, run_dir=str(run_dir))

    # Exit code: 0 = success, 1 = requires human review
    return 1 if report.requires_human_review else 0


if __name__ == "__main__":
    sys.exit(main())
