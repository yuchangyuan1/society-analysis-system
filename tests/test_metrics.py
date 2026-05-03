"""Metrics registry tests - production hardening Day 8."""
from __future__ import annotations

import pytest

from services.metrics import metrics, timing


@pytest.fixture(autouse=True)
def _reset():
    metrics.reset()
    yield
    metrics.reset()


def test_inc_counter():
    metrics.inc("foo.calls")
    metrics.inc("foo.calls")
    metrics.inc("foo.calls", value=3)
    snap = metrics.snapshot()
    counters = {c["name"]: c["count"] for c in snap["counters"]}
    assert counters["foo.calls"] == 5


def test_counter_labels_separate_bucket():
    metrics.inc("v.kind", labels={"k": "a"})
    metrics.inc("v.kind", labels={"k": "a"})
    metrics.inc("v.kind", labels={"k": "b"})
    snap = metrics.snapshot()
    by_label = {tuple(sorted(c["labels"].items())): c["count"]
                for c in snap["counters"]}
    assert by_label[(("k", "a"),)] == 2
    assert by_label[(("k", "b"),)] == 1


def test_observe_histogram_quantiles():
    for v in range(100):
        metrics.observe("foo.latency_ms", float(v))
    snap = metrics.snapshot()
    hist = next(h for h in snap["histograms"] if h["name"] == "foo.latency_ms")
    assert hist["n"] == 100
    assert 49 <= hist["p50"] <= 50
    assert 88 <= hist["p90"] <= 90
    assert hist["p99"] >= 98


def test_timing_context_manager():
    import time as t
    with timing("foo.latency_ms"):
        t.sleep(0.01)
    snap = metrics.snapshot()
    hist = next(h for h in snap["histograms"] if h["name"] == "foo.latency_ms")
    assert hist["n"] == 1
    assert hist["p50"] >= 5  # at least 5ms


def test_health_metrics_endpoint_is_registered():
    from api.app import app
    routes = {r.path for r in app.routes}
    assert "/health/metrics" in routes
