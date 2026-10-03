# Raw data — повтор прогона graph-vs-grep (2026-10-03 вечер, vesma 5.4.0)

Исполнитель: vitals-executor. Все графовые вызовы с `agent="vitals-executor"`.
Выдачи как есть; длинные JSON обрезаны по хвосту с пометкой `[обрезано]`.

## Стартовое состояние

- `list_graph_projects` (19:5xZ): githubcopilotworkflow 3428/7097/130 · jira-tempo-mcp 4890/12364/222 (poisoned 2) · mnemos **root_missing** (призрак, 0 узлов — известный дефект ghost-delete, не трогали) · mnemos-mesh 5/5/1 (корень уже репоинчен на текущий) · vesma **8654/18917/376, poisoned 0, fresh 100%, last_indexed 2026-10-03T19:01:07Z**.
- `project_graph_status(vesma)`: `poisoned_count: 0`, `parse_error_count: 0`, staleness fresh 100.0%, changed_files []. **Poisoned-пруф: до рестарта было 24 (тест-фикстуры), после allowlist+unpoison — 0.**
- В status появился блок peer-хартбитов: project vesma — user last 19:50Z, tech-lead last 19:27Z.

## T1 — search_graph "validate_tag_contract" (1 вызов)

```json
{"results": [
  {"kind": "Function", "name": "validate_tag_contract", "qname": "validate_tag_contract",
   "path": "src/vesmaro/models.py", "start_line": 414, "end_line": 600, "match_kind": "symbol"},
  {"kind": "Function", "name": "_normalize_slug", "qname": "validate_tag_contract._normalize_slug",
   "path": "src/vesmaro/models.py", "start_line": 549, "end_line": 560, "match_kind": "symbol"}],
 "total_matches": 2}
```

rg-сторона: `rg -n 'validate_tag_contract' src/` → **0.223s**, 35 строк (def + 34 использования; см. файл целиком ниже в «rg-выводы»).

## T2 — trace_path "update_fields" (1 вызов, было: отказ + 3 вызова)

```json
{"start": "SQLiteStore.update_fields", "depth": 2,
 "nodes": [
  {"qname": "SQLiteStore.update_fields", "path": "src/vesmaro/storage/sqlite_store.py", "start_line": 1922, "end_line": 1963, "depth": 0},
  {"qname": "SQLiteStore._get_conn", "...": "1201–1215, depth 1"},
  {"qname": "SQLiteStore._invalidate_caches", "...": "1667–1678, depth 1"},
  {"qname": "_TTLCache.invalidate", "...": "538–540, depth 2"},
  {"qname": "_TTLCache.invalidate_prefix", "...": "542–547, depth 2"}],
 "edges": [{"from": "update_fields", "to": "_get_conn", "kind": "USES"}, {"from": "update_fields", "to": "_invalidate_caches", "kind": "USES"},
           {"from": "_invalidate_caches", "to": "_TTLCache.invalidate", "kind": "USES"},
           {"from": "_invalidate_caches", "to": "_TTLCache.invalidate_prefix", "kind": "USES"}],
 "truncated": false}
```

Голый хвост `update_fields` уникально разрешился → сразу трейс окрестности. Отказа («not found or ambiguous», прогон 1) нет.

rg-сторона: `rg -n 'def update_fields' src/` → **0.233s**, 1 строка (`sqlite_store.py:1922`). Callers grep-ом не находятся (динамический диспатч) — как и в прогоне 1.

Бонус — дизамбигуация (новое в W-H): `trace_path("close")` →

