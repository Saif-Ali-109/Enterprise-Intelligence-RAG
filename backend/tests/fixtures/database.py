"""The test database's schema comes from the migrations, not from the models.

A previous version of this fixture called `Base.metadata.create_all` before each
test. That works right up to the moment a model gains a *column*: `create_all`
creates missing tables and is silent about everything else, so the new column
never appears, and the failure surfaces as an `UndefinedColumnError` in a test
that has nothing to do with schema changes — twenty-four errors from one missing
column, all reported as fixture errors.

Applying the migrations instead removes the whole category. The models cannot
outrun the migrations, because a suite that needs a column the migration does
not add fails at the *migration*, where the error names the revision. And a
migration that does not work is a defect the suite now catches, rather than one
that surfaces in a deployment.

Two mechanics worth knowing:

**It runs synchronously, once per session.** `app/db/migrations/env.py` calls
`asyncio.run` at import time, so it cannot be invoked from inside a running loop
— and a pytest-asyncio fixture is one. Hence a sync fixture.

**`config_file_name` is cleared.** `env.py` hands the Alembic config to
`logging.config.fileConfig`, which by default disables loggers it did not
configure. Under pytest that would silence the application logger for the rest of
the session, and any test asserting on log records would fail for a reason that
has nothing to do with logs.
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config

#: `backend/tests/fixtures/database.py` -> `backend/`.
BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _database_url() -> str:
    """Resolve the URL the same way `env.py` does, for the error message only.

    The migration environment resolves it itself. This exists so that a failure
    to connect can name the database it failed to reach without the name, the
    host, or the password.
    """
    url = os.environ.get("DATABASE_URL")
    if url:
        return _redact(url)
    from app.core.config import get_settings

    return _redact(get_settings().database_url)


def _redact(url: str) -> str:
    """Strip the credential from a URL, keeping the part that identifies it."""
    if "@" not in url or "//" not in url:
        return "<database>"
    scheme, _, rest = url.partition("://")
    _, _, host = rest.rpartition("@")
    return f"{scheme}://<credentials>@{host}"


def migration_config() -> Config:
    """An Alembic configuration pointed at this repository's migrations."""
    config = Config()
    config.set_main_option("script_location", str(BACKEND_ROOT / "app" / "db" / "migrations"))
    # See the module docstring: `env.py` would otherwise reconfigure logging.
    config.config_file_name = None
    return config


def migrate_to_head() -> None:
    """Apply every migration.

    Raises `OperationalError` when the database cannot be reached, so the
    calling fixture can turn an absent database into a skip instead of an error
    (Principle XIII: a missing dependency is a skip, a broken guarantee is a
    failure).
    """
    command.upgrade(migration_config(), "head")


async def truncate_all() -> None:
    """Empty every table, leaving the schema in place.

    `sources` is the root of every foreign key in the registry, so truncating it
    cascades. Truncating rather than dropping is deliberate: another suite may
    run after this one and expects the tables to exist — `routes_config` counts
    documents and units to report the corpus size, and a suite that leaves the
    schema behind fails that test for a reason that has nothing to do with
    either of them.
    """
    from app.db.session import get_engine
    from sqlalchemy import text

    async with get_engine().begin() as connection:
        # `query_logs` is here because it has no foreign key to `sources`, so
        # truncating `sources` never touched it: audit rows accumulated across
        # suites, and a test that counts them was counting its own history.
        # `citations` follows `query_logs` by cascade.
        await connection.execute(
            text("TRUNCATE sources, query_logs, evaluation_questions, evaluation_runs CASCADE")
        )


def describe_connection_failure() -> str:
    """A skip message that names the database without naming its credential."""
    return f"PostgreSQL is not reachable at {_database_url()}"
