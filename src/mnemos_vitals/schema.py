"""Born-final sidecar schema — the allowlist is the whole contract.

Phase A/A2 (docs/architecture.md §2; ADR-0026 mnemos C1-C5). The metrics
sqlite sidecar is created with this FINAL schema on first open — no
migrations exist, by decision D-0002 (a sidecar with migrations would
re-create the very operational risk (migration trains) that the
sidecar exists to avoid).

Privacy is by structure, not by filter (ADR-0026):
  - no column anywhere may hold raw text: the recall ``query`` is never
    persisted, ``file`` is stored as a stem only, memory ``content`` never
    leaves the assemble pipeline;
  - the verb ledger has no session/principal columns — forever (C3);
  - content fingerprints are keyed HMAC under a per-install random key
    that is never stored in the sidecar (plain hashes of governance-class
    content are banned, CWE-759);
  - ``meta_json`` values pass an allowlist fail-closed (C5): unknown key
    → the write is refused, never silently dropped.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

SIDECAR_FILENAME = "metrics.sqlite"

#: Retention policy (days). Raw verb rows are cheap to lose; the hourly
#: rollup outlives them; the assemble family is the analysis corpus.
RETENTION_DAYS: dict[str, int] = {
    "verb_metrics": 30,
    "verb_metrics_hourly": 400,
    "assemble_metrics": 90,
    "injection_blocks": 90,
    "usage_reports": 90,
}


@dataclass(frozen=True)
class Column:
    name: str
    decl: str

    @property
    def is_integer_pk(self) -> bool:
        return "INTEGER PRIMARY KEY" in self.decl


@dataclass(frozen=True)
class TableSchema:
    name: str
    columns: tuple[Column, ...]

    @property
    def column_names(self) -> frozenset[str]:
        return frozenset(c.name for c in self.columns)

    @property
    def create_sql(self) -> str:
        body = ",\n    ".join(f"{c.name} {c.decl}" for c in self.columns)
        return f"CREATE TABLE IF NOT EXISTS {self.name} (\n    {body}\n)"


def _table(name: str, *cols: tuple[str, str]) -> TableSchema:
    return TableSchema(name=name, columns=tuple(Column(n, d) for n, d in cols))


# ── The five born-final tables ────────────────────────────────────────────────

#: Universal verb backbone — one row per call on any surface. C3: no
#: session/principal columns, ever; ``project``/``agent`` are slugs that
#: surface owners themselves declare, not principals.
VERB_METRICS = _table(
    "verb_metrics",
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("ts", "REAL NOT NULL"),
    ("surface", "TEXT NOT NULL CHECK (surface IN ('mcp','rest','background','cli'))"),
    ("verb", "TEXT NOT NULL"),
    ("status", "TEXT NOT NULL CHECK (status IN ('ok','error'))"),
    ("status_code", "INTEGER"),
    ("latency_ms", "REAL NOT NULL"),
    ("project", "TEXT"),
    ("agent", "TEXT"),
    ("meta_json", "TEXT"),
)

#: Hourly rollup — the only table the Prometheus exposer may read (survives
#: restarts; no per-event exposition). Percentiles are computed at rollup
#: time; ``avg`` is deliberately absent — means hide tails (§4).
VERB_HOURLY = _table(
    "verb_metrics_hourly",
    ("hour", "INTEGER NOT NULL"),  # unix epoch hours
    ("surface", "TEXT NOT NULL"),
    ("verb", "TEXT NOT NULL"),
    ("status", "TEXT NOT NULL"),
    ("project", "TEXT"),
    ("count", "INTEGER NOT NULL"),
    ("p50_ms", "REAL"),
    ("p95_ms", "REAL"),
    ("p99_ms", "REAL"),
    ("max_ms", "REAL"),
)

#: Specialized domain table of assemble calls (NOT a view over the ledger —
#: carries tokens, stage counts and keyed fingerprints). Linked to its verb
#: row when the call crossed a metered surface; the hook path may have no
#: verb row (hook-only writes are legit and the link stays NULL).
ASSEMBLE_METRICS = _table(
    "assemble_metrics",
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("verb_row_id", "INTEGER REFERENCES verb_metrics(id)"),
    ("session", "TEXT"),
    ("project", "TEXT NOT NULL"),
    ("agent", "TEXT"),
    ("ts", "REAL NOT NULL"),
    ("mode", "TEXT NOT NULL"),
    ("budget", "INTEGER NOT NULL"),
    ("tokens_estimated", "INTEGER NOT NULL"),
    ("blocks_count", "INTEGER NOT NULL"),
    ("blocks_refused", "INTEGER NOT NULL"),
    ("redactions", "INTEGER NOT NULL"),
    ("ccr_expanded", "INTEGER NOT NULL DEFAULT 0"),
    ("query_source", "TEXT NOT NULL CHECK (query_source IN ('explicit','derived'))"),
    ("file_stem", "TEXT"),
    ("stage_stats_json", "TEXT"),
    ("fingerprint", "TEXT"),  # keyed-HMAC of the assembled block text
)

#: One row per injected block — the composition plane (what the window was
#: made of). ``ccr_origin`` holds origin hashes of expanded markers; they
#: are already digests produced by the host, not sidecar secrets.
INJECTION_BLOCKS = _table(
    "injection_blocks",
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("metrics_id", "INTEGER NOT NULL REFERENCES assemble_metrics(id)"),
    ("block_id", "TEXT NOT NULL"),
    ("memory_id", "TEXT NOT NULL"),
    ("source", "TEXT NOT NULL"),
    ("score", "REAL NOT NULL"),
    ("tokens", "INTEGER NOT NULL"),
    ("ccr_origin", "TEXT"),
)

#: Phase C loop — one row per harness response. ``block_ids_touched_json``
#: is an allowlisted JSON array of opaque block ids, never text.
USAGE_REPORTS = _table(
    "usage_reports",
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("metrics_id", "INTEGER NOT NULL REFERENCES assemble_metrics(id)"),
    ("block_ids_touched_json", "TEXT"),
    ("tokens_out", "INTEGER"),
    ("wrong_tool_flag", "INTEGER"),
)

TABLE_SCHEMAS: dict[str, TableSchema] = {
    t.name: t
    for t in (VERB_METRICS, VERB_HOURLY, ASSEMBLE_METRICS, INJECTION_BLOCKS, USAGE_REPORTS)
}
TABLE_NAMES: tuple[str, ...] = tuple(TABLE_SCHEMAS)

INDEXES_SQL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_verb_ts ON verb_metrics(ts)",
    "CREATE INDEX IF NOT EXISTS idx_verb_surface_verb_ts ON verb_metrics(surface, verb, ts)",
    "CREATE INDEX IF NOT EXISTS idx_hourly_key"
    " ON verb_metrics_hourly(hour, surface, verb, status, project)",
    "CREATE INDEX IF NOT EXISTS idx_assemble_session_ts ON assemble_metrics(session, ts)",
    "CREATE INDEX IF NOT EXISTS idx_assemble_project_ts ON assemble_metrics(project, ts)",
    "CREATE INDEX IF NOT EXISTS idx_injection_metrics ON injection_blocks(metrics_id)",
    "CREATE INDEX IF NOT EXISTS idx_usage_metrics ON usage_reports(metrics_id)",
)

#: C5 — ``meta_json`` allowlist, fail-closed. Unknown key → the write is
#: refused with a warning, never silently dropped. Values are validated to
#: be scalar/JSON-safe by the sink before they ever reach SQLite.
META_ALLOWLIST: frozenset[str] = frozenset(
    {
        "error_type",  # exception CLASS name only — text never
        "budget",  # mcp-only: the requested budget
        "retry",  # int, background surfaces
        "exit_code",  # int, CLI surface
        "queue_depth",  # int, processor/federation points
        "items",  # int count, background points
        "rule_id",  # scanner/watcher rule identifier (id, not text)
        "detector",  # scanner detector NAME (id, not matched text)
        "peer_id",  # federation peer slug
        "trigger_code",  # federation pull code
        "counters",  # dict of small ints (e.g. ccr cleanup tallies)
    }
)

SCHEMA_SQL: tuple[str, ...] = tuple(
    [*(t.create_sql for t in TABLE_SCHEMAS.values()), *INDEXES_SQL]
)

#: Security invariant inputs — the exposer computes these from gates and
#: canaries, never from non-fatal telemetry (RL-S4).
__all__ = [
    "META_ALLOWLIST",
    "RETENTION_DAYS",
    "SCHEMA_SQL",
    "SIDECAR_FILENAME",
    "TABLE_NAMES",
    "TABLE_SCHEMAS",
]


# ── C5 meta gate (pure, enforced before any verb write) ──────────────────────

#: ``error_type`` must look like an exception CLASS name — exception text
#: never enters the sidecar.
_ERROR_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,63}$")
#: ``counters`` keys are identifier-capped (N4 — same drift-leak class the
#: stats projection closed).
_COUNTER_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")

_META_STR_LIMIT = 64
#: Keys whose values are integers by definition — strings refused.
_INT_ONLY_KEYS = frozenset({"exit_code", "retry", "queue_depth", "items", "budget"})


def validate_meta(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """C5 gate for verb ``meta_json`` — fail-closed, enforced.

    Returns the sanitised dict, or ``None`` when the meta must be
    REFUSED: unknown key (not in ``META_ALLOWLIST``), non-scalar value,
    non-finite float, over-long string, ``error_type`` that is not a
    class name, or a ``counters`` dict with bad keys/sizes. The caller
    logs the refusal — loud, never a silent drop, never fatal to the
    host.
    """
    if meta is None:
        return {}
    if not isinstance(meta, dict):
        return None
    clean: dict[str, Any] = {}
    for key, value in meta.items():
        if key not in META_ALLOWLIST:
            return None
        if key == "counters":
            # check BEFORE the scalar branch: bool/int would otherwise slip
            # past the dict validation (m6)
            if (
                not isinstance(value, dict)
                or len(value) > 16
                or not all(isinstance(k, str) and _COUNTER_KEY_RE.match(k) for k in value)
                or not all(
                    isinstance(v, int) and not isinstance(v, bool) and abs(v) <= 10**12
                    for v in value.values()
                )
            ):
                return None
            clean[key] = dict(value)
        elif value is None or isinstance(value, (bool, int)):
            clean[key] = value
        elif isinstance(value, float):
            if not math.isfinite(value):  # NaN/inf: json would emit garbage
                return None
            clean[key] = value
        elif isinstance(value, str) and len(value) <= _META_STR_LIMIT:
            if key == "error_type" and not _ERROR_TYPE_RE.match(value):
                return None
            if key in _INT_ONLY_KEYS:
                return None  # integer-by-definition key carrying a string
            clean[key] = value
        else:
            return None
    return clean
