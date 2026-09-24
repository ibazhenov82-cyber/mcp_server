"""
Точка входа: `python -m mcp_server`.

Запускает единственный процесс, обслуживающий и MCP-протокол (Streamable
HTTP, под `/mcp` — для AgentsCore как MCP-клиента) и REST API (под `/api`
— для AgentsApp), плюс единственный экземпляр APScheduler внутри этого же
процесса (см. README, "Однократный экземпляр — обязательно").

Требует `fastapi`+`uvicorn` (см. requirements.txt) — недоступны в
песочнице разработки, поэтому этот модуль не покрыт тестами напрямую;
вся логика, которую он запускает, протестирована в `tests/`.
"""

from __future__ import annotations

import logging

from .app import create_app
from .config import MCPConfig


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, MCPConfig.LOG_LEVEL.upper(), logging.INFO),
        filename=MCPConfig.LOG_FILE or None,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    import uvicorn

    app = create_app()

    def _onoff(flag: bool) -> str:
        return "включено" if flag else "выключено"

    print(f"[mcp_server] Локальный Git (MCP_LOCAL_GIT_ENABLED): {_onoff(MCPConfig.LOCAL_GIT_ENABLED)}")
    print(f"[mcp_server] Периодические задачи (MCP_SCHEDULER_ENABLED): {_onoff(MCPConfig.SCHEDULER_ENABLED)}")
    uvicorn.run(app, host=MCPConfig.HOST, port=MCPConfig.PORT)


if __name__ == "__main__":
    main()
