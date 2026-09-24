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

## MCP-инструменты (`/mcp`, для AgentsCore)

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
| GET | `/api/status` | `{scheduler_running, tool_count, scheduled_count}` |
| GET | `/api/tools` | доступные инструменты `{name, description, parameters, schedulable}` |
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
- `execute_git_command` выполняет **произвольную** git-подкоманду
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
