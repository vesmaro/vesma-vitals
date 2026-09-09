# Статус mnemos-vitals

Обновлено: 2026-09-09 (сессия разработки: фаза A, библиотечная часть)

## Доска

| Линия | Прогресс | Состояние |
|---|---|---|
| АрхКом-решения (ядро + аддендум) | 100% | Завершено — mnemos `7ec9dda3`, `061398fe`; ADR-0026 mnemos записан |
| Методология (канон) | 100% | Завершено — `docs/methodology.md` |
| Архитектура (спецификация) | 100% | Завершено — `docs/architecture.md` |
| Фундамент репозитория | 100% | Завершено — README, decisions, статус, каркас |
| Фаза A (sink + пассивный сбор) | 70% | **В работе** (ветка `feat/phase-a-sink`): born-final схема + C1-канарейка (`31d5258`), MetricsStore + контракт-тесты (`b8113d8`) — 22 теста зелёных, ruff 0. Осталось: интеграционная точка в mnemos (hook/MCP-граница — PR в репо mnemos), решение о default-on |
| Фаза A2 (verb-леджер, весь функционал) | 0% | Не начато — первым шагом чинит mnemos #249 |
| Фаза B (динамичность + S5 v1 + F8) | 0% | Не начато — преперегистрация до прогона |
| Фазы C–E, F9-волны, doctor-P3 | 0% | Не начато |

## Ждут владельца

1. Default-on пассивного сбора в local-first (рекомендация комитета: да).
2. Green-light фаз A + B.
3. Подтверждение аддендума (A2 + F9) этой же директивой.

## Блокеры

- **mnemos #249 (P0)**: `_METRICS_BYPASS` — metrics-эндпоинты mnemos обходят auth
  безусловно; экспортируют by_project/by_agent. Блокирует экспозер-часть фазы A2
  (RL-S1); чинится в mnemos первым шагом фазы A2. Не блокирует библиотечную
  часть фазы A (sink без exposition).
- **Интеграция в mnemos**: точка вызова `MetricsStore.record_assemble` на
  hook/MCP-границе — PR в репо mnemos (гость-контракт §1 architecture.md);
  требует решения владельца о default-on (вопрос 1).

## Фаза A — состояние деталей (сессия 2026-09-09)

Залито на ветке `feat/phase-a-sink`:

- `src/mnemos_vitals/schema.py` — born-final allowlist, 5 таблиц, C5-allowlist
  meta_json, retention-константы;
- `src/mnemos_vitals/sink.py` — `MetricsStore`: non-fatal write path
  (TraceRecorder-паттерн), busy_timeout 250 мс, keyed-HMAC-фингерпринты (ключ
  в `metrics.sqlite.hkey` вне sidecar, 0600, без ротации), allowlist-проекция
  stage_stats (query — никогда, file — stem), retention fail-loud с
  child-table каскадом, chmod 0600;
- `tests/` — 9 канареек C1 + 13 контракт-тестов sink; ruff 0 (line-length 100
  по канону mnemos);
- `docs/claims.md` — claims-ledger засеян (методология §6).

Не входит в фазу A (по архитектуре): verb-леджер (A2), экспозер (A2, за
фисксом #249), динамичность/S5 (B).

## Дальнейшая работа — в отдельной сессии

Точка входа: mnemos recall `7ec9dda3` + `061398fe`, читать `docs/methodology.md`,
затем `docs/architecture.md`. Рабочая среда: прогонная — K3s devops-dev-cluster
владельца с универсальными манифестами `~/LABs/**/` (вердикт Round-4 v2 `84e6b444`).

## Соседние артефакты (не в этом репо)

- Протоколы АрхКома: `~/.gcw/architectural-committee/2026-09-09-*.md` (team-local)
- Карта покрытия функционала: `2026-09-09-full-server-coverage-map.md` (там же)
- ADR-0026 mnemos: `docs/project/adr/0026-memory-value-observability.md` (untracked
  на ветке fix/245-sqlite-merge-gate)