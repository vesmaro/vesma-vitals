"""Universal verb ledger + hourly rollup (phase A2).

record(surface, verb, status, latency_ms, project, agent, meta_json) — one row
per call on any surface (mcp | rest | background | cli). Ten collection
points on boundary surfaces (docs/architecture.md §3); the assemble_context
pipeline and the main store are never touched.

A2 wiring gates (from the 2026-09-20 review, verdict APPROVE):
  - every meta must route through mnemos_vitals.sink.validate_meta() —
    refusal is loud, never silent, never fatal;
  - validate_meta must, when record_verb lands, also validate `counters`
    KEYS (identifier charset + length + entry-count caps — same drift-leak
    class as m5) and refuse NaN/inf floats (json non-standard);
  - rollup: exact quantiles per (hour, surface, verb, status, project),
    INSERT OR REPLACE per hour (idempotent re-runs).
"""
