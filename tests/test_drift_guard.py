"""Drift guard — the sink vs the LIVE assemble_context contract (W1.3).

The fixture below mirrors ``assemble_context`` in
``src/vesmaro/assemble.py`` (vesmaro main `3c8f270`, 2026-09-20): the
result keys, the block fields — including the ADR-0025 ``origin``
column and the optional ADR-0025-E1 ``lane`` field — and the stage
stats keys, including the optional lanes/lens/type_boost/task_scoped
telemetry. Synthetic ``*_future_*`` keys simulate additive drift that
has not happened yet: the sink must absorb the current shape without
loss of allowlisted fields, must never persist raw text, and must drop
unknown keys into the allowlist gap silently (that IS the contract).

When vesmaro changes the assemble shape, update this fixture AND the
projection allowlist in ``sink.py`` in the SAME commit — that keeps
phase-B analysis lossless.
"""

from __future__ import annotations

import json
import sqlite3

from mnemos_vitals.sink import MetricsStore

SECRET_QUERY = "SECRET-QUERY how does the payment retry loop work?"
SECRET_CONTENT = "SECRET-RULE-PAYLOAD rotation policy details"
SECRET_FUTURE = "FUTURE-LEAK-CARRYOVER"


def make_live_result() -> dict:
    """Full-fidelity assemble_context result, vesmaro main 3c8f270 shape."""
    blocks = [
        {
            "memory_id": "mem-rules-1",
            "project": "demo",
            "status": "published",
            "pipeline_phase": "refined",
            "origin": "fresh",
            "marker_version": 3,
            "score": 0.87,
            "search_type": "hybrid",
            "content_type": "rule",
            "provenance": (
                "[mnemos:mem-rules-1 project=demo status=published"
                " origin=fresh pipeline=refined v=3"
                " retrieved=2026-09-20T18:00:00+00:00]"
            ),
            "content": f"{SECRET_CONTENT} #1",
            "tokens": 64,
            "redactions": 1,
            "ccr_expanded": False,
            "ccr_hashes": [],
            "lane": "rules",
            "redacted_patterns": ["high-signal"],
        },
        {
            "memory_id": "mem-decision-7",
            "project": "demo",
            "status": "published",
            "pipeline_phase": None,
            "origin": "fresh",
            "marker_version": None,
            "score": 0.71,
            "search_type": "vector",
            "content_type": "decision",
            "provenance": (
                "[mnemos:mem-decision-7 project=demo status=published"
                " origin=fresh v=3 retrieved=2026-09-20T18:00:00+00:00]"
            ),
            "content": "decision content — never persisted",
            "tokens": 91,
            "redactions": 0,
            "ccr_expanded": True,
            "ccr_hashes": ["cafe1234", "beef5678"],
            "lane": "decisions",
        },
        {  # flag-off legacy shape: no lane/origin/marker fields at all
            "memory_id": "mem-note-2",
            "score": 0.55,
            "search_type": "fts",
            "content_type": "note",
            "provenance": "[mnemos:mem-note-2 project=demo status=raw]",
            "content": "plain note content — never persisted",
            "tokens": 33,
            "redactions": 0,
            "ccr_expanded": False,
            "ccr_hashes": [],
        },
    ]
    return {
        "session": "sess-live",
        "project": "demo",
        "agent": "gcw-tech-lead",
        "file": "/home/user/proj/src/main.py",
        "mode": "sync",
        "content_type": None,
        "text": "assembled text — never persisted",
        "blocks": blocks,
        "tokens": {"budget": 2000, "estimated": 180},
        "stats": {
            "stages": ["recall", "ccr", "filter", "scan", "align", "budget"],
            "recall": {
                "query": SECRET_QUERY,  # raw text — MUST NOT survive
                "query_source": "explicit",
                "candidates": 25,
                "admissible": 18,
                "content_type_filtered": 4,
                "content_type_fallbacks": 1,
                "applyto_pinned": 1,
                # ADR-0025-E1 lanes telemetry (flag-on)
                "lanes": {
                    "rules": 3,
                    "decisions": 2,
                    "knowledge": 13,
                    "governance_excluded_from_knowledge": 1,
                },
                # ADR-0027 Phase 0 lens telemetry (flag-on)
                "lens": {"name": "code", "active": True, "future": SECRET_FUTURE},
                "some_future_metric": 999,  # additive drift that hasn't happened
            },
            "ccr": {
                "enabled": True,
                "markers_found": 2,
                "expanded": 1,
                "skipped_missing": 0,
                "skipped_budget": 1,
                "skipped_refused": 0,
            },
            "filter": {"profiles": ["log", "terminal"]},
            "scan": {"blocks_scanned": 21, "blocks_refused": 3, "new_future_counter": 7},
            "align": {"blocks_aligned": 3, "moved_chars": 512},
            "budget": {
                "budget": 2000,
                "estimated_tokens": 180,
                "blocks_included": 3,
                "blocks_skipped": 4,
            },
            "task_scoped": True,
        },
    }


