# Статус mnemos-vitals

Обновлено: 2026-09-25 (фаза C: препегистрация калибровки каппы —
`docs/experiments/touched-rate-kappa.md` — и петля использования в библиотеке;
предыдущая актуализация — 2026-09-20)

## Доска

| Линия | Прогресс | Состояние |
|---|---|---|
| АрхКом-решения (ядро + аддендум) | 100% | Завершено — решения `7ec9dda3` + `061398fe`; **ADR-0026 закоммичен в main vesmaro** (PR #256, 2026-09-13) |
| Методология (канон) | 100% | Завершено — `docs/methodology.md` |
| Архитектура (спецификация) | 100% | Завершено — `docs/architecture.md` |
| Фундамент репозитория | 100% | Завершено — README, decisions, статус, каркас |
| Фаза A (sink + пассивный сбор) | 100% ✅ | **Закрыта**: библиотека в main vitals (`1a2380b`, APPROVE), интеграция в main vesmaro (**PR #392**, squash `d6cbee3`, 22.09) — гейт канарейки C1 выполнен первым коммитом; полный сьют vesmaro 4060/0, ruff+mypy чистые, 2 прохода ревью с обеих сторон |
| Фаза A2 (verb-леджер, весь функционал) | 100% ✅ | **Закрыта**: библиотека main vitals (`c87fdf1`+фиксапы, 47 тестов), интеграция main vesmaro (**PR #393**, squash `57a9d82`, 22.09) — 10 границ, rollup-тик перед retention, экспозиция /api/v1/metrics (RL-S2: только global-строки). Ревью 2 прохода (REQUEST-CHANGES → M1/M2+миноры → APPROVE-уровень). Гейт-факты INSERT p95 < 2 мс и объём/день — с первого живого прогона (не кодовая работа) |
| Фаза B (динамичность + S5 v1 + F8) | 55% | Зона 4 (динамичность) готова в библиотеке; S5-раннер зоны 5 реализован (ветка `feat/phase-b-s5-runner`): workload-лента (300 событий, fp `30959647e4…`), 4 симметричные ноги (M / B0-naive / B0-file / B0-full), H1–H4 с H2-мандатом заголовка, PASS/FAIL/NO-DATA, детерминизм BLAKE2b; 42 новых теста (116 зелёных), ruff 0. **Baseline-прогон S5 — pending** (пороги замораживаются только после него), дальше F8-светофор |
| Фазы C–E, F9-волны, doctor-P3 | ~10% | Фаза C: петля использования — библиотека готова (`MetricsStore.record_usage` + `UsageAnalyzer` в `usage.py`); **раннер абляции исполнен, решающий прогон закрыт (2026-09-29): κ̂ и CI95/превалентность/PABAK НЕ вычислимы — на коммитной S5-ленте нет блоков ≥ 40 токенов (макс 25 из 981 кандидатов), честное NO-DATA → вердикт H-K0: touched_rate — НЕ коридорная метрика в v1** (`reports/local/s5-ablation-kappa/`, single-look исчерпан, tape-fp `830fa553f4a4…`); 36 новых тестов (204 зелёных), ruff 0; перекалибровка — только новая препрегистрация; дальше — интеграция в vesmaro; D–E, F9-волны, doctor-P3 — не начато |

## Актуализация 2026-09-20 — что изменилось в экосистеме

- **Ребренд mnemos → vesmaro** (ADR-0031): GitHub org `Korrnals/mnemos` →
  `vesmaro/vesmaro`; локальный чекаут прежний
  (`~/LABs/Projects/Project-Mnemos/mnemos`; с 2026-10-01 — `~/LABs/Projects/Project-Vesma/vesma`); код в dual-layout
  `src/mnemos` + `src/vesmaro`; окно полного переименования — релиз 5.0.0
  (за «да» владельца). **Открытый вопрос для этого репо**: имя
  `mnemos-vitals` → `vesmaro-vitals` в окне 5.0.0 (семейство наследует имя:
  прецедент vesmaro-embed / vesmaro-refine).
- **Актуализация 2026-10-01 (эко-ребрендинг Vesmaro → Vesma)**: канон ядра —
  `vesmaro/vesma`; это репо перенесено в `vesmaro/vesma-vitals` (зеркальный пуш,
  refs верифицированы побитно) — открытый вопрос имени выше ЗАКРЫТ: канон
  `vesma-vitals`. Локальные пути `Project-Mnemos` → `Project-Vesma` (выше
  поправлены на живые).
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

Нет. Фазы A и A2 закрыты (vitals main + vesmaro `d6cbee3`, `57a9d82`).
Наблюдение ревьюера для TL основного плана (вне скоупа vitals): host
`_prometheus_text` экспортирует `mnemos_memories_by_project/by_agent` —
RL-S2-скоуп-решение (эндпоинт- vs плоскость-гранулярность) — оформить тикетом.

## Дальше — фаза C (petля использования) + зоны 1–2 вердиктов

Фаза C (P2/S): post_llm_call петля (usage_reports: touched/cited/wrong_tool),
touched_rate калибруется каппой ≥ 0.6 против абляции ~50 ходов — до коридоров.
F8-светофор отчёта владельца — собранный S5-вердикт уже даёт первую строку.
Гейт-факты A2 (INSERT p95 < 2 мс, объём/день) — снять с живого прогона vesmaro
`57a9d82` (деплой за TL основного плана).

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


## Актуализация 2026-09-29 — живой деплой phase C

**Исполнено 29.09:** phase C (usage-петля) деплоена на живой прод-сервер
vscode-box: полные бекапы (sqlite backup API с integrity-check, raw
sidecar/hkey, конфиги, старый venv-metrics — 72M,
`/var/home/abyss/backup/mnemos-phase-c-deploy-20260929`), атомарная
замена трёх файлов в прод-venv, рестарт обоих сервисов
(`Restart=on-failure` юнитов), пост-верификация:
- интеграционный E2E на живом снапшоте-клоне: closure 1.0, touched 1.0
  (informational/kappa gate), hostile refusals + atomic FK, RL-S2
  label-free экспозиция, retention cascade — все OK;
- живая экс­пози­ция содержит verb-семейства (45+ серий) и **громко не
  содержит usage-семей­ство** (0 assemble-строк = NO-DATA как отсутствие
  серии, не ноль — конструк­тив­но правильно);
- sidecar Health­y: verb растёт, 66,355+ строк.

**Остаток фазы C:** экспозиция usage-семей­ства в `/api/v1/metrics`
появится после подключения assemble-хар­несс­а (клиенты
`assemble_context`/`pre_llm_call`). Каппа-абляция: NO-DATA (H-K0),
перек­а­ing­бровка на корпусе с ≥40-ток­ен­ны­ми блоками (новая пре­егист­рация).


---

## Сес­сия закрыта (2026-09-30, пост-деплой phase C + E2E зелёные)

**Итог сессии в одном абзаце:** фазы A, A2, B, C библиотеки и интеграции
ЗАВЕРШЕНЫ и ЗА­ДЕПЛОЕ­НЫ: vitals main `d62dba3` (168 тестов, ruff/mypy 0),
живой прод vscode-box = vesmaro 4.3.0 + phase C (usage-петля
`record_usage` atomic FK + экспо­сиция; Рест­арт=on-failure юнитов;
все гейты зелёные; sidecar пишется). S5 вер­дикт синт­етики v1:
**net_savings 163 533 токенов (H1–H4 PASS)**. Каппа-абляция: **H-K0 —
громкое отсутствие данных, `touched_rate` не яв­ляет­ся кор­идор­ной мет­ри­кой
v1** (кор­пус синт­ет­ики макс. 25-токен­ных блоков про­тив сиг­наль­но­го дома ≥40);
перек­а­либ­ровка — НОВАЯ пре­егистрация.

## Беклог на следующую сессию (после за­верше­ния реб­рен­динга)

1. **Пуш + PR** хост-ветки `feat/vitals-phase-c-integration` в vesmaro
   (локальная ветка `5402ed8`+`7b5a9c5` в worktree `wt/vit-c-vesmaro`,
   хендофф-пакет `Project-Vesma/VITALS-C-HOST-PR-HANDOFF.md` — конфликтный
   рецепт внутри: только `docs/project/dev-plan.md`, «ставить обе стороны»).
2. **Деплой `57a9d82`+ в прод** — тогда сним­аются live гейт-факты A2
   (INSERT p95 hooks-пути, объём/день) и закрывается вер­дикт фазы C
   чис­лами живо­го пути.
3. **Под­клю­чение assemble-харнес­а** (`mnemos_assemble_context` /
   `pre_llm_call` MCP на живом деплое) — без него usage-семей­ство не
    появится в `/api/v1/metrics` (0 assemble-строк это громкое NO-DATA
    по дизайну).
4. **Фаза D (replay живых сессий)** — opt-in per capture, сани­ти­зация
   fail-closed на границе; следующая ве­личайшая цель.
5. **Каппа-пере­калибровка** на корпусе с ≥40-токен­ными блоками — НОВАЯ
   пре­егистрация (старая заморожена по H-K0).
6. **PyPI-публикация `mnemos-vitals`** (wheel готов, имя local-only):
   pip-путь для всех новых экосистем­ных проектов (vendoring — специфика
   vesmaro). Гейт: решение владельца.
7. **Фаза E (wrong-tool A/B) + F9-волны 2–3 + doctor-P3** — очередь.
8. **RL-S2 by_project/by_agent скоупинг** — на АрхКоме 27.09 отработан кадр,
   но финальное скоупинг-решение по `mnemos_memories_by_project/by_agent`
   не закрыто — тикеты не могут быть созданы PAT (нет Issues:write),
   передано основной сессии.
9. **Жест­кая память:** ин­ци­дент wt/ свипа 14:58 (кто снёс каталог —
   пост­радали соседние worktree) — вопрос владельцу; потеря лед­жера
   single-look задокументирована в каппа-доке.


## Актуализация 2026-10-01 (вечер) — PR открыт: **vesmaro/vesma #461**

- Ребренд экосистемы завершён Фазой-6: репо переехали в
  `Project-Vesma/` (vitals = `vesma-vitals`, package `vesma_vitals` —
  коммиты `47aa9dd`/`2188a46`/`d60f56b`); сервер теперь **vesmaro/vesma**
  (v5.1.2, имя пакета `vesma` / dist `vesma_memory_server`).
- Хост-ветка `feat/vitals-phase-c-integration` восстановлена (worktree
  пересоздан — сломанный .gitdir после Фазы-6 сбросили), **ребейз на
  origin/main `4a343e9`**: единственный конфликт `dev-plan.md` решён,
  +1 фикс работы-independent пинов (#436-прецедент). Полный сьют на
  ребейзе: **4907 passed / 4 skipped / 0 failed**. Запушено, **PR #461
  открыт** (через vesmaro/vesma — org переименован, старый alias даёт
  Moved Permanently).
- Прод сегодня сам перезапустился на **venv-5.1.1** (сборка
  vesma_memory_server 5.1.1): metrics/ содержит phase A/A2, **НО БЕЗ**
  `usage.py` — phase C gap в новой прод-сборке. После мержа #461 —
  повтор деплой-runbook (бекап → swap → рестарт → E2E).
