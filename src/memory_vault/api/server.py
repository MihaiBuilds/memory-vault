"""Uvicorn entrypoint for the Memory Vault REST API."""

from __future__ import annotations

import uvicorn

from memory_vault.api.app import create_app
from memory_vault.config import settings

app = create_app()


def main() -> None:
    uvicorn.run(
        "memory_vault.api.server:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
