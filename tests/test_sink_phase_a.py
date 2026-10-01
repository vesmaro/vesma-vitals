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

from vesma_vitals.sink import MetricsStore


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
        # Phase B payload shape: {"hashes": [...], "block_fp": "<hmac>"}
        payload = json.loads(rows[1]["ccr_origin"])
        assert payload["hashes"] == ["abc123"]
        assert payload["block_fp"] == store.fingerprint(
            "RAW CONTENT 1 — must never reach the sidecar"
        )
        assert payload["block_fp"] is not None  # HMAC hex, not a raw-text echo
        assert json.loads(rows[0]["ccr_origin"])["hashes"] == []
        assert json.loads(rows[0]["ccr_origin"])["block_fp"] is not None
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

    def test_corrupt_key_file_disables_without_rotation(self, tmp_path: Path):
        """N3: a corrupt key file is preserved (forensics) and fingerprints
        degrade to None — no silent regeneration, no accidental rotation."""
        db = tmp_path / "metrics.sqlite"
        key_path = tmp_path / "metrics.sqlite.hkey"
        key_path.write_bytes(b"short")  # corrupt: not 32 bytes
        s = MetricsStore(db)
        assert s.fingerprint("anything") is None  # degraded, not rekeyed
        assert key_path.read_bytes() == b"short"  # corrupt file untouched
        s.close()


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

    def test_filesystem_errors_do_not_reach_host(self, tmp_path: Path, monkeypatch):
        """M1: mkdir/chmod OSError (FUSE/NFS) must degrade, never raise."""
        s = MetricsStore(tmp_path / "metrics.sqlite")
        monkeypatch.setattr(
            "vesma_vitals.sink.os.chmod",
            lambda *a, **k: (_ for _ in ()).throw(OSError("simulated FUSE chmod")),
        )
        assert s.record_assemble(make_result()) is None  # no exception
        s.close()

    def test_failed_write_rolls_back_no_phantom_rows(self, store: MetricsStore):
        """M2: a mid-transaction failure must not commit a phantom row on
        the next call, and must not leave the write transaction open."""
        bad = make_result()
        bad["blocks"] = [{"memory_id": "m", "score": "NOT-A-FLOAT"}]  # float() raises
        assert store.record_assemble(bad) is None  # swallowed

        # the SINK's own connection must not hold the write transaction
        # (a fresh connection is never in a transaction — that proves nothing)
        assert not store._local.conn.in_transaction, "failed write left the tx open"

        conn = sqlite3.connect(store.db_path)
        n_assemble = conn.execute("SELECT COUNT(*) FROM assemble_metrics").fetchone()[0]
        n_blocks = conn.execute("SELECT COUNT(*) FROM injection_blocks").fetchone()[0]
        conn.close()
        assert n_assemble == 0, "phantom assemble row committed by a later call"
        assert n_blocks == 0, "orphan injection rows committed"

        # the next GOOD call commits exactly its own rows
        good_id = store.record_assemble(make_result())
        assert good_id is not None
        conn = sqlite3.connect(store.db_path)
        n_assemble = conn.execute("SELECT COUNT(*) FROM assemble_metrics").fetchone()[0]
        n_good_blocks = conn.execute(
            "SELECT COUNT(*) FROM injection_blocks WHERE metrics_id=?", (good_id,)
        ).fetchone()[0]
        conn.close()
        assert n_assemble == 1
        assert n_good_blocks == 2

    def test_missing_memory_id_gets_sentinel_not_none_string(self, store: MetricsStore):
        result = make_result()
        result["blocks"] = [{"content_type": "note", "score": 0.5, "tokens": 10}]
        mid = store.record_assemble(result)
        assert mid is not None
        conn = sqlite3.connect(store.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT memory_id FROM injection_blocks WHERE metrics_id=?", (mid,)
        ).fetchone()
        assert row["memory_id"] == "unknown"  # row parity kept, no "None" lie
        conn.close()


class TestMetaAllowlist:
    """m3: C5 meta gate is enforced in code before A2 wires record_verb."""

    def test_unknown_key_refuses(self):
        from vesma_vitals.sink import validate_meta

        assert validate_meta({"error_type": "ValueError"}) == {"error_type": "ValueError"}
        assert validate_meta({"nonsense_key": 1}) is None

    def test_error_type_must_be_class_name(self):
        from vesma_vitals.sink import validate_meta

        assert validate_meta({"error_type": "sqlite3.OperationalError"}) is not None
        assert validate_meta({"error_type": "boom: detail text"}) is None

    def test_counters_are_small_int_dict(self):
        from vesma_vitals.sink import validate_meta

        assert validate_meta({"counters": {"ttl_deleted": 3, "lru_evicted": 1}}) is not None
        assert validate_meta({"counters": {"bad": "text"}}) is None
        assert validate_meta({"counters": {"bad": True}}) is None

    def test_nonscalar_and_overlong_refuse(self):
        from vesma_vitals.sink import validate_meta

        assert validate_meta({"peer_id": ["list"]}) is None
        assert validate_meta({"peer_id": "x" * 65}) is None
        assert validate_meta("not-a-dict") is None
        assert validate_meta(None) == {}


class TestRetention:
    def test_ttl_constants_pinned(self):
        """m4: a TTL drift must fail here, not silently in production."""
        from vesma_vitals.schema import RETENTION_DAYS

        assert RETENTION_DAYS == {
            "verb_metrics": 30,
            "verb_metrics_hourly": 400,
            "assemble_metrics": 90,
            "injection_blocks": 90,
            "usage_reports": 90,
        }

    def test_ttls_applied_with_child_cascade(self, store: MetricsStore):
        import time

        mid = store.record_assemble(make_result())
        assert mid is not None
        # age the row: backdate its ts by 400 days
        conn = sqlite3.connect(store.db_path)
        conn.row_factory = sqlite3.Row
        old = time.time() - 400 * 86400
        conn.execute("UPDATE assemble_metrics SET ts=? WHERE id=?", (old, mid))
        # a usage row hangs off the same (now-stale) parent — phase C shape
        conn.execute(
            "INSERT INTO usage_reports (metrics_id, block_ids_touched_json,"
            " tokens_out, wrong_tool_flag) VALUES (?, '[]', 5, 0)",
            (mid,),
        )
        conn.commit()
        deleted = store.run_retention()
        assert deleted["assemble_metrics"] == 1
        counts = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("assemble_metrics", "injection_blocks", "usage_reports")
        }
        assert counts == {
            "assemble_metrics": 0,
            "injection_blocks": 0,  # m4: the C4 cascade is actually asserted
            "usage_reports": 0,
        }
        conn.close()

    def test_fail_loud_on_missing_sidecar(self, tmp_path: Path):
        s = MetricsStore(tmp_path / "metrics.sqlite")
        s.close()
        with pytest.raises(RuntimeError, match="sidecar unavailable"):
            s.run_retention()


class TestPermissions:
    def test_sidecar_created_0600_all_three_files(self, tmp_path: Path):
        """M3: the sidecar footprint is db + wal + shm — all 0600 (C2)."""
        s = MetricsStore(tmp_path / "metrics.sqlite")
        s.record_assemble(make_result())
        for name in ("metrics.sqlite", "metrics.sqlite-wal", "metrics.sqlite-shm"):
            p = tmp_path / name
            assert p.exists(), f"{name} missing"
            assert p.stat().st_mode & 0o777 == 0o600, f"{name} is not 0600"
        s.close()
