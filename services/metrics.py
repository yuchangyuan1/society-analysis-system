"""
Lightweight metrics registry - production hardening Day 8.

Self-contained Counter / Histogram (no prometheus dependency). Thread-safe.
Surfaces JSON snapshots through `/health/metrics` for the Reflection
Performance tab.

Usage:
    from services.metrics import metrics
    metrics.inc("rewriter.calls")
    metrics.inc("critic.verdict", labels={"passed": "true"})
    metrics.observe("nl2sql.latency_ms", elapsed_ms)
    metrics.observe("nl2sql.repair_rounds", n)

Naming convention:
    <component>.<metric>     e.g. "rewriter.calls"
    <component>.latency_ms   for histograms

Histogram buckets are automatic (P50 / P90 / P99 from a fixed-size
ring buffer per name + label set). 1024 most recent observations.
"""
from __future__ import annotations

import bisect
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from time import time
from typing import Any, Optional

_RING_SIZE = 1024


def _label_key(labels: Optional[dict[str, Any]]) -> tuple:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


@dataclass
class _CounterEntry:
    count: int = 0
    last_at: float = 0.0


@dataclass
class _HistEntry:
    samples: deque = field(default_factory=lambda: deque(maxlen=_RING_SIZE))
    last_at: float = 0.0

    def quantile(self, q: float) -> float:
        if not self.samples:
            return 0.0
        sorted_samples = sorted(self.samples)
        idx = max(0, min(len(sorted_samples) - 1,
                         int(round(q * (len(sorted_samples) - 1)))))
        return float(sorted_samples[idx])


class MetricsRegistry:
    def __init__(self) -> None:
        self._counters: dict[tuple, _CounterEntry] = defaultdict(_CounterEntry)
        self._hists: dict[tuple, _HistEntry] = defaultdict(_HistEntry)
        self._lock = threading.Lock()

    def inc(
        self, name: str, value: int = 1,
        labels: Optional[dict[str, Any]] = None,
    ) -> None:
        key = (name, _label_key(labels))
        with self._lock:
            e = self._counters[key]
            e.count += value
            e.last_at = time()

    def observe(
        self, name: str, value: float,
        labels: Optional[dict[str, Any]] = None,
    ) -> None:
        key = (name, _label_key(labels))
        with self._lock:
            e = self._hists[key]
            e.samples.append(float(value))
            e.last_at = time()

    def snapshot(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {"counters": [], "histograms": []}
        with self._lock:
            for (name, lkey), e in self._counters.items():
                out["counters"].append({
                    "name": name,
                    "labels": dict(lkey),
                    "count": e.count,
                    "last_at": e.last_at,
                })
            for (name, lkey), e in self._hists.items():
                if not e.samples:
                    continue
                out["histograms"].append({
                    "name": name,
                    "labels": dict(lkey),
                    "n": len(e.samples),
                    "p50": e.quantile(0.5),
                    "p90": e.quantile(0.9),
                    "p99": e.quantile(0.99),
                    "last_at": e.last_at,
                })
        out["counters"].sort(key=lambda d: d["name"])
        out["histograms"].sort(key=lambda d: d["name"])
        return out

    def reset(self) -> None:
        """Test-only helper."""
        with self._lock:
            self._counters.clear()
            self._hists.clear()


# Singleton
metrics = MetricsRegistry()


# Convenience timing context manager
class timing:
    """Use: with timing("rewriter.latency_ms"): ...
    On exit, observes elapsed milliseconds."""

    def __init__(self, name: str, labels: Optional[dict] = None) -> None:
        self._name = name
        self._labels = labels
        self._t0 = 0.0

    def __enter__(self) -> "timing":
        self._t0 = time()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        elapsed_ms = (time() - self._t0) * 1000
        metrics.observe(self._name, elapsed_ms, labels=self._labels)
