"""Phase C contract tests — the usage loop (record_usage + UsageAnalyzer).

Hard rules under test (docs/architecture.md §2 ``usage_reports``;
docs/methodology.md §3.2 relevance ladder; frozen pre-registration
docs/experiments/touched-rate-kappa.md):

  - ``record_usage`` is the ONLY client-supplied write into the plane —
    every non-conforming shape refuses the WHOLE write loudly (warning
    + None), never a silent partial drop, never a raise into the host;
  - FK discipline: an unknown metrics_id is a loud refusal;
  - analytics are read-only, global, non-fatal; NO-DATA is loud and is
    never a zero;
  - the kappa gate is structural: touched_share stays informational,
    ``corridor_eligible`` is False while calibration is pending.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from mnemos_vitals.sink import MetricsStore
from mnemos_vitals.usage import (
    KAPPA_CI_FLOOR,
    KAPPA_MIN,
    KAPPA_PENDING_REASON,
    KAPPA_PREREGISTRATION,
    UsageAnalyzer,
    kappa_calibration_pending,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def store(tmp_path: Path) -> MetricsStore:
    s = MetricsStore(tmp_path / "metrics.sqlite")
    yield s
    s.close()


def make_assemble(store: MetricsStore, n_blocks: int = 2) -> int:
    """One assemble row with n_blocks injected blocks; returns metrics_id."""
    blocks = [
        {
            "memory_id": f"mem-{i}",
            "content_type": "note",
            "score": 0.5,
            "tokens": 50,
            "redactions": 0,
            "ccr_expanded": False,
            "ccr_hashes": [],
            "content": f"RAW BLOCK CONTENT {i}",
        }
        for i in range(n_blocks)
    ]
    mid = store.record_assemble(
        {
            "session": "sess-u",
            "project": "demo",
            "agent": "gcw-tech-lead",
            "mode": "sync",
            "text": "assembled window",
            "blocks": blocks,
            "tokens": {"budget": 1000, "estimated": 100},
            "stats": {},
        }
    )
    assert mid is not None
    return mid


def usage_rows(store: MetricsStore) -> list[sqlite3.Row]:
    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM usage_reports ORDER BY id").fetchall()
    conn.close()
    return rows


class TestRecordUsage:
    def test_roundtrip(self, store: MetricsStore):
        mid = make_assemble(store, n_blocks=3)
        rid = store.record_usage(
            mid,
            block_ids_touched=[f"{mid}:0", f"{mid}:2"],
            tokens_out=42,
            wrong_tool_flag=True,
            ts=123.5,
        )
        assert isinstance(rid, int) and rid > 0
        rows = usage_rows(store)
        assert len(rows) == 1
        row = rows[0]
        assert row["metrics_id"] == mid
        assert json.loads(row["block_ids_touched_json"]) == [f"{mid}:0", f"{mid}:2"]
        assert row["tokens_out"] == 42
        assert row["wrong_tool_flag"] == 1  # bool stored as INTEGER 1/0

    def test_defaults_and_zero_boundary(self, store: MetricsStore):
        mid = make_assemble(store)
        assert store.record_usage(mid, block_ids_touched=[], tokens_out=0) is not None
        row = usage_rows(store)[0]
        assert json.loads(row["block_ids_touched_json"]) == []  # touched nothing: legit
        assert row["tokens_out"] == 0  # 0 is a value, not absence
        assert row["wrong_tool_flag"] == 0

    def test_dedup_preserves_order(self, store: MetricsStore):
        mid = make_assemble(store)
        store.record_usage(mid, block_ids_touched=["b", "a", "b", "c", "a"])
        stored = json.loads(usage_rows(store)[0]["block_ids_touched_json"])
        assert stored == ["b", "a", "c"]

    def test_boundary_256_entries_accepted(self, store: MetricsStore):
        mid = make_assemble(store)
        ids = [f"blk-{i:04d}" for i in range(256)]
        assert store.record_usage(mid, block_ids_touched=ids) is not None

    def test_no_ts_column_born_final(self, store: MetricsStore):
        """ts is accepted for call-site symmetry but never stored."""
        mid = make_assemble(store)
        store.record_usage(mid, block_ids_touched=["x"], ts=1.0)
        conn = sqlite3.connect(store.db_path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(usage_reports)")]
        conn.close()
        assert "ts" not in cols  # born-final pin (test_canary_c1) holds


class TestUsageRefusals:
    """Every refusal: None returned, loud warning, zero rows written."""

    @pytest.fixture(autouse=True)
    def _parent(self, store: MetricsStore):
        self.mid = make_assemble(store, n_blocks=4)

    def _assert_refused(self, store: MetricsStore, caplog, result):
        assert result is None
        assert "record_usage failed" in caplog.text  # loud, never silent
        assert usage_rows(store) == []  # WHOLE write refused, no partial row

    def test_unknown_metrics_id_loud_refusal(self, store: MetricsStore, caplog):
        caplog.set_level(logging.WARNING, logger="mnemos_vitals.sink")
        self._assert_refused(
            store, caplog, store.record_usage(self.mid + 999, block_ids_touched=["x"])
        )

    @pytest.mark.parametrize("bad_id", ["5", 5.0, True, None, 0, -1])
    def test_bad_metrics_id(self, store: MetricsStore, caplog, bad_id):
        caplog.set_level(logging.WARNING, logger="mnemos_vitals.sink")
        self._assert_refused(store, caplog, store.record_usage(bad_id, block_ids_touched=["x"]))

    @pytest.mark.parametrize("bad", ["b:0", ("b:0",), {"b:0"}, None, 5])
    def test_non_list_touched(self, store: MetricsStore, caplog, bad):
        caplog.set_level(logging.WARNING, logger="mnemos_vitals.sink")
        self._assert_refused(store, caplog, store.record_usage(self.mid, block_ids_touched=bad))

    @pytest.mark.parametrize(
        "bad_entries",
        [
            [123],  # non-string entry
            [None],
            [""],  # empty string identifies no block
            ["x" * 129],  # over 128 chars
            ["a\nb"],  # newline
            ["a\rb"],  # carriage return
            ["ok", 7],  # one bad entry poisons the whole write
        ],
    )
    def test_bad_entries(self, store: MetricsStore, caplog, bad_entries):
        caplog.set_level(logging.WARNING, logger="mnemos_vitals.sink")
        refused = store.record_usage(self.mid, block_ids_touched=bad_entries)
        self._assert_refused(store, caplog, refused)

    def test_too_many_entries(self, store: MetricsStore, caplog):
        caplog.set_level(logging.WARNING, logger="mnemos_vitals.sink")
        ids = [f"b-{i}" for i in range(257)]
        self._assert_refused(store, caplog, store.record_usage(self.mid, block_ids_touched=ids))

    @pytest.mark.parametrize("bad_tokens", [-1, 1.5, "5", True])
    def test_bad_tokens_out(self, store: MetricsStore, caplog, bad_tokens):
        caplog.set_level(logging.WARNING, logger="mnemos_vitals.sink")
        refused = store.record_usage(
            self.mid, block_ids_touched=["x"], tokens_out=bad_tokens
        )
        self._assert_refused(store, caplog, refused)

    @pytest.mark.parametrize("bad_flag", [1, 0, "yes", None])
    def test_bad_wrong_tool_flag(self, store: MetricsStore, caplog, bad_flag):
        caplog.set_level(logging.WARNING, logger="mnemos_vitals.sink")
        self._assert_refused(
            store,
            caplog,
            store.record_usage(self.mid, block_ids_touched=["x"], wrong_tool_flag=bad_flag),
        )

    @pytest.mark.parametrize("bad_ts", ["now", float("nan"), float("inf")])
    def test_bad_ts(self, store: MetricsStore, caplog, bad_ts):
        caplog.set_level(logging.WARNING, logger="mnemos_vitals.sink")
        self._assert_refused(
            store, caplog, store.record_usage(self.mid, block_ids_touched=["x"], ts=bad_ts)
        )


class TestNonFatal:
    def test_closed_store_returns_none(self, tmp_path: Path):
        s = MetricsStore(tmp_path / "metrics.sqlite")
        s.close()
        assert s.record_usage(1, block_ids_touched=["x"]) is None  # never raises

    def test_analyzer_never_raises_into_host(self, tmp_path: Path):
        s = MetricsStore(tmp_path / "metrics.sqlite")
        s.close()
        a = UsageAnalyzer(s)
        for report in (a.assemble_usage_rate(), a.touched_share(), a.wrong_tool_rate()):
            assert report["status"] == "NO-DATA"
            assert report["reasons"][0] == "analyzer degraded: RuntimeError"
        # the touched_share degradation ALSO carries the frozen-prereg reason
        # (review nit): the operator must see why the gate is soft-locked
        a2 = UsageAnalyzer(s)
        ts = a2.touched_share()
        assert any("kappa" in r for r in ts["reasons"])
        s.close()  # idempotent


class TestUsageAnalytics:
    def test_assemble_usage_rate_exact(self, store: MetricsStore):
        mids = [make_assemble(store) for _ in range(3)]
        store.record_usage(mids[0], block_ids_touched=[f"{mids[0]}:0"])
        store.record_usage(mids[2], block_ids_touched=[f"{mids[2]}:1"])
        report = UsageAnalyzer(store).assemble_usage_rate()
        assert report["status"] == "OK"
        assert report["assemble_calls"] == 3
        assert report["closed_calls"] == 2
        assert report["assemble_usage_rate"] == pytest.approx(2 / 3)

    def test_assemble_usage_rate_no_data(self, store: MetricsStore):
        report = UsageAnalyzer(store).assemble_usage_rate()
        assert report["status"] == "NO-DATA"
        assert report["assemble_usage_rate"] is None  # never zero without data
        assert report["reasons"]

    def test_touched_share_exact_math(self, store: MetricsStore):
        a = make_assemble(store, n_blocks=4)  # touch 1 of 4 -> 0.25
        b = make_assemble(store, n_blocks=2)  # touch 2 of 2 -> 1.00
        store.record_usage(a, block_ids_touched=[f"{a}:0"])
        store.record_usage(b, block_ids_touched=[f"{b}:0", f"{b}:1"])
        report = UsageAnalyzer(store).touched_share()
        assert report["status"] == "OK"
        assert report["reports"] == 2
        assert report["calls_measured"] == 2
        assert report["touched_share"] == pytest.approx((0.25 + 1.0) / 2)

    def test_touched_share_real_zero_is_ok(self, store: MetricsStore):
        """A report touching nothing is a REAL zero, not NO-DATA."""
        mid = make_assemble(store, n_blocks=2)
        store.record_usage(mid, block_ids_touched=[])
        report = UsageAnalyzer(store).touched_share()
        assert report["status"] == "OK"
        assert report["touched_share"] == 0.0

    def test_touched_share_no_reports_loud_no_data(self, store: MetricsStore):
        make_assemble(store)  # assemble exists, reports do not
        report = UsageAnalyzer(store).touched_share()
        assert report["status"] == "NO-DATA"
        assert report["touched_share"] is None
        assert report["reports"] == 0
        assert "no usage reports" in " ".join(report["reasons"])

    def test_touched_share_skips_empty_assembly(self, store: MetricsStore):
        """0/0 (nothing injected) is undefined — skipped, never counted as 0."""
        a = make_assemble(store, n_blocks=2)
        empty_mid = make_assemble(store, n_blocks=0)
        store.record_usage(a, block_ids_touched=[f"{a}:1"])
        store.record_usage(empty_mid, block_ids_touched=[])
        report = UsageAnalyzer(store).touched_share()
        assert report["skipped_no_blocks"] == 1
        assert report["calls_measured"] == 1
        assert report["touched_share"] == pytest.approx(0.5)
        assert report["status"] == "OK"

    def test_touched_share_garbage_json_skipped(self, store: MetricsStore):
        mid = make_assemble(store, n_blocks=2)
        conn = sqlite3.connect(store.db_path)
        conn.execute(
            "INSERT INTO usage_reports (metrics_id, block_ids_touched_json, tokens_out,"
            " wrong_tool_flag) VALUES (?,?,?,?)",
            (mid, "not-json{", 0, 0),
        )
        conn.commit()
        conn.close()
        report = UsageAnalyzer(store).touched_share()
        assert report["skipped_unreadable"] == 1
        assert report["calls_measured"] == 0
        assert report["status"] == "NO-DATA"  # no measurable reports — loud, not 0.0
        assert report["touched_share"] is None

    def test_wrong_tool_rate_exact(self, store: MetricsStore):
        mids = [make_assemble(store) for _ in range(3)]
        store.record_usage(mids[0], block_ids_touched=[], wrong_tool_flag=True)
        store.record_usage(mids[1], block_ids_touched=[])
        store.record_usage(mids[2], block_ids_touched=[])
        report = UsageAnalyzer(store).wrong_tool_rate()
        assert report["status"] == "OK"
        assert report["reports"] == 3
        assert report["wrong_tool_calls"] == 1
        assert report["wrong_tool_rate"] == pytest.approx(1 / 3)

    def test_wrong_tool_rate_no_data(self, store: MetricsStore):
        report = UsageAnalyzer(store).wrong_tool_rate()
        assert report["status"] == "NO-DATA"
        assert report["wrong_tool_rate"] is None
        assert report["reasons"]


class TestKappaGate:
    def test_block_id_boundary_128_accepted_129_refused(self, store):
        """The 128-char id limit is exact: 128 accepted, 129 refused."""
        mid = make_assemble(store)
        assert mid is not None
        assert store.record_usage(mid, block_ids_touched=["x" * 128]) is not None
        assert store.record_usage(mid, block_ids_touched=["x" * 129]) is None

    def test_frozen_preregistration_constants(self):
        assert kappa_calibration_pending() is True
        assert KAPPA_MIN == 0.6
        assert KAPPA_CI_FLOOR == 0.5
        assert KAPPA_PREREGISTRATION == "docs/experiments/touched-rate-kappa.md"
        assert (REPO_ROOT / KAPPA_PREREGISTRATION).exists()  # the frozen doc is in-tree
        # the doc must actually carry the frozen thresholds (a rewrite that
        # silently drops them would leave these constants lying)
        doc = (REPO_ROOT / KAPPA_PREREGISTRATION).read_text(encoding="utf-8")
        assert "0.6" in doc and "0.5" in doc, "frozen thresholds missing from the doc"


    def test_corridor_eligible_false_while_pending(self, store: MetricsStore):
        mid = make_assemble(store, n_blocks=2)
        store.record_usage(mid, block_ids_touched=[f"{mid}:0"])
        report = UsageAnalyzer(store).touched_share()
        assert report["kappa_calibration_pending"] is True
        assert report["corridor_eligible"] is False  # structural: corridors closed
        assert KAPPA_PENDING_REASON in report["reasons"]

    def test_no_data_paths_also_corridor_closed(self, store: MetricsStore):
        report = UsageAnalyzer(store).touched_share()
        assert report["corridor_eligible"] is False
        assert report["status"] == "NO-DATA"

    def test_analytics_carry_no_principal_columns(self, store: MetricsStore):
        mid = make_assemble(store)
        store.record_usage(mid, block_ids_touched=[f"{mid}:0"])
        a = UsageAnalyzer(store)
        for report in (a.assemble_usage_rate(), a.touched_share(), a.wrong_tool_rate()):
            assert not ({"session", "principal", "session_id", "user"} & set(report))
