# Архитектура mnemos-vitals

Дата: 2026-09-09 · Статус: спецификация (до реализации) · Источник: контракт АрхКома
2026-09-09 (§1–8), проверенные точки вставки в коде mnemos

## 1. Позиционирование в системе

mnemos-vitals — **гость** в сервере mnemos. Контракт:

- mnemos не зависит от mnemos-vitals в runtime (импорт-изоляция; тест-гвард как в
  mcp-core-прецеденте mnemos #185).
- mnemos-vitals встраивается **только** в определённые границы (§3); конвейер
  `assemble_context` и основной стор не затрагиваются.
- Отказ записи метрик — non-fatal (warning-лог); сервер работает при выключенном
  или сломанном сборе.

## 2. Хранилище: metrics.sqlite sidecar

Отдельный файл рядом с основным DB mnemos. Born-final: схема создаётся целиком при
первом запуске, миграций не существует. WAL, busy_timeout **250 мс** (не 5000
прод-стора — contention на метриках не может заморозить hook-путь), права 0600,
encryption-at-rest наследуется тем же PR.

Пять таблиц (born-final одним PR):

| Таблица | Гранулярность | Схема (allowlist) |
|---|---|---|
| `verb_metrics` | 1 строка на вызов любого глагола | `id, ts, surface (mcp\|rest\|background\|cli), verb, status (ok\|error), status_code, latency_ms, project, agent, meta_json` — **без session/principal-колонок, навсегда** |
| `verb_metrics_hourly` | rollup по (hour, surface, verb, status, project) | `count, p50_ms, p95_ms, p99_ms, max_ms` |
| `assemble_metrics` | 1 строка на assemble-вызов (специализированная, НЕ view) | `id, verb_row_id → verb_metrics.id, session, project, agent, ts, mode, budget, tokens_estimated, blocks_count, blocks_refused, redactions, ccr_expanded, query_source (explicit\|derived), file_stem, stage_stats_json, fingerprints (keyed-HMAC)` |
| `injection_blocks` | 1 строка на инжектированный блок | `metrics_id, block_id, memory_id, source, score, tokens, ccr_origin` |
| `usage_reports` | 1 строка на ответ харнеса (фаза C) | `metrics_id, block_ids_touched_json, tokens_out, wrong_tool_flag` |

Приватность — **структурой, не фильтром**: колонок для сырого текста не существует;
`stats` dict не персистится verbatim (query — никогда, file — только stem);
отпечатки контента — keyed-HMAC (ключ per-install random, не хранится в sidecar,
без ротации — ротация ломает продольную уникальность); шинглы — только как
HMAC-отпечатки. `meta_json` — allowlist fail-closed (C5): неизвестный ключ → отказ
записи с warning; `error_type` — имя класса исключения; текст никогда.

Retention: `verb_metrics` TTL **30 дней** (~200–360 МБ при верхнем сценарии
30–40k строк/день), `verb_metrics_hourly` ~400 дней (~50 МБ/год),
`assemble_metrics`-семейство 90 дней; ночной DELETE + VACUUM в тихом окне; отказ
джобы = алерт (C4). Метрики объявлены «не аудит-записи».

## 3. Точки сбора (10, на границах поверхностей)

Одна запись на вызов — вызов проходит ровно одну поверхность. Двойной счёт
исключён; конвейер менеджера не тронут (S2-стенд mnemos вызывает менеджера
напрямую — обёртки вне его пути, коридор не задет).

| # | Точка | Код mnemos (проверено) | verb-примеры |
|---|---|---|---|
| 1 | `call_tool` — вся MCP-поверхность одной обёрткой | `mcp_server.py:1318` | `mnemos_add`, `mnemos_search`, … |
| 2 | `MetricsMiddleware` на app (рядом с AuthMiddleware) | `api/main.py:211`; route-template, не raw path; служебные `/health`, `/metrics`, `/api/v1/stats*` исключены | `rest:POST:/search` |
| 3 | `_processor_loop` — факт цикла при queue_depth > 0 | `manager.py:3144` | `pipeline.cycle {published, refined, quarantined}` |
| 4 | `_maybe_run_ccr_cleanup` | `manager.py:3200` | `ccr.cleanup {ttl_deleted, lru_evicted}` |
| 5 | `reclaim_stale_refinements` / `heal_stale_embeddings` | `manager.py:2838` / `2880` | `refine.reclaim`, `embed.heal` |
| 6 | scanner `_loop` | `scanner.py:226` | `scanner.scan` |
| 7 | `handle_pull` (федерация, сервер) | `federation_server.py:315` | `federation.pull {trigger_code, imported, skipped}` |
| 8 | `pull_from_peer` (федерация, клиент) | `federation_client.py:49` | `federation.pull_client {peer_id, fallback}` |
| 9 | watchers ingest | `watchers/path_scoped.py:206` | `watcher.ingest {rules}` |
| 10 | CLI after-callback (Typer; API подтвердить при реализации) | `cli/main.py` | `cli:add`, … |

Для assemble-вызовов (точки 1–2 + hook `pre_llm_call`) пишется дополнительная
строка `assemble_metrics` с `verb_row_id`-линком: леджер отвечает «кто/что/как
долго/статус», домен-таблица — «что именно собрано» (токены, стейджи,
HMAC-фингерпринты). Это две плоскости факта, не двойной счёт.

## 4. Экспозер: Prometheus-гибрид

- Счётчики `mnemos_verb_calls_total{surface,verb,status}` и гистограммы
  `mnemos_verb_latency_ms{verb,quantile="0.5|0.95"}` — из `verb_metrics_hourly`
  (переживают рестарт; закрывает дыру «since restart»); scrape читает только
  rollup.
- `avg_latency_ms` **удаляется** — среднее скрывает хвосты.
- Объёмные gauge (`memories_total`, by_status, vectors, sessions) — остаются из
  `dashboard_stats()` mnemos (main-store факты).
- Публичная exposition: **ноль лейблов endpoint/project/detector** на
  security-метриках (RL-S2); гранулярность выше глобального агрегата — только
  приватный контур (sidecar + операторские API). Per-principal — запрещён везде.
- Предусловие: фикс mnemos #249 (`_METRICS_BYPASS`) — auth на metrics-эндпоинтах
  для non-loopback-бинов или split public-safe/operator-only; чинится **до**
  любой новой exposition.

## 5. Стенд S5 «memory-value»

`benchmarks/`-структура mnemos-vitals (в репозитории mnemos стенды не меняются —
их baselines стабильны).

- Каркас обобщается из `s3_session/run.py` mnemos (single-command runner,
  `--record`, детерминизм BLAKE2b lexical embedder): `workload.py` (JSONL-лента
  событий: hints, tool-вызовы, записи памяти; источники — синтетический скрипт
  [гейтовый] и экспорт реальных сессий [фаза D, informational]), `run.py` (два
  рукава memory-on/off на изолированной копии стора — SQLite backup API,
  `actor=benchmark`; wall-clock в метрики не входит).
- Comparator-ноги: B0-naive / B0-file / B0-full (см. methodology.md §3.5);
  заголовок экономии — только при равной успешности.
- Baseline: раскладка как у mnemos s1/s3/s4; отчёт-страница mnemos
  (`report_page.py`) расширяется аддитивно (строка `s5` + светофор F8), отчёт
  vitals — свой рендер PASS / FAIL / NO-DATA с exit-1 при breach.
- v1 — только синтетика; реальные сессии (фаза D): opt-in per capture,
  санитизация fail-closed на границе захвата, хранение вне стора mnemos и вне
  репозиториев (0600), born no-federate, retention + команда удаления.

## 6. Отчёт владельца

Светофор по семьям: F1–F7 (mnemos, существующие) + F8 (memory value: вердикты) +
F9 (Subsystem health: суб-светофоры по подсистемам). Три состояния — PASS / FAIL /
NO-DATA; NO-DATA рендерится явно, никогда не зелёный. Генератор аварийит (exit 1),
а не редактирует. Claims-ledger (`docs/claims.md`) связывает каждое обещание с
семьёй и статусом.

## 7. Остаточные риски (принятые АрхКомом)

- Shingle-touched меряет эхо, не причинность → калибровка каппой ≥ 0.6 до коридоров.
- Judge имеет собственную надёжность → двойная разметка, каппа в отчёте.
- Потеря строк при contention (busy_timeout 250 мс, non-fatal) → S5-гейты
  считаются по стенду; инварианты — из гейтов/канареек, не из телеметрии.
- INSERT-латентность и объём — оценки до фазы A2; её гейты дают факт
  (p95 < 2 мс; объём/день; retention зелёная).
- Вердикты не блокируют мержи → защита от Goodhart-подгонки оплачена тем, что
  красный вердикт — операционный триггер (exit 1 + тикет), а не CI-гейт.

## 8. Верификация реализации

- Canary C1 — первый коммит: bug-report / backup / export / federation не включают
  metrics.sqlite; перечислены все 5 таблиц.
- Unit-тест allowlist: неизвестное поле → отказ; секрет в query → не попадает в
  метрики.
- Инвариант cross-principal-leak = 0 расширен на metrics-плоскость.
- INSERT-латентность hooks-пути — измерение в гейте A2.
- S2-коридоры mnemos не задеты: стенд вызывает менеджера напрямую, обёртки вне
  пути.