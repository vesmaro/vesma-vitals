"""MetricsSink — allowlist write path into the metrics.sqlite sidecar.

Phase A/A2 contract (docs/architecture.md §2-3). NOT IMPLEMENTED YET.

Hard rules (security decisions C1-C5, mnemos 7ec9dda3 + 061398fe):
  - allowlist only: unknown meta_json key -> refuse the write (fail-closed,
    warning log), never silent drop;
  - no raw text columns anywhere in the schema; query is never persisted,
    file is persisted as stem only;
  - verb tables never get session/principal columns — forever;
  - keyed-HMAC fingerprints (per-install random key, not stored in the sidecar);
  - write failure is non-fatal to the host (busy_timeout 250 ms);
  - retention: verb rows 30 d, rollup ~400 d, assemble family 90 d; the
    nightly retention job failing loud is itself an alert.
"""
