# Changelog

## [Unreleased]

## [0.1.0] — 2026-10-04

First public release of vesma-vitals: honest measurement methodology and
instrumentation for the vesma memory server (ADR-0026), released as a
standalone pip-installable library.

### Added

- Phase A library core: born-final sidecar schema (`schema.py` — five
  tables, allowlist meta_json, retention constants) and `MetricsStore`
  (`sink.py`) — the passive-collection write path with non-fatal
  degradation, 250 ms busy_timeout, keyed-HMAC fingerprints (key outside
  the sidecar, no rotation), allowlisted stage-stats projection and
  fail-loud nightly retention.
- C1 isolation canary — FIRST implementation commit, before any runtime
  metric exists (ADR-0026).
- Claims-ledger seeded (`docs/claims.md`) per methodology §6.
- Stage-stats allowlist synced to the live vesmaro assemble shape
  (drift found by code inspection: 6 counters were silently lost);
  drift-guard test pins the live result shape incl. ADR-0025/0027
  telemetry and synthetic future keys.
- Review hardening (REQUEST-CHANGES slice fully addressed): OSError
  can no longer reach the host; failed writes roll back (no phantom
  rows / open transactions); -wal/-shm pinned 0600 (C2, three files);
  born-final column pin + explicit C3 session ban in the canary;
  validate_meta() lands the C5 gate ahead of phase A2; retention
  constants pinned, child cascade asserted.
- Phase A2: verb ledger (`ledger.py`) — per-verb counters, hourly
  rollup, Prometheus exposition (`exposer.py`); structural mixin
  contract for strict mypy; int-only enforcement for exit_code/retry/
  queue_depth/items/budget.
- Phase B1: S5 memory-value pre-registration — two-arm replay protocol
  frozen before the first run.
- Phase B2: dynamism zone library (`dynamism.py` — zone 4 of
  methodology §3.4) — block fingerprints, session reports, corridor
  gate, structural falsifier, privacy invariant.
- Phase B3: S5 memory-value stand runner (`stand_s5.py`) — two-arm
  replay per frozen pre-registration, F5 frozen-delta guard; decisive
  run verdict **H1–H4 PASS, net_savings 163,533 tokens** (synthetic
  pre-registration v1).
- Phase C1: post-llm-call usage loop — `record_usage` + UsageAnalyzer
  (`usage.py`); overflow-proof client boundary, atomic FK, honest pins.
- Phase C2: touched-rate kappa calibration protocol pre-registered.
- Phase C3: kappa ablation runner; decisive look landed **loud NO-DATA
  (H-K0)** — touched_rate is not a v1 corridor metric.
- Test suite: 204 tests (canary, sink/meta/retention, drift-guard,
  ledger/rollup/exposition, dynamism, S5 runner, usage loop, kappa
  ablation); ruff clean.

### Changed

- Rebrand mnemos-vitals → **vesma-vitals**: package renamed to
  `vesma_vitals` (wave W-D prose sweep — metric names `mnemos_*`,
  `project:mnemos` tags and owner-ADR history deliberately untouched as
  canon).
- README: sibling cross-links added to the rebranded ecosystem repos —
  [vesmaro/vesma](https://github.com/vesmaro/vesma) and
  [vesmaro/vesma-eyes](https://github.com/vesmaro/vesma-eyes).
- Experiments recorded: graph-vs-grep n=5 benchmark (2026-10-03) and
  repeat #1 on vesma 5.4.0 (W-H — both first-run losses closed, living
  Go-index 2367 nodes / 6824 edges / 152 files, 0 parse_errors, day-0
  telemetry baseline, poisoned proof 24→0); token economy
  documented as the #1 evaluation metric (owner directive 2026-10-04).

## 0.0.1 — 2026-09-09

- Project founded: methodology, architecture and instrumentation track split
  out of the Mnemos server repo per owner directive.
- Canon imported from Architectural Committee 2026-09-09 (core `7ec9dda3` +
  full-coverage addendum `061398fe`; ADR-0026 in the mnemos repo is the formal
  record): two-circuit frame, metrics.sqlite sidecar, F8/F9 families, invariant /
  corridor / verdict taxonomy, security conditions C1–C5 + RL-S1–S7.
- Module contract stubs laid down for phases A/A2 (sink, ledger, exposer,
  report, S5 stand). No runtime code yet — implementation awaits owner
  green-light.
