"""
mcp_server.users
==================

Группа «Пользователи» (`MCP_USERS_ENABLED`): справочник пользователей
сервиса поддержки. Базы пользователей нет — данные лежат в JSON-файле
`MCP_USERS_FILE` (по умолчанию `MCP_DATA_DIR/users.json`), сервер читает
его и перечитывает при изменении (по mtime и размеру файла), так что файл
можно править «на ходу».

Формат файла — список пользователей или объект `{"users": [...]}`:

    {"users": [
      {"id": "u-001", "last_name": "Петров", "first_name": "Иван",
       "middle_name": "Сергеевич", "project": "AgentsCore",
       "profile": "Технический специалист"}
    ]}

`middle_name` (отчество) необязательно. Допускаются и «человеческие»
ключи: «Фамилия», «Имя», «Отчество», «Проект», «Профиль»; лишние поля
сохраняются в `extra` и отдаются как есть (например, должность, телефон).

`version` в ответах — отпечаток содержимого файла: AgentsCore по нему
понимает, что кэш устарел, не сравнивая данные целиком.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Any, Dict, List, Optional


class UsersError(ValueError):
    """Ошибка справочника пользователей — текст уходит модели/клиенту."""


_KEY_ALIASES = {
    "id": ("id", "user_id", "ID", "Id"),
    "last_name": ("last_name", "lastName", "surname", "Фамилия", "фамилия"),
    "first_name": ("first_name", "firstName", "name", "Имя", "имя"),
    "middle_name": ("middle_name", "middleName", "patronymic", "Отчество", "отчество"),
    "project": ("project", "Проект", "проект"),
    "profile": ("profile", "Профиль", "профиль"),
}
_ALL_ALIASES = {alias for aliases in _KEY_ALIASES.values() for alias in aliases}


def _pick(raw: Dict[str, Any], key: str) -> str:
    for alias in _KEY_ALIASES[key]:
        value = raw.get(alias)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def normalize_user(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise UsersError("каждый пользователь — объект JSON")
    user = {key: _pick(raw, key) for key in _KEY_ALIASES}
    if not user["id"]:
        raise UsersError(f"у пользователя нет id: {raw!r}")
    if not user["first_name"]:
        raise UsersError(f"у пользователя {user['id']} нет имени")
    user["middle_name"] = user["middle_name"] or None
    user["full_name"] = " ".join(p for p in (user["last_name"], user["first_name"], user["middle_name"]) if p)
    # Обращение «как к человеку»: имя и отчество, без отчества — имя.
    user["display_name"] = " ".join(p for p in (user["first_name"], user["middle_name"]) if p)
    extra = {k: v for k, v in raw.items() if k not in _ALL_ALIASES}
    if extra:
        user["extra"] = extra
    return user


class UserDirectory:
    """Справочник из JSON-файла с кэшем в памяти: файл читается при первом
    обращении и перечитывается, только если изменились его mtime/размер."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._stamp: Optional[tuple] = None
        self._users: List[Dict[str, Any]] = []
        self._by_id: Dict[str, Dict[str, Any]] = {}
        self._version = ""

    # ---- загрузка -----------------------------------------------------------

    def _load(self) -> None:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            raise UsersError(f"файл пользователей не найден: {self.path} (MCP_USERS_FILE)") from None
        stamp = (st.st_mtime_ns, st.st_size)
        with self._lock:
            if stamp == self._stamp:
                return
            with open(self.path, "rb") as fh:
                data = fh.read()
            try:
                parsed = json.loads(data.decode("utf-8-sig"))
            except ValueError as exc:
                raise UsersError(f"файл пользователей — некорректный JSON: {exc}") from None
            items = parsed.get("users") if isinstance(parsed, dict) else parsed
            if not isinstance(items, list):
                raise UsersError("файл пользователей: ожидается список или {\"users\": [...]}")
            users = [normalize_user(item) for item in items]
            by_id: Dict[str, Dict[str, Any]] = {}
            for user in users:
                if user["id"] in by_id:
                    raise UsersError(f"повторяющийся id пользователя: {user['id']}")
                by_id[user["id"]] = user
            self._users = users
            self._by_id = by_id
            self._version = hashlib.sha1(data).hexdigest()[:12]
            self._stamp = stamp

    # ---- запросы ------------------------------------------------------------

    def version(self) -> str:
        self._load()
        return self._version

    def get(self, user_id: str) -> Dict[str, Any]:
        self._load()
        user_id = (user_id or "").strip()
        if not user_id:
            raise UsersError("не задан user_id")
        user = self._by_id.get(user_id)
        if user is None:
            raise UsersError(f"пользователь {user_id} не найден")
        return {"user": user, "version": self._version}

    def list(self, project: Optional[str] = None, profile: Optional[str] = None,
             limit: int = 500, offset: int = 0) -> Dict[str, Any]:
        self._load()
        users = self._filter(self._users, project, profile)
        limit = max(1, min(int(limit or 500), 5000))
        offset = max(0, int(offset or 0))
        return {
            "users": users[offset:offset + limit], "total": len(users),
            "offset": offset, "version": self._version,
        }

    def search(self, query: str, project: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
        self._load()
        words = [w for w in (query or "").casefold().split() if w]
        if not words:
            raise UsersError("пустой запрос")
        found = []
        for user in self._filter(self._users, project, None):
            hay = " ".join([user["id"], user["full_name"], user["project"], user["profile"]]).casefold()
            if all(w in hay for w in words):
                found.append(user)
        limit = max(1, min(int(limit or 20), 200))
        return {"users": found[:limit], "total": len(found), "version": self._version}

    @staticmethod
    def _filter(users, project, profile):
        project_key = (project or "").strip().casefold()
        profile_key = (profile or "").strip().casefold()
        return [
            u for u in users
            if (not project_key or u["project"].casefold() == project_key)
            and (not profile_key or u["profile"].casefold() == profile_key)
        ]
