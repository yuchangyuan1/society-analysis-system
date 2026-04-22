"""
Phase 3 — Real-time streaming / continuous monitoring service.

Corresponds to:
  Task 2.1  — Real-time streaming data analysis
  Task 3.3  — Continuous watch mode

Design:
  Polling-based monitor that re-runs the PlannerAgent pipeline at configurable
  intervals.  A lightweight event loop checks for new or accelerating topics
  and emits alerts when thresholds are crossed.

  Alert conditions (configurable via MonitorConfig):
    - velocity_threshold: topic posts/hr exceeds this value → HIGH_VELOCITY alert
    - risk_threshold:     topic misinfo_risk exceeds this value → HIGH_RISK alert
    - cascade_threshold:  predicted 24h posts exceeds this value → CASCADE_WARNING

  Usage from main.py (--watch mode):
    monitor = MonitorService(planner, config=MonitorConfig())
    monitor.start(query="5G vaccines", interval_seconds=300)

  The monitor runs until KeyboardInterrupt and prints a live summary after each
  polling cycle.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

import structlog

log = structlog.get_logger(__name__)


@dataclass
class MonitorConfig:
    """Tuneable thresholds for the monitor alert system."""
    velocity_threshold: float = 5.0        # posts/hr → HIGH_VELOCITY
    risk_threshold: float = 0.70           # misinfo_risk → HIGH_RISK
    cascade_threshold: int = 200           # predicted 24h posts → CASCADE_WARNING
    max_cycles: Optional[int] = None       # None = run forever
    print_full_report: bool = False        # print full IncidentReport each cycle


@dataclass
class MonitorAlert:
    """A single alert emitted during a monitoring cycle."""
    cycle: int
    alert_type: str       # HIGH_VELOCITY | HIGH_RISK | CASCADE_WARNING | NEW_TOPIC
    topic_label: str
    detail: str
    severity: str         # LOW | MEDIUM | HIGH | CRITICAL
    detected_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class CycleResult:
    """Summary of one monitoring cycle."""
    cycle: int
    ran_at: datetime
    query: str
    topics_found: int
    trending_topics: int
    alerts: list[MonitorAlert] = field(default_factory=list)
    error: Optional[str] = None


class MonitorService:
    """
    Polling-based continuous monitor.

    Parameters
    ----------
    planner : PlannerAgent
        The fully wired planner to re-invoke each cycle.
    config : MonitorConfig
        Thresholds and behaviour flags.
    on_alert : Callable[[MonitorAlert], None] | None
        Optional hook called whenever an alert fires (e.g. for Slack/email dispatch).
    """

    def __init__(
        self,
        planner,   # PlannerAgent — avoid circular import
        config: Optional[MonitorConfig] = None,
        on_alert: Optional[Callable[[MonitorAlert], None]] = None,
    ) -> None:
        self._planner = planner
        self._cfg = config or MonitorConfig()
        self._on_alert = on_alert
        self._seen_topics: set[str] = set()
        self._history: list[CycleResult] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(
        self,
        query: str,
        interval_seconds: int = 300,
        intent_type: str = "TREND_ANALYSIS",
    ) -> None:
        """
        Block and poll until KeyboardInterrupt or max_cycles reached.

        Parameters
        ----------
        query : str
            The search / topic query forwarded to PlannerAgent.
        interval_seconds : int
            Seconds to wait between polling cycles.
        intent_type : str
            Planner intent (default: TREND_ANALYSIS).
        """
        log.info("monitor.start", query=query, interval_seconds=interval_seconds)
        print(f"\n{'='*60}")
        print(f"  Real-time Monitor  |  query='{query}'  |  interval={interval_seconds}s")
        print(f"{'='*60}\n")

        cycle = 0
        try:
            while True:
                if self._cfg.max_cycles and cycle >= self._cfg.max_cycles:
                    print(f"\n[monitor] Max cycles ({self._cfg.max_cycles}) reached — stopping.")
                    break

                cycle += 1
                result = self._run_cycle(cycle, query, intent_type)
                self._history.append(result)
                self._print_cycle_summary(result)

                if self._cfg.max_cycles and cycle >= self._cfg.max_cycles:
                    break

                print(f"\n[monitor] Next poll in {interval_seconds}s  (Ctrl+C to stop)\n")
                time.sleep(interval_seconds)

        except KeyboardInterrupt:
            print("\n\n[monitor] Interrupted by user.")
        finally:
            self._print_session_summary()

    def run_once(
        self,
        query: str,
        intent_type: str = "TREND_ANALYSIS",
        cycle: int = 1,
    ) -> CycleResult:
        """Run a single monitoring cycle without the polling loop."""
        result = self._run_cycle(cycle, query, intent_type)
        self._history.append(result)
        return result

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_cycle(self, cycle: int, query: str, intent_type: str) -> CycleResult:
        ran_at = datetime.utcnow()
        log.info("monitor.cycle_start", cycle=cycle, query=query)
        alerts: list[MonitorAlert] = []

        try:
            report = self._planner.run(
                user_query=query,
                intent_type=intent_type,
            )
            topics = report.topic_summaries or []

            for ts in topics:
                label = ts.label
                is_new = label not in self._seen_topics
                if is_new:
                    self._seen_topics.add(label)
                    alerts.append(MonitorAlert(
                        cycle=cycle,
                        alert_type="NEW_TOPIC",
                        topic_label=label,
                        detail=f"New trending topic detected: '{label}'",
                        severity="LOW",
                    ))

                if ts.velocity >= self._cfg.velocity_threshold:
                    severity = "CRITICAL" if ts.velocity >= self._cfg.velocity_threshold * 2 else "HIGH"
                    alerts.append(MonitorAlert(
                        cycle=cycle,
                        alert_type="HIGH_VELOCITY",
                        topic_label=label,
                        detail=f"velocity={ts.velocity:.1f} posts/hr (threshold={self._cfg.velocity_threshold})",
                        severity=severity,
                    ))

                if ts.misinfo_risk >= self._cfg.risk_threshold:
                    severity = "CRITICAL" if ts.misinfo_risk >= 0.9 else "HIGH"
                    alerts.append(MonitorAlert(
                        cycle=cycle,
                        alert_type="HIGH_RISK",
                        topic_label=label,
                        detail=f"misinfo_risk={ts.misinfo_risk:.2f} (threshold={self._cfg.risk_threshold})",
                        severity=severity,
                    ))

            # Check cascade predictions
            for pred in (report.cascade_predictions or []):
                if pred.predicted_posts_24h >= self._cfg.cascade_threshold:
                    severity = "CRITICAL" if pred.predicted_posts_24h >= self._cfg.cascade_threshold * 2 else "HIGH"
                    alerts.append(MonitorAlert(
                        cycle=cycle,
                        alert_type="CASCADE_WARNING",
                        topic_label=pred.topic_label,
                        detail=(
                            f"predicted_posts_24h={pred.predicted_posts_24h} "
                            f"(threshold={self._cfg.cascade_threshold}), "
                            f"confidence={pred.confidence}"
                        ),
                        severity=severity,
                    ))

            for alert in alerts:
                log.warning(
                    "monitor.alert",
                    cycle=cycle,
                    type=alert.alert_type,
                    topic=alert.topic_label,
                    severity=alert.severity,
                )
                if self._on_alert:
                    try:
                        self._on_alert(alert)
                    except Exception as exc:
                        log.error("monitor.alert_hook_error", error=str(exc))

            if self._cfg.print_full_report:
                _print_report_stub(report)

            return CycleResult(
                cycle=cycle,
                ran_at=ran_at,
                query=query,
                topics_found=len(topics),
                trending_topics=sum(1 for t in topics if t.is_trending),
                alerts=alerts,
            )

        except Exception as exc:
            log.error("monitor.cycle_error", cycle=cycle, error=str(exc))
            return CycleResult(
                cycle=cycle,
                ran_at=ran_at,
                query=query,
                topics_found=0,
                trending_topics=0,
                error=str(exc),
            )

    @staticmethod
    def _print_cycle_summary(result: CycleResult) -> None:
        ts = result.ran_at.strftime("%H:%M:%S")
        status = "ERROR" if result.error else "OK"
        print(f"[{ts}] Cycle #{result.cycle}  status={status}  "
              f"topics={result.topics_found}  trending={result.trending_topics}  "
              f"alerts={len(result.alerts)}")
        for alert in result.alerts:
            icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢"}.get(alert.severity, "•")
            print(f"  {icon} [{alert.alert_type}] {alert.topic_label}: {alert.detail}")
        if result.error:
            print(f"  ⚠ Error: {result.error}")

    def _print_session_summary(self) -> None:
        total_cycles = len(self._history)
        total_alerts = sum(len(r.alerts) for r in self._history)
        print(f"\n{'='*60}")
        print(f"  Monitor Session Summary")
        print(f"  Cycles completed : {total_cycles}")
        print(f"  Total alerts     : {total_alerts}")
        print(f"  Topics tracked   : {len(self._seen_topics)}")
        print(f"{'='*60}\n")


def _print_report_stub(report) -> None:
    """Minimal report print for watch mode (avoids duplicating main.py logic)."""
    print(f"  Report ID: {report.id}")
    print(f"  Risk level: {report.risk_level}")
    for ts in (report.topic_summaries or [])[:3]:
        print(f"  Topic: {ts.label[:60]}  risk={ts.misinfo_risk:.2f}  vel={ts.velocity}")
