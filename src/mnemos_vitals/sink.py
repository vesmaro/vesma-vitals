"""MetricsStore — allowlist write path into the metrics.sqlite sidecar.

Phase A (docs/architecture.md §2-3): born-final schema, one insert per
assemble call + its blocks, keyed-HMAC fingerprints, non-fatal writes.

Host contract (the ONLY integration surface mnemos touches):
  - ``MetricsStore(path)`` — opens (and creates) the sidecar;
  - ``record_assemble(result, *, latency_ms)`` — one call after
    ``assemble_context`` built its result, at the hook/MCP boundary;
  - ``close()`` — idempotent shutdown.

Hard rules carried from the ArchCom decisions (7ec9dda3 + 061398fe):
  - write failure is non-fatal to the host: every public method swallows
    sqlite errors into a warning (TraceRecorder precedent) — a broken
    metrics plane must never break the memory server;
  - ``busy_timeout`` is 250 ms, not the prod store's 5000 — metric
    contention can never freeze the hook path;
  - the raw ``query`` is never persisted; ``file`` is stored as a stem;
  - the stats dict is never stored verbatim — an allowlisted projection
    (``_PROJECT_STAGE_STATS``) decides what stage telemetry survives;
  - content fingerprints are keyed HMAC under a per-install random key
    stored OUTSIDE the sidecar (no rotation — rotation breaks
    longitudinal uniqueness; plain hashes of governance content are
    banned, CWE-759).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mnemos_vitals.schema import (
    RETENTION_DAYS,
    SCHEMA_SQL,
    TABLE_NAMES,
)

logger = logging.getLogger("mnemos_vitals.sink")

#: The assembled text is fingerprinted, never stored. Shingling for the
#: dynamism corridor (phase B) re-derives fingerprints from the same
#: keyed HMAC — the raw text never crosses this boundary.
_SHINGLE_RE = re.compile(r"\w+", re.UNICODE)
_SHINGLE_SIZE = 5

#: Stage stats that survive into ``stage_stats_json`` (allowlist, C5
#: applied to the stats dict — it is NOT persisted verbatim). Key set
#: mirrors src/vesmaro/assemble.py stage builders as of main `3c8f270`
#: (2026-09-20); the projection is drift-tolerant: any key not listed
#: here — including raw ``recall.query`` and future keys — is dropped.
_STAGE_STATS_ALLOWLIST = frozenset(
    {
        "stages",  # list of stage names (order contract), capped
        # recall (query itself is raw text — never listed)
        "recall.query_source",
        "recall.candidates",
        "recall.admissible",
        "recall.content_type_filtered",
        "recall.content_type_fallbacks",
        "recall.applyto_pinned",
        # ccr stage
        "ccr.enabled",
        "ccr.markers_found",
        "ccr.expanded",
        "ccr.skipped_missing",
        "ccr.skipped_budget",
        "ccr.skipped_refused",
        # filter stage (profile names are enums, capped list)
        "filter.profiles",
        # scan / align / budget stages
        "scan.blocks_scanned",
        "scan.blocks_refused",
        "align.blocks_aligned",
        "align.moved_chars",
        "budget.blocks_included",
        "budget.blocks_skipped",
        # ADR-0025/0027 optional telemetry — present only when flags on
        "recall.lanes.rules",
        "recall.lanes.decisions",
        "recall.lanes.knowledge",
        "recall.lanes.governance_excluded_from_knowledge",
        "recall.lanes.task_filtered",
        "recall.type_boost.boosted",
        "recall.lens.name",
        "recall.lens.active",
        "task_scoped",
    }
)

#: String values allowed through the projection: short enums only.
_STR_VALUE_LIMIT = 32
_LIST_VALUE_LIMIT = 16
_ENUM_STR_PATHS = frozenset({"recall.query_source", "recall.lens.name"})


def _project_stage_stats(stats: dict) -> dict:
    """Allowlisted flattening of the assemble stats dict (never verbatim).

    Walks ``stats`` two levels deep into dotted ``stage.key`` paths and
    keeps only allowlisted paths with scalar values (numbers/bools), a
    capped list of profile names, or capped enum strings. Raw text —
    ``recall.query`` above all — is structurally absent from the
    allowlist, so no code path can leak it.
    """
    if not isinstance(stats, dict):
        return {}
    out: dict = {}
    for stage, payload in stats.items():
        if stage == "stages":
            if isinstance(payload, list) and len(payload) <= _LIST_VALUE_LIMIT:
                out["stages"] = [str(s)[:_STR_VALUE_LIMIT] for s in payload]
            continue
        if stage in _STAGE_STATS_ALLOWLIST and (
            payload is None or isinstance(payload, (int, float, bool))
        ):
            out[stage] = payload  # bare top-level scalars (task_scoped)
            continue
        if not isinstance(payload, dict):
            continue
        for key, value in payload.items():
            path = f"{stage}.{key}"
            self_ok = path in _STAGE_STATS_ALLOWLIST
            if self_ok and (value is None or isinstance(value, (int, float, bool))):
                out[path] = value
                continue
            if self_ok and path == "filter.profiles" and isinstance(value, list):
                out[path] = [str(p)[:_STR_VALUE_LIMIT] for p in value[:_LIST_VALUE_LIMIT]]
                continue
            if self_ok and path in _ENUM_STR_PATHS and isinstance(value, str):
                out[path] = value[:_STR_VALUE_LIMIT]
                continue
            if isinstance(value, dict):
                # sub-dicts (recall.lanes.*, recall.lens.*) — one more level
                for sub, sub_value in value.items():
                    sub_path = f"{path}.{sub}"
                    if sub_path not in _STAGE_STATS_ALLOWLIST:
                        continue  # drift-tolerant: unknown keys dropped
                    if sub_value is None or isinstance(sub_value, (int, float, bool)):
                        out[sub_path] = sub_value
                    elif sub_path in _ENUM_STR_PATHS and isinstance(sub_value, str):
                        out[sub_path] = sub_value[:_STR_VALUE_LIMIT]
            # anything else (raw text, deeper nesting) never leaves
    return out


class MetricsStore:
    """Allowlist sink into the sidecar — the guest's ONLY write path.

    Thread-safety mirrors the host's ``SQLiteStore`` bootstrap pattern
    (per-thread connections, a lock only for first-connect schema
    creation) so concurrent hook/MCP/background threads can record
    without serialising, while a broken sidecar degrades to a warning.
    """

    def __init__(self, db_path: Path, *, hmac_key: bytes | None = None) -> None:
        self.db_path = Path(db_path)
        self._local = threading.local()
        self._bootstrap_lock = threading.Lock()
        self._closed = False
        self._hmac_key = hmac_key if hmac_key is not None else self._load_or_create_key()

    # ── Key management (key lives OUTSIDE the sidecar) ───────────────────

    def _load_or_create_key(self) -> bytes:
        """Per-install random key, sibling file ``metrics.sqlite.hkey``.

        Never inside the sidecar DB: storing the key next to the
        fingerprints it protects would make them plain hashes. Not in
        config either — this file IS per-install identity for the
        plane. No rotation: rotating breaks longitudinal uniqueness of
        fingerprints (the dynamism corridor compares them across days).
        chmod 0600 (C2 parity with the main store).
        """
        key_path = self.db_path.parent / (self.db_path.name + ".hkey")
        try:
            if key_path.exists():
                key = key_path.read_bytes()
                if len(key) == 32:
                    return key
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            key = secrets.token_bytes(32)
            key_path.write_bytes(key)
            os.chmod(key_path, 0o600)
            return key
        except OSError as exc:
            logger.warning("vitals: hmac key unavailable (fingerprints disabled): %s", exc)
            return b""

    def fingerprint(self, text: str) -> str | None:
        """Keyed HMAC-SHA256 of ``text`` — the only trace of content.

        Returns ``None`` when no key is available (degraded, never fatal).
        """
        if not self._hmac_key:
            return None
        return hmac.new(self._hmac_key, text.encode("utf-8"), hashlib.sha256).hexdigest()

    def shingles(self, text: str) -> list[str]:
        """Keyed-HMAC word shingles (dynamism corridor input, phase B).

        Each shingle is HMAC'd with the same install key, so cross-request
        Jaccard is computable from the sidecar alone while raw text never
        enters it.
        """
        words = _SHINGLE_RE.findall(text)
        if not self._hmac_key:
            return []
        n = max(1, len(words) - _SHINGLE_SIZE + 1)
        raw = [" ".join(words[i : i + _SHINGLE_SIZE]) for i in range(n)]
        return [
            hmac.new(self._hmac_key, r.encode("utf-8"), hashlib.sha256).hexdigest()
            for r in raw
        ]

    # ── Connection (TraceRecorder-grade degradation) ──────────────────────

    def _conn(self) -> sqlite3.Connection | None:
        if self._closed:
            return None
        conn = getattr(self._local, "conn", None)
        if conn is None:
            with self._bootstrap_lock:
                conn = getattr(self._local, "conn", None)
                if conn is None:
                    try:
                        self.db_path.parent.mkdir(parents=True, exist_ok=True)
                        conn = sqlite3.connect(str(self.db_path), timeout=0.25)
                        conn.row_factory = sqlite3.Row
                        conn.execute("PRAGMA journal_mode=WAL")
                        conn.execute("PRAGMA busy_timeout=250")
                        for stmt in SCHEMA_SQL:
                            conn.execute(stmt)
                        conn.commit()
                        os.chmod(self.db_path, 0o600)  # C2 parity with the main store
                        self._local.conn = conn
                    except sqlite3.Error as exc:
                        logger.warning("vitals: sidecar unavailable (non-fatal): %s", exc)
                        return None
        return conn

    def _fail(self, op: str, exc: Exception) -> None:
        logger.warning("vitals: %s failed (non-fatal): %s", op, exc)

    # ── Phase A write path: the assemble domain ───────────────────────────

    def record_assemble(
        self,
        result: dict,
        *,
        verb_row_id: int | None = None,
        latency_ms: float | None = None,
    ) -> int | None:
        """Record one assemble call + its injected blocks. Non-fatal.

        ``result`` is the ContextBlock dict returned by mnemos
        ``assemble_context`` (see mnemos ``assemble.py``): ``text``,
        ``blocks`` (with memory_id/score/tokens/redactions/ccr_hashes),
        ``tokens`` and ``stats``. This is the ONLY place the host needs
        to call; the boundary rule (§3) keeps the assemble pipeline
        itself write-free — recording happens after the result exists.

        Returns the new ``assemble_metrics.id`` or ``None`` on failure.
        """
        try:
            conn = self._conn()
            if conn is None:
                return None
            ts = datetime.now(UTC).timestamp()
            tokens = result.get("tokens") or {}
            stats = result.get("stats") or {}
            blocks = result.get("blocks") or []
            file_val = result.get("file")
            session = result.get("session")
            project = result.get("project")
            mode = result.get("mode") or "sync"

            cur = conn.execute(
                "INSERT INTO assemble_metrics (verb_row_id, session, project, agent, ts,"
                " mode, budget, tokens_estimated, blocks_count, blocks_refused, redactions,"
                " ccr_expanded, query_source, file_stem, stage_stats_json, fingerprint)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    verb_row_id,
                    session,
                    project,
                    result.get("agent"),
                    ts,
                    mode,
                    tokens.get("budget", 0),
                    tokens.get("estimated", 0),
                    len(blocks),
                    (stats.get("scan") or {}).get("blocks_refused", 0),
                    sum(int(b.get("redactions") or 0) for b in blocks),
                    sum(1 for b in blocks if b.get("ccr_expanded")),
                    (stats.get("recall") or {}).get("query_source", "derived"),
                    Path(file_val).stem if file_val else None,  # stem only — never the path
                    json.dumps(_project_stage_stats(stats), separators=(",", ":")),
                    self.fingerprint(result.get("text") or ""),
                ),
            )
            metrics_id = int(cur.lastrowid)
            self._record_blocks(conn, metrics_id, blocks)
            conn.commit()
            return metrics_id
        except (sqlite3.Error, ValueError, TypeError, AttributeError) as exc:
            self._fail("record_assemble", exc)
            return None

    def _record_blocks(self, conn: sqlite3.Connection, metrics_id: int, blocks: list[dict]) -> None:
        for i, b in enumerate(blocks):
            conn.execute(
                "INSERT INTO injection_blocks (metrics_id, block_id, memory_id, source,"
                " score, tokens, ccr_origin) VALUES (?,?,?,?,?,?,?)",
                (
                    metrics_id,
                    f"{metrics_id}:{i}",  # opaque positional id (no content echo)
                    str(b.get("memory_id")),
                    str(b.get("content_type") or "memory"),
                    float(b.get("score") or 0.0),
                    int(b.get("tokens") or 0),
                    json.dumps(b.get("ccr_hashes") or [], separators=(",", ":")),
                ),
            )

    # ── Retention (C4: nightly DELETE + VACUUM; refusal to run = alert) ──

    def run_retention(self, *, now: datetime | None = None) -> dict[str, int]:
        """Apply per-table TTLs. Fail-loud to the CALLER (returns counts).

        The nightly job's failure is itself an alert (C4) — so unlike the
        write path, a retention exception propagates; a dict is returned
        on success with per-table deleted counts (0 is a healthy night).
        """
        now = now or datetime.now(UTC)
        conn = self._conn()
        if conn is None:
            raise RuntimeError("vitals: retention job cannot run — sidecar unavailable")
        deleted: dict[str, int] = {}
        # usage_reports/injection_blocks have no ts of their own — they are
        # deleted as children of assemble_metrics inside that branch.
        child_tables = {"injection_blocks", "usage_reports"}
        try:
            for table, days in RETENTION_DAYS.items():
                if table in child_tables:
                    continue  # counted implicitly via the parent delete
                if table == "verb_metrics_hourly":
                    cutoff = int((now - timedelta(days=days)).timestamp() // 3600)
                    cur = conn.execute(f"DELETE FROM {table} WHERE hour < ?", (cutoff,))
                elif table == "assemble_metrics":
                    cutoff = (now - timedelta(days=days)).timestamp()
                    # children first — their only time anchor is the parent's ts
                    conn.execute(
                        "DELETE FROM injection_blocks WHERE metrics_id IN"
                        " (SELECT id FROM assemble_metrics WHERE ts < ?)",
                        (cutoff,),
                    )
                    conn.execute(
                        "DELETE FROM usage_reports WHERE metrics_id IN"
                        " (SELECT id FROM assemble_metrics WHERE ts < ?)",
                        (cutoff,),
                    )
                    cur = conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
                else:
                    cutoff = (now - timedelta(days=days)).timestamp()
                    cur = conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
                deleted[table] = cur.rowcount
            conn.commit()
            conn.execute("VACUUM")
        finally:
            pass
        return deleted

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
                self._local.conn = None

    @property
    def tables(self) -> tuple[str, ...]:
        return TABLE_NAMES
