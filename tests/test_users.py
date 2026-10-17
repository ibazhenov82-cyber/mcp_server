"""Тесты группы «Пользователи»: справочник из JSON-файла и MCP-инструменты."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest

from mcp_server.features import Features
from mcp_server.server import DIRECT_ONLY_META_KEY, GROUP_META_KEY, build_mcp_server
from mcp_server.users import UserDirectory, UsersError

USERS = [
    {"id": "u1", "last_name": "Петров", "first_name": "Иван", "middle_name": "Сергеевич",
     "project": "AgentsCore", "profile": "Технический специалист", "position": "инженер"},
    {"Фамилия": "Смирнова", "Имя": "Анна", "Проект": "agentscore", "Профиль": "Пользователь", "id": "u2"},
    {"id": "u3", "last_name": "Орлов", "first_name": "Дмитрий", "project": "Knowledge", "profile": "Пользователь"},
]


def _write(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)


class UserDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "users.json")
        _write(self.path, {"users": USERS})
        self.users = UserDirectory(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_get_normalizes_and_builds_names(self):
        user = self.users.get("u1")["user"]
        self.assertEqual(user["full_name"], "Петров Иван Сергеевич")
        self.assertEqual(user["display_name"], "Иван Сергеевич")
        self.assertEqual(user["extra"], {"position": "инженер"})
        anna = self.users.get("u2")["user"]
        self.assertEqual((anna["last_name"], anna["first_name"], anna["middle_name"]), ("Смирнова", "Анна", None))
        self.assertEqual(anna["display_name"], "Анна")

    def test_list_filters_case_insensitive(self):
        result = self.users.list(project="AGENTSCORE")
        self.assertEqual([u["id"] for u in result["users"]], ["u1", "u2"])
        self.assertEqual(self.users.list(profile="пользователь")["total"], 2)
        self.assertTrue(result["version"])

    def test_search(self):
        self.assertEqual([u["id"] for u in self.users.search("иван петров")["users"]], ["u1"])
        self.assertEqual(self.users.search("пользователь", project="knowledge")["total"], 1)

    def test_errors(self):
        with self.assertRaises(UsersError):
            self.users.get("nope")
        _write(self.path, [{"id": "a", "first_name": "A"}, {"id": "a", "first_name": "B"}])
        with self.assertRaises(UsersError):
            self.users.get("a")

    def test_reload_on_change(self):
        v1 = self.users.version()
        time.sleep(0.01)
        _write(self.path, USERS + [{"id": "u4", "first_name": "Ольга", "project": "AgentsCore"}])
        os.utime(self.path, ns=(time.time_ns(), time.time_ns() + 10_000_000))
        self.assertEqual(self.users.get("u4")["user"]["first_name"], "Ольга")
        self.assertNotEqual(self.users.version(), v1)

    def test_missing_file(self):
        with self.assertRaises(UsersError):
            UserDirectory(os.path.join(self.tmp.name, "none.json")).version()


class UserToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self.tmp.name, "users.json")
        _write(path, USERS)
        self.mcp = build_mcp_server(features=Features(users=True), user_directory=UserDirectory(path))

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def _call(self, name, args):
        _, structured = await self.mcp.call_tool(name, args)
        return structured.get("result", structured)

    async def test_tools_meta(self):
        tools = {t.name: t for t in await self.mcp.list_tools()}
        self.assertEqual({"user_get", "users_search", "users_list"} - set(tools), set())
        self.assertEqual(tools["user_get"].meta[GROUP_META_KEY], "Пользователи")
        self.assertTrue(tools["users_list"].meta.get(DIRECT_ONLY_META_KEY))
        self.assertFalse(tools["user_get"].meta.get(DIRECT_ONLY_META_KEY))

    async def test_calls(self):
        self.assertEqual((await self._call("user_get", {"user_id": "u1"}))["user"]["project"], "AgentsCore")
        self.assertIn("не найден", (await self._call("user_get", {"user_id": "x"}))["error"])
        self.assertEqual((await self._call("users_list", {"project": "agentscore"}))["total"], 2)
        self.assertEqual((await self._call("users_search", {"query": "Орлов"}))["users"][0]["id"], "u3")

    async def test_disabled_by_default(self):
        names = {t.name for t in await build_mcp_server(features=Features()).list_tools()}
        self.assertNotIn("user_get", names)


if __name__ == "__main__":
    unittest.main()
