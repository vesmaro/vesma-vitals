"""C1 isolation canary — the FIRST commit, before any runtime metric exists.

docs/architecture.md §8 + ADR-0026 C1: the metrics.sqlite sidecar must never
be reachable from bug-report / backup / export / federation paths. This test
pins the isolation structurally: the sidecar schema is an explicit allowlist,
every table's exact column set is pinned (any schema change at all fails the
canary and forces a fresh born-final decision — that is what "forever"
means), and no column of any table may hold raw text.

The mnemos-side of C1 (bug-report/backup/export/federation paths exclude the
sidecar) lands with the integration PR in the vesmaro repo — this file pins
the library contract that integration must preserve.

It runs against the born-final schema constant (not a live DB) so the canary
cannot pass by accident of a missing file — the contract it checks is the
code, and the code is what ships.
"""

from __future__ import annotations

from mnemos_vitals.schema import SCHEMA_SQL, TABLE_NAMES, TABLE_SCHEMAS

# Exact column names that may never exist in ANY sidecar table (privacy by
# structure, not by filter): raw-text carriers, credentials, principals.
# Exact-match on purpose — "tokens" (a counter column) must NOT trip the
# "token" ban, while a future "token"/"hmac_key" column must.
FORBIDDEN_COLUMNS = frozenset(
    {
        "query",
        "content",
        "text",
        "raw_text",
        "prompt",
        "body",
        "snippet",
        "matched_text",
        "provenance",
        "principal",
        "session_id",
        "user",
        "user_id",
        "user_agent",
        "peer_id",
        "ip",
        "path",
        "endpoint",
        "key",
        "hmac_key",
        "api_key",
        "secret",
        "token",
        "password",
        "credential",
    }
)

#: C3 "forever": session columns live ONLY in the per-request assemble
#: domain table (and even there gated by config-lint at integration).
SESSION_BEARING_TABLES = frozenset({"assemble_metrics"})

#: Born-final pin — the exact column tuple of every table, in order. ANY
#: drift (added/renamed/reordered column) fails the canary: schema
#: evolution means a new sidecar file, never an ALTER (D-0002).
EXPECTED_COLUMNS: dict[str, tuple[str, ...]] = {
    "verb_metrics": (
        "id",
        "ts",
        "surface",
        "verb",
        "status",
        "status_code",
        "latency_ms",
        "project",
        "agent",
        "meta_json",
    ),
    "verb_metrics_hourly": (
        "hour",
        "surface",
        "verb",
        "status",
        "project",
        "count",
        "p50_ms",
        "p95_ms",
        "p99_ms",
        "max_ms",
    ),
    "assemble_metrics": (
        "id",
        "verb_row_id",
        "session",
        "project",
        "agent",
        "ts",
        "mode",
        "budget",
        "tokens_estimated",
        "blocks_count",
        "blocks_refused",
        "redactions",
        "ccr_expanded",
        "query_source",
        "file_stem",
        "stage_stats_json",
        "fingerprint",
    ),
    "injection_blocks": (
        "id",
        "metrics_id",
        "block_id",
        "memory_id",
        "source",
        "score",
        "tokens",
        "ccr_origin",
    ),
    "usage_reports": (
        "id",
        "metrics_id",
        "block_ids_touched_json",
        "tokens_out",
        "wrong_tool_flag",
    ),
}


class TestC1Canary:
    def test_all_five_tables_born_final(self):
        assert set(TABLE_NAMES) == {
            "verb_metrics",
            "verb_metrics_hourly",
            "assemble_metrics",
            "injection_blocks",
            "usage_reports",
        }

    def test_no_migrations_exist(self):
        from mnemos_vitals import schema

        assert not hasattr(schema, "MIGRATIONS")
        assert not hasattr(schema, "run_migrations")

    def test_columns_pinned_born_final(self):
        """Any schema change at all must fail here and force a decision."""
        for table, expected in EXPECTED_COLUMNS.items():
            actual = tuple(c.name for c in TABLE_SCHEMAS[table].columns)
            assert actual == expected, (
                f"{table} drifted from the born-final pin: {actual}. "
                "Schema evolution means a NEW sidecar file, never an ALTER."
            )

    def test_no_raw_text_or_credential_column(self):
        for table in TABLE_SCHEMAS:
            for col in TABLE_SCHEMAS[table].columns:
                assert col.name not in FORBIDDEN_COLUMNS, (
                    f"{table}.{col.name} violates the no-raw-text/no-credential rule"
                )

    def test_session_columns_only_in_assemble_domain(self):
        """C3, enforced — not just declared: verb tables never get session."""
        for table in TABLE_SCHEMAS:
            names = TABLE_SCHEMAS[table].column_names
            if table not in SESSION_BEARING_TABLES:
                assert "session" not in names, f"C3: {table} must never carry session"
            else:
                assert "session" in names  # the pin above keeps it honest

    def test_sidecar_filename_is_fixed(self):
        from mnemos_vitals.schema import SIDECAR_FILENAME

        assert SIDECAR_FILENAME == "metrics.sqlite"

    def test_key_not_stored_in_sidecar(self):
        """The HMAC key must live outside the sidecar (keyed fingerprints)."""
        joined_sql = " ".join(SCHEMA_SQL)
        assert "hmac_key" not in joined_sql
        assert "secret" not in joined_sql.lower()
        banned = {"key", "hmac_key", "api_key", "secret", "token", "password"}
        present = {c.name for t in TABLE_SCHEMAS.values() for c in t.columns}
        assert not (banned & present), f"key-ish column leaked into schema: {banned & present}"
