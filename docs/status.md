# Статус mnemos-vitals

Обновлено: 2026-09-20 (актуализация состояния экосистемы после 11-дневного
перерыва сессии; библиотечное ядро фазы A — от 2026-09-09)

## Доска

| Линия | Прогресс | Состояние |
|---|---|---|
| АрхКом-решения (ядро + аддендум) | 100% | Завершено — решения `7ec9dda3` + `061398fe`; **ADR-0026 закоммичен в main vesmaro** (PR #256, 2026-09-13) |
| Методология (канон) | 100% | Завершено — `docs/methodology.md` |
| Архитектура (спецификация) | 100% | Завершено — `docs/architecture.md` |
| Фундамент репозитория | 100% | Завершено — README, decisions, статус, каркас |
| Фаза A (sink + пассивный сбор) | 75% | **Библиотечный слайс закрыт и влит в main** (`1a2380b`, ревью в 2 прохода — REQUEST-CHANGES → все мажоры верифицированы → APPROVE; 33 теста, ruff 0). Осталось: интеграционный PR в vesmaro (hook/MCP-граница) + решение о default-on |
| Фаза A2 (verb-леджер, весь функционал) | 0% | **Разблокирована**: #249 закрыт (2026-09-14, фикс #304). Первый шаг A2 — verb-леджер + экспозер (подтверждение аддендума у владельца) |
| Фаза B (динамичность + S5 v1 + F8) | 0% | Не начато — преперегистрация до прогона |
| Фазы C–E, F9-волны, doctor-P3 | 0% | Не начато |

## Актуализация 2026-09-20 — что изменилось в экосистеме

- **Ребренд mnemos → vesmaro** (ADR-0031): GitHub org `Korrnals/mnemos` →
  `vesmaro/vesmaro`; локальный чекаут прежний
  (`~/LABs/Projects/Project-Mnemos/mnemos`); код в dual-layout
  `src/mnemos` + `src/vesmaro`; окно полного переименования — релиз 5.0.0
  (за «да» владельца). **Открытый вопрос для этого репо**: имя
  `mnemos-vitals` → `vesmaro-vitals` в окне 5.0.0 (семейство наследует имя:
  прецедент vesmaro-embed / vesmaro-refine).
- **#249 (`_METRICS_BYPASS`) закрыт** — «metrics endpoints require auth on
  non-loopback binds» (#304). Прекондиция экспозера (RL-S1) снята.
- **Точка интеграции переехала и задрейфила — найдено и починено**: `assemble_context`
  теперь `src/vesmaro/assemble.py:860`; сигнатура +`task` / +`lens` (эпик #308);
  результат — те же 9 ключей; в blocks добавлены `origin` (ADR-0025) и
  опциональный `lane`; ключи stage-stats сместились (`filter.profiles`,
  `align.blocks_aligned`/`moved_chars`, `ccr.skipped_*`, `scan.blocks_scanned`).
  Старая проекция теряла 6 живых счётчиков — allowlist синхронизирован
  (коммит `a59edee`), живая форма закреплена drift-guard тестом.
- **Место трека в дорожной карте сервера** (dev-plan, живой документ):
  vitals = «параллельный фон» — «Cache-Phase-2 M1 живёт в vitals-сессии
  (ADR-0026 sidecar)»; не на критическом пути. Активные волны vesmaro:
  граф A0→A1 (контрольная точка АрхКома 27.09), мультиконтекст Ф1-раннер,
  mesh v2, ребренд 5.0.0.
- **Механика параллельной работы** (из хендоффа основного плана 2026-09-20):
  vesmaro-токен — `GH_TOKEN` из хостового `~/.secrets/gh_token_vesmaro`;
  параллельные сессии — worktree-изоляция + fetch перед merge; мерж —
  squash с полным SHA + [agent-review]-комментарий.

## Ждут владельца

Ничего срочного. По сигналу владельца 2026-09-20 («уверен, что моё
вмешательство требуется?» — снятие перестраховки TL) приняты как действующие
рекомендации комитета, без содержательных развилок: default-on local-first =
**да**; аддендум A2+F9 = **подтверждён** (это канон АрхКома, не развилка);
имя репо `vesmaro-vitals` — в окне 5.0.0 по ADR-0031. Вето владельца
пост-фактум сохранено. Активная работа: **волна 2 — интеграционный PR в
vesmaro** (ветка `feat/vitals-phase-a`, worktree `vesmaro-vitals-integration`,
base origin/main `03a0004`).

## Блокеры

- Прямых блокеров нет: #249 закрыт, ADR-0026 закоммичен, контракт
  `assemble_context` совместим.
- Координационная предосторожность: интеграционный PR в vesmarо делается с
  учётом активной TL-сессии основного плана (worktree-изоляция, fetch перед
  merge, их базлайн-гейт: `.venv/bin/{pytest,ruff,mypy}` после
  `uv sync --extra dev --python 3.12`).

## Фаза A — состояние деталей

Влит в main `1a2380b` (2026-09-20; история — ветка `feat/phase-a-sink`, 7 коммитов):

- `src/mnemos_vitals/schema.py` — born-final allowlist, 5 таблиц, C5-allowlist
  meta_json, retention-константы;
- `src/mnemos_vitals/sink.py` — `MetricsStore`: non-fatal write path
  (TraceRecorder-паттерн), busy_timeout 250 мс, keyed-HMAC-фингерпринты (ключ
  в `metrics.sqlite.hkey` вне sidecar, 0600, без ротации), allowlist-проекция
  stage_stats (query — никогда, file — stem), retention fail-loud с
  child-table каскадом, chmod 0600;
- `tests/` — 7 канареек C1 (born-final pin колонок каждой таблицы + C3-ассерт
  «session только в assemble_metrics») + 22 контракт-теста sink/meta/retention
  + 4 drift-guard; ruff 0 (line-length 100 по канону mnemos);
- ревью-история: проход 1 — REQUEST-CHANGES (4 мажора: OSError в host,
  фантомные строки при failed write, WAL/SHM 0644, незакреплённый C3),
  проход 2 — все фиксы верифицированы воспроизведением, APPROVE;
- `docs/claims.md` — claims-ledger засеян (методология §6).

Не входит в фазу A (по архитектуре): verb-леджер (A2), экспозер (A2,
разблокирован закрытием #249), динамичность/S5 (B).

## Дальнейшая работа

Точка входа сессии: mnemos recall `bbc2a3db` + `567f9e81` + хендофф
«основной план vesmaro 2026-09-20» → `docs/methodology.md` →
`docs/architecture.md` → этот файл. Прогонная среда стендов — K3s
devops-dev-cluster, манифесты `~/LABs/**/` (вердикт Round-4 v2 `84e6b444`).

## Соседние артефакты

- Протоколы АрхКома: `~/.gcw/architectural-committee/` — файлы
  `2026-09-09-memory-value-observability*.md` и
  `2026-09-09-full-server-coverage-*.md` В ДИРЕКТОРИИ ОТСУТСТВУЮТ (проверено
  2026-09-20); полные источники схем — ADR-0026 (main vesmaro) и mnemos-записи
  `7ec9dda3` / `061398fe` / `9a3cd4a2`.
- Новые протоколы после 09-09: mnemos-rename (14–15.09), ocp-unified-vision (15.09),
  mesh-v2-contracts (20.09), multi-context-memory (14.09) — на методологию
  vitals не влияют, кроме аддитивного дрейфа assemble (см. выше).
- Хендофф основного плана vesmaro: mnemos-запись `20849538` (2026-09-20 вечер).
