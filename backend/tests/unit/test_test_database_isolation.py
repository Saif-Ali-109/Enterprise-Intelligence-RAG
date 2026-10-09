"""The suite must not be able to truncate a development database.

The mechanism under test is a safety property rather than a feature: a test run
empties nine tables, so the question that matters is which tables. Before
2026-10-09 the answer was "the development ones", and `make test-all` erased a
crawled corpus.

These tests are pure — no PostgreSQL — because the property is about deciding
and refusing, not about connecting. The live half is the integration suite, which
runs against the redirected database and would fail loudly if the redirect were
absent rather than silently doing nothing.
"""

from __future__ import annotations

import pytest

from tests.fixtures.database import (
    TEST_DATABASE_SUFFIX,
    is_test_database,
    prepare_test_database,
)
from tests.fixtures.database import test_database_url as derived_database_url

pytestmark = pytest.mark.unit

_DEV_URL = "postgresql+asyncpg://knowledge:knowledge@localhost:5433/knowledge"
_UNREACHABLE = "postgresql+asyncpg://u:p@127.0.0.1:1/knowledge"


def test_derived_url_only_changes_the_database_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Host, port, user and password are identical; only the name gains a suffix.

    Everything else must match, or the suite would need its own credentials and
    its own `docker compose` file to run — a second thing to keep in step.
    """
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", _DEV_URL)

    derived = derived_database_url()

    assert derived.endswith(f"knowledge{TEST_DATABASE_SUFFIX}")
    assert derived.startswith("postgresql+asyncpg://knowledge:knowledge@localhost:5433/")


def test_an_explicit_test_database_url_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI can name the database it owns rather than accepting a derived one."""
    monkeypatch.setenv("DATABASE_URL", _DEV_URL)
    monkeypatch.setenv("TEST_DATABASE_URL", _UNREACHABLE.replace("/knowledge", "/ci_own"))

    assert derived_database_url() == _UNREACHABLE.replace("/knowledge", "/ci_own")


def test_the_suffix_is_not_applied_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Running against `knowledge_test` must stay `knowledge_test`.

    Without this, `prepare_test_database` is not idempotent and a second
    invocation would point at `knowledge_test_test`, which does not exist — a
    failure whose message names a database nobody ever created.
    """
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "DATABASE_URL", _DEV_URL.replace("/knowledge", f"/knowledge{TEST_DATABASE_SUFFIX}")
    )

    assert derived_database_url().endswith(f"knowledge{TEST_DATABASE_SUFFIX}")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (_DEV_URL, False),
        (_DEV_URL.replace("/knowledge", "/knowledge_test"), True),
        (_DEV_URL.replace("/knowledge", "/knowledge_ci"), False),
        ("sqlite+aiosqlite:///./test.db", False),
    ],
)
def test_only_a_test_database_is_truncatable(
    url: str, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The predicate `truncate_all` gates on.

    A prefix is not a suffix: `knowledge_ci` is not `knowledge_test`, and
    treating it as disposable would be the same mistake in a new place.
    """
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    assert is_test_database(url) is expected


def test_an_explicit_url_makes_exactly_one_database_truncatable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With `TEST_DATABASE_URL` set, the suffix convention is not consulted.

    Otherwise an operator could name a disposable database `knowledge` and find
    it refused, or worse, find `knowledge_test` accepted when they meant their own
    CI database.
    """
    monkeypatch.setenv("TEST_DATABASE_URL", _UNREACHABLE)

    assert is_test_database(_UNREACHABLE) is True
    assert is_test_database(_DEV_URL) is False


def test_a_name_that_is_not_an_identifier_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """`CREATE DATABASE` takes an identifier, not a bound parameter.

    The name reaches DDL by interpolation, so it is validated. The check exists
    because the interpolated value is derived from an environment variable, and
    the alternative — trusting it — would put a DDL injection in a test helper
    that runs on every integration invocation.
    """
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "DATABASE_URL", _DEV_URL.replace("/knowledge", '/x"; DROP DATABASE knowledge; --')
    )

    with pytest.raises(ValueError, match="unusable test database name"):
        prepare_test_database()


def test_truncate_refuses_a_development_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guarantee, stated as a test: the corpus survives a test run.

    Exercised through `truncate_all` rather than the predicate, because the
    predicate is an implementation detail and the refusal is the contract. No
    connection is attempted, which is why this needs no PostgreSQL: the refusal
    happens before the engine is asked for anything.
    """
    import asyncio

    from tests.fixtures import database as fixture

    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", _DEV_URL)
    monkeypatch.setattr(fixture, "migrate_to_head", lambda: None)

    with pytest.raises(RuntimeError, match="refusing to truncate a non-test database"):
        asyncio.run(fixture.truncate_all())
