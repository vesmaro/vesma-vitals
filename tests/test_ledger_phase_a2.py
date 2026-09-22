"""Phase A2 contract tests — verb ledger, hourly rollup, exposition.

Gates carried from the ArchCom decision: C5 fail-closed meta (incl. the
N4 hardening), exact quantiles in the rollup (never averages), global
rollup rows for public exposition without project labels (RL-S2),
idempotent hours, non-fatal semantics everywhere.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from pathlib import Path

import pytest

from mnemos_vitals.exposer import render_exposition
from mnemos_vitals.sink import MetricsStore, validate_meta


@pytest.fixture()
def store(tmp_path: Path) -> MetricsStore:
    s = MetricsStore(tmp_path / "metrics.sqlite")
    yield s
    s.close()


def _verb(**overrides: object) -> dict:
    payload: dict[str, object] = {
        "surface": "mcp",
        "verb": "mnemos_search",
        "status": "ok",
        "latency_ms": 12.0,
        "project": "demo",
        "agent": "gcw-tech-lead",
    }
    payload.update(overrides)
    return payload


class TestRecordVerb:
    def test_roundtrip(self, store: MetricsStore):
        vid = store.record_verb(**_verb())  # type: ignore[arg-type]
        assert vid is not None
        conn = sqlite3.connect(store.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM verb_metrics WHERE id=?", (vid,)).fetchone()
        assert row["surface"] == "mcp"
        assert row["verb"] == "mnemos_search"
        assert row["status"] == "ok"
        assert row["latency_ms"] == 12.0
        conn.close()

    def test_meta_roundtrip_and_c5_refusal(self, store: MetricsStore):
        ok = store.record_verb(**_verb(meta={"counters": {"ttl_deleted": 2}}))  # type: ignore[arg-type]
        assert ok is not None
        conn = sqlite3.connect(store.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT meta_json FROM verb_metrics WHERE id=?", (ok,)).fetchone()
        assert json.loads(row["meta_json"]) == {"counters": {"ttl_deleted": 2}}
        conn.close()
        # C5: unknown key → write refused, warning, no row, no raise
        vid = store.record_verb(**_verb(meta={"mystery": 1}))  # type: ignore[arg-type]
        assert vid is None
        conn = sqlite3.connect(store.db_path)
        n = conn.execute("SELECT COUNT(*) FROM verb_metrics").fetchone()[0]
        conn.close()
        assert n == 1  # only the good row

    def test_invalid_surface_status_latency_refused(self, store: MetricsStore):
        assert store.record_verb(**_verb(surface="carrier-pigeon")) is None  # type: ignore[arg-type]
        assert store.record_verb(**_verb(status="meh")) is None  # type: ignore[arg-type]
        assert store.record_verb(**_verb(latency_ms=-1.0)) is None  # type: ignore[arg-type]
        assert store.record_verb(**_verb(latency_ms=math.inf)) is None  # type: ignore[arg-type]
        assert store.record_verb(**_verb(verb="bad\nverb")) is None  # type: ignore[arg-type]

    def test_n4_counters_and_nan_gates(self):
        # counters keys must be identifier-capped
        assert validate_meta({"counters": {"bad-key": 1}}) is None
        # counters entries capped at 16
        assert validate_meta({"counters": {f"k{i}": i for i in range(17)}}) is None
        assert validate_meta({"counters": {f"k{i}": i for i in range(16)}}) is not None
        # NaN/inf refused anywhere in the meta
        assert validate_meta({"budget": float("nan")}) is None
        assert validate_meta({"budget": float("inf")}) is None
        assert validate_meta({"budget": 2048}) is not None

    def test_insert_p95_smoke(self, store: MetricsStore):
        """Smoke bound only (10x headroom): the REAL A2 gate (p95 < 2 ms
        in the live hooks path) is measured on the running server."""
        samples: list[float] = []
        for i in range(500):
            t0 = time.perf_counter()
            store.record_verb(**_verb(verb=f"v{i % 7}"))  # type: ignore[arg-type]
            samples.append((time.perf_counter() - t0) * 1000)
        samples.sort()
        p95 = samples[int(0.95 * (len(samples) - 1))]
        assert p95 < 20.0, f"smoke p95 {p95:.2f} ms — regression, investigate"


class TestRollupHourly:
    def test_exact_quantiles_and_global_group(self, store: MetricsStore):
        hour = 29000000  # fixed epoch hour
        h0 = hour * 3600
        # project A: latencies 10..19 (p50=14.5, p95=18.55), project B: 100
        for i in range(10):
            store.record_verb(
                **_verb(ts=h0 + i, latency_ms=10.0 + i, project="A")  # type: ignore[arg-type]
            )
        store.record_verb(**_verb(ts=h0 + 50, latency_ms=100.0, project="B"))  # type: ignore[arg-type]
        written = store.rollup_hourly(hour=hour)
        # 2 per-project groups + 1 global group
        assert written == 3
        conn = sqlite3.connect(store.db_path)
        conn.row_factory = sqlite3.Row
        rows = {
            (r["surface"], r["verb"], r["status"], r["project"]): r
            for r in conn.execute(
                "SELECT * FROM verb_metrics_hourly WHERE hour=?", (hour,)
            ).fetchall()
        }
        glob = rows[("mcp", "mnemos_search", "ok", None)]
        assert glob["count"] == 11  # global = A + B, no project label
        pa = rows[("mcp", "mnemos_search", "ok", "A")]
        assert pa["count"] == 10
        assert pa["p50_ms"] == 14.5
        assert pa["p95_ms"] == pytest.approx(18.55)
        assert pa["max_ms"] == 19.0
        conn.close()

    def test_idempotent_rerun(self, store: MetricsStore):
        hour = 29000001
        store.record_verb(**_verb(ts=hour * 3600 + 7, latency_ms=5.0))  # type: ignore[arg-type]
        # global + per-project("demo") rows; re-run must not duplicate them
        assert store.rollup_hourly(hour=hour) == 2
        assert store.rollup_hourly(hour=hour) == 2
        conn = sqlite3.connect(store.db_path)
        n = conn.execute("SELECT COUNT(*) FROM verb_metrics_hourly").fetchone()[0]
        conn.close()
        assert n == 2

    def test_empty_hour_writes_nothing(self, store: MetricsStore):
        assert store.rollup_hourly(hour=29000002) == 0


class TestExposition:
    def _prepare(self, store: MetricsStore) -> None:
        hour = 29000010
        h0 = hour * 3600
        store.record_verb(**_verb(ts=h0 + 1, latency_ms=10.0, project="A"))  # type: ignore[arg-type]
        store.record_verb(**_verb(ts=h0 + 2, latency_ms=30.0, project="B"))  # type: ignore[arg-type]
        store.record_verb(  # type: ignore[arg-type]
            **_verb(ts=h0 + 3, latency_ms=99.0, project="A", status="error")
        )
        store.rollup_hourly(hour=hour)

    def test_format_and_rl_s2_no_project_labels(self, store: MetricsStore):
        self._prepare(store)
        text = render_exposition(store, gauges={"memories_total": 7})
        ok_line = 'mnemos_verb_calls_total{surface="mcp",verb="mnemos_search",status="ok"} 2'
        err_line = (
            'mnemos_verb_calls_total{surface="mcp",verb="mnemos_search",status="error"} 1'
        )
        assert ok_line in text
        assert err_line in text
        assert "# TYPE mnemos_verb_calls_total counter" in text
        assert 'quantile="0.5"' in text and 'quantile="0.95"' in text
        assert "memories_total 7" in text
        # RL-S2: project must never appear as a label or value
        assert "demo" not in text
        assert 'project="' not in text

    def test_empty_sidecar_renders_headers_only(self, store: MetricsStore):
        text = render_exposition(store)
        assert text.startswith("# HELP mnemos_verb_calls_total")
        assert "mnemos_verb_calls_total{" not in text

    def test_labels_escaped(self, store: MetricsStore):
        hour = 29000020
        assert store.record_verb(**_verb(ts=hour * 3600, latency_ms=1.0)) is not None
        conn = sqlite3.connect(store.db_path)
        conn.execute(
            "INSERT INTO verb_metrics (ts, surface, verb, status, latency_ms)"
            " VALUES (?, 'mcp', 'weird\"verb', 'ok', 1.0)",
            (hour * 3600,),
        )
        conn.commit()
        conn.close()
        store.rollup_hourly(hour=hour)
        text = render_exposition(store)
        assert 'verb="weird\\"verb"' in text
