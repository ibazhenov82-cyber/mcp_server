"""
mcp_server.server
====================

`build_mcp_server()` собирает `FastMCP` с инструментами сервера. Функции
здесь — тонкие обёртки: разбирают аргументы, вызывают `actions.py` или
`git_hosts/*`, ловят ожидаемые ошибки и возвращают `{"error": ...}` вместо
исключения — модели полезнее прочитать текст ошибки, чем получить
оборванный вызов.

Группы инструментов (передаются в `_meta["agentscore/group"]` каждого
инструмента, см. `GROUP_*`):

- «GIT API» — Git-хостинги (`git_host_*`), всегда;
- «Локальный GIT» (`execute_git_command`, `git_pull`) — при `MCP_LOCAL_GIT_ENABLED`;
- «HTTP-запросы» (`http_fetch`) — при `MCP_HTTP_FETCH_ENABLED`.

Периодических задач здесь больше нет — они в отдельном сервисе
планировщика (scheduler_service), который вызывает эти же инструменты по
протоколу MCP.

Транспорт — только Streamable HTTP (см. `app.py`)."""

from __future__ import annotations

import asyncio
import functools
import inspect
from typing import Any, Dict, List, Optional, Union

from mcp.server.fastmcp import Context, FastMCP

from .actions import ActionError, git_pull as git_pull_action, http_fetch as http_fetch_action, run_git_command
from .config import MCPConfig
from .features import Features
from .files import FileStore
from .git_hosts import GitHostError, SUPPORTED_HOSTS, get_provider
from .llm import LLMClient
from .pipeline import PipelineError, run_pipeline as run_pipeline_impl
from .pipeline_tools import PipelineTools, ToolError
from .results import ResultNotFoundError, ResultStore
from .snapshots import SnapshotError, SnapshotStore, extract_items
from .web_pages import fetch_page
from .web_search import make_ddgs_search

_EXPECTED_ERRORS = (
    ActionError, GitHostError, ValueError, ToolError, ResultNotFoundError, PipelineError, SnapshotError,
)

#: git_host_* возвращают список нормализованных элементов, один dict или
#: {"error": ...} — конкретная аннотация нужна, чтобы FastMCP построил
#: structured output.
GitHostResult = Union[List[Dict[str, Any]], Dict[str, Any]]

#: Ключ `_meta` инструмента с его группой — AgentsCore и приложение
#: показывают её в списке инструментов вместо названия сервера.
GROUP_META_KEY = "agentscore/group"
GROUP_GIT_API = "GIT API"
GROUP_LOCAL_GIT = "Локальный GIT"
GROUP_HTTP = "HTTP-запросы"
GROUP_WEB_SEARCH = "Интернет поиск"
GROUP_LLM = "Обработка LLM"
GROUP_FILES = "Работа с файлами"
GROUP_PIPELINES = "Пайплайны"


def _group(name: str) -> Dict[str, Any]:
    return {GROUP_META_KEY: name}


#: `_meta` вызова от AgentsCore: из какого чата он пришёл и какие
#: инструменты этого сервера в чате разрешены.
META_CHAT_ID = "agentscore/chat_id"
META_ALLOWED_TOOLS = "agentscore/allowed_tools"

#: Инструменты-источники, ответ которых можно сохранять и сравнивать.
TRACKABLE_PREFIXES = ("git_host_list_",)
TRACKABLE_TOOLS = {"duckduckgo_search"}


def _caller_meta(ctx: Optional[Context]) -> Dict[str, Any]:
    try:
        meta = ctx.request_context.meta if ctx is not None else None
    except (ValueError, LookupError, AttributeError):
        return {}
    return meta.model_dump() if meta is not None else {}


def caller_chat_id(ctx: Optional[Context]) -> Optional[str]:
    """Чат, из которого пришёл вызов (None — вызов не из чата AgentsCore)."""
    return _caller_meta(ctx).get(META_CHAT_ID) or None


