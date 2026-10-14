"""Тесты группы «Репозиторий» (`mcp_server.repos` и MCP-инструменты
`repo_*`): реальный git над временными репозиториями, «удалённый»
репозиторий — локальный bare по file://."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from mcp_server.features import Features
from mcp_server.repos import RepoError, RepoManager, check_rel_path, parse_remote_link
from mcp_server.server import build_mcp_server


def git(*args: str, cwd) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout


def _identity(path) -> None:
    git("config", "user.email", "t@example.com", cwd=path)
    git("config", "user.name", "Tester", cwd=path)


def _tool(result) -> dict:
    _, structured = result
    return structured.get("result", structured)


class ParseLinkTests(unittest.TestCase):
    def test_github_variants(self):
        self.assertEqual(parse_remote_link("https://github.com/acme/widgets"), ("github.com", "acme/widgets", None))
        self.assertEqual(parse_remote_link("https://github.com/acme/widgets.git"), ("github.com", "acme/widgets", None))
        self.assertEqual(parse_remote_link("github.com/acme/widgets/tree/feature/x"),
                         ("github.com", "acme/widgets", "feature/x"))
        self.assertEqual(parse_remote_link("https://github.com/acme/widgets/blob/dev/README.md"),
                         ("github.com", "acme/widgets", "dev"))
        self.assertEqual(parse_remote_link("https://github.com/acme/widgets/issues/5"),
                         ("github.com", "acme/widgets", None))
        self.assertEqual(parse_remote_link("git@github.com:acme/widgets.git"), ("github.com", "acme/widgets", None))

    def test_gitlab_and_gitea(self):
        self.assertEqual(parse_remote_link("https://gitlab.com/group/sub/repo/-/tree/main"),
                         ("gitlab.com", "group/sub/repo", "main"))
        self.assertEqual(parse_remote_link("https://gitea.com/o/r/src/branch/dev"), ("gitea.com", "o/r", "dev"))

    def test_bad_links(self):
        for link in ("https://github.com/acme", "ftp://x.com/a/b", "https://github.com/acme/-x/tree/--upload-pack=x"):
            with self.assertRaises(RepoError, msg=link):
                parse_remote_link(link)

    def test_rel_path_checks(self):
        self.assertEqual(check_rel_path("./docs/a.md"), "docs/a.md")
        # Ведущий «/» — от корня репозитория (так часто пишет модель).
        self.assertEqual(check_rel_path("/docs/a.md"), "docs/a.md")
        for bad in ("../etc/passwd", "C:/Windows", "-rf", ".git/config", "a/../../b"):
            with self.assertRaises(RepoError, msg=bad):
                check_rel_path(bad)


class _RepoFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        # «Удалённый» репозиторий acme/widgets: main и feature.
        self.remote = root / "remotes" / "acme" / "widgets.git"
        self.remote.mkdir(parents=True)
        git("init", "--bare", "-b", "main", cwd=self.remote)
        seed = root / "seed"
        seed.mkdir()
        git("init", "-b", "main", cwd=seed)
        _identity(seed)
        (seed / "README.md").write_text("# Widgets\n\nПланировщик запускает задачи.\n", encoding="utf-8")
        (seed / "docs").mkdir()
        (seed / "docs" / "api.md").write_text("GET /items\n", encoding="utf-8")
        (seed / "app.py").write_text("def plan():\n    return 'schedule'\n", encoding="utf-8")
        git("add", ".", cwd=seed)
        git("commit", "-m", "initial", cwd=seed)
        git("remote", "add", "origin", self.remote.as_uri(), cwd=seed)
        git("push", "origin", "main", cwd=seed)
        git("checkout", "-b", "feature", cwd=seed)
        (seed / "app.py").write_text("def plan():\n    return 'schedule v2'\n", encoding="utf-8")
        git("commit", "-am", "feature: v2", cwd=seed)
        git("push", "origin", "feature", cwd=seed)
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.remote)
        self.seed = seed

        # Локальный проект внутри разрешённого корня.
        self.projects = root / "projects"
        self.local = self.projects / "local"
        git("clone", self.remote.as_uri(), str(self.local), cwd=root)
        _identity(self.local)

        remotes = root / "remotes"
        self.manager = RepoManager(
            str(root / "data" / "repos"), [str(self.projects)], data_dir=str(root / "data"), clone_depth=50,
            fetch_interval=0, allowed_hosts=["github.com"],
            remote_url=lambda host, slug: (remotes / f"{slug}.git").as_uri(),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()


class RemoteRepoTests(_RepoFixture):
    async def test_open_clones_default_branch_and_reads(self):
        info = await self.manager.open("https://github.com/acme/widgets")
        self.assertEqual(info["repo_id"], "git:github.com/acme/widgets#main")
        self.assertEqual(info["branch"], "main")
        self.assertFalse(info["writable"])
        self.assertEqual(info["head"]["subject"], "initial")
        ref = self.manager.resolve(info["repo_id"])
        files = await self.manager.list_files(ref)
        self.assertIn("README.md", [e["path"] for e in files["entries"]])
        self.assertIn("docs/api.md", [e["path"] for e in files["entries"]])
        text = await self.manager.read_file(ref, "README.md")
        self.assertIn("Планировщик", text["content"])
        self.assertTrue(text["content"].startswith("1| # Widgets"))
        found = await self.manager.grep(ref, "schedule")
        self.assertEqual(found["matches"][0]["path"], "app.py")

    async def test_branch_link_and_diff_between_branches(self):
        info = await self.manager.open("https://github.com/acme/widgets/tree/feature")
        self.assertEqual(info["branch"], "feature")
        ref = self.manager.resolve(info["repo_id"])
        # main не был в клоне ветки — догружается по требованию.
        diff = await self.manager.diff(ref, "main..feature")
        self.assertEqual([f["path"] for f in diff["files"]], ["app.py"])
        self.assertIn("schedule v2", diff["patch"])
        log = await self.manager.log(ref, limit=5)
        self.assertEqual(log["commits"][0]["subject"], "feature: v2")

    async def test_pull_request_diff(self):
        # PR #5 — ветка feature, опубликованная хостингом как refs/pull/5/head.
        git("push", "origin", "feature:refs/pull/5/head", cwd=self.seed)
        info = await self.manager.open("https://github.com/acme/widgets")
        ref = self.manager.resolve(info["repo_id"])
        diff = await self.manager.diff(ref, "pr:5")
        self.assertEqual([f["path"] for f in diff["files"]], ["app.py"])
        self.assertIn("PR #5", diff["title"])
        self.assertIn("schedule v2", diff["patch"])
        text = await self.manager.read_file(ref, "app.py", rev="pr:5")
        self.assertIn("schedule v2", text["content"])
        # PR обновился — повторный diff видит новый коммит.
        git("checkout", "feature", cwd=self.seed)
        (self.seed / "new.py").write_text("x = 1\n", encoding="utf-8")
        git("add", ".", cwd=self.seed)
        git("commit", "-m", "more", cwd=self.seed)
        git("push", "origin", "feature:refs/pull/5/head", cwd=self.seed)
        diff = await self.manager.diff(ref, "pr:5")
        self.assertEqual(sorted(f["path"] for f in diff["files"]), ["app.py", "new.py"])
        with self.assertRaises(RepoError):
            await self.manager.diff(ref, "pr:99")

    async def test_pull_request_diff_in_shallow_clone(self):
        # Длинная история main: точка ответвления PR глубже, чем клон.
        git("checkout", "main", cwd=self.seed)
        git("checkout", "-b", "old-feature", cwd=self.seed)
        (self.seed / "old.py").write_text("old\n", encoding="utf-8")
        git("add", ".", cwd=self.seed)
        git("commit", "-m", "old feature", cwd=self.seed)
        git("push", "origin", "old-feature:refs/pull/7/head", cwd=self.seed)
        git("checkout", "main", cwd=self.seed)
        for i in range(5):
            (self.seed / f"m{i}.txt").write_text(str(i), encoding="utf-8")
            git("add", ".", cwd=self.seed)
            git("commit", "-m", f"main {i}", cwd=self.seed)
        git("push", "origin", "main", cwd=self.seed)
        self.manager.clone_depth = 2
        info = await self.manager.open("https://github.com/acme/widgets")
        ref = self.manager.resolve(info["repo_id"])
        diff = await self.manager.diff(ref, "pr:7")
        self.assertEqual([f["path"] for f in diff["files"]], ["old.py"])
        self.assertNotIn("общий предок не найден", diff["title"])

    async def test_reopen_fetches_new_commits(self):
        info = await self.manager.open("https://github.com/acme/widgets")
        git("checkout", "main", cwd=self.seed)
        (self.seed / "CHANGELOG.md").write_text("1.1\n", encoding="utf-8")
        git("add", ".", cwd=self.seed)
        git("commit", "-m", "changelog", cwd=self.seed)
        git("push", "origin", "main", cwd=self.seed)
        again = await self.manager.open("https://github.com/acme/widgets")
        self.assertEqual(again["repo_id"], info["repo_id"])
        self.assertEqual(again["head"]["subject"], "changelog")

    async def test_host_not_allowed(self):
        with self.assertRaises(RepoError):
            await self.manager.open("https://evil.example.com/acme/widgets")

    async def test_commit_refused_for_remote_clone(self):
        info = await self.manager.open("https://github.com/acme/widgets")
        ref = self.manager.resolve(info["repo_id"])
        with self.assertRaises(RepoError):
            await self.manager.commit(ref, "msg", ["README.md"])

    async def test_unknown_repo_id(self):
        with self.assertRaises(RepoError):
            self.manager.resolve("git:github.com/acme/other#main")
        with self.assertRaises(RepoError):
            self.manager.resolve("/etc")


class LocalRepoTests(_RepoFixture):
    async def test_open_local_and_status(self):
        info = await self.manager.open(str(self.local))
        self.assertEqual(info["kind"], "local")
        self.assertTrue(info["writable"])
        self.assertEqual(info["default_branch"], "main")
        (self.local / "app.py").write_text("changed\n", encoding="utf-8")
        (self.local / "new.txt").write_text("new\n", encoding="utf-8")
        ref = self.manager.resolve(info["repo_id"])
        status = await self.manager.status(ref)
        self.assertEqual(status["changed_files"], 2)
        self.assertEqual(status["untracked"], 1)
        diff = await self.manager.diff(ref)
        self.assertEqual([f["path"] for f in diff["files"]], ["app.py"])
        self.assertEqual(diff["untracked"], ["new.txt"])

    async def test_local_outside_roots_refused(self):
        with self.assertRaises(RepoError):
            await self.manager.open(str(self.seed))

    async def test_commit_and_undo(self):
        info = await self.manager.open(str(self.local))
        ref = self.manager.resolve(info["repo_id"])
        (self.local / "app.py").write_text("changed\n", encoding="utf-8")
        result = await self.manager.commit(ref, "Обновить app.py", ["app.py"])
        self.assertEqual(result["subject"], "Обновить app.py")
        self.assertEqual(result["files"], ["app.py"])
        blame = await self.manager.blame(ref, "app.py")
        self.assertEqual(blame["lines"][0]["summary"], "Обновить app.py")
        undone = await self.manager.undo_commit(ref, result["commit"])
        self.assertEqual(undone["head"]["subject"], "initial")
        status = await self.manager.status(ref)
        self.assertEqual(status["staged"], 1)
        # Повторный откат того же коммита невозможен.
        with self.assertRaises(RepoError):
            await self.manager.undo_commit(ref, result["commit"])

    async def test_undo_only_own_commits(self):
        info = await self.manager.open(str(self.local))
        ref = self.manager.resolve(info["repo_id"])
        head = git("rev-parse", "HEAD", cwd=self.local).strip()
        with self.assertRaises(RepoError):
            await self.manager.undo_commit(ref, head)

    async def test_commit_without_changes(self):
        info = await self.manager.open(str(self.local))
        ref = self.manager.resolve(info["repo_id"])
        with self.assertRaises(RepoError):
            await self.manager.commit(ref, "empty")

    async def test_read_file_path_escape(self):
        info = await self.manager.open(str(self.local))
        ref = self.manager.resolve(info["repo_id"])
        os.symlink("/etc/hostname", self.local / "link")
        with self.assertRaises(RepoError):
            await self.manager.read_file(ref, "link")
        with self.assertRaises(RepoError):
            await self.manager.read_file(ref, "../seed/README.md")


class RepoToolsTests(_RepoFixture):
    async def test_tools_registered_with_groups(self):
        mcp = build_mcp_server(features=Features(repos=True), repo_manager=self.manager)
        tools = {t.name: t for t in await mcp.list_tools()}
        self.assertEqual(tools["repo_status"].meta["agentscore/group"], "Репозиторий")
        self.assertTrue(tools["repo_commit"].meta["agentscore/direct_only"])
        self.assertNotIn("direct_only", str(tools["repo_read_file"].meta))
        names = {t.name for t in await build_mcp_server(features=Features()).list_tools()}
        self.assertFalse(any(n.startswith("repo_") for n in names))

    async def test_open_and_status_via_mcp(self):
        mcp = build_mcp_server(features=Features(repos=True), repo_manager=self.manager)
        opened = _tool(await mcp.call_tool("repo_open", {"link": str(self.local)}))
        self.assertIn("repo_id", opened)
        status = _tool(await mcp.call_tool("repo_status", {"repo_id": opened["repo_id"]}))
        self.assertTrue(status["clean"])
        bad = _tool(await mcp.call_tool("repo_status", {"repo_id": "local:/etc"}))
        self.assertIn("error", bad)


if __name__ == "__main__":
    unittest.main()
