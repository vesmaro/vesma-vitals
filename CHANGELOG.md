# Changelog

## [Unreleased]

### Added

- Phase A library core (branch `feat/phase-a-sink`): born-final sidecar schema
  (`schema.py` — five tables, allowlist meta_json, retention constants) and
  `MetricsStore` (`sink.py`) — the passive-collection write path with non-fatal
  degradation, 250 ms busy_timeout, keyed-HMAC fingerprints (key outside the
  sidecar, no rotation), allowlisted stage-stats projection and fail-loud
  nightly retention.
- C1 isolation canary — FIRST implementation commit, before any runtime
  metric exists (ADR-0026 mnemos).
- Claims-ledger seeded (`docs/claims.md`) per methodology §6.
- Test suite: 22 tests (9 canary + 13 sink contract); ruff clean.

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