def caller_allowed_tools(ctx: Optional[Context]) -> Optional[set]:
    """Инструменты, выбранные в чате (None — вызов не из чата: планировщик,
    прямой вызов — ограничений нет). Через `run_pipeline` и
    `save_tool_result` нельзя вызвать инструмент, не выбранный в чате."""
    allowed = _caller_meta(ctx).get(META_ALLOWED_TOOLS)
    return {str(n) for n in allowed} if isinstance(allowed, list) else None


def _is_trackable(name: str) -> bool:
    return name in TRACKABLE_TOOLS or name.startswith(TRACKABLE_PREFIXES)


def build_pipeline_tools(features: Features) -> PipelineTools:
    """Инструменты-звенья пайплайнов по настройкам `MCPConfig`."""
    cfg = MCPConfig
    return PipelineTools(
        ResultStore(cfg.DATA_DIR, cfg.RESULT_TTL_HOURS),
        search_fn=make_ddgs_search(cfg.DDGS_BACKEND, cfg.DDGS_TIMEOUT) if features.web_search else None,
        region=cfg.DDGS_REGION,
        llm=LLMClient(cfg.LLM_BASE_URL, cfg.LLM_API_KEY, cfg.LLM_MODEL, cfg.LLM_TIMEOUT, cfg.LLM_MAX_TOKENS)
        if features.llm else None,
        page_reader=lambda url: fetch_page(url, timeout=cfg.HTTP_TIMEOUT),
        files=FileStore(cfg.FILES_DIR) if features.files else None,
    )


