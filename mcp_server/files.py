"""
mcp_server.files
==================

«Работа с файлами»: сохранение текста в файл внутри каталога
`MCP_FILES_DIR` и чтение списка/содержимого для приложения.

Запись возможна только внутрь этого каталога: абсолютные пути, `..`,
обратные слэши и скрытые файлы отклоняются; расширения — только текстовые.
Существующий файл без `overwrite=true` не перезаписывается.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

ALLOWED_EXTENSIONS = {".md", ".txt", ".csv", ".json", ".log"}
DEFAULT_EXTENSION = ".md"
MAX_FILE_BYTES = 2_000_000
_SEGMENT_RE = re.compile(r"^[\w\-. ()]+$", re.UNICODE)


class FileToolError(Exception):
    pass


@dataclass
class SavedFile:
    path: str
    bytes: int
    sha256: str
    overwritten: bool


def render_filename(template: str, now: Optional[datetime] = None) -> str:
    """Подстановки `{date}` (2026-09-28), `{time}` (09-00), `{datetime}`."""
    now = now or datetime.now()
    return (
        template.replace("{datetime}", now.strftime("%Y-%m-%d_%H-%M"))
        .replace("{date}", now.strftime("%Y-%m-%d"))
        .replace("{time}", now.strftime("%H-%M"))
    )


def safe_relative_path(filename: str) -> Path:
    name = (filename or "").strip()
    if not name:
        raise FileToolError("не указано имя файла")
    if "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise FileToolError("имя файла должно быть относительным путём внутри каталога файлов (через /)")
    parts = [p for p in name.split("/") if p]
    for part in parts:
        if part in (".", "..") or part.startswith(".") or not _SEGMENT_RE.match(part):
            raise FileToolError(f"недопустимая часть пути {part!r}")
    rel = Path(*parts)
    if not rel.suffix:
        rel = rel.with_suffix(DEFAULT_EXTENSION)
    if rel.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise FileToolError(f"расширение {rel.suffix!r} не поддерживается: допустимо {sorted(ALLOWED_EXTENSIONS)}")
    return rel


class FileStore:
    def __init__(self, files_dir: str):
        self.root = Path(files_dir)

    def _resolve(self, rel: Path) -> Path:
        root = self.root.resolve()
        target = (root / rel).resolve()
        if root not in target.parents:
            raise FileToolError("путь выходит за пределы каталога файлов")
        return target

    def save(self, filename: str, content: str, overwrite: bool = False) -> SavedFile:
        rel = safe_relative_path(render_filename(filename))
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise FileToolError(f"слишком большой текст: {len(data)} байт (максимум {MAX_FILE_BYTES})")
        target = self._resolve(rel)
        existed = target.exists()
        if existed and not overwrite:
            raise FileToolError(f"файл {rel.as_posix()} уже существует — укажите overwrite=true или другое имя")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        return SavedFile(
            path=rel.as_posix(), bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), overwritten=existed,
        )

    def list(self) -> List[Dict[str, object]]:
        if not self.root.exists():
            return []
        items = []
        for path in self.root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in ALLOWED_EXTENSIONS:
                continue
            rel = path.relative_to(self.root)
            if any(part.startswith(".") for part in rel.parts):
                continue
            stat = path.stat()
            items.append({"path": rel.as_posix(), "bytes": stat.st_size, "modified_at": int(stat.st_mtime)})
        items.sort(key=lambda i: i["modified_at"], reverse=True)
        return items

    def read(self, filename: str) -> Dict[str, object]:
        rel = safe_relative_path(filename)
        target = self._resolve(rel)
        if not target.is_file():
            raise FileNotFoundError(rel.as_posix())
        data = target.read_bytes()
        return {
            "path": rel.as_posix(), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "modified_at": int(target.stat().st_mtime), "content": data.decode("utf-8", errors="replace"),
        }
