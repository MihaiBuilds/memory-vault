"""Uvicorn entrypoint for the Memory Vault REST API."""

from __future__ import annotations

import uvicorn

from src.api.app import create_app
from src.config import settings

app = create_app()


def main() -> None:
    uvicorn.run(
        "src.api.server:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
