# MCP-сервер

Третий, полностью самостоятельный компонент (наравне с AgentsCore и
AgentsApp): отдельный процесс, отдельная SQLite-база, отдельный деплой.
Предоставляет:

1. **MCP-протокол** (Streamable HTTP, спецификация 2026-07-28, stateless) под
   `/mcp` — им пользуется **AgentsCore** как MCP-клиент (`list_tools`/`call_tool`).
2. **Обычный REST API** под `/api` — им пользуется **AgentsApp** напрямую, в
   обход AgentsCore (статус, список инструментов, регистрация периодических
   задач, история запусков, статус Git-хостингов).
3. **Один экземпляр APScheduler** внутри этого же процесса, исполняющий
   зарегистрированные периодические задачи (`scheduled_tools`).

## Запуск

```bash
pip install -r requirements.txt
cp .env.example .env   # и отредактировать под себя
python -m mcp_server
```

**Версия SDK `mcp` жёстко закреплена `<2`** (см. `requirements.txt`) — код
написан против API `mcp` 1.x (`from mcp.server.fastmcp import FastMCP`). В
`mcp` 2.x `FastMCP` переименован в `MCPServer` и переехал в
`mcp.server.mcpserver` — если увидите при запуске
`ModuleNotFoundError: No module named 'mcp.server.fastmcp'`, значит в
окружении уже стоит 2.x (например, было установлено раньше для другого
проекта) — переустановите зависимость: `pip install "mcp[cli]<2"`.

По умолчанию слушает `0.0.0.0:8001`. AgentsCore должен указывать на
`http://<host>:8001/mcp` (конфиг `MCP_SERVER_URL`), AgentsApp — на
`http://<host>:8001/api/...`.

## Однократный экземпляр — обязательно

**Не запускайте больше одной реплики этого процесса одновременно.**
Планировщик (`APScheduler`) живёт внутри процесса и не координируется между
репликами — при двух и более запущенных процессах каждая периодическая
задача будет выполняться параллельно в каждой реплике, что для `git_pull`
безобидно, но для `http_fetch`/`git_host_poll` с одинаковыми параметрами
означает дублирование внешних запросов и, потенциально, дублирование
побочных эффектов на удалённой стороне (если резолвер `action` в будущем
станет вызывать что-то с эффектами, скажем — вебхук). Если нужна
отказоустойчивость — используйте supervisor/systemd с перезапуском
единственного процесса, а не горизонтальное масштабирование.

## Конфигурация

См. `.env.example` — всё прокомментировано на русском. Ключевые переменные:

| Переменная | Назначение |
|---|---|
| `MCP_HOST`, `MCP_PORT` | адрес/порт HTTP-сервера |
| `AGENT_MCP_DB_PATH` | путь к собственной SQLite-базе |
| `MCP_API_KEY` | заготовка под будущую аутентификацию (пока не используется) |
| `GITHUB_TOKEN`/`GITLAB_TOKEN`/`GITEA_TOKEN` + `*_API_URL` | доступ к Git-хостингам; без токена — анонимный режим для публичных репозиториев |
| `MCP_HTTP_TIMEOUT` | таймаут HTTP-запросов (сек) |
| `MCP_LOCAL_GIT_ENABLED` | работа с локальными рабочими копиями git (`execute_git_command`, задачи `git_pull`); по умолчанию **выключено** |
| `MCP_SCHEDULER_ENABLED` | периодические задачи и планировщик; по умолчанию **выключено** |

### Включаемые возможности

Пока настройка не задана (или `false`), инструменты группы **не попадают в
список доступных** — ни агенту (AgentsCore получает их через MCP
`tools/list`), ни приложению (`GET /api/tools`):

| Настройка | Что включает |
|---|---|
| `MCP_LOCAL_GIT_ENABLED=true` | `execute_git_command`; вид задачи `git_pull` (если включены и периодические задачи) |
| `MCP_SCHEDULER_ENABLED=true` | `register_scheduled_tool`, `list_scheduled_tools`, `cancel_scheduled_tool`, `save_result`; REST `/api/scheduled-tools*` (без настройки — `409 {"error": ...}`); виды задач в `/api/tools`; сам планировщик |