def build_mcp_server(features: Optional[Features] = None, pipeline_tools: Optional[PipelineTools] = None) -> FastMCP:
    """`features` — какие включаемые группы регистрировать; по умолчанию —
    по настройкам `MCPConfig`. `pipeline_tools` — звенья пайплайнов (тесты
    передают свои, с подменённым поиском и моделью)."""
    features = features if features is not None else Features.from_config()
    pipeline_tools = pipeline_tools if pipeline_tools is not None else build_pipeline_tools(features)
    # stateless_http — каждый HTTP-запрос самодостаточен (без сессии);
    # json_response — обычный JSON вместо SSE. `streamable_http_path`
    # оставлен по умолчанию ("/mcp"), приложение монтируется под корнем
    # (см. app.py).
    mcp = FastMCP("mcp-server", stateless_http=True, json_response=True)
    #: Имя -> функция инструмента: то, что может вызывать run_pipeline.
    registry: Dict[str, Any] = {}

    def tool(title: str, group: str):
        def decorator(fn):
            mcp.tool(title=title, meta=_group(group))(fn)
            registry[fn.__name__] = fn
            return fn

        return decorator

    if features.local_git:

        @tool("Выполнение git-команды в локальном репозитории", GROUP_LOCAL_GIT)
        async def execute_git_command(repo_path: str, command: str, args: Optional[List[str]] = None) -> Dict[str, Any]:
            """Выполняет git-команду над локальной рабочей копией
            (`git -C repo_path <command> <args...>`), не через shell."""
            try:
                return await run_git_command(repo_path, command, args)
            except _EXPECTED_ERRORS as exc:
                return {"error": str(exc)}

        @tool("Обновление локального репозитория", GROUP_LOCAL_GIT)
        async def git_pull(repo_path: str, remote: str = "origin", branch: Optional[str] = None) -> Dict[str, Any]:
            """`git pull` в локальной рабочей копии `repo_path`."""
            try:
                return await git_pull_action(repo_path, remote, branch)
            except _EXPECTED_ERRORS as exc:
                return {"error": str(exc)}

    if features.http_fetch:

        @tool("HTTP-запрос", GROUP_HTTP)
        async def http_fetch(
            url: str, method: str = "GET", headers: Optional[Dict[str, str]] = None,
            body: Optional[Dict[str, Any]] = None, timeout: float = 30,
        ) -> Dict[str, Any]:
            """HTTP-запрос с сервера: код ответа, заголовки и тело (обрезается до 5000 символов)."""
            try:
                return await http_fetch_action(url, method, headers, body, timeout)
            except _EXPECTED_ERRORS as exc:
                return {"error": str(exc)}

    async def _git_host_call(host: str, coro_factory) -> GitHostResult:
        if host not in SUPPORTED_HOSTS:
            return {"error": f"Неизвестный host={host!r}: допустимо {SUPPORTED_HOSTS}"}
        try:
            provider = get_provider(host)
            return await coro_factory(provider)
        except _EXPECTED_ERRORS as exc:
            return {"error": str(exc)}

    @tool("Получение списка коммитов", GROUP_GIT_API)
    async def git_host_list_commits(
        host: str, owner: str, repo: str, since: Optional[str] = None, until: Optional[str] = None,
        branch: Optional[str] = None, limit: int = 50,
    ) -> GitHostResult:
        """`host` — 'github' | 'gitlab' | 'gitea'. `since`/`until` — ISO8601
        либо относительное значение вида '-1h'/'-1d'."""
        return await _git_host_call(host, lambda p: p.list_commits(owner, repo, since=since, until=until, branch=branch, limit=limit))

    @tool("Получение списка pull request'ов", GROUP_GIT_API)
    async def git_host_list_pull_requests(
        host: str, owner: str, repo: str, state: str = "all", since: Optional[str] = None, limit: int = 50,
    ) -> GitHostResult:
        """`state` — 'open' | 'closed' | 'all'."""
        return await _git_host_call(host, lambda p: p.list_pull_requests(owner, repo, state=state, since=since, limit=limit))

    @tool("Получение списка issues", GROUP_GIT_API)
    async def git_host_list_issues(
        host: str, owner: str, repo: str, state: str = "all", since: Optional[str] = None, limit: int = 50,
    ) -> GitHostResult:
        """`state` — 'open' | 'closed' | 'all'."""
        return await _git_host_call(host, lambda p: p.list_issues(owner, repo, state=state, since=since, limit=limit))

    @tool("Получение списка релизов", GROUP_GIT_API)
    async def git_host_list_releases(host: str, owner: str, repo: str, limit: int = 20) -> GitHostResult:
        return await _git_host_call(host, lambda p: p.list_releases(owner, repo, limit=limit))

    @tool("Получение информации о репозитории", GROUP_GIT_API)
    async def git_host_get_repo(host: str, owner: str, repo: str) -> GitHostResult:
        return await _git_host_call(host, lambda p: p.get_repo(owner, repo))

    @tool("Получение файла из репозитория", GROUP_GIT_API)
    async def git_host_get_file(host: str, owner: str, repo: str, path: str, ref: Optional[str] = None) -> GitHostResult:
        return await _git_host_call(host, lambda p: p.get_file(owner, repo, path, ref=ref))

    async def _call(coro) -> Dict[str, Any]:
        try:
            return await coro
        except _EXPECTED_ERRORS as exc:
            return {"error": str(exc)}

    if features.web_search:

        @tool("Поиск в DuckDuckGo", GROUP_WEB_SEARCH)
        async def duckduckgo_search(
            query: str, max_results: int = 10, timelimit: Optional[str] = None, region: Optional[str] = None,
        ) -> Dict[str, Any]:
            """Поиск в интернете через DuckDuckGo. Возвращает только список найденных страниц
            (title, url, snippet — короткий фрагмент), сами страницы НЕ читает: чтобы узнать
            содержимое (новости, статью, факты), открой нужную ссылку инструментом read_web_page.
            Если пользователь назвал конкретный сайт (например, rbc.ru), его можно открыть сразу
            через read_web_page, без поиска. `timelimit`: 'd' | 'w' | 'm' | 'y' — за день/неделю/
            месяц/год. `region` — например 'ru-ru', 'us-en'. `result_id` выдачи можно передать
            следующему шагу (summarize, save_to_text_file)."""
            return await _call(pipeline_tools.duckduckgo_search(query, max_results, timelimit, region))

        @tool("Чтение страницы", GROUP_WEB_SEARCH)
        async def read_web_page(url: str, max_chars: int = 8000, include_links: bool = True) -> Dict[str, Any]:
            """Открывает веб-страницу и возвращает её читаемый текст (без скриптов и меню) и
            ссылки со страницы (links: text, url). Адрес можно без https:// — например 'rbc.ru'.
            Главная страница новостного сайта даёт список заголовков со ссылками: чтобы
            пересказать новость подробно, открой ссылку на статью ещё одним вызовом.
            `max_chars` — сколько текста вернуть (до 20000; полный текст сохраняется, его
            `result_id` можно передать в summarize или save_to_text_file). Не открывает
            внутренние адреса; страницы, которые показывают содержимое только через
            JavaScript, могут вернуться почти пустыми."""
            return await _call(pipeline_tools.read_web_page(url, max_chars, include_links))

    if features.llm:

        @tool("Суммарный ответ", GROUP_LLM)
        async def summarize(
            result_id: Optional[str] = None, text: Optional[str] = None, question: Optional[str] = None,
            depth: str = "snippets", max_pages: int = 3, length: str = "medium",
        ) -> Dict[str, Any]:
            """Суммарный ответ (Markdown), составленный моделью. Материал — `result_id`
            предыдущего шага (например, duckduckgo_search) ИЛИ произвольный `text`.
            Для результатов поиска ответ ссылается на источники [n] и заканчивается
            списком источников. `question` — на что ответить (по умолчанию — поисковый
            запрос). `depth`: 'snippets' — по фрагментам выдачи, 'pages' — загрузить и
            прочитать `max_pages` первых страниц (до 5). `length`: 'short' | 'medium' | 'long'.
            Возвращает `result_id` ответа и сам текст `markdown`."""
            return await _call(pipeline_tools.summarize(result_id, text, question, depth, max_pages, length))

    if features.files:

        @tool("Сохранить в текстовый файл", GROUP_FILES)
        async def save_to_text_file(
            filename: str, result_id: Optional[str] = None, content: Optional[str] = None, overwrite: bool = False,
        ) -> Dict[str, Any]:
            """Сохраняет в текстовый файл (.md, .txt, .csv, .json, .log; без расширения —
            .md) результат предыдущего шага (`result_id`) ИЛИ текст `content`. `filename` —
            относительный путь в каталоге файлов сервера, можно с подстановками {date},
            {time}, {datetime}: 'reports/{date}-mcp.md'. Существующий файл перезаписывается
            только при `overwrite=true`. Возвращает путь, размер и sha256."""
            return await _call(pipeline_tools.save_to_text_file(filename, result_id, content, overwrite))

    save_tool_result_impl = None
    if features.files and pipeline_tools.files is not None:
        snapshots = SnapshotStore(str(pipeline_tools.files.root))

        @tool("Сохранять ответ инструмента", GROUP_FILES)
        async def save_tool_result(
            tool: str, args: Optional[Dict[str, Any]] = None, reset: bool = False, max_items: int = 100,
            ctx: Optional[Context] = None,
        ) -> Dict[str, Any]:
            """Отслеживание изменений: вызывает инструмент-источник `tool` с аргументами `args`
            (git_host_list_commits, git_host_list_pull_requests, git_host_list_issues,
            git_host_list_releases, duckduckgo_search), сохраняет его ответ в JSON для ЭТОГО чата
            и сравнивает с прошлым сохранённым ответом. Первый вызов (first_run=true) возвращает
            все элементы — по ним делается полный отчёт. Следующие вызовы с теми же параметрами
            возвращают только изменения с прошлого раза: new_items (новые коммиты, PR, issues)
            и changed_items (изменившиеся, например закрытый PR); has_changes=false — ничего
            нового. Используй ВМЕСТО прямого вызова инструмента-источника, когда просят следить
            за обновлениями или сообщать только о новом. Пример: tool="git_host_list_commits",
            args={"host": "github", "owner": "octocat", "repo": "hello-world"}. `reset=true` —
            начать заново (забыть сохранённое). `max_items` — сколько элементов вернуть."""
            return await save_tool_result_impl(
                tool, args, reset, max_items, chat_id=caller_chat_id(ctx), allowed=caller_allowed_tools(ctx),
            )

        async def save_tool_result_impl(
            tool: str, args: Optional[Dict[str, Any]] = None, reset: bool = False, max_items: int = 100,
            *, chat_id: Optional[str] = None, allowed: Optional[set] = None,
        ) -> Dict[str, Any]:
            if not _is_trackable(tool) or tool not in registry:
                available = sorted(n for n in registry if _is_trackable(n))
                return {"error": f"инструмент {tool!r} нельзя отслеживать; доступны: {available}"}
            if allowed is not None and tool not in allowed:
                return {"error": f"инструмент {tool!r} не выбран в настройках этого чата"}
            source = registry[tool]
            call_args = dict(args or {})
            try:
                output = await source(**call_args)
            except TypeError as exc:
                return {"error": f"неверные аргументы для {tool}: {exc}"}
            try:
                items = extract_items(output)
            except SnapshotError as exc:
                return {"error": f"{tool}: {exc}"}
            params = inspect.signature(source).parameters
            limit_arg = next((a for a in ("limit", "max_results") if a in params), None)
            requested_limit = None
            if limit_arg:
                requested_limit = call_args.get(limit_arg, params[limit_arg].default)
            result = await asyncio.to_thread(
                snapshots.compare_and_save, chat_id or "shared", tool, call_args, items, reset,
                requested_limit if isinstance(requested_limit, int) else None,
            )
            limit = max(1, min(int(max_items or 100), 500))
            if len(result["new_items"]) > limit:
                result["new_items_omitted"] = len(result["new_items"]) - limit
                result["new_items"] = result["new_items"][:limit]
            result["chat_scoped"] = chat_id is not None
            result["summary"] = (
                f"Первый вызов: сохранено {result['new_count']} элементов — это полный список."
                if result["first_run"] else
                f"С прошлого вызова ({result['previous_run_at']}): новых {result['new_count']}, "
                f"изменившихся {result['changed_count']}."
                + (" Возможно, новых больше, чем поместилось в ответ, — увеличьте limit."
                   if result["possibly_incomplete"] else "")
            )
            return result

    chain_tools = dict(registry)
    tracking_impl = save_tool_result_impl

    @tool("Цепочка инструментов", GROUP_PIPELINES)
    async def run_pipeline(steps: List[Dict[str, Any]], ctx: Optional[Context] = None) -> Dict[str, Any]:
        """Выполняет инструменты этого сервера по очереди за один вызов. Шаг —
        {"tool": "<имя>", "args": {...}}. В аргументах "$prev" — result_id предыдущего
        шага, "$prev.<поле>" — его поле, "$2" / "$2.<поле>" — шага 2. Останавливается на
        первой ошибке. Возвращает трассу шагов и проверки передачи данных между ними.
        В цепочке можно использовать только инструменты, выбранные в этом чате. Ответ шага
        без result_id (например, git_host_list_commits) сохраняется автоматически, так что
        "$prev" работает для любого шага.
        Пример: [{"tool": "duckduckgo_search", "args": {"query": "..."}},
        {"tool": "summarize", "args": {"result_id": "$prev"}},
        {"tool": "save_to_text_file", "args": {"result_id": "$prev", "filename": "reports/{date}.md"}}]"""
        allowed = caller_allowed_tools(ctx)
        tools = dict(chain_tools)
        if tracking_impl is not None:
            # Снимки — в папке этого чата, источник — только из разрешённых.
            tools["save_tool_result"] = functools.partial(
                tracking_impl, chat_id=caller_chat_id(ctx), allowed=allowed,
            )
        return await _call(run_pipeline_impl(steps, tools, allowed=allowed, store=pipeline_tools.store))

    return mcp
