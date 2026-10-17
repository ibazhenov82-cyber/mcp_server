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
- «HTTP-запросы» (`http_fetch`) — при `MCP_HTTP_FETCH_ENABLED`;
- «Репозиторий» (`repo_*` — проект по ссылке, чтение кода и истории) и
  «Репозиторий: запись» (`repo_commit`, `repo_undo_commit`, с пометкой
  `agentscore/direct_only` — модели не предлагаются, их вызывает AgentsCore
  после подтверждения пользователя) — при `MCP_REPOS_ENABLED`;
- «Пользователи» (`user_get`, `users_search`, `users_list` — справочник
  пользователей сервиса поддержки из JSON-файла; `users_list` — только для
  прямого вызова сервисом) — при `MCP_USERS_ENABLED`.

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
from .repos import RepoError, RepoManager
from .users import UserDirectory, UsersError
from .results import ResultNotFoundError, ResultStore
from .snapshots import SnapshotError, SnapshotStore, extract_items
from .web_pages import fetch_page
from .web_search import make_ddgs_search

_EXPECTED_ERRORS = (
    ActionError, GitHostError, ValueError, ToolError, ResultNotFoundError, PipelineError, SnapshotError, RepoError,
    UsersError,
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
GROUP_REPO = "Репозиторий"
GROUP_REPO_WRITE = "Репозиторий: запись"
GROUP_USERS = "Пользователи"

#: Ключ `_meta` инструмента: вызывается только сервисом напрямую (после
#: подтверждения пользователя), модели не предлагается.
DIRECT_ONLY_META_KEY = "agentscore/direct_only"


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


def build_repo_manager() -> RepoManager:
    cfg = MCPConfig
    return RepoManager(
        cfg.repos_dir(), cfg.PROJECT_ROOTS, data_dir=cfg.DATA_DIR, clone_depth=cfg.REPO_CLONE_DEPTH,
        fetch_interval=cfg.REPO_FETCH_INTERVAL, clone_timeout=cfg.REPO_CLONE_TIMEOUT,
        allowed_hosts=cfg.repo_allowed_hosts(), credentials=cfg.git_credentials,
    )


def build_mcp_server(
    features: Optional[Features] = None, pipeline_tools: Optional[PipelineTools] = None,
    repo_manager: Optional[RepoManager] = None, user_directory: Optional[UserDirectory] = None,
) -> FastMCP:
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

    if features.repos:
        repos = repo_manager if repo_manager is not None else build_repo_manager()
        _register_repo_tools(mcp, repos)

    if features.users:
        users = user_directory if user_directory is not None else UserDirectory(MCPConfig.users_file())
        _register_user_tools(mcp, users)

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


def _register_user_tools(mcp: FastMCP, users: UserDirectory) -> None:
    """Группа «Пользователи»: справочник пользователей поддержки. AgentsCore
    в чате поддержки подставляет `user_id` сам — модель видит только
    данные своего собеседника."""

    def reg(title: str, direct_only: bool = False):
        meta = _group(GROUP_USERS)
        if direct_only:
            meta[DIRECT_ONLY_META_KEY] = True
        return mcp.tool(title=title, meta=meta)

    async def run(ctx: Optional[Context], name: str, fn) -> Dict[str, Any]:
        allowed = caller_allowed_tools(ctx)
        if allowed is not None and name not in allowed:
            return {"error": f"инструмент {name} не разрешён в этом чате"}
        try:
            return await asyncio.to_thread(fn)
        except _EXPECTED_ERRORS as exc:
            return {"error": str(exc)}

    @reg("Данные пользователя")
    async def user_get(user_id: str, ctx: Optional[Context] = None) -> Dict[str, Any]:
        """Карточка пользователя по id: фамилия, имя, отчество, полное имя, обращение (имя и отчество),
        проект, профиль (например «Технический специалист» или «Пользователь») и дополнительные поля."""
        return await run(ctx, "user_get", lambda: users.get(user_id))

    @reg("Поиск пользователей")
    async def users_search(query: str, project: Optional[str] = None, limit: int = 20,
                           ctx: Optional[Context] = None) -> Dict[str, Any]:
        """Поиск пользователей по словам из id, ФИО, проекта и профиля (все слова должны встретиться);
        project — только пользователи этого проекта (без учёта регистра)."""
        return await run(ctx, "users_search", lambda: users.search(query, project, limit))

    @reg("Список пользователей", direct_only=True)
    async def users_list(project: Optional[str] = None, profile: Optional[str] = None, limit: int = 500,
                         offset: int = 0, ctx: Optional[Context] = None) -> Dict[str, Any]:
        """Список пользователей (постранично) с фильтром по проекту и профилю; version — отпечаток файла
        справочника (меняется при любой правке)."""
        return await run(ctx, "users_list", lambda: users.list(project, profile, limit, offset))


def _register_repo_tools(mcp: FastMCP, repos: RepoManager) -> None:
    """Группа «Репозиторий». Все инструменты, кроме `repo_open`, принимают
    `repo_id` из ответа `repo_open` (AgentsCore подставляет его сам — модель
    этот параметр не видит)."""

    def reg(title: str, group: str = GROUP_REPO, direct_only: bool = False):
        meta = _group(group)
        if direct_only:
            meta[DIRECT_ONLY_META_KEY] = True
        return mcp.tool(title=title, meta=meta)

    def _check_allowed(ctx: Optional[Context], name: str) -> Optional[Dict[str, Any]]:
        allowed = caller_allowed_tools(ctx)
        if allowed is not None and name not in allowed:
            return {"error": f"инструмент {name} не разрешён в этом чате"}
        return None

    async def run(ctx: Optional[Context], name: str, repo_id: str, fn) -> Dict[str, Any]:
        denied = _check_allowed(ctx, name)
        if denied:
            return denied
        try:
            ref = repos.resolve(repo_id)
            return await fn(ref)
        except _EXPECTED_ERRORS as exc:
            return {"error": str(exc)}

    @reg("Открыть проект по ссылке")
    async def repo_open(link: str, branch: Optional[str] = None, refresh: bool = True,
                        ctx: Optional[Context] = None) -> Dict[str, Any]:
        """Открывает проект: URL репозитория GitHub / GitLab / Gitea (можно с /tree/<ветка>) — неглубокий
        клон на сервере, при повторном открытии обновляется; либо локальный путь рабочей копии внутри
        разрешённых папок. Возвращает repo_id (его принимают остальные инструменты «Репозиторий»), ветку,
        ветку по умолчанию, адрес origin, последний коммит и writable (коммиты возможны только в
        локальной рабочей копии)."""
        denied = _check_allowed(ctx, "repo_open")
        if denied:
            return denied
        try:
            return await repos.open(link, branch, refresh)
        except _EXPECTED_ERRORS as exc:
            return {"error": str(exc)}

    @reg("Состояние репозитория")
    async def repo_status(repo_id: str, ctx: Optional[Context] = None) -> Dict[str, Any]:
        """Текущая ветка, последний коммит, отставание/опережение origin (ahead/behind), изменённые,
        подготовленные и новые файлы."""
        return await run(ctx, "repo_status", repo_id, repos.status)

    @reg("История коммитов")
    async def repo_log(repo_id: str, path: Optional[str] = None, ref: Optional[str] = None, limit: int = 20,
                       author: Optional[str] = None, grep: Optional[str] = None, since: Optional[str] = None,
                       ctx: Optional[Context] = None) -> Dict[str, Any]:
        """Коммиты (sha, автор, дата, заголовок), новые сверху. `path` — только затрагивающие файл или
        папку; `ref` — ветка или коммит (по умолчанию текущая ветка); `grep` — поиск по сообщению;
        `since` — например '2 weeks ago' или '2026-09-01'; `limit` до 100."""
        return await run(ctx, "repo_log", repo_id, lambda r: repos.log(r, path, ref, limit, author, grep, since))

    @reg("Коммит подробно")
    async def repo_show(repo_id: str, commit: str, path: Optional[str] = None, max_chars: int = 20000,
                        ctx: Optional[Context] = None) -> Dict[str, Any]:
        """Сообщение коммита, список файлов с числом строк и патч (обрезается до `max_chars`);
        `path` — только изменения этого файла."""
        return await run(ctx, "repo_show", repo_id, lambda r: repos.show(r, commit, path, max_chars))

    @reg("Поиск по коду")
    async def repo_grep(repo_id: str, pattern: str, path: Optional[str] = None, ignore_case: bool = True,
                        fixed: bool = False, max_results: int = 50, ref: Optional[str] = None,
                        ctx: Optional[Context] = None) -> Dict[str, Any]:
        """git grep по файлам репозитория: строки (path, line, text). `pattern` — регулярное выражение
        (fixed=true — обычная строка); `path` — папка или файл; `ref` — искать в ветке или коммите."""
        return await run(ctx, "repo_grep", repo_id,
                         lambda r: repos.grep(r, pattern, path, ignore_case, fixed, max_results, ref))

    @reg("Изменения (diff)")
    async def repo_diff(repo_id: str, spec: Optional[str] = None, path: Optional[str] = None,
                        max_chars: int = 30000, ctx: Optional[Context] = None) -> Dict[str, Any]:
        """Diff: без `spec` — незакоммиченные изменения (против HEAD) и новые файлы; 'staged' —
        подготовленные к коммиту; 'main..feature' — между ветками или коммитами ('a...b' — с момента
        ответвления); 'pr:12' — pull request (merge request в GitLab) от точки ответвления от базовой
        ветки, в том числе из форка; одна ветка или коммит — рабочая копия против него. Файлы со статусом
        и числом строк, патч обрезается до `max_chars`. Версию файла из PR читает repo_read_file с
        ref='pr:12'."""
        return await run(ctx, "repo_diff", repo_id, lambda r: repos.diff(r, spec, path, max_chars))

    @reg("Авторство строк (blame)")
    async def repo_blame(repo_id: str, path: str, start_line: Optional[int] = None, end_line: Optional[int] = None,
                         ref: Optional[str] = None, ctx: Optional[Context] = None) -> Dict[str, Any]:
        """Кто и в каком коммите последний раз менял строки файла (до 200 строк за вызов) и сводка по
        коммитам."""
        return await run(ctx, "repo_blame", repo_id, lambda r: repos.blame(r, path, start_line, end_line, ref))

    @reg("Список файлов проекта")
    async def repo_list_files(repo_id: str, path: Optional[str] = None, depth: int = 2,
                              pattern: Optional[str] = None, max_entries: int = 300,
                              ctx: Optional[Context] = None) -> Dict[str, Any]:
        """Дерево файлов под контролем git: папки (с числом файлов) и файлы до глубины `depth` от `path`.
        `pattern` — маска ('*.kt', 'docs/*.md') — плоский список подходящих файлов."""
        return await run(ctx, "repo_list_files", repo_id,
                         lambda r: repos.list_files(r, path, depth, pattern, max_entries))

    @reg("Чтение файла проекта")
    async def repo_read_file(repo_id: str, path: str, ref: Optional[str] = None, start_line: Optional[int] = None,
                             end_line: Optional[int] = None, max_chars: int = 20000,
                             ctx: Optional[Context] = None) -> Dict[str, Any]:
        """Текст файла с номерами строк (диапазон start_line..end_line, обрезка до `max_chars`);
        `ref` — версия из ветки или коммита."""
        return await run(ctx, "repo_read_file", repo_id,
                         lambda r: repos.read_file(r, path, ref, start_line, end_line, max_chars))

    @reg("Коммит изменений", GROUP_REPO_WRITE, direct_only=True)
    async def repo_commit(repo_id: str, message: str, files: Optional[List[str]] = None,
                          ctx: Optional[Context] = None) -> Dict[str, Any]:
        """Коммит в локальной рабочей копии: `files` — какие файлы добавить в коммит (без них —
        только уже подготовленные). Вызывается после подтверждения пользователя."""
        return await run(ctx, "repo_commit", repo_id, lambda r: repos.commit(r, message, files))

    @reg("Откат коммита", GROUP_REPO_WRITE, direct_only=True)
    async def repo_undo_commit(repo_id: str, commit: str, ctx: Optional[Context] = None) -> Dict[str, Any]:
        """Откатывает последний коммит, сделанный через repo_commit (git reset --soft HEAD~1, изменения
        остаются в индексе), если он ещё не отправлен в удалённый репозиторий."""
        return await run(ctx, "repo_undo_commit", repo_id, lambda r: repos.undo_commit(r, commit))