def _db_dump(store: MetricsStore) -> str:
    conn = sqlite3.connect(store.db_path)
    dump = "\n".join(
        str(tuple(r)) for t in store.tables for r in conn.execute(f"SELECT * FROM {t}")
    )
    conn.close()
    return dump


class TestLiveContractAbsorbed:
    def test_write_succeeds_and_rows_are_complete(self, tmp_path):
        store = MetricsStore(tmp_path / "metrics.sqlite")
        mid = store.record_assemble(make_live_result())
        assert mid is not None
        conn = sqlite3.connect(store.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM assemble_metrics WHERE id=?", (mid,)).fetchone()
        assert row["session"] == "sess-live"
        assert row["mode"] == "sync"
        assert row["budget"] == 2000
        assert row["tokens_estimated"] == 180
        assert row["blocks_count"] == 3
        assert row["blocks_refused"] == 3
        assert row["redactions"] == 1  # only block 1 carries redactions
        assert row["ccr_expanded"] == 1  # only block 2 was expanded
        assert row["query_source"] == "explicit"
        assert row["file_stem"] == "main"
        blocks = conn.execute(
            "SELECT * FROM injection_blocks WHERE metrics_id=? ORDER BY id", (mid,)
        ).fetchall()
        assert len(blocks) == 3
        assert [b["source"] for b in blocks] == ["rule", "decision", "note"]
        # Phase B: ccr_origin is {"hashes": [...], "block_fp": "<hmac>"}
        # (the born-final column pins its COLUMNS, not its payload shape);
        # a ccr-expanded block has origin hashes, a plain one has none.
        payload_b1 = json.loads(blocks[1]["ccr_origin"])
        assert payload_b1["hashes"] == ["cafe1234", "beef5678"]
        assert payload_b1["block_fp"] == store.fingerprint("decision content — never persisted")
        assert json.loads(blocks[0]["ccr_origin"]) == {
            "hashes": [],
            "block_fp": store.fingerprint(f"{SECRET_CONTENT} #1"),
        }
        conn.close()
        store.close()

    def test_stage_stats_current_keys_land(self, tmp_path):
        store = MetricsStore(tmp_path / "metrics.sqlite")
        mid = store.record_assemble(make_live_result())
        conn = sqlite3.connect(store.db_path)
        conn.row_factory = sqlite3.Row
        raw = conn.execute(
            "SELECT stage_stats_json FROM assemble_metrics WHERE id=?", (mid,)
        ).fetchone()["stage_stats_json"]
        stats = json.loads(raw)
        # core stage counters
        assert stats["recall.candidates"] == 25
        assert stats["recall.admissible"] == 18
        assert stats["scan.blocks_scanned"] == 21
        assert stats["scan.blocks_refused"] == 3
        assert stats["align.moved_chars"] == 512
        assert stats["ccr.skipped_budget"] == 1
        assert stats["filter.profiles"] == ["log", "terminal"]
        assert stats["budget.blocks_skipped"] == 4
        # optional lane/lens/task telemetry (flags on)
        assert stats["recall.lanes.knowledge"] == 13
        assert stats["recall.lens.name"] == "code"
        assert stats["recall.lens.active"] is True
        assert stats["task_scoped"] is True
        assert stats["stages"][0] == "recall"
        conn.close()
        store.close()

    def test_unknown_and_raw_keys_dropped(self, tmp_path):
        store = MetricsStore(tmp_path / "metrics.sqlite")
        mid = store.record_assemble(make_live_result())
        conn = sqlite3.connect(store.db_path)
        conn.row_factory = sqlite3.Row
        raw = conn.execute(
            "SELECT stage_stats_json FROM assemble_metrics WHERE id=?", (mid,)
        ).fetchone()["stage_stats_json"]
        stats = json.loads(raw)
        # future/additive drift lands in the allowlist gap — dropped
        assert "recall.some_future_metric" not in stats
        assert "scan.new_future_counter" not in stats
        assert "recall.lens.future" not in stats
        # raw text never survives — structural, not filter
        assert "recall.query" not in stats
        conn.close()
        store.close()

    def test_no_raw_text_or_provenance_anywhere_in_sidecar(self, tmp_path):
        store = MetricsStore(tmp_path / "metrics.sqlite")
        store.record_assemble(make_live_result())
        dump = _db_dump(store)
        for secret in (
            SECRET_QUERY,
            SECRET_CONTENT,
            SECRET_FUTURE,
            "retrieved=2026-09-20",  # provenance lines
            "[mnemos:mem-rules-1",
            "assembled text",  # result["text"] itself
        ):
            assert secret not in dump, f"raw material leaked: {secret!r}"
        store.close()
