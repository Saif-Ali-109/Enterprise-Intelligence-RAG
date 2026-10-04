"""Alembic environment.

Two things this file does that the default template does not.

**The URL is taken from the environment, always.** `alembic.ini` carries a
placeholder that is overwritten here from `DATABASE_URL`. The consequence is
that no database credential is in version control, and there is no second place
to update when it changes (FR-042).

**The same `Base.metadata` the application uses is the autogenerate target.**
Pointing autogenerate at a hand-maintained copy of the schema is how a migration
silently fails to include a table: the copy drifts, autogenerate emits a
no-op, and the difference only appears when a query fails at run time. There is
one metadata object, imported from the models.

An async driver is configured, so migrations run through the same
`create_async_engine` path as the application. A synchronous migration against
an async application means the two disagree about transactions.
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

# The repository root is on the path before the models are imported: alembic
# runs with `backend/` as its working directory, and `app` lives there. Making
# the import unconditional would work locally and fail in a container where the
# working directory is `/app` and the package is at `/app/app` — so this is
# explicit rather than relying on the incidental path.
BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from alembic import context  # noqa: E402
from sqlalchemy import pool  # noqa: E402
from sqlalchemy.engine import Connection  # noqa: E402
from sqlalchemy.ext.asyncio import async_engine_from_config  # noqa: E402

from app.db.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

#: Autogenerate compares against this. One metadata object, not a copy.
target_metadata = Base.metadata


def _database_url() -> str:
    """The URL to migrate, from the environment.

    Read directly from `os.environ` rather than through `Settings`, so a
    migration can run against a database that is not the application's — a
    disposable schema in a test, or a staging database — without also having to
    satisfy the two required vendor keys. `Settings` would refuse to construct.
    """
    import os

    url = os.environ.get("DATABASE_URL")
    if url:
        return url

    from app.core.config import get_settings

    return get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting.

    Used to review a migration before applying it. `--sql` in a deployment
    pipeline should be the only way a production schema changes: the SQL that
    will run is the SQL a human read.
    """
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Configure the migration context against a live connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Type and server-default comparison are both ON. Autogenerate that
        # compares only names produces a migration that changes nothing visible
        # and then a schema that does not match the models.
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations over an async engine.

    Built from the configuration with the URL replaced, so a pool is created and
    disposed the way the application creates and disposes one.
    """
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for an online (connected) migration run."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