```json
{"candidates": true,
 "candidate_list": [
  {"qname": "AgentTokenStore.close", "path": "src/vesmaro/agent_tokens.py", "start_line": 593},
  {"qname": "AuthStore.close", "path": "src/vesmaro/api/auth_store.py", "start_line": 515},
  {"qname": "AutoIndexer.close", "path": "src/vesmaro/codegraph/autoindex.py", "start_line": 200},
  {"qname": "CodeGraphService.close", "...": "360–362"}, {"qname": "CodeGraphStore.close", "...": "297–308"},
  {"qname": "GraphAudit.close", "...": "148–152"}, {"qname": "GraphWatchScheduler.close", "...": "240–248"},
  {"qname": "HermesMemoryAdapter.close", "...": "221–223"}, {"qname": "MemoryManager.close", "...": "696–714"},
  {"qname": "MeshClient.close", "...": "361–368"}], "[обрезано: 10 из 16]"],
 "candidate_count": 16,
 "hint": "ambiguous symbol tail — re-run trace_path with the qualified name (qname) of the intended candidate"}
```

Нюансы (не блокеры):
- `trace_path("get_manager")` — 4 top-level функции с ОДИНАКОВЫМ qname `get_manager` → тул молча взял первый (api/federation.py) и оттрейсил, БЕЗ candidates-флага. Кандидаты возвращаются только при неоднозначном хвосте с разными qname.
- `trace_path("invalidate")` → уникально разрешился в `_TTLCache.invalidate` (1 узел, 0 рёбер).

## T3 — регистрация тула (литерал)

Шаг 1 (grep): `rg -n 'name="vesma_|"vesma_search_graph"' src/vesmaro/mcp_server.py` → **0 хитов**: литералов `vesma_*` в коде нет. `rg -n 'search_graph' src/vesmaro/mcp_server.py`:

```
1864:            name="mnemos_search_graph",
3212:        "mnemos_search_graph",
3284:        if name == "mnemos_search_graph":
```

Актуальный литерал регистрации: `name="mnemos_search_graph"` (легаси-имя; наружу сервер отдаёт vesma_*).

Шаг 2 (граф): `search_graph("mnemos_search_graph")` → `"fallback_used": true`, все строки `match_kind: "literal"`:

```
ARCHITECTURE.md:192  | Project graph (ADR-0032...) | `mnemos_index_project`, ..., `mnemos_search_graph`, ...
CHANGELOG.md:13      - **Hybrid `search_graph`: literal-content fallback + bare-tail `trace_path` disambiguation (wave W-H...)**
docs/en/user/mcp-tools.md:1075   "snippet": "name=\"mnemos_search_graph\","
docs/ru/user/project-graph.md:132  ...W-H `mnemos_search_graph` — **гибридный**...
[обрезано: 20 literal-хитов на первой странице]
```

Каверзная деталь: `"total_matches": 0` при непустом фолбэке — счётчик считает только символьные совпадения. Снппет точной строки регистрации (`name="mnemos_search_graph"`) на первой странице не показан (доки ранжируются выше кода) — rg находит код-хит одним вызовом.

rg-сторона: `rg -n 'mnemos_search_graph' src/` → **0.248s**, 12 строк, первый хит — сама регистрация (mcp_server.py:1864).

## T4 — структура doctor

`search_graph("doctor")` (1 вызов): File-узел `src/vesmaro/cli/doctor.py 1–1229` (в прогоне 1 было 1–1198 — файл вырос), всего 133 совпадения, первая страница 43 строки: CheckStatus 45–52, _check_config 66–85, _check_mcp_server 333–434, _run_all_checks 845–883, doctor 1114+, тест-классы в 6 файлах. `[обрезано]`

rg-сторона: `rg -n '^class |^def |^    def ' src/vesmaro/cli/doctor.py` → **0.374s**, 36 строк (полный список — в «rg-выводы»).

## T5 — fan-out get_manager

`search_graph("get_manager", kind=Function)` (1 вызов) → 5 хитов: mcp_server.py 162–168, api/federation.py 62–78, api/main.py 93–107, cli/_manager.py 20–32, tests/test_cli.py 222–232.

rg-сторона: `rg -n 'def get_manager' src/` → **0.289s**, 4 строки (+1 тестовый за пределами src/ фильтра — см. вывод).

## rg-выводы (полные, из /tmp/vesma-vitals-rg/)

