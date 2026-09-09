"""C1 isolation canary — the FIRST commit, before any runtime metric exists.

docs/architecture.md §8 + ADR-0026 C1: the metrics.sqlite sidecar must never
be reachable from bug-report / backup / export / federation paths. This test
pins the isolation structurally: the sidecar schema is an explicit allowlist,
every table is enumerated, and no column of any table may hold raw text.

It runs against the born-final schema constant (not a live DB) so the canary
cannot pass by accident of a missing file — the contract it checks is the
code, and the code is what ships.
"""

from __future__ import annotations

import re

import pytest

from mnemos_vitals.schema import TABLE_NAMES, TABLE_SCHEMAS

# Columns that may never exist in any sidecar table (privacy by structure,
# not by filter). "query" is never persisted; "content"/"text" columns would
# be raw memory payloads; principal/session columns are banned from verb
# tables by C3 — the ban is total because no table here needs them: the
# assemble domain keys by opaque metrics_id, not by principal.
FORBIDDEN_COLUMN_RE = re.compile(
    r"^(query|content|text|raw_text|prompt|body|principal|session_id|"
    r"user_agent|peer_id|ip|path|endpoint|matched_text)$"
)


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

    @pytest.mark.parametrize("table", list(TABLE_SCHEMAS))
    def test_no_raw_text_or_principal_column(self, table):
        for col in TABLE_SCHEMAS[table].columns:
            assert not FORBIDDEN_COLUMN_RE.match(
                col.name
            ), f"{table}.{col.name} violates the no-raw-text/no-principal rule"

    def test_sidecar_filename_is_fixed(self):
        from mnemos_vitals.schema import SIDECAR_FILENAME

        assert SIDECAR_FILENAME == "metrics.sqlite"

    def test_key_not_stored_in_sidecar(self):
        """The HMAC key must live outside the sidecar (keyed fingerprints)."""
        from mnemos_vitals.schema import SCHEMA_SQL

        assert "hmac_key" not in SCHEMA_SQL
        assert "key" not in {c.name for t in TABLE_SCHEMAS.values() for c in t.columns}
