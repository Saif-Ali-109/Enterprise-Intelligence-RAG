"""The async engine and session factory.

Constitution Principle VIII: one database access path, so there is one place
where connection behaviour is decided. The engine is created once per process
and shared — an engine owns a connection pool, and creating one per request
defeats the pool and exhausts PostgreSQL's connection slots under a modest load.

Two decisions that are not obvious:

**`pool_pre_ping` is on.** A pooled connection can be dead by the time the
application lends it out — the database was restarted, or a proxy timed the
connection out while it sat idle. Without a ping, that surfaces as an
`OperationalError` from the first query on a borrowed connection, which is
indistinguishable from a genuine outage. The ping costs one round trip and turns
a spurious failure into a transparent reconnect.

**`expire_on_commit=False`.** The default expires every attribute on commit,
which forces a SELECT to read back anything touched before the commit. The
pipeline writes a document, commits, and then needs its id and timestamps for the
vector metadata it is about to send — with expiry on, that is an extra query per
object, and a detached instance becomes a `DetachedInstanceError` rather than a
plain attribute read.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.logging import get_logger
from app.db.models import Base

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.engine import URL

_log = get_logger("db.session")

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def build_engine(
    database_url: str,
    *,
    echo: bool = False,
    pool_size: int = 5,
    max_overflow: int = 5,
) -> AsyncEngine:
    """Construct an engine for a URL.

    Separate from `get_engine` so a test can build an engine against a different
    database without replacing the process-wide one. That is how the integration
    suite gets an isolated schema.

    `poolclass` is not specified: SQLAlchemy selects the right pool for the
    dialect, and `asyncpg` wants `AsyncAdaptedQueuePool` rather than the
    synchronous default.
    """
    kwargs: dict[str, Any] = {
        "echo": echo,
        # A pooled connection can be dead by the time the application lends it
        # out — the database restarted, or a proxy timed it out while idle.
        # Without a ping that surfaces as an `OperationalError` from the first
        # query, which is indistinguishable from a genuine outage.
        "pool_pre_ping": True,
        "pool_size": pool_size,
        "max_overflow": max_overflow,
        "future": True,
    }

    # SQLite has no pool to configure and rejects the pool arguments outright.
    # The integration suite uses it for a fast schema check, so the branch is
    # here rather than forcing every test to provision PostgreSQL.
    if database_url.startswith("sqlite"):
        kwargs.pop("pool_size")
        kwargs.pop("max_overflow")
        kwargs.pop("pool_pre_ping")

    return create_async_engine(database_url, **kwargs)


def get_engine(database_url: str | None = None) -> AsyncEngine:
    """The process-wide engine, created on first use."""
    global _engine

    if _engine is None:
        from app.core.config import get_settings

        settings = get_settings()
        url = database_url or settings.database_url
        _engine = build_engine(
            url,
            # `echo=True` logs every statement. Useful for one debugging run,
            # and a way to put every row of every query into a log sink, so it
            # is a deliberate setting rather than a debugging leftover.
            echo=settings.log_level == "DEBUG",
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
        )
        _log.info("database engine created", extra={"dialect": _engine.dialect.name})

    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """The process-wide session factory."""
    global _session_factory

    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, committed or rolled back.

    A generator dependency rather than a middleware, because the transaction
    scope belongs to the request handler. A handler that raises must not leave a
    half-applied ingest behind, and the only place that can be guaranteed is
    where the session was opened.
    """
    factory = get_session_factory()

    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@contextlib.asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """A session outside the request cycle: CLI, the crawler, the evaluator.

    Same commit/rollback discipline as `get_session`, for code that is not
    running inside a request. The evaluator and the crawl orchestrator both need
    this, and neither should be reimplementing the rollback.
    """
    factory = get_session_factory()

    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_all() -> None:
    """Create every table directly from the models.

    For the test suite and for `make migrate` on a disposable database. NOT the
    production path: Alembic owns the schema, and `create_all` cannot express a
    data migration or a partial index added later. Using it against a real
    database would leave that database with no record of how it got its shape.
    """
    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def drop_all() -> None:
    """Drop every table. Test teardown only."""
    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)


async def dispose_engine() -> None:
    """Close the pool. Called on application shutdown.

    Without this, shutting down leaves connections open until the garbage
    collector notices, and PostgreSQL logs the abrupt disconnects. It also makes
    the engine replaceable, so a test can point the process at a different
    database.
    """
    global _engine, _session_factory

    if _engine is not None:
        await _engine.dispose()
        _log.info("database engine disposed")
    _engine = None
    _session_factory = None


def describe_url(url: str | URL | None = None) -> str:
    """A loggable description of a database URL with the password removed.

    Used in the startup diagnostic. `str(url)` on a SQLAlchemy URL renders the
    password, and a startup message is exactly the kind of line that ends up in
    a log aggregator for months.
    """
    if url is None:
        from app.core.config import get_settings

        url = get_settings().database_url

    try:
        from sqlalchemy.engine import make_url

        parsed = make_url(str(url))
        return f"{parsed.drivername}://{parsed.username or ''}@{parsed.host or ''}:{parsed.port or ''}/{parsed.database or ''}"
    except Exception:  # pragma: no cover - a malformed URL is a startup failure
        return "<unparseable database url>"


__all__ = [
    "build_engine",
    "create_all",
    "describe_url",
    "dispose_engine",
    "drop_all",
    "get_engine",
    "get_session",
    "get_session_factory",
    "session_scope",
]