Тайминги (`TIMEFORMAT=%R`): T1 0.223s · T2 0.233s · T3 0.248s · T4 0.374s · T5 0.289s.

t5 (def get_manager, src/): 4 строки — mcp_server.py:162, api/main.py:93, api/federation.py:62, cli/_manager.py:20.
t2 (def update_fields, src/): 1 строка — storage/sqlite_store.py:1922.
t1: 35 строк использований/импортов validate_tag_contract (main.py ×4, mcp_server.py ×4, hermes.py ×2, context_rewrite.py ×3, cli/main.py ×4, sdk.py ×3, models.py ×5, import_.py ×3, manager.py ×3, lanes.py, докстроки).
t4: 36 строк — классы CheckStatus:45, CheckResult:56, _FixAction:904; чеки _check_config:66 … _run_health_checks:1033; входы doctor:1114, doctor_fix:1182, doctor_paths:1211.

## (b) vesma-mesh: register → live index

`vesma graph register vesma-mesh <root> --agent vitals-executor` → отказ:

```
already-registered project 'mnemos-mesh' at /var/home/abyss/LABs/Projects/Project-Vesma/vesma-mesh
— root already registered under project 'mnemos-mesh'
next: vesma mcp / mnemos_index_project to build the graph
```

Корень уже зарегистрирован под `mnemos-mesh` (repoint сделан ранее; призрака со старым корнем /Project-Mnemos нет — есть только призрак проекта `mnemos`, root_missing, не трогали). Дублирующий id не создавал.

Живой индекс — stdio-MCP `mnemos_index_project(project_id="mnemos-mesh", agent="vitals-executor", reason="vitals repeat run: live Go index W-J #470")`:

```json
{"status": "ok", "nodes": 2367, "edges": 6824, "files_indexed": 152, "files_skipped": 0,
 "poisoned": [], "unpoisoned": [], "parse_errors": {}, "duration_sec": 1.66, "incremental": true,
 "staleness": {"total_files": 152, "fresh_percent": 100.0, "changed_files": [],
 "last_indexed_at": "2026-10-03T20:30:30.144825+00:00"}}
```

## (c) Телеметрия day-0 базлайн

- Peer-хартбиты пишутся: project vesma — user 19:50Z / tech-lead 19:27Z; project mnemos-mesh — user 19:56Z / gcw-tech-lead 19:43Z (в ответах status/index, блок «Peer awareness — heartbeat»).
- Счётчик-поверхность: `code_graph.db → graph_audit` (93 записи, cols id/project/action/actor/session/reason/details/ts). Примеры: `graph-read … "literal_fallback": true`; `trace-candidates {"query": "close", "candidates": 16}`; `reindex {"nodes": 2367, ...}`.
- Агрегаты: by actor — tech-lead 38, gcw-tech-lead 16, zcode-qa 15, vitals-executor 12, zcode 7, user 3, hermes-agent 2; by action — graph-read 32, index 26, reindex 11, auto-register-reused 11, auto-register 7, snippet-read 3, repoint 2, manual-register-reused 1. Сегодня (2026-10-03): 17 записей (vitals-executor 12, tech-lead 4, gcw-tech-lead 1).
- Базлайн для цели «доля первого-вызова графом ≥30% к месяцу»: adoption=0 из прогона 1 больше не верно — граф пишут 7 actor-ов, 32 graph-read суммарно. Ограничение: graph_audit не различает «первый вызов задачи» и не знает о rg-вызовах — автоматический расчёт доли потребует разметки reason при первом поисковом вызове задачи или парсинга сессий (потенциальная карточка).
- `vesma stats`: статус ok, версия 5.4.0, память 3443 записи (published 3418), search hybrid fts+vector живой.

## (d) Poisoned-пруф

`project_graph_status(project_id="vesma")`: `poisoned_count: 0`, `parse_error_count: 0`, fresh 100% (376 файлов), last_indexed 2026-10-03T19:01:07Z. Unpoison 24→0 после secret_allowlist (tests/**, benchmarks/**) подтверждён.
