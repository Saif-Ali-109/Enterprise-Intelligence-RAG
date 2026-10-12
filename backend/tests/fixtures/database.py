"""The suite runs against its own database, and refuses to truncate anything else.

**Why the suite has a separate database.** Every module that touches the
registry calls `truncate_all`, which empties nine tables. Pointed at the
development database — which it was until 2026-10-09 — a `make test-all` erased
a corpus that a crawl had spent minutes building, and cost one measured
evaluation run whose numbers had to be taken again from scratch. A test suite
that destroys the thing it is supposed to protect is not a fast suite, it is an
expensive one.

**How the redirect happens.** `tests/conftest.py` inspects the `-m` marker
expression at `pytest_configure` time. A selection that includes `integration`
or `e2e` — or no selection at all — repoints `DATABASE_URL` at a database whose
name ends in `_test`, before anything imports the settings singleton. A
`-m "unit or contract"` run is left alone: those suites do not truncate, they
read the corpus size through `/config`, and they must keep working with no
PostgreSQL at all.

**Why there is also a guard.** The redirect is a convenience; the guard is the
guarantee. `truncate_all()` refuses to run against any database that is not a
test database, so a suite invoked in a way that skipped the redirect — directly,
from another runner, with `TEST_DATABASE_URL` unset — fails loudly instead of
emptying a registry.

The original rationale for this module, which still holds:

The test database's schema comes from the migrations, not from the models.

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

import asyncio
import os
import re
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError

#: `backend/tests/fixtures/database.py` -> `backend/`.
BACKEND_ROOT = Path(__file__).resolve().parents[2]

#: Suffix that marks a database as disposable. `truncate_all` trusts this.
TEST_DATABASE_SUFFIX = "_test"

#: PostgreSQL identifiers we are willing to interpolate into DDL. A bound
#: parameter is not available for a database name, and `CREATE DATABASE` cannot
#: run inside a transaction, so the name is validated instead of escaped.
_SAFE_DATABASE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _source_url() -> URL:
    """The configured development URL, from the environment or the settings.

    Read the environment first for the same reason `app/db/migrations/env.py`
    does: it runs before the settings singleton is built, and a test that has
    already repointed the environment must not be handed the old DSN.
    """
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        from app.core.config import get_settings

        raw = get_settings().database_url
    return make_url(raw)


def test_database_url() -> str:
    """The disposable DSN for this run.

    `TEST_DATABASE_URL` wins when set, so CI can point the suite at a database
    it owns. Otherwise the development database's name gains a `_test` suffix —
    `knowledge` becomes `knowledge_test` — which keeps the host, port, user and
    password identical and changes only which data is allowed to be destroyed.
    """
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    source = _source_url()
    name = source.database or ""
    if name.endswith(TEST_DATABASE_SUFFIX):
        return _usable(source)
    return _usable(source.set(database=f"{name}{TEST_DATABASE_SUFFIX}"))


def _usable(url: URL) -> str:
    """A DSN string that can actually be connected with.

    `str(url)` masks the password as `***` — deliberately, because `str(url)` is
    what ends up in log lines. Building the redirect from it produced a DSN that
    looked right and refused to authenticate; caught by
    `tests/unit/test_test_database_isolation.py::test_derived_url_only_changes_the_database_name`.
    """
    return url.render_as_string(hide_password=False)


def is_test_database(url: str | URL | None = None) -> bool:
    """Whether this DSN is one `truncate_all` is permitted to empty."""
    target = make_url(url) if url is not None else _source_url()
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return _usable(target) == explicit
    name = target.database
    return bool(name) and name.endswith(TEST_DATABASE_SUFFIX)


async def _create_database(url: URL) -> None:
    """Create the disposable database if it does not exist.

    `CREATE DATABASE` cannot run inside a transaction, hence AUTOCOMMIT. The
    `postgres` maintenance database is the one that can always be connected to
    for this, and it is not the one being created.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": url.database},
            )
            if not exists:
                await connection.execute(text(f'CREATE DATABASE "{url.database}"'))
    finally:
        await engine.dispose()


def prepare_test_database() -> str:
    """Point this process at the disposable database and migrate it.

    Returns the DSN. Raises `OperationalError` when PostgreSQL is unreachable —
    the calling module turns that into a skip, which is the right outcome: a
    missing dependency is a skip, a broken guarantee is a failure.

    The settings and engine singletons are reset because both cache the DSN
    they first saw. Without the reset the process would truncate the disposable
    database's schema while the application wrote to the development one.
    """
    url = make_url(test_database_url())
    if not url.database or not _SAFE_DATABASE_NAME.match(url.database):
        raise ValueError(f"unusable test database name: {url.database!r}")

    os.environ["DATABASE_URL"] = _usable(url)

    from app.core.config import reset_settings
    from app.db.session import dispose_engine

    reset_settings()
    asyncio.run(dispose_engine())

    asyncio.run(_create_database(url))
    migrate_to_head()
    return _usable(url)


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

    **This refuses to run against anything but a test database.** The redirect in
    `tests/conftest.py` is what normally points the suite at `knowledge_test`; this
    check is what makes it a guarantee rather than a convention. A run that
    reached a development database some other way fails with an explanation
    instead of erasing a crawled corpus, and the failure costs nothing to
    reverse — an emptied corpus does.
    """
    from app.db.session import get_engine

    if not is_test_database():
        raise RuntimeError(
            "refusing to truncate a non-test database: "
            f"{_redact(str(_source_url()))}. The suite runs against a separate "
            "database (see tests/conftest.py); set TEST_DATABASE_URL or run the "
            "suite through pytest so the redirect applies."
        )

    # `query_logs` is listed because it has no foreign key to `sources`, so
    # truncating `sources` never touched it: audit rows accumulated across
    # suites, and a test that counts them was counting its own history.
    # `citations` follows `query_logs` by cascade.
    statement = text("TRUNCATE sources, query_logs, evaluation_questions, evaluation_runs CASCADE")

    # A suite and a running dev server share this database by design, and
    # TRUNCATE wants an AccessExclusive lock on every table it touches. When the
    # server is mid-request it holds a RowExclusive lock on one of them, and the
    # pair deadlocks — measured, twice, as an error in whichever suite happened
    # to truncate second. A short lock timeout with one retry turns that
    # deadlock into a fraction of a second of waiting.
    for attempt in range(2):
        async with get_engine().begin() as connection:
            await connection.execute(text("SET LOCAL lock_timeout = '3s'"))
            try:
                await connection.execute(statement)
                return
            except DBAPIError as exc:  # noqa: PERF203 - the retry is the point
                if attempt == 1 or "lock" not in str(exc).lower():
                    raise


def describe_connection_failure() -> str:
    """A skip message that names the database without naming its credential."""
    return f"PostgreSQL is not reachable at {_database_url()}"
