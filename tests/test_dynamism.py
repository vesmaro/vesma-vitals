"""Phase B contract tests — dynamism zone (zone 4, methodology §3.4).

Mechanical checks for every hard rule: static boundary at exactly 80%,
ratio = 1 - static_share, explicit_hint_share from stage stats,
deterministic pair sampling, corridor-gate statuses (PASS/FAIL/NO-DATA),
the structural falsifier, and the privacy invariant — raw text never
lands in the sidecar (whole-DB grep, test_drift_guard pattern).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mnemos_vitals.dynamism import (
    PAIR_FULL_LIMIT,
    PAIR_SAMPLE_CAP,
    DynamismAnalyzer,
    corridor_gate,
    falsifier_level,
)
from mnemos_vitals.sink import MetricsStore

SECRET_BLOCK = "SECRET-RULE dynamism test block content"


def make_result(
    session: str,
    *,
    contents: list[str],
    memory_ids: list[str] | None = None,
    query_source: str = "derived",
    project: str = "demo",
) -> dict:
    """One assemble_context result with the given block CONTENTS."""
    ids = memory_ids or [f"mem-{i}" for i in range(len(contents))]
    blocks = [
        {
            "memory_id": ids[i],
            "content_type": "note",
            "score": 0.9,
            "tokens": 40,
            "redactions": 0,
            "ccr_expanded": False,
            "ccr_hashes": [],
            "content": contents[i],
        }
        for i in range(len(contents))
    ]
    return {
        "session": session,
        "project": project,
        "agent": "gcw-tech-lead",
        "file": None,
        "mode": "sync",
        "text": "assembled text — never persisted",
        "blocks": blocks,
        "tokens": {"budget": 2000, "estimated": 120},
        "stats": {
            "stages": ["recall", "budget"],
            "recall": {"query": "raw query text", "query_source": query_source},
        },
    }


def record_session(
    store: MetricsStore,
    session: str,
    block_sets: list[list[str]],
    *,
    query_sources: list[str] | None = None,
    memory_ids: list[str] | None = None,
    project: str = "demo",
) -> list[int | None]:
    """Record one assembly per element of ``block_sets``."""
    sources = query_sources or ["explicit"] * len(block_sets)
    return [
        store.record_assemble(
            make_result(
                session,
                contents=blocks,
                memory_ids=memory_ids,
                query_source=sources[i],
                project=project,
            )
        )
        for i, blocks in enumerate(block_sets)
    ]


@pytest.fixture()
def store(tmp_path: Path) -> MetricsStore:
    s = MetricsStore(tmp_path / "metrics.sqlite")
    yield s
    s.close()


@pytest.fixture()
def analyzer(store: MetricsStore) -> DynamismAnalyzer:
    return DynamismAnalyzer(store)


class TestStaticShareBoundary:
    """The 80% boundary is EXACT: 4/5 assemblies => static, 3/5 => not."""

    def test_four_of_five_is_static(self, store: MetricsStore, analyzer: DynamismAnalyzer):
        # One distinct block in 4 of 5 assemblies, a fresh one in the 5th.
        record_session(
            store,
            "sess-a",
            [
                ["shared block"],
                ["shared block"],
                ["shared block"],
                ["shared block"],
                ["unique block"],
            ],
        )
        report = analyzer.session_report("sess-a")
        assert report["assemblies"] == 5
        # Per-pair: "shared" appears 4/5 => static; "unique" 1/5 => not.
        # Aggregate over block occurrences: 4 static of 5 total => 0.8.
        assert report["static_share"] == pytest.approx(0.8)
        assert report["context_dynamism_ratio"] == pytest.approx(0.2)

    def test_three_of_five_is_not_static(self, store: MetricsStore, analyzer: DynamismAnalyzer):
        record_session(
            store,
            "sess-b",
            [
                ["shared block"],
                ["shared block"],
                ["shared block"],
                ["unique one"],
                ["unique two"],
            ],
        )
        report = analyzer.session_report("sess-b")
        # 3/5 = 0.6 < 0.80 => NO static pair at all.
        assert report["static_share"] == 0.0
        assert report["context_dynamism_ratio"] == 1.0

    def test_exactly_at_threshold_counts_as_static(
        self, store: MetricsStore, analyzer: DynamismAnalyzer
    ):
        # 4/5 == 0.80 exactly — >= is the canon wording.
        record_session(
            store,
            "sess-c",
            [["only block"]] * 4 + [["other block"]],
        )
        report = analyzer.session_report("sess-c")
        # 4/5 == 0.80 exactly => the pair IS static (canon: >= 80%); but
        # the 5th assembly's other block keeps the occurrence share at 4/5.
        assert report["static_share"] == pytest.approx(0.8)
        assert report["context_dynamism_ratio"] == pytest.approx(0.2)
        assert report["static_pairs"] == 1

    def test_memory_id_matters_not_only_text(
        self, store: MetricsStore, analyzer: DynamismAnalyzer
    ):
        # Same text under DIFFERENT memory_ids is a different pair.
        for i in range(5):
            store.record_assemble(
                make_result("sess-d", contents=["same text"], memory_ids=[f"mem-{i}"])
            )
        report = analyzer.session_report("sess-d")
        assert report["static_share"] == 0.0  # 5 distinct pairs, none >= 80%
        assert report["distinct_pairs"] == 5

    def test_disjoint_blocks_full_dynamism(
        self, store: MetricsStore, analyzer: DynamismAnalyzer
    ):
        record_session(
            store,
            "sess-e",
            [[f"block {i} unique {i}"] for i in range(5)],
        )
        report = analyzer.session_report("sess-e")
        assert report["static_share"] == 0.0
        assert report["context_dynamism_ratio"] == 1.0
        assert report["pair_uniqueness_mean"] == 1.0  # disjoint => Jaccard 0
        assert report["zero_uniqueness_pair_share"] == 0.0


class TestExplicitHintShare:
    def test_explicit_hint_share_from_stage_stats(
        self, store: MetricsStore, analyzer: DynamismAnalyzer
    ):
        record_session(
            store,
            "sess-hint",
            [[f"b{i}"] for i in range(4)],
            query_sources=["explicit", "derived", "explicit", "derived"],
        )
        report = analyzer.session_report("sess-hint")
        assert report["explicit_hint_share"] == 0.5

    def test_zero_explicit_is_a_value_not_missing(
        self, store: MetricsStore, analyzer: DynamismAnalyzer
    ):
        record_session(
            store,
            "sess-all-derived",
            [[f"b{i}"] for i in range(3)],
            query_sources=["derived"] * 3,
        )
        report = analyzer.session_report("sess-all-derived")
        assert report["explicit_hint_share"] == 0.0  # present, not None


class TestPairSampling:
    def test_small_session_full_pairs(self, store: MetricsStore, analyzer: DynamismAnalyzer):
        record_session(store, "sess-p", [[f"b{i}"] for i in range(5)])
        report = analyzer.session_report("sess-p")
        assert report["pairs_sampled"] == 10  # C(5,2)
        assert report["sampling"] == "full"

    def test_large_session_sampled_and_capped(
        self, store: MetricsStore, analyzer: DynamismAnalyzer
    ):
        n = PAIR_FULL_LIMIT + 15  # 35 assemblies => C(35,2) = 595 pairs
        record_session(store, "sess-big", [[f"b{i}"] for i in range(n)])
        report = analyzer.session_report("sess-big")
        assert report["pairs_sampled"] == PAIR_SAMPLE_CAP
        assert "deterministic" in report["sampling"]

    def test_same_session_twice_same_pairs(
        self, store: MetricsStore, analyzer: DynamismAnalyzer
    ):
        n = PAIR_FULL_LIMIT + 15
        record_session(store, "sess-det", [[f"b{i}"] for i in range(n)])
        first = analyzer.session_report("sess-det")
        second = analyzer.session_report("sess-det")
        assert first == second  # full report equality: no RNG state anywhere

    def test_sampling_never_includes_invalid_pairs(
        self, store: MetricsStore, analyzer: DynamismAnalyzer
    ):
        n = PAIR_FULL_LIMIT + 15
        record_session(store, "sess-valid", [[f"b{i}"] for i in range(n)])
        report = analyzer.session_report("sess-valid")
        # mean of sampled uniqueness stays a float in [0, 1]
        mean = report["pair_uniqueness_mean"]
        assert isinstance(mean, float) and 0.0 <= mean <= 1.0


class TestCorridorGate:
    def test_pass_path(self):
        report = {
            "assemblies": 5,
            "context_dynamism_ratio": 0.9,
            "zero_uniqueness_pair_share": 0.0,
            "explicit_hint_share": 0.2,
        }
        verdict = corridor_gate(report, min_dynamism=0.5, max_zero_share=0.2)
        assert verdict["status"] == "PASS"
        assert verdict["reasons"] == []

    def test_fail_on_low_dynamism(self):
        report = {
            "assemblies": 5,
            "context_dynamism_ratio": 0.1,
            "zero_uniqueness_pair_share": 0.0,
            "explicit_hint_share": 0.2,
        }
        verdict = corridor_gate(report, min_dynamism=0.5, max_zero_share=0.2)
        assert verdict["status"] == "FAIL"
        assert any("context_dynamism_ratio" in r for r in verdict["reasons"])

    def test_fail_on_zero_share(self):
        report = {
            "assemblies": 5,
            "context_dynamism_ratio": 0.9,
            "zero_uniqueness_pair_share": 0.7,
            "explicit_hint_share": 0.2,
        }
        verdict = corridor_gate(report, min_dynamism=0.5, max_zero_share=0.2)
        assert verdict["status"] == "FAIL"
        assert any("zero_uniqueness_pair_share" in r for r in verdict["reasons"])

    def test_no_data_when_single_assembly(self):
        report = {
            "assemblies": 1,
            "context_dynamism_ratio": 0.9,
            "zero_uniqueness_pair_share": 0.0,
            "explicit_hint_share": 0.0,
        }
        assert corridor_gate(report, min_dynamism=0.5, max_zero_share=0.2)["status"] == "NO-DATA"

    def test_no_data_when_metrics_undefined(self):
        report = {"assemblies": 5, "explicit_hint_share": None}
        assert corridor_gate(report, min_dynamism=0.5, max_zero_share=0.2)["status"] == "NO-DATA"

    def test_refuses_to_guess_thresholds(self):
        with pytest.raises(ValueError, match="preregister"):
            corridor_gate({"assemblies": 5}, min_dynamism=None, max_zero_share=None)


class TestFalsifierHook:
    def test_falsifier_level_is_zero_pair_share(
        self, store: MetricsStore, analyzer: DynamismAnalyzer
    ):
        # 5 identical assemblies => every pair has zero uniqueness.
        record_session(store, "sess-f", [["same block"] for _ in range(5)])
        report = analyzer.session_report("sess-f")
        assert falsifier_level(report) == 1.0
        assert report["zero_uniqueness_pair_share"] == 1.0
        assert report["pair_uniqueness_mean"] == 0.0

    def test_falsifier_none_without_pairs(self, analyzer: DynamismAnalyzer):
        assert falsifier_level(analyzer.session_report("sess-empty")) is None


class TestNoDataPaths:
    def test_unknown_session_is_no_data(self, analyzer: DynamismAnalyzer):
        report = analyzer.session_report("sess-nonexistent")
        assert report["status"] == "NO-DATA"
        assert report["assemblies"] == 0
        assert report["static_share"] is None  # never zero — loud

    def test_single_assembly_yields_no_uniqueness(
        self, store: MetricsStore, analyzer: DynamismAnalyzer
    ):
        record_session(store, "sess-solo", [["b0"]])
        report = analyzer.session_report("sess-solo")
        assert report["assemblies"] == 1
        assert report["pairs_sampled"] == 0
        assert report["pair_uniqueness_mean"] is None
        assert report["zero_uniqueness_pair_share"] is None
        assert report["static_share"] is not None  # static axis still defined

    def test_pre_phase_b_rows_degrade_loud(
        self, store: MetricsStore, analyzer: DynamismAnalyzer
    ):
        """Legacy ccr_origin payload (JSON array) => NO-DATA, never zero."""
        record_session(store, "sess-legacy", [["b0"], ["b1"]])
        conn = sqlite3.connect(store.db_path)
        conn.execute(
            "UPDATE injection_blocks SET ccr_origin = ?",
            (json.dumps(["cafe1234"]),),
        )
        conn.commit()
        conn.close()
        report = analyzer.session_report("sess-legacy")
        assert report["status"] == "NO-DATA"
        assert "pre-phase-B corpus" in " ".join(report["reasons"])
        assert report["static_share"] is None

    def test_mixed_legacy_and_new_rows_still_computable(
        self, store: MetricsStore, analyzer: DynamismAnalyzer
    ):
        """Half the corpus pre-upgrade: fingerprint-bearing rows still count."""
        record_session(store, "sess-mixed", [["b0"], ["b1"], ["b0"], ["b0"]])
        conn = sqlite3.connect(store.db_path)
        conn.execute(
            "UPDATE injection_blocks SET ccr_origin = ? WHERE metrics_id IN (1, 2)",
            (json.dumps(["cafe1234"]),),  # legacy shape on the first two
        )
        conn.commit()
        conn.close()
        report = analyzer.session_report("sess-mixed")
        # Assemblies 3-4 share (mem-0, fp(b0)) => present in 2/4 = 0.5 < 0.8
        assert report["static_share"] == 0.0
        assert report["status"] == "OK"


class TestNonFatal:
    def test_broken_sidecar_degrades_not_raises(self, tmp_path: Path):
        store = MetricsStore(tmp_path / "metrics.sqlite")
        analyzer = DynamismAnalyzer(store)
        store.close()  # _conn() now returns None
        report = analyzer.session_report("sess-x")
        assert report["status"] == "NO-DATA"
        assert any("degraded" in r for r in report["reasons"])

    def test_project_scoping_reads_only_matching_rows(
        self, store: MetricsStore, analyzer: DynamismAnalyzer
    ):
        record_session(store, "sess-multi", [["shared"] * 1], project="alpha")
        record_session(store, "sess-multi", [["other"] * 1], project="beta")
        alpha = analyzer.session_report("sess-multi", project="alpha")
        assert alpha["assemblies"] == 1
        unscoped = analyzer.session_report("sess-multi")
        assert unscoped["assemblies"] == 2


class TestPrivacy:
    def test_no_raw_text_anywhere_in_sidecar(self, store: MetricsStore):
        """Whole-DB grep (drift-guard pattern): only keyed HMACs land."""
        record_session(store, "sess-priv", [[SECRET_BLOCK, "plain second block"]])
        conn = sqlite3.connect(store.db_path)
        dump = "\n".join(
            str(tuple(r)) for t in store.tables for r in conn.execute(f"SELECT * FROM {t}")
        )
        conn.close()
        assert SECRET_BLOCK not in dump
        assert "plain second block" not in dump
        assert "assembled text" not in dump
        # and the fingerprint DID land (the analyzer has something to read)
        payload = json.loads(
            sqlite3.connect(store.db_path)
            .execute("SELECT ccr_origin FROM injection_blocks LIMIT 1")
            .fetchone()[0]
        )
        assert payload["block_fp"] == store.fingerprint(SECRET_BLOCK)
        assert len(payload["block_fp"]) == 64  # sha256 hex

    def test_analyzer_never_writes(self, store: MetricsStore, analyzer: DynamismAnalyzer):
        record_session(store, "sess-ro", [["b0"], ["b1"]])
        conn = sqlite3.connect(store.db_path)
        before = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in store.tables
        }
        conn.close()
        analyzer.session_report("sess-ro")
        conn = sqlite3.connect(store.db_path)
        after = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in store.tables
        }
        conn.close()
        assert before == after
