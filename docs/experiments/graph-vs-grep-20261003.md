# Прогон: граф проектов против grep (n=5) — 2026-10-03

Первый прогон серии «инструментальная ценность графов» (трек graph-adoption).
Вопрос владельца: граф работает и даёт ли профит, или функционал есть, а пользуются
ли им. Метод и выводы — ниже; сырые выдачи запросов — `raw-data.md` рядом.

## Метод

- Объект: репо vesma (Python, 368 файлов в индексе графа, 8412 узлов / 18406 рёбер,
  100% freshness на момент прогона).
- 5 задач, взятых из РЕАЛЬНОЙ сессии разработки того же дня (T1–T5: найти символ,
  найти определение+callers, найти регистрацию MCP-тула, понять структуру модуля,
  fan-out определений).
- Каждая задача решалась двумя способами: графовыми тулами (search_graph /
  trace_path) и grep (rg). Считались: число вызовов, время grep-стороны,
  структура ответа (диапазоны строк, qname, «докручивать ли чтением»).

## Результаты

| Задача | Граф | Grep | Исход |
|---|---|---|---|
| T1 найти validate_tag_contract | 1 вызов, точное место + диапазон + вложенный хелпер | 1 вызов, 0.20s | ничья (граф даёт структуру) |
| T2 update_fields: определение → callers | trace_path на голом хвосте отказал; search_graph → qname → нужен 3-й вызов | 1 вызов; callers grep не находит в принципе | граф выигрывает только на шаге callers |
| T3 найти регистрацию тулa `mnemos_search` | 0 результатов — строковый литерал, не символ | 1 вызов, 0.23s | **grep** |
| T4 структура doctor (куда вживить фикс) | 1 вызов: файл 1–1198 + классы/функции | 1–2 вызова | ничья-граф (карта файла) |
| T5 fan-out: где определения get_manager | 1 вызов, 5 определений с типами | 1 вызов, 0.22s | ничья |

Итог: паритет на «найти X»; уникальные преимущества графа — **callers/окрестность
символа** (grep не умеет), **структура с диапазонами строк** (меньше дочитываний),
**гигиена покрытия**. Проигрыши: строковые литералы (T3), отказ trace_path на
неоднозначном хвосте (T2). Главная находка вне замера: **adoption = 0** — за ~15
часов активной разработки ни одного вызова графовых тулов.

## Вердикт и последствия (что из этого родилось)

- Функционал работоспособен (zero-touch автоиндекс доказан живьём: индекс обновился
  во время мержа волн в тот же день). Проблема — не код, а интеграция в процессы.
- Слито в продукт следом (main, 2026-10-03): W-G — пак-инструкция «graph-first»
  (search_graph → trace_path → snippet; grep для литералов), hints в
  project_graph_status; W-H — гибридный search_graph (литерал-фолбэк, PG3/PG4
  redaction) и дизамбигуация trace_path — оба проигрыша замера закрыты кодом.
- GCW: 8 кодовых субагентов получили read-only графовые тула (wiring-gap починен:
  installer дропал vesma/* из профилей).
- Конфиг машины: code_graph.secret_allowlist (tests/**, benchmarks/**) — 24
  отравленных тест-фикстуры; awareness.native_heartbeat_mode: shadow — телеметрия
  использования тулов для следующих прогонов.

## Протокол повтора

1. После ближайшего релиза + рестарта серверов повторить T1–T5 тем же методом —
   критерий: все бывшие проигрыши закрываются графом (T3 литерал-фолбэком,
   T2 дизамбигуацией).
2. Через ~месяц shadow-телеметрии: доля поисковых задач, закрытых графом первым
   вызовом (цель ≥30%), разбивка по агентам (оркестратор vs субагенты).
3. Данные писать рядом (`raw-data.md`), вердикт — в этот report.md, дата — в имя.

---
# Приложение: сырые данные

Выдачи как есть (сокращено до значимых полей). Проект графа: `vesma`,
индекс на момент прогона: 8412 узлов / 18406 рёбер / 368 файлов, last indexed
2026-10-02T22:09:57Z.

## T1 search_graph "validate_tag_contract" → 2 хита

```
Function validate_tag_contract            src/vesmaro/models.py 414–600
  └ _normalize_slug (qname validate_tag_contract._normalize_slug) models.py 549–560
```

## T2 trace_path "update_fields" → отказ

```
{"error": "symbol 'update_fields' not found or ambiguous in the project graph —
use search_graph to locate the exact qname", "code": "refused"}
```
search_graph kind=Method "update_fields" → SQLiteStore.update_fields,
src/vesmaro/storage/sqlite_store.py 1922–1963 (+5 тестовых методов).

## T3 search_graph "mnemos_search" → 0

```
{"results": [], "total_matches": 0}
```
Причина: имя тула — строковый литерал `name="mnemos_search"` в коде, символом не является.
grep-сторона: `rg -n mnemos_search src` → 1 вызов, 0.23s.

## T4 search_graph "doctor" → 131 совпадений (первые 10)

```
File  src/vesmaro/cli/doctor.py 1–1198
Class CheckStatus doctor.py 45–52
Class TestDoctorAgentWiring tests/test_agent_wiring.py 737–811
Class TestDoctorCommand tests/test_cli.py 417–481
Class TestDoctorCompletionCheck tests/test_completion.py 313–350
Class TestDoctorConfigPath tests/test_env_dual_prefix.py 250–268
Class TestDoctorFix tests/test_proposals.py 385–536
Class TestDoctorPassCase tests/test_agent_wiring_edge.py 591–619
Function _cap_listing doctor.py 437–450
Function _check_agent_wiring doctor.py 692–742
```

## T5 search_graph kind=Function "get_manager" → 5 хитов

```
Function get_manager mcp_server.py 162–168
Function get_manager api/federation.py 62–78
Function get_manager api/main.py 93–107
Function get_manager cli/_manager.py 20–32
Test    test_get_manager_returns_memory_manager tests/test_cli.py 222–232
```

## Grep-сторона (время, rg)

```
validate_tag_contract   0.20s
update_fields(          0.25s
mnemos_search           0.23s
get_manager             0.22s
```

## Пробы состояния (контекст прогона)

- project_graph_status: fresh 100%, poisoned 24 (все — tests/**/benchmarks/**
  фикстуры с фейковыми ключами; allowlist на машине включён следом за прогоном).
- list_graph_projects: Go-репо пустые (vesmaro-agent 1 узел, mnemos-mesh 5) — #470;
  призрак project=mnemos (root_missing) — delete отказывает (confinement) → карточка.
- awareness_events: сайдкара нет (native_heartbeat_mode off) → телеметрия включена
  в shadow следом за прогоном.