Инструменты Git-хостингов (`git_host_*`) доступны всегда. Текущее состояние
обеих настроек печатается при старте и отдаётся в `GET /api/status`
(`scheduling_enabled`, `local_git_enabled`) — AgentsApp по ним прячет раздел
периодических задач. Если выключить настройку, уже заведённые задачи
остаются в базе: без `MCP_SCHEDULER_ENABLED` они не срабатывают вовсе, а
задачи `git_pull` без `MCP_LOCAL_GIT_ENABLED` при срабатывании записывают в
историю ошибку «действие выключено», не выполняясь.

## MCP-инструменты (`/mcp`, для AgentsCore)

Инструменты периодических задач и `execute_git_command` регистрируются,
только если включены соответствующей настройкой (см. «Включаемые
возможности» выше).

- `register_scheduled_tool(name, schedule, action, params, description=None, input_schema=None)` —
  завести периодическую задачу. `schedule`: `"every:<N><s|m|h>"` (например
  `"every:30m"`), `"daily:HH:MM"`, либо обычный 5-полевой cron
  (`"0 */2 * * *"`). `action` — одно из `git_pull`/`http_fetch`/`git_host_poll`.
- `list_scheduled_tools(status=None)` — `status`: `"enabled"`/`"disabled"`/`None` (все).
- `cancel_scheduled_tool(task_id)` — удаляет задачу и её задание в планировщике.
- `execute_git_command(repo_path, command, args=None)` — разовый вызов
  произвольной git-подкоманды над локальной рабочей копией (не только pull).
- `save_result(task_id, data)` — сохранить результат вручную в историю запусков задачи.
- `git_host_list_commits(host, owner, repo, since=None, until=None, branch=None, limit=50)`
- `git_host_list_pull_requests(host, owner, repo, state="all", since=None, limit=50)`
- `git_host_list_issues(host, owner, repo, state="all", since=None, limit=50)`
- `git_host_list_releases(host, owner, repo, limit=20)`
- `git_host_get_repo(host, owner, repo)`
- `git_host_get_file(host, owner, repo, path, ref=None)`

`host` — один из `"github"`/`"gitlab"`/`"gitea"`. `since`/`until` принимают
ISO8601 (`"2026-09-01T00:00:00Z"`) либо относительный формат (`"-1h"`, `"-1d"`).
Ответы `git_host_*` включают `rate_limit_remaining`, когда хостинг его сообщает.

Все инструменты возвращают `{"error": "..."}` при ожидаемых сбоях (неверные
параметры, задача не найдена, 401/403/404/429, сетевая ошибка) — вместо
исключения, чтобы модель на стороне AgentsCore могла среагировать на текст
ошибки, а не получить оборванный вызов.

### Пример: периодический опрос GitHub

```python
register_scheduled_tool(
    name="poll-acme-widgets",
    action="git_host_poll",
    schedule="every:1h",
    params={"host": "github", "owner": "acme", "repo": "widgets", "resource": "commits", "since": "-1d"},
)
```

Каждый час выполнится `git_host_list_commits(owner="acme", repo="widgets", since="-1d", ...)`
и результат сохранится в `scheduled_tool_runs` (виден через
`GET /api/scheduled-tools/{id}/runs`).

`resource` для `git_host_poll` — один из `commits`/`pull_requests`/`issues`/
`releases`/`repo`/`file` (для `file` также обязателен `path`, опционально `ref`).

