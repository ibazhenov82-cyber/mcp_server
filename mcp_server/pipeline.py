"""
mcp_server.pipeline
=====================

`run_pipeline` — выполнение цепочки инструментов этого сервера за один
вызов, без участия модели в передаче данных между шагами.

Шаг: `{"tool": "<имя>", "args": {...}}`. В аргументах можно сослаться на
результаты предыдущих шагов (значением целиком, строкой):

- `"$prev"` — `result_id` предыдущего шага;
- `"$prev.<поле>"` — поле ответа предыдущего шага (`"$prev.path"`);
- `"$2"`, `"$2.<поле>"` — то же для шага с номером 2 (нумерация с 1).

Цепочка останавливается на первой ошибке. В ответе — трасса шагов и
проверки передачи данных: что каждый шаг получил результат предыдущего
(`source_result_id`) и что содержимое файла совпадает с сохранённым
результатом (`sha256`).
"""

from __future__ import annotations

import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from .results import KIND_TOOL_OUTPUT, ResultStore

MAX_STEPS = 10
_REF_RE = re.compile(r"^\$(prev|\d+)(?:\.([A-Za-z_][\w]*))?$")

ToolFn = Callable[..., Awaitable[Any]]


class PipelineError(Exception):
    pass


def _resolve(value: Any, outputs: List[Dict[str, Any]]) -> Any:
    if isinstance(value, str):
        match = _REF_RE.match(value.strip())
        if not match:
            return value
        ref, field = match.groups()
        if ref == "prev":
            if not outputs:
                raise PipelineError(f"{value}: у первого шага нет предыдущего")
            source = outputs[-1]
        else:
            index = int(ref)
            if not 1 <= index <= len(outputs):
                raise PipelineError(f"{value}: шаг {index} ещё не выполнен")
            source = outputs[index - 1]
        key = field or "result_id"
        if key not in source:
            raise PipelineError(f"{value}: в ответе шага нет поля {key!r}")
        return source[key]
    if isinstance(value, dict):
        return {k: _resolve(v, outputs) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, outputs) for v in value]
    return value


def _brief(value: Any, limit: int = 1500) -> Any:
    """Сокращённая копия ответа шага для трассы."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"… (всего {len(value)} симв.)"
    if isinstance(value, list):
        return [_brief(v, limit) for v in value[:10]] + ([f"… ещё {len(value) - 10}"] if len(value) > 10 else [])
    if isinstance(value, dict):
        return {k: _brief(v, limit) for k, v in value.items()}
    return value


async def run_pipeline(
    steps: List[Dict[str, Any]], registry: Dict[str, ToolFn], allowed: Optional[Set[str]] = None,
    store: Optional[ResultStore] = None,
) -> Dict[str, Any]:
    """`allowed` — инструменты, выбранные в чате (None — без ограничений);
    `store` — куда сохранять ответы шагов без `result_id`."""
    if not isinstance(steps, list) or not steps:
        raise PipelineError("steps — непустой список шагов {\"tool\": ..., \"args\": {...}}")
    if len(steps) > MAX_STEPS:
        raise PipelineError(f"не больше {MAX_STEPS} шагов")
    for i, step in enumerate(steps, start=1):
        if not isinstance(step, dict) or not isinstance(step.get("tool"), str):
            raise PipelineError(f"шаг {i}: нужен объект с полем tool")
        if step["tool"] not in registry:
            raise PipelineError(f"шаг {i}: инструмент {step['tool']!r} недоступен в цепочке; есть {sorted(registry)}")
        if allowed is not None and step["tool"] not in allowed:
            raise PipelineError(f"шаг {i}: инструмент {step['tool']!r} не выбран в настройках этого чата")
        if not isinstance(step.get("args", {}), dict):
            raise PipelineError(f"шаг {i}: args должен быть объектом")

    outputs: List[Dict[str, Any]] = []
    trace: List[Dict[str, Any]] = []
    ok = True
    for i, step in enumerate(steps, start=1):
        started = time.monotonic()
        entry: Dict[str, Any] = {"n": i, "tool": step["tool"]}
        try:
            args = _resolve(step.get("args", {}), outputs)
            entry["args"] = _brief(args, 300)
            result = await registry[step["tool"]](**args)
        except PipelineError as exc:
            result = {"error": str(exc)}
        except TypeError as exc:
            result = {"error": f"неверные аргументы: {exc}"}
        output = result if isinstance(result, dict) else {"result": result}
        entry["duration_ms"] = int((time.monotonic() - started) * 1000)
        if "error" in output:
            entry.update(ok=False, error=str(output["error"]))
            trace.append(entry)
            ok = False
            break
        entry["ok"] = True
        if "result_id" not in output and store is not None:
            # Ответ без result_id (например, список коммитов) сохраняется,
            # чтобы следующий шаг мог сослаться на него через "$prev".
            items = output.get("result") if isinstance(output.get("result"), list) else None
            data = {"tool": step["tool"], "items": items} if items is not None else {"tool": step["tool"], **output}
            stored = store.put(KIND_TOOL_OUTPUT, data)
            output = {"sha256": stored.sha256, **output, "result_id": stored.id}
            entry["auto_stored"] = True
        for key in (
            "result_id", "source_result_id", "sha256", "count", "chars", "path", "bytes", "content_matches_source",
        ):
            if key in output:
                entry[key] = output[key]
        entry["output"] = _brief(output)
        trace.append(entry)
        outputs.append(output)

    return {
        "ok": ok,
        "completed_steps": len(outputs),
        "total_steps": len(steps),
        "steps": trace,
        "checks": _checks(trace),
    }


def _checks(trace: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Проверки передачи данных между успешными шагами."""
    checks = []
    for prev, cur in zip(trace, trace[1:]):
        if not cur.get("ok") or "source_result_id" not in cur or not prev.get("result_id"):
            continue
        linked = cur["source_result_id"] == prev["result_id"]
        checks.append({
            "check": f"шаг {cur['n']} ({cur['tool']}) получил результат шага {prev['n']} ({prev['tool']})",
            "ok": linked,
        })
        if linked and "path" in cur and cur.get("content_matches_source") is not None:
            checks.append({
                "check": f"содержимое файла {cur['path']} совпадает с результатом шага {prev['n']} (sha256)",
                "ok": cur["sha256"] == prev["sha256"],
            })
    return checks
