"""Universal verb ledger + hourly rollup (phase A2).

record(surface, verb, status, latency_ms, project, agent, meta_json) — one row
per call on any surface (mcp | rest | background | cli). Ten collection
points on boundary surfaces (docs/architecture.md §3); the assemble_context
pipeline and the main store are never touched.
"""