## REST API (`/api`, для AgentsApp)

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/api/status` | `{scheduler_running, tool_count, scheduled_count, scheduling_enabled, local_git_enabled}` |
| GET | `/api/tools` | доступные инструменты `{name, title, description, parameters, schedulable}`; `title` — краткое русское описание для интерфейса |
| GET | `/api/scheduled-tools` | список периодических задач |
| POST | `/api/scheduled-tools` | создать задачу |
| PATCH | `/api/scheduled-tools/{id}` | частично изменить (в т.ч. `enabled`) |
| DELETE | `/api/scheduled-tools/{id}` | удалить |
| GET | `/api/scheduled-tools/{id}/runs?limit=50` | история запусков |
| GET | `/api/git-hosts` | `[{host, configured, api_url, rate_limit_remaining, rate_limit_reset_at}]` |

`schedulable=true` в `/api/tools` стоит только у трёх `action`
(`git_pull`/`http_fetch`/`git_host_poll`) — это единственное, что можно
поставить на расписание через `POST /api/scheduled-tools` (сама
`register_scheduled_tool` принимает `action`-enum, а не произвольное имя
MCP-инструмента). Остальные перечисленные инструменты (например
`execute_git_command`, `git_host_get_file`) — разовые вызовы для модели,
не для расписания.

### Git-хостинги через API

`GET /api/git-hosts` всегда возвращает все три хостинга, даже если токен не
задан (`configured: false`) — AgentsApp должен показывать такие хостинги как
доступные в анонимном режиме с предупреждением "публичные репозитории,
жёсткие лимиты", а не скрывать их. Если хостинг временно недоступен по сети,
эндпоинт всё равно вернёт запись для него (просто без данных о лимите),
чтобы одна недоступная сторона не ломала весь список.

## Безопасность

- Аутентификация между сервисами (AgentsCore/AgentsApp → этот сервис) **не
  реализована** в этой версии — есть только пустая заготовка `MCP_API_KEY` в
  конфигурации на всех трёх сторонах, чтобы включить проверку позже без
  переделки кода. До тех пор сервер следует разворачивать в доверенной сети
  (не выставлять напрямую в интернет).
- `execute_git_command` по умолчанию **выключен** (`MCP_LOCAL_GIT_ENABLED`).
  Когда включён, он выполняет **произвольную** git-подкоманду
  (`create_subprocess_exec`, не через shell — классическая инъекция вида
  `; rm -rf /` через аргумент невозможна), включая потенциально разрушительные
  (`push --force`, `reset --hard`). Сам по себе инструмент не проверяет,
  насколько вызов "безопасен" — это ответственность вызывающей стороны
  (модели через AgentsCore). Явно не рекомендуется открывать этот сервис
  без сетевой изоляции, пока не введена аутентификация.
- Токены Git-хостингов никогда не попадают в логи (проверено в
  `git_hosts/*`: логируются код ответа и хост, не тело запроса/заголовки).

## Ограничения окружения разработки

Этот проект писался в песочнице, где недоступны для установки `fastapi`,
`apscheduler`, `respx`, `croniter`, `pytest` (сеть к pip заблокирована для
части пакетов). Из-за этого:

- `mcp_server/cron.py` — собственная чистая реализация разбора расписаний
  (`every:`/`daily:`/cron), а не обёртка над `croniter`. `scheduler.py`
  использует `apscheduler`'ные `IntervalTrigger`/`CronTrigger` напрямую в
  реальном деплое (там `croniter` не нужен — у apscheduler свой парсер);
  `cron.py` используется независимо для `next_run_at` в REST-ответах и
  валидации, тестируемых без установленного apscheduler.
- `mcp_server/scheduler.py` импортирует `apscheduler.*` только внутри тел
  функций — модуль импортируется даже без установленного пакета;
  `tests/test_scheduler.py` помечен `@unittest.skipUnless(...)` и пропускается
  в этом окружении (написан и готов выполняться при реальном деплое).
- `tests/test_api.py` (FastAPI `TestClient`) аналогично помечен
  `skipUnless` — сама бизнес-логика, которую вызывают REST-роуты
  (`store.py`, `git_hosts/*`), протестирована отдельно и полно.
- `tests/test_git_hosts.py`/`test_actions.py` используют `httpx.MockTransport`
  вместо `respx` (тоже недоступен) — фейковый HTTP-транспорт того же уровня
  детализации (проверка URL/заголовков/тела запроса, симуляция 401/403/404/429
  и повторов с backoff).

Перед деплоем: `pip install -r requirements.txt` в окружении с доступом к
PyPI и `python -m unittest discover -s tests` — тогда пропущенные здесь
тесты (`test_scheduler.py`, `test_api.py`) тоже выполнятся.

**Урок на будущее (зафиксирован здесь намеренно):** именно из-за того, что
`test_api.py` не выполнялся в песочнице, где писался этот код, два
реальных бага в монтировании `/mcp` (см. «Устранение неполадок» ниже)
обнаружились только при первом живом запуске у пользователя, а не тестами.
`tests/test_api.py::McpEndpointTests` — тест именно на этот случай
(реальный HTTP-запрос к `/mcp`, а не только к `/api/*`), добавлен когда
баг уже был найден и исправлен; при доступном `fastapi` обязательно
прогонять его вместе с остальными.

## Устранение неполадок

**`AgentsCore` пишет в лог `MCP list_tools недоступен: MCP-сервер ответил
404` при любом значении `MCP_SERVER_URL`** (и с `/mcp` на конце, и без) —
это была ошибка на стороне САМОГО mcp_server (уже исправлена в этой
версии), а не в настройке AgentsCore. Причина — двойное монтирование:
`mcp.server.fastmcp.FastMCP.streamable_http_app()` по умолчанию сама
регистрирует единственный маршрут под `/mcp` (внутри Starlette-приложения,
которое она возвращает), а `mcp_server/app.py` монтировал результат ЕЩЁ
РАЗ под внешним префиксом `/mcp` — реально отвечающий путь получался
`/mcp/mcp`, а сам `/mcp` отвечал `404 Not Found`. Исправлено монтированием
под корнем (`app.mount("/", ...)` вместо `app.mount("/mcp", ...)`) — так
внутренний маршрут `/mcp` остаётся единственным и ничем не задваивается.
Если после обновления `mcp_server` до этой версии `404` всё ещё
воспроизводится — проверьте, что запущен именно обновлённый код
(`python -m mcp_server` из этого архива), а не старая версия.

**После исправления 404 запрос к `/mcp` мог бы вместо этого падать с
`RuntimeError: Task group is not initialized. Make sure to use run()`** —
второй, независимый баг (тоже уже исправлен): у Starlette-приложения,
смонтированного через `app.mount(...)`, собственный lifespan НЕ
запускается автоматически (ASGI lifespan-события доставляются только
приложению верхнего уровня) — а `streamable_http_app()` требует, чтобы
именно её lifespan (`session_manager.run()`) был запущен, иначе она не
может обработать вообще ни одного запроса. Исправлено явным объединением
lifespan'ов в `create_app_with_store` (`contextlib.AsyncExitStack`,
`mcp.session_manager.run()`) — приём из докстринга самого
`FastMCP.session_manager`, официально рассчитанного как раз на
монтирование FastMCP внутри стороннего ASGI-приложения.

## Структура

```
mcp_server/
  __main__.py       # точка входа: python -m mcp_server
  app.py             # сборка FastAPI: монтирует /mcp и /api, lifespan (старт/стоп планировщика)
  config.py          # MCPConfig — вся конфигурация из окружения/.env
  server.py          # 11 MCP-инструментов (@mcp.tool())
  api.py             # REST-роутер для AgentsApp
  schemas.py         # Pydantic-схемы REST API
  deps.py            # FastAPI Depends (store/scheduler/mcp)
  db.py              # sqlite3: scheduled_tools, scheduled_tool_runs
  store.py           # бизнес-логика над db.py (валидация, next_run_at, история)
  scheduler.py        # обёртка над APScheduler (один инстанс на процесс)
  actions.py          # исполнение action: git_pull/http_fetch/git_host_poll + execute_git_command
  cron.py            # свой разбор расписаний (see "Ограничения окружения разработки")
  models.py          # dataclasses ScheduledTool/ScheduledToolRun/GitHostStatus
  git_hosts/
    base.py          # общая retry/backoff/rate-limit логика, GitHostError
    github.py, gitlab.py, gitea.py   # конкретные провайдеры
    __init__.py       # get_provider(host), кэш httpx.AsyncClient, close_all_clients()
tests/                # 110 тестов (14 пропущено в этом окружении, см. выше)
```
