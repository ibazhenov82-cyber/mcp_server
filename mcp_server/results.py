"""
mcp_server.results
=====================

Хранилище промежуточных результатов инструментов-звеньев пайплайна.

Инструменты передают друг другу не сами данные, а `result_id`: поиск
сохраняет найденное и возвращает короткую справку, суммаризация читает
результат поиска по id и сохраняет свой, сохранение в файл читает результат
суммаризации. Так большие данные не гоняются через контекст модели, а
цепочку можно проверить: у каждого результата есть `source_result_id`
(из чего он сделан) и `sha256` содержимого.

Результат — JSON-файл `<DATA_DIR>/results/<id>.json`; старше
`RESULT_TTL_HOURS` удаляются при записи новых.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

#: Виды результатов.
KIND_SEARCH_RESULTS = "search_results"
KIND_MARKDOWN = "markdown"
KIND_TEXT = "text"
KIND_WEB_PAGE = "web_page"
KIND_TOOL_OUTPUT = "tool_output"

_ID_RE = re.compile(r"^r_[0-9a-f]{12}$")


class ResultNotFoundError(Exception):
    """Нет результата с таким id (не было или истёк срок хранения)."""


@dataclass
class StoredResult:
    id: str
    kind: str
    data: Dict[str, Any]
    sha256: str
    created_at: float
    source_result_id: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def text(self) -> str:
        """Текстовое содержимое (для markdown/text); для остальных видов —
        канонический JSON данных."""
        if "text" in self.data and isinstance(self.data["text"], str):
            return self.data["text"]
        return canonical_json(self.data)


def canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_of(data: Dict[str, Any]) -> str:
    """Контрольная сумма содержимого: у текстовых результатов — самого
    текста (совпадает с `sha256` файла, в который он сохранён), у остальных —
    канонического JSON."""
    payload = data["text"] if isinstance(data.get("text"), str) else canonical_json(data)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ResultStore:
    def __init__(self, data_dir: str, ttl_hours: float = 24.0):
        self.dir = Path(data_dir) / "results"
        self.ttl_seconds = max(ttl_hours, 0.0) * 3600

    def put(
        self, kind: str, data: Dict[str, Any], source_result_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> StoredResult:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._cleanup()
        result = StoredResult(
            id=f"r_{secrets.token_hex(6)}", kind=kind, data=data, sha256=sha256_of(data),
            created_at=time.time(), source_result_id=source_result_id, meta=dict(meta or {}),
        )
        tmp = self.dir / f"{result.id}.json.tmp"
        tmp.write_text(json.dumps(result.__dict__, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.dir / f"{result.id}.json")
        return result

    def get(self, result_id: str) -> StoredResult:
        if not isinstance(result_id, str) or not _ID_RE.match(result_id.strip()):
            raise ResultNotFoundError(f"некорректный result_id {result_id!r}: ожидается вида r_0123456789ab")
        path = self.dir / f"{result_id.strip()}.json"
        if not path.exists():
            raise ResultNotFoundError(
                f"результат {result_id} не найден (не существовал или истёк срок хранения)"
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
        return StoredResult(**raw)

    def _cleanup(self) -> None:
        if not self.ttl_seconds:
            return
        border = time.time() - self.ttl_seconds
        for path in self.dir.glob("r_*.json"):
            try:
                if path.stat().st_mtime < border:
                    path.unlink()
            except OSError:
                pass
