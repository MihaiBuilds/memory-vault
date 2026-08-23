"""
Configuration — loads from environment variables with sensible defaults.

All settings in one place. No hardcoded paths. Docker and local both work.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv
from psycopg.conninfo import make_conninfo

load_dotenv()


@dataclass(frozen=True)
class Settings:
    # Database
    db_host: str = os.getenv("DB_HOST", "localhost")
    db_port: int = int(os.getenv("DB_PORT", "5432"))
    db_name: str = os.getenv("DB_NAME", "memory_vault")
    db_user: str = os.getenv("DB_USER", "memory_vault")
    db_password: str = os.getenv("DB_PASSWORD", "memory_vault")
    # Credentials used only while applying migrations. Unset means "use
    # DB_USER" — the single-credential setup every existing deployment has.
    # Setting them lets the runtime role drop DDL rights without stopping
    # migrations from running at start-up.
    db_migration_user: str | None = os.getenv("DB_MIGRATION_USER") or None
    db_migration_password: str | None = os.getenv("DB_MIGRATION_PASSWORD") or None

    # API
    api_host: str = os.getenv("API_HOST", "0.0.0.0")  # nosec B104 — Memory Vault is designed to run inside a Docker container; binding 0.0.0.0 is required to be reachable from the host. Operators expose only :8000 from compose.
    api_port: int = int(os.getenv("API_PORT", "8000"))

    # Embedding
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    embedding_dimensions: int = int(os.getenv("EMBEDDING_DIMENSIONS", "384"))
    embedding_batch_size: int = int(os.getenv("EMBEDDING_BATCH_SIZE", "32"))

    # Search
    rrf_k: int = int(os.getenv("RRF_K", "60"))
    search_default_limit: int = int(os.getenv("SEARCH_DEFAULT_LIMIT", "10"))

    @property
    def database_url(self) -> str:
        """psycopg conninfo string for the configured database.

        Built with ``psycopg.conninfo.make_conninfo`` so URI-reserved characters
        in DB_USER or DB_PASSWORD (``/``, ``@``, ``:``, ``#``, etc.) are quoted
        correctly. The historical name is kept for API compatibility even
        though the result is a keyword=value conninfo string, not a URI.
        """
        return make_conninfo(
            host=self.db_host,
            port=self.db_port,
            dbname=self.db_name,
            user=self.db_user,
            password=self.db_password,
        )

    @property
    def migration_database_url(self) -> str:
        """Conninfo for applying migrations.

        Falls back to the runtime credentials when DB_MIGRATION_USER is unset,
        so a deployment that never heard of role separation keeps working
        exactly as before. When it is set, only this connection carries DDL
        rights and the pool that serves requests does not.
        """
        if not self.db_migration_user:
            return self.database_url
        return make_conninfo(
            host=self.db_host,
            port=self.db_port,
            dbname=self.db_name,
            user=self.db_migration_user,
            # An empty migration password is legitimate (peer/trust auth, or a
            # .pgpass file), so fall back only when the key is absent entirely.
            password=(
                self.db_migration_password
                if self.db_migration_password is not None
                else self.db_password
            ),
        )


settings = Settings()
