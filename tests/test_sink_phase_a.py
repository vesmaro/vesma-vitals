"""Phase A contract tests — MetricsStore write path.

Every hard rule from the ArchCom decision (7ec9dda3) gets a mechanical
test here: non-fatal degradation, allowlisted stage stats, stem-only
file, keyed fingerprints, retention fail-loud, key isolation.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mnemos_vitals.sink import MetricsStore


def make_result(
    *,
    text: str = "block one\nblock two",
    n_blocks: int = 2,
    redactions: int = 1,
    file: str | None = "/long/path/to/module.py",
    query: str = "raw query text that must never be persisted",
) -> dict:
    blocks = [
        {
            "memory_id": f"mem-{i}",
            "content_type": "note",
            "score": 0.9 - i * 0.1,
            "tokens": 40 + i,
            "redactions": redactions if i == 0 else 0,
            "ccr_expanded": i == 1,
            "ccr_hashes": ["abc123"] if i == 1 else [],
            "content": f"RAW CONTENT {i} — must never reach the sidecar",
        }
        for i in range(n_blocks)
    ]
    return {
        "session": "sess-1",
        "project": "demo",
        "agent": "gcw-tech-lead",
        "file": file,
        "mode": "sync",
        "text": text,
        "blocks": blocks,
        "tokens": {"budget": 2000, "estimated": 120},
        "stats": {
            "stages": ["recall", "ccr", "filter", "scan", "align", "budget"],
            "recall": {
                "query": query,  # raw text — MUST NOT survive
                "query_source": "explicit",
                "candidates": 12,
                "admissible": 12,
                "content_type_filtered": 0,
                "content_type_fallbacks": 0,
                "applyto_pinned": 2,
            },
            "ccr": {
                "enabled": True,
                "markers_found": 2,
                "expanded": 1,
                "skipped_missing": 0,
                "skipped_budget": 0,
                "skipped_refused": 0,
            },
            "filter": {"profiles": ["log"]},
            "scan": {"blocks_scanned": 5, "blocks_refused": 3},
            "align": {"blocks_aligned": 2, "moved_chars": 140},
            "budget": {
                "budget": 2000,
                "estimated_tokens": 120,
                "blocks_included": 2,
                "blocks_skipped": 1,
            },
        },
    }


@pytest.fixture()
def store(tmp_path: Path) -> MetricsStore:
    s = MetricsStore(tmp_path / "metrics.sqlite")
    yield s
    s.close()


class TestRecordAssemble:
    def test_full_write_and_shape(self, store: MetricsStore):
        mid = store.record_assemble(make_result(), latency_ms=5.0)
        assert mid is not None
        conn = sqlite3.connect(store.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM assemble_metrics WHERE id=?", (mid,)).fetchone()
        assert row is not None
        assert row["session"] == "sess-1"
        assert row["project"] == "demo"
        assert row["mode"] == "sync"
        assert row["budget"] == 2000
        assert row["tokens_estimated"] == 120
        assert row["blocks_count"] == 2
        assert row["blocks_refused"] == 3
        assert row["redactions"] == 1
        assert row["ccr_expanded"] == 1
        assert row["query_source"] == "explicit"
        assert row["file_stem"] == "module"  # stem only — full path banned
        conn.close()

    def test_injection_blocks_written(self, store: MetricsStore):
        mid = store.record_assemble(make_result())
        conn = sqlite3.connect(store.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM injection_blocks WHERE metrics_id=? ORDER BY id", (mid,)
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["memory_id"] == "mem-0"
        assert rows[0]["source"] == "note"
        assert rows[0]["tokens"] == 40
        assert json.loads(rows[1]["ccr_origin"]) == ["abc123"]
        conn.close()

    def test_raw_query_never_persisted(self, store: MetricsStore):
        """The honesty core: grep the whole sidecar for the raw query."""
        mid = store.record_assemble(make_result(), latency_ms=1.0)
        assert mid is not None
        conn = sqlite3.connect(store.db_path)
        conn.row_factory = sqlite3.Row
        dump = "\n".join(
            str(tuple(r)) for t in store.tables for r in conn.execute(f"SELECT * FROM {t}")
        )
        assert "raw query text" not in dump
        assert "RAW CONTENT" not in dump  # block content either
        assert "/long/path/to/module.py" not in dump  # full path banned
        conn.close()

    def test_stage_stats_allowlisted_projection(self, store: MetricsStore):
        mid = store.record_assemble(make_result(), latency_ms=1.0)
        conn = sqlite3.connect(store.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT stage_stats_json FROM assemble_metrics WHERE id=?", (mid,)
        ).fetchone()
        stats = json.loads(row["stage_stats_json"])
        assert stats["recall.candidates"] == 12
        assert stats["recall.query_source"] == "explicit"
        assert "recall.query" not in stats  # raw key never survives
        assert all((isinstance(k, str) and "." in k) or k == "stages" for k in stats)
        conn.close()


class TestFingerprint:
    def test_keyed_and_stable(self, store: MetricsStore):
        fp1 = store.fingerprint("hello world")
        fp2 = store.fingerprint("hello world")
        fp3 = store.fingerprint("hello worlds")
        assert fp1 == fp2 != fp3

    def test_key_lives_outside_sidecar(self, tmp_path: Path):
        db = tmp_path / "metrics.sqlite"
        s = MetricsStore(db)
        s.record_assemble(make_result())
        assert (tmp_path / "metrics.sqlite.hkey").exists()
        assert (tmp_path / "metrics.sqlite.hkey").stat().st_mode & 0o777 == 0o600
        # the sidecar itself must not contain the key bytes
        raw = db.read_bytes()
        assert s._hmac_key.hex().encode() not in raw
        assert s._hmac_key not in raw
        s.close()

    def test_shingles_are_hmacs(self, store: MetricsStore):
        text = "alpha beta gamma delta epsilon zeta"
        sh = store.shingles(text)
        assert len(sh) >= 2
        joined = " ".join(sh)
        assert "alpha" not in joined  # no plaintext shingle material

    def test_fingerprint_survives_store_reopen(self, tmp_path: Path):
        db = tmp_path / "metrics.sqlite"
        s1 = MetricsStore(db)
        fp_a = s1.fingerprint("same text")
        s1.close()
        s2 = MetricsStore(db)
        fp_b = s2.fingerprint("same text")
        assert fp_a == fp_b  # no rotation: longitudinal uniqueness holds
        s2.close()


class TestNonFatalDegradation:
    def test_record_swallows_sqlite_error(self, tmp_path: Path):
        """A corrupted/unwritable sidecar degrades to None — host lives."""
        s = MetricsStore(tmp_path / "metrics.sqlite")
        s.close()  # closed store → _conn returns None
        assert s.record_assemble(make_result()) is None  # no exception

    def test_bad_result_shape_is_not_fatal(self, store: MetricsStore):
        # garbage shapes degrade to a warning — never a raise (host contract)
        for garbage in (
            {"tokens": "not-a-dict"},
            {},
            {"blocks": "garbage", "tokens": {}},
        ):
            try:
                store.record_assemble(garbage)
            except Exception:
                pytest.fail(f"record_assemble raised on {garbage!r}")


class TestRetention:
    def test_ttls_applied(self, store: MetricsStore, tmp_path: Path):
        import time

        mid = store.record_assemble(make_result())
        assert mid is not None
        # age the row: backdate its ts by 400 days
        conn = sqlite3.connect(store.db_path)
        conn.row_factory = sqlite3.Row
        old = time.time() - 400 * 86400
        conn.execute("UPDATE assemble_metrics SET ts=? WHERE id=?", (old, mid))
        conn.commit()
        conn.close()
        deleted = store.run_retention()
        assert deleted["assemble_metrics"] == 1
        conn = sqlite3.connect(store.db_path)
        conn.row_factory = sqlite3.Row
        n = conn.execute("SELECT COUNT(*) FROM assemble_metrics").fetchone()[0]
        assert n == 0
        conn.close()

    def test_fail_loud_on_missing_sidecar(self, tmp_path: Path):
        s = MetricsStore(tmp_path / "metrics.sqlite")
        s.close()
        with pytest.raises(RuntimeError, match="sidecar unavailable"):
            s.run_retention()


class TestPermissions:
    def test_sidecar_created_0600(self, tmp_path: Path):
        s = MetricsStore(tmp_path / "metrics.sqlite")
        s.record_assemble(make_result())
        mode = s.db_path.stat().st_mode & 0o777
        assert mode == 0o600  # C2 parity with the main store
        s.close()
