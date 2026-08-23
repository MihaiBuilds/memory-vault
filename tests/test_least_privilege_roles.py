"""
Least-privilege database roles.

Migration 009 defines three group roles. The property that matters is
asymmetric: the application role must be able to do everything the running
application does, and must NOT be able to change the schema. A grant that is
too generous fails silently — nothing breaks, the protection just is not
there — so these tests assert the denial as carefully as the permission.

The roles are NOLOGIN groups. These tests create a temporary login role,
grant it one group, and connect as it, which is exactly how an operator
adopts the split.
"""

from __future__ import annotations

import psycopg
import pytest

from memory_vault.config import settings
from memory_vault.models.db import fetch_all, fetch_one


async def _run_role_ddl(*statements: str) -> None:
    """
    Role DDL cannot be parameterised, and `execute_query` forwards a `params`
    argument that psycopg rejects for these statements, so go through a raw
    connection. Every value interpolated here is a constant defined in this
    file, never test input.
    """
    from memory_vault.models.db import get_pool

    pool = await get_pool()
    async with pool.connection() as conn:
        for stmt in statements:
            await conn.execute(stmt)  # nosec B608
        await conn.commit()


async def _make_login_role(name: str, group: str) -> None:
    await _run_role_ddl(
        f'DROP ROLE IF EXISTS "{name}"',
        f"CREATE ROLE \"{name}\" LOGIN PASSWORD 'probe_pw'",
        f'GRANT "{group}" TO "{name}"',
    )


async def _drop_login_role(name: str) -> None:
    # Privileges granted to the login role must go before the role can be
    # dropped, or Postgres refuses with "role cannot be dropped because some
    # objects depend on it".
    await _run_role_ddl(
        f'REASSIGN OWNED BY "{name}" TO {settings.db_user}',
        f'DROP OWNED BY "{name}"',
        f'DROP ROLE IF EXISTS "{name}"',
    )


def _conninfo_as(user: str) -> str:
    return psycopg.conninfo.make_conninfo(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=user,
        password="probe_pw",
    )


class TestRolesExist:
    async def test_all_three_roles_created(self):
        rows = await fetch_all(
            """SELECT rolname, rolcanlogin FROM pg_roles
               WHERE rolname IN ('memory_vault_app',
                                 'memory_vault_readonly',
                                 'memory_vault_migrator')
               ORDER BY rolname"""
        )
        assert [r["rolname"] for r in rows] == [
            "memory_vault_app",
            "memory_vault_migrator",
            "memory_vault_readonly",
        ]

    async def test_roles_cannot_log_in_directly(self):
        """
        They are group roles. A role that could log in would be a new
        credential this migration created without anyone asking for it.
        """
        rows = await fetch_all(
            """SELECT rolname FROM pg_roles
               WHERE rolname LIKE 'memory_vault_%' AND rolcanlogin"""
        )
        assert rows == [], "migration 009 must not create anything that can log in"

    async def test_migration_is_idempotent(self):
        """Re-running must not fail on roles that already exist."""
        from pathlib import Path

        from memory_vault.models.db import get_pool

        sql = (
            Path(__file__).resolve().parent.parent
            / "src/memory_vault/migrations/009_least_privilege_roles.sql"
        ).read_text()

        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute(sql)
            await conn.commit()


class TestAppRolePrivileges:
    ROLE = "mv_probe_app"

    @pytest.fixture(autouse=True)
    async def _role(self):
        await _make_login_role(self.ROLE, "memory_vault_app")
        yield
        await _drop_login_role(self.ROLE)

    async def test_app_role_can_read_and_write_rows(self):
        """Whatever else is true, the application must still work."""
        async with await psycopg.AsyncConnection.connect(_conninfo_as(self.ROLE)) as conn:
            await conn.execute(
                "INSERT INTO memory_spaces (name, description) VALUES (%s, %s)",
                ("role-probe", "written by the app role"),
            )
            cur = await conn.execute(
                "SELECT name FROM memory_spaces WHERE name = %s", ("role-probe",)
            )
            row = await cur.fetchone()
            assert row is not None, "app role must be able to INSERT then SELECT"

            await conn.execute(
                "UPDATE memory_spaces SET description = %s WHERE name = %s",
                ("updated", "role-probe"),
            )
            await conn.execute("DELETE FROM memory_spaces WHERE name = %s", ("role-probe",))
            await conn.commit()

    async def test_app_role_cannot_create_tables(self):
        async with await psycopg.AsyncConnection.connect(_conninfo_as(self.ROLE)) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await conn.execute("CREATE TABLE mv_probe_ddl (id int)")

    async def test_app_role_cannot_drop_tables(self):
        """The failure this whole migration exists to prevent."""
        async with await psycopg.AsyncConnection.connect(_conninfo_as(self.ROLE)) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await conn.execute("DROP TABLE chunks")

        # And the table is still there.
        row = await fetch_one("SELECT to_regclass('public.chunks') AS t")
        assert row["t"] is not None, "chunks must survive a denied DROP"

    async def test_app_role_cannot_alter_tables(self):
        async with await psycopg.AsyncConnection.connect(_conninfo_as(self.ROLE)) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await conn.execute("ALTER TABLE chunks ADD COLUMN mv_probe_col int")


class TestReadonlyRolePrivileges:
    ROLE = "mv_probe_ro"

    @pytest.fixture(autouse=True)
    async def _role(self):
        await _make_login_role(self.ROLE, "memory_vault_readonly")
        yield
        await _drop_login_role(self.ROLE)

    async def test_readonly_role_can_select(self):
        async with await psycopg.AsyncConnection.connect(_conninfo_as(self.ROLE)) as conn:
            cur = await conn.execute("SELECT count(*) AS n FROM memory_spaces")
            assert (await cur.fetchone()) is not None

    async def test_readonly_role_cannot_insert(self):
        async with await psycopg.AsyncConnection.connect(_conninfo_as(self.ROLE)) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await conn.execute("INSERT INTO memory_spaces (name) VALUES (%s)", ("ro-probe",))

    async def test_readonly_role_cannot_delete(self):
        async with await psycopg.AsyncConnection.connect(_conninfo_as(self.ROLE)) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await conn.execute("DELETE FROM memory_spaces")


class TestMigrationCredentialFallback:
    """
    The fallback is what keeps this from being a breaking change: a deployment
    that never sets DB_MIGRATION_USER must connect exactly as it did before.
    """

    def test_unset_migration_user_falls_back_to_runtime_credentials(self):
        from memory_vault.config import Settings

        s = Settings(db_migration_user=None, db_migration_password=None)
        assert s.migration_database_url == s.database_url

    def test_set_migration_user_changes_the_connection(self):
        from memory_vault.config import Settings

        s = Settings(db_migration_user="migrator_login", db_migration_password="pw")
        assert "migrator_login" in s.migration_database_url
        assert s.migration_database_url != s.database_url

    def test_migration_user_without_password_reuses_the_runtime_password(self):
        """
        Half-configured is the likely operator mistake. Reusing DB_PASSWORD is
        wrong less often than sending an empty one.
        """
        from memory_vault.config import Settings

        s = Settings(
            db_user="app_login",
            db_password="shared_pw",
            db_migration_user="migrator_login",
            db_migration_password=None,
        )
        assert "shared_pw" in s.migration_database_url
        assert "migrator_login" in s.migration_database_url
