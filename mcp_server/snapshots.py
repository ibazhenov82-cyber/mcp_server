"""
mcp_server.snapshots
======================

«Сохранять ответ инструмента» (`save_tool_result`): сохранённый ответ
инструмента-источника (коммиты, PR, issues, релизы, выдача поиска) в JSON и
сравнение с ним нового ответа.

Первый вызов сохраняет весь ответ и возвращает его целиком (`first_run`).
Следующие вызовы с теми же параметрами возвращают только **дельту**: новые
элементы (которых не было ни в одном прошлом ответе) и изменённые (тот же
элемент, другое содержимое — например, PR закрыт). Снимок хранит все
когда-либо увиденные элементы, а не только последний ответ: элементы,
выпавшие из окна `limit`, не считаются удалёнными.

Снимок — JSON-файл в каталоге файлов сервера:
`snapshots/<chat_id | shared>/<имя>.json` — его видно в приложении в
разделе «Файлы». Каждый чат ведёт свои снимки: `chat_id` приходит в `_meta`
вызова от AgentsCore; вызов без чата (планировщик, run_pipeline) пишет в
`shared`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: Поля, по которым элемент узнаётся между вызовами — первое найденное.
IDENTITY_FIELDS = ("sha", "number", "tag", "url", "id")
#: Поля, не участвующие в сравнении содержимого (позиция в выдаче и т.п.).
VOLATILE_FIELDS = {"n", "rate_limit_remaining"}
#: Аргументы, не влияющие на «что отслеживаем» (окно и объём ответа).
WINDOW_ARGS = {"limit", "max_results", "since", "until", "timelimit"}
#: Сколько элементов хранить в снимке (самые новые).
MAX_STORED_ITEMS = 2000
_SAFE_RE = re.compile(r"[^\w.-]+", re.UNICODE)


class SnapshotError(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def extract_items(output: Any) -> List[Dict[str, Any]]:
    """Список элементов из ответа инструмента: сам список или поле
    `results`/`items`/`result` словаря."""
    if isinstance(output, dict):
        if "error" in output:
            raise SnapshotError(str(output["error"]))
        for field in ("results", "items", "result"):
            if isinstance(output.get(field), list):
                output = output[field]
                break
    if not isinstance(output, list):
        raise SnapshotError("инструмент вернул не список элементов — сравнивать нечего")
    return [item for item in output if isinstance(item, dict)]


def item_key(item: Dict[str, Any]) -> str:
    for field in IDENTITY_FIELDS:
        value = item.get(field)
        if value not in (None, ""):
            return f"{field}:{value}"
    return "hash:" + item_hash(item)[:16]


def item_hash(item: Dict[str, Any]) -> str:
    stable = {k: v for k, v in item.items() if k not in VOLATILE_FIELDS}
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def tracking_key(tool: str, args: Dict[str, Any]) -> str:
    """Что отслеживаем: инструмент + аргументы без окна (limit, since, …)."""
    subject = {k: v for k, v in sorted(args.items()) if k not in WINDOW_ARGS and v not in (None, "")}
    return tool + ":" + json.dumps(subject, ensure_ascii=False, sort_keys=True)


def snapshot_filename(tool: str, args: Dict[str, Any], key: str) -> str:
    """Понятное имя файла: `git_host_list_commits_github_owner_repo_1a2b3c.json`."""
    parts = [tool] + [str(v) for k, v in sorted(args.items()) if k not in WINDOW_ARGS and v not in (None, "")]
    readable = _SAFE_RE.sub("_", "_".join(parts)).strip("_")[:80]
    return f"{readable}_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:6]}.json"


def _changed_fields(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    keys = (set(before) | set(after)) - VOLATILE_FIELDS
    return sorted(k for k in keys if before.get(k) != after.get(k))


class SnapshotStore:
    def __init__(self, files_dir: str):
        self.root = Path(files_dir)

    def path_for(self, namespace: str, filename: str) -> Tuple[Path, str]:
        ns = _SAFE_RE.sub("_", namespace or "shared").strip("_") or "shared"
        rel = f"snapshots/{ns}/{filename}"
        return self.root / rel, rel

    def compare_and_save(
        self, namespace: str, tool: str, args: Dict[str, Any], items: List[Dict[str, Any]],
        reset: bool = False, requested_limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        key = tracking_key(tool, args)
        path, rel = self.path_for(namespace, snapshot_filename(tool, args, key))
        snapshot: Optional[Dict[str, Any]] = None
        if path.exists() and not reset:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        first_run = snapshot is None
        now = now_iso()
        stored: List[Dict[str, Any]] = [] if first_run else list(snapshot.get("items", []))
        by_key = {entry["key"]: entry for entry in stored}

        new_entries, changed = [], []
        for item in items:
            k, h = item_key(item), item_hash(item)
            known = by_key.get(k)
            if known is None:
                entry = {"key": k, "hash": h, "first_seen_at": now, "item": item}
                by_key[k] = entry
                new_entries.append(entry)
            elif known["hash"] != h:
                changed.append({"item": item, "changed_fields": _changed_fields(known["item"], item)})
                known.update(hash=h, item=item, updated_at=now)

        # Новые — в начало (в порядке ответа: обычно сначала самые свежие).
        merged = new_entries + [e for e in stored if e["key"] not in {n["key"] for n in new_entries}]
        merged = merged[:MAX_STORED_ITEMS]
        data = {
            "tool": tool,
            "args": {k: v for k, v in args.items() if k not in WINDOW_ARGS},
            "namespace": namespace,
            "created_at": now if first_run else snapshot.get("created_at", now),
            "updated_at": now,
            "previous_run_at": None if first_run else snapshot.get("updated_at"),
            "runs": 1 if first_run else int(snapshot.get("runs", 0)) + 1,
            "items_count": len(merged),
            "items": merged,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

        new_items = [e["item"] for e in new_entries]
        # Все элементы ответа новые и их ровно столько, сколько просили, —
        # между вызовами могло появиться больше, чем помещается в ответ.
        possibly_incomplete = (
            not first_run and bool(items) and len(new_items) == len(items)
            and requested_limit is not None and len(items) >= requested_limit
        )
        return {
            "snapshot": rel,
            "first_run": first_run,
            "runs": data["runs"],
            "previous_run_at": data["previous_run_at"],
            "items_in_response": len(items),
            "known_items": len(merged),
            "new_count": len(new_items),
            "changed_count": len(changed),
            "has_changes": bool(new_items or changed),
            "possibly_incomplete": possibly_incomplete,
            "new_items": new_items,
            "changed_items": changed,
        }
