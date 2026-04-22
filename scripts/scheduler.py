"""scripts/scheduler.py — periodic precompute runner.

Per `complete_project_transformation_plan.md` §8: the precompute pipeline
should be runnable on a schedule so fresh runs appear without manual
invocation of `python main.py`.

Two execution modes are supported — same underlying job function, so no
logic is duplicated:

1. **Long-running APScheduler mode** (default)
       python scripts/scheduler.py --mode apscheduler
   Spins up a BlockingScheduler and registers one job per configured task
   block. This is the "run it yourself" path (nohup / systemd / docker).

2. **One-shot mode** (for cron / GitHub Actions cron / Windows Task Scheduler)
       python scripts/scheduler.py --mode once --task telegram_RealHealthRanger
   Executes exactly one task and exits. This is the "let the OS scheduler
   do the timing" path — simpler in production.

Tasks are declared in `scripts/scheduler_tasks.yaml`. The file is optional;
if missing, a minimal default task runs against the frozen fixture so the
scheduler can be smoke-tested without network access.

The scheduler does **not** touch the online chat path — it only drives the
offline batch pipeline. Chat queries continue to hit whatever runs already
exist under `data/runs/`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Make repo root importable when scheduler is invoked as a plain script.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import structlog

log = structlog.get_logger(__name__)


# ─── Task model ──────────────────────────────────────────────────────────────

@dataclass
class TaskSpec:
    name: str
    cron: Optional[str] = None  # APScheduler cron expression, e.g. "0 */6 * * *"
    interval_seconds: Optional[int] = None  # mutex with cron
    query: Optional[str] = None
    channel: Optional[str] = None
    subreddit: Optional[str] = None
    reddit_query: Optional[str] = None
    reddit_sort: str = "hot"
    claims_from: Optional[str] = None
    days_back: int = 7
    extra: dict[str, Any] = field(default_factory=dict)


# ─── Task loading ────────────────────────────────────────────────────────────

DEFAULT_TASKS_FILE = REPO_ROOT / "scripts" / "scheduler_tasks.yaml"

DEFAULT_TASKS: list[TaskSpec] = [
    TaskSpec(
        name="fixture_smoke",
        interval_seconds=24 * 3600,
        claims_from="tests/fixtures/claims_conspiracy_baseline.json",
    ),
]


def load_tasks(path: Optional[Path]) -> list[TaskSpec]:
    if path is None or not path.exists():
        log.info(
            "scheduler.tasks_default",
            note="no scheduler_tasks.yaml found, using fixture smoke task",
        )
        return list(DEFAULT_TASKS)

    try:
        import yaml  # type: ignore
    except ImportError:
        log.warning(
            "scheduler.yaml_missing",
            note="PyYAML not installed — falling back to default tasks. "
                 "`pip install pyyaml` to load scheduler_tasks.yaml.",
        )
        return list(DEFAULT_TASKS)

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_tasks = data.get("tasks") or []
    tasks: list[TaskSpec] = []
    for raw in raw_tasks:
        if not raw.get("name"):
            continue
        tasks.append(TaskSpec(
            name=raw["name"],
            cron=raw.get("cron"),
            interval_seconds=raw.get("interval_seconds"),
            query=raw.get("query"),
            channel=raw.get("channel"),
            subreddit=raw.get("subreddit"),
            reddit_query=raw.get("reddit_query"),
            reddit_sort=raw.get("reddit_sort", "hot"),
            claims_from=raw.get("claims_from"),
            days_back=int(raw.get("days_back", 7)),
            extra=raw.get("extra") or {},
        ))
    if not tasks:
        return list(DEFAULT_TASKS)
    return tasks


# ─── Job function ────────────────────────────────────────────────────────────

def _derive_query(task: TaskSpec) -> str:
    if task.query:
        return task.query
    if task.channel:
        return (
            f"Discover trending topics and identify misinformation in posts "
            f"from @{task.channel}"
        )
    if task.subreddit:
        subs = task.subreddit.replace(",", " and ")
        return (
            f"Discover trending topics and identify misinformation in r/{subs}"
        )
    if task.reddit_query:
        return (
            f"Discover trending topics and identify misinformation in Reddit "
            f"posts about: {task.reddit_query}"
        )
    if task.claims_from:
        return f"Scheduled fixture run: {Path(task.claims_from).stem}"
    return "Scheduled misinformation scan"


def run_task(task: TaskSpec) -> dict[str, Any]:
    """Execute a single precompute run. Returns a small summary dict."""
    log.info("scheduler.task_start", task=task.name)

    # Import lazily to avoid pulling in heavy deps when the scheduler is just
    # listing tasks or loading config.
    from main import build_precompute_pipeline, load_claim_fixture
    from services.manifest_service import ManifestService

    pipeline = build_precompute_pipeline()
    ms = ManifestService()

    query = _derive_query(task)
    subreddits = (
        [s.strip() for s in task.subreddit.split(",") if s.strip()]
        if task.subreddit else None
    )

    fixture_posts = None
    if task.claims_from:
        fixture_posts, _ = load_claim_fixture(Path(task.claims_from))

    manifest = ms.new_run(
        query_text=query,
        subreddits=subreddits,
        reddit_query=task.reddit_query,
        reddit_sort=task.reddit_sort,
        channel=task.channel,
        jsonl_path=None,
        image_url=None,
        image_path=None,
        days_back=task.days_back,
    )
    run_dir = ms.run_dir(manifest.run_id)

    started = datetime.now(tz=timezone.utc)
    try:
        if fixture_posts is not None:
            report = pipeline.run(
                query=query,
                posts=fixture_posts,
                run_dir=run_dir,
            )
        else:
            report = pipeline.run(
                query=query,
                channel=task.channel,
                channel_days_back=task.days_back,
                subreddits=subreddits,
                reddit_query=task.reddit_query,
                reddit_sort=task.reddit_sort,
                reddit_days_back=task.days_back,
                run_dir=run_dir,
            )
        ms.finalize(
            manifest,
            posts_snapshot_sha256=report.posts_snapshot_sha256,
            post_count=report.post_count,
            report_id=report.id,
        )
        elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds()
        log.info(
            "scheduler.task_ok",
            task=task.name,
            run_id=manifest.run_id,
            post_count=report.post_count,
            elapsed_s=round(elapsed, 2),
        )
        return {
            "task": task.name,
            "status": "ok",
            "run_id": manifest.run_id,
            "post_count": report.post_count,
            "elapsed_s": round(elapsed, 2),
        }
    except Exception as exc:  # noqa: BLE001
        log.error(
            "scheduler.task_failed",
            task=task.name,
            error=str(exc),
            traceback=traceback.format_exc()[:2000],
        )
        return {
            "task": task.name,
            "status": "failed",
            "error": str(exc),
        }


# ─── Entry-point modes ───────────────────────────────────────────────────────

def run_once(task_name: str, tasks: list[TaskSpec]) -> int:
    matches = [t for t in tasks if t.name == task_name]
    if not matches:
        log.error("scheduler.task_not_found", name=task_name,
                  available=[t.name for t in tasks])
        return 2
    result = run_task(matches[0])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "ok" else 1


def run_apscheduler(tasks: list[TaskSpec]) -> int:
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        log.error(
            "scheduler.apscheduler_missing",
            note="pip install apscheduler, or use --mode once with OS cron.",
        )
        return 3

    scheduler = BlockingScheduler(timezone=os.environ.get("TZ", "UTC"))
    registered = 0
    for task in tasks:
        if task.cron:
            trigger = CronTrigger.from_crontab(task.cron)
        elif task.interval_seconds:
            trigger = IntervalTrigger(seconds=int(task.interval_seconds))
        else:
            log.warning(
                "scheduler.task_skipped_no_trigger",
                task=task.name,
                note="task has neither cron nor interval_seconds",
            )
            continue
        scheduler.add_job(
            run_task,
            trigger,
            args=[task],
            id=task.name,
            name=task.name,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )
        registered += 1
        log.info("scheduler.task_registered", task=task.name,
                 trigger=str(trigger))

    if registered == 0:
        log.error("scheduler.no_tasks")
        return 4

    log.info("scheduler.starting", tasks=registered)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler.shutdown")
    return 0


def list_tasks(tasks: list[TaskSpec]) -> int:
    rows = [
        {
            "name": t.name,
            "trigger": (
                f"cron={t.cron}" if t.cron else
                (f"interval={t.interval_seconds}s" if t.interval_seconds else "manual")
            ),
            "source": (
                t.channel and f"telegram:{t.channel}"
            ) or (
                t.subreddit and f"reddit:{t.subreddit}"
            ) or (
                t.reddit_query and f"reddit_q:{t.reddit_query}"
            ) or (
                t.claims_from and f"fixture:{Path(t.claims_from).name}"
            ) or "query-only",
        }
        for t in tasks
    ]
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/scheduler.py",
        description=(
            "Periodic precompute runner. Use --mode apscheduler for a "
            "long-running process; use --mode once with OS cron / GH Actions."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=["apscheduler", "once", "list"],
        default="apscheduler",
    )
    parser.add_argument("--task", default=None,
                        help="Task name (required for --mode once)")
    parser.add_argument(
        "--tasks-file",
        type=Path,
        default=DEFAULT_TASKS_FILE,
        help=f"YAML task config (default: {DEFAULT_TASKS_FILE.name})",
    )
    args = parser.parse_args(argv)

    tasks = load_tasks(args.tasks_file)

    if args.mode == "list":
        return list_tasks(tasks)

    if args.mode == "once":
        if not args.task:
            parser.error("--mode once requires --task NAME")
        return run_once(args.task, tasks)

    return run_apscheduler(tasks)


if __name__ == "__main__":
    raise SystemExit(main())
