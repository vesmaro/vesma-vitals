# Changelog

## [Unreleased]

### Changed

- Финальная зачистка ребрендинга (волна W-D): проза и комментарии переведены на бренд vesma. Контракты не тронуты: теги project:mnemos/mnemos:*, метрики mnemos_*, канон-данные и ADR-история владельца.

### Added

- Повтор эксперимента graph-vs-grep (вечер 2026-10-03, vesma 5.4.0): оба проигрыша
  первого прогона закрыты — T2 bare-tail `trace_path` (1 вызов вместо отказа+3),
  T3 literal-фолбэк (`fallback_used=true`); живой Go-индекс vesma-mesh
  (2367 узлов / 6824 ребра / 152 файла, 0 parse_errors), day-0 базлайн телеметрии
  (`graph_audit`, 7 actor-ов) и poisoned-пруф 24→0. — `docs/experiments/graph-vs-grep-20261003.md`
- Phase A library core (branch `feat/phase-a-sink`): born-final sidecar schema
  (`schema.py` — five tables, allowlist meta_json, retention constants) and
  `MetricsStore` (`sink.py`) — the passive-collection write path with non-fatal
  degradation, 250 ms busy_timeout, keyed-HMAC fingerprints (key outside the
  sidecar, no rotation), allowlisted stage-stats projection and fail-loud
  nightly retention.
- C1 isolation canary — FIRST implementation commit, before any runtime
  metric exists (ADR-0026 mnemos).
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
- Test suite: 33 tests (7 canary + 22 sink/meta/retention + 4
  drift-guard); ruff clean.

### Changed

- README: sibling cross-links added to the rebranded ecosystem repos — [vesmaro/vesma](https://github.com/vesmaro/vesma) and [vesmaro/vesma-eyes](https://github.com/vesmaro/vesma-eyes).

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
