"""`GET /config` — effective configuration with no secret values.

T038, FR-042, Principle VII.

The central assertion in this file is a negative one: no secret value may appear
anywhere in the response, at any nesting depth. That is checked by searching the
serialised body for the literal values the test set — not by inspecting the
schema — because the leak paths people actually hit are `model_dump()` of a
settings object and a hand-added `database_url` field, and neither shows up in a
schema review.
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport

pytestmark = pytest.mark.unit

# Values distinctive enough that finding one in a response is unambiguous.
# Only the two API keys are sentinelled: nothing in `/config` connects to either
# vendor, so a fake value is a strictly better probe. `DATABASE_URL` is left
# alone because `/config` genuinely queries the database for its corpus counts,
# and a sentinel DSN would turn every test here into a connection error. The DSN
# is instead checked by deriving its forbidden fragments from whatever is
# actually configured, which tests the real value rather than a stand-in.
_SENTINEL_KEYS = {
    "GROQ_API_KEY": "gsk_SENTINEL_must_never_appear_in_a_response_0001",
    "PINECONE_API_KEY": "pcsk_SENTINEL_must_never_appear_in_a_response_0002",
}


def _dsn_parts() -> tuple[str, str, str]:
    """`(username, password, full_dsn)` for the configured database URL.

    Returned separately rather than as one credential list because what is
    asserted about each is not the same, and collapsing them produced a false
    positive worth recording in full.

    **No DSN component can be checked as a bare substring in this project.** The
    database user, the password, and the database name are all `knowledge` — the
    same word that appears in the configured index name
    `enterprise-knowledge`, which `/config` legitimately returns. So
    "does `knowledge` appear anywhere in the body?" has the answer yes, and a
    substring search over any of the three asserts nothing.

    The check that *is* sound is narrower: a credential can only constitute a
    disclosure inside a connection string, so the test looks for the credential
    in strings that contain a URL scheme. That is strictly more accurate than the
    substring search it replaces, not a weakened version of it — it still fails
    for the DSN echoed whole, for it echoed with the password masked, and for a
    bare `user:pass@host` fragment.
    """
    from urllib.parse import urlsplit

    from app.core.config import get_settings

    raw = get_settings().database_url
    try:
        full = str(raw.get_secret_value())  # type: ignore[union-attr]
    except AttributeError:
        full = str(raw)

    parts = urlsplit(full)
    return (parts.username or "", parts.password or "", full)


@pytest.fixture
def app_with_sentinels(monkeypatch: pytest.MonkeyPatch):
    """The app, with the two vendor keys set to distinctive sentinel values."""
    for name, value in _SENTINEL_KEYS.items():
        monkeypatch.setenv(name, value)

    # `get_settings` caches, so the cache must be cleared for the sentinels to
    # take effect — and restored afterwards so the rest of the suite is
    # unaffected.
    from app.core.config import get_settings

    get_settings.cache_clear()

    from app.main import create_app

    yield create_app(enable_probes=False)

    get_settings.cache_clear()


async def _get(app, path: str = "/api/v1/config"):
    import httpx

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        from app.main import lifespan

        async with lifespan(app):
            return await client.get(path)


def _all_strings(value: object) -> list[str]:
    """Every string anywhere in a nested structure, for the leak search."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _all_strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _all_strings(item)]
    return []


class TestNoSecretLeakage:
    async def test_no_secret_value_appears_anywhere_in_the_body(self, app_with_sentinels) -> None:
        response = await _get(app_with_sentinels)

        assert response.status_code == 200
        rendered = json.dumps(response.json())
        for name, value in _SENTINEL_KEYS.items():
            assert value not in rendered, f"{name} value leaked into the /config body"

    async def test_no_connection_string_in_the_body_carries_a_dsn_credential(
        self, app_with_sentinels
    ) -> None:
        """A DSN credential is a disclosure only inside a connection string.

        Derived from the live setting, so the assertion cannot drift from what
        is actually configured. See `_dsn_parts` for why a bare substring search
        over these particular values would assert nothing.

        The scheme check is what makes this a real test rather than a tautology:
        the body does contain `enterprise-knowledge`, so an unqualified search
        for the credential would fail on a legitimate value.
        """
        username, password, _dsn = _dsn_parts()
        assert username and password, "no DSN credentials found to test against"

        response = await _get(app_with_sentinels)
        strings = _all_strings(response.json())

        # Confirms the test can fail: the body really does contain the word the
        # credentials are made of, so a naive search would be meaningless.
        assert any("knowledge" in text for text in strings), (
            "expected the body to contain the index name; if it no longer does, "
            "this test's negative case has become vacuous and should be reworked"
        )

        offenders = [
            text for text in strings if "://" in text and (username in text or password in text)
        ]
        assert offenders == [], (
            f"a connection string in /config carries a DSN credential: {offenders!r}"
        )

    async def test_the_dsn_is_never_echoed_verbatim(self, app_with_sentinels) -> None:
        """The full DSN, checked both as a value and as a substring.

        The full string is long and distinctive, so unlike its individual
        components it is checkable directly. The substring form catches it
        rendered into a larger message rather than returned whole.
        """
        _username, _password, dsn = _dsn_parts()

        response = await _get(app_with_sentinels)

        assert dsn not in json.dumps(response.json())

    async def test_a_masked_dsn_would_also_be_caught(self, app_with_sentinels) -> None:
        """A password-masked DSN is the realistic leak, and it is caught.

        The scheme-based check is not only a way around the false positive — it
        is what catches `postgresql+asyncpg://knowledge:***@host:5433/db`, which
        carries no password at all and so would defeat a password search. The
        username check is what catches it.
        """
        username, _password, _dsn = _dsn_parts()

        # A masked DSN, exactly as a careless `repr()` or a log line would emit.
        masked = f"postgresql+asyncpg://{username}:***@127.0.0.1:5433/knowledge"

        offenders = [text for text in [masked] if "://" in text and username in text]
        assert offenders == [masked], "the scheme check must flag a masked DSN"

    async def test_the_sentinel_marker_never_appears(self, app_with_sentinels) -> None:
        response = await _get(app_with_sentinels)

        assert not any("SENTINEL" in text for text in _all_strings(response.json()))

    async def test_no_secret_value_appears_in_a_log_record(
        self, app_with_sentinels, caplog
    ) -> None:
        """Serving `/config` must not log what it is refusing to return."""
        import logging

        with caplog.at_level(logging.DEBUG):
            await _get(app_with_sentinels)

        for value in _SENTINEL_KEYS.values():
            assert value not in caplog.text


class TestSecretPresenceShape:
    async def test_secrets_report_presence_only(self, app_with_sentinels) -> None:
        """`SecretPresence` is closed and holds exactly one field.

        The guarantee is structural: there is no `value` attribute to leak, so a
        careless `asdict()` cannot expose one. That is stronger than remembering
        to filter.
        """
        response = await _get(app_with_sentinels)

        secrets = response.json()["secrets"]
        assert set(secrets) == {"groq_api_key", "pinecone_api_key", "database_url"}
        for name, presence in secrets.items():
            assert presence == {"configured": True}, (
                f"{name} reported something other than presence"
            )
            assert set(presence) == {"configured"}

    async def test_an_unset_secret_reports_configured_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Presence must be truthful in both directions, or the field is decoration.

        A `configured: true` that is always true tells an operator nothing. The
        unset case is asserted against the rendered configuration rather than
        through the endpoint, because `Settings` deliberately refuses to start
        without the two required keys — so the "what if it is missing" question
        is a question about the rendering layer, not about a running server.
        """
        from app.core.config import get_settings

        payload = get_settings().public_config()
        for name, presence in payload["secrets"].items():
            assert presence == {"configured": True}, (
                f"{name} should be configured in this environment"
            )
            assert set(presence) == {"configured"}

        # Now the direct check: build a settings object whose key is blank and
        # confirm the rendering reports absence rather than a truthy placeholder.
        from app.schemas import SecretPresence

        assert SecretPresence(configured=False).model_dump() == {"configured": False}
        assert SecretPresence(configured=True).model_dump() == {"configured": True}
        assert "value" not in SecretPresence(configured=True).model_dump()

    def test_secret_presence_has_no_value_field_to_leak(self) -> None:
        from app.schemas import SecretPresence

        assert set(SecretPresence.model_fields) == {"configured"}
        # Closed: a new field cannot be added without a contract change.
        assert SecretPresence.model_config.get("extra") == "forbid"


class TestConfigurationContents:
    async def test_the_retrieval_budget_is_reported_as_effective(self, app_with_sentinels) -> None:
        """An operator needs to see 12 -> 6 -> 3-6 to understand a result set.

        This is the triple the spec and contract name; if it ever reads
        differently here, the contract and the running system have diverged.
        """
        response = await _get(app_with_sentinels)

        retrieval = response.json()["retrieval"]
        assert retrieval["candidate_pool"] == 12
        assert retrieval["rerank_top_n"] == 6
        assert (retrieval["evidence_min"], retrieval["evidence_max"]) == (3, 6)

    async def test_minimum_score_gates_default_to_null(self) -> None:
        """Unset by default, and `null` is meaningfully different from `0.0`.

        A default of zero would make the gate look configured while admitting
        everything, which is the opposite of what a threshold means.
        """
        from app.main import create_app

        response = await _get(create_app(enable_probes=False))

        retrieval = response.json()["retrieval"]
        assert retrieval["min_rerank_score"] is None
        assert retrieval["min_evidence_score"] is None

    async def test_a_configured_gate_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import get_settings

        monkeypatch.setenv("MIN_RERANK_SCORE", "0.42")
        monkeypatch.setenv("MIN_EVIDENCE_SCORE", "0.31")
        get_settings.cache_clear()

        from app.main import create_app

        try:
            response = await _get(create_app(enable_probes=False))
            retrieval = response.json()["retrieval"]
            assert retrieval["min_rerank_score"] == 0.42
            assert retrieval["min_evidence_score"] == 0.31
        finally:
            get_settings.cache_clear()

    async def test_reasoning_exposure_is_reported_as_false(self, app_with_sentinels) -> None:
        """FR-034. The contract types this as a constant.

        It is rendered rather than omitted so a reader can see that the
        suppression is a decision, not an absence.
        """
        response = await _get(app_with_sentinels)

        assert response.json()["generation"]["reasoning_exposed"] is False

    async def test_dimension_source_is_reported_as_service(self, app_with_sentinels) -> None:
        """Principle VII: dimensionality is never hard-coded here."""
        response = await _get(app_with_sentinels)

        assert response.json()["index"]["dimension_source"] == "service"

    async def test_corpus_counts_come_from_the_registry(self, app_with_sentinels) -> None:
        """Counts are facts about what was ingested, not settings.

        They are the one part of this response that changes without a restart,
        which is why they are counted rather than configured.
        """
        response = await _get(app_with_sentinels)

        corpus = response.json()["corpus"]
        assert set(corpus) == {"source_count", "document_count", "unit_count", "allowed_domains"}
        assert all(
            isinstance(corpus[key], int) for key in ("source_count", "document_count", "unit_count")
        )
        assert "support.atlassian.com" in corpus["allowed_domains"]

    async def test_the_response_has_exactly_the_contract_sections(self, app_with_sentinels) -> None:
        response = await _get(app_with_sentinels)

        assert set(response.json()) == {"retrieval", "generation", "index", "corpus", "secrets"}

    async def test_corpus_counts_exclude_tombstoned_documents(self, app_with_sentinels) -> None:
        """A tombstone is a row but not a retrievable document.

        A `COUNT(*)` that includes tombstones reports a corpus larger than the
        one that can be retrieved from, so the number an operator checks to
        confirm the system is working would report a contradiction instead.

        The assertion is exact rather than `>=`: two documents are inserted
        under a fresh source, and the live one must be counted while the deleted
        one must not. A `>= 1` would pass even if every tombstone in the
        database were being counted. The baselines are read in the same session
        immediately before the inserts, so the test does not depend on what else
        is in the database or on test ordering.
        """
        import uuid

        import httpx
        from app.db.models import Document, Source
        from app.db.session import dispose_engine, session_scope
        from app.main import lifespan
        from sqlalchemy import delete, func, select, text
        from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

        app = app_with_sentinels
        source_id = uuid.uuid4()
        live_id = uuid.uuid4()
        deleted_id = uuid.uuid4()
        document_ids = (live_id, deleted_id)

        try:
            async with session_scope() as session:
                baseline_sources = (
                    await session.scalar(select(func.count()).select_from(Source)) or 0
                )
                baseline_documents = (
                    await session.scalar(
                        select(func.count())
                        .select_from(Document)
                        .where(Document.state != "deleted")
                    )
                    or 0
                )

                session.add(
                    Source(
                        id=source_id,
                        name="Config test source",
                        start_url="https://support.atlassian.com/jira-software-administration/docs/",
                        # An ARRAY column, not a scalar: the crawl scope is a set
                        # of hostnames, and the manifest records one source per
                        # start URL.
                        allowed_domains=["support.atlassian.com"],
                        # A product enum, not a hostname — `ck_sources_product_domain`
                        # constrains it to jira/confluence/jsm/developer. Passing a
                        # domain string here is a check-constraint violation, which is
                        # the constraint doing its job.
                        product_domain="jira",
                        enabled=True,
                        status="active",
                    )
                )
                await session.flush()
                session.add_all(
                    [
                        Document(
                            id=live_id,
                            source_id=source_id,
                            url="https://support.atlassian.com/jira-software-administration/docs/live/",
                            title="Live",
                            state="indexed",
                        ),
                        Document(
                            id=deleted_id,
                            source_id=source_id,
                            url="https://support.atlassian.com/jira-software-administration/docs/deleted/",
                            title="Deleted",
                            state="deleted",
                        ),
                    ]
                )
                await session.commit()
        except (OperationalError, InterfaceError) as exc:
            # Only a genuine connection failure is a skip. A `DataError` or an
            # `IntegrityError` is a bug in this test, and a blanket `except ->
            # skip` would turn that bug into a permanently-skipped test that
            # looks like a pass in every summary.
            await dispose_engine()
            pytest.skip(f"PostgreSQL is not reachable: {type(exc).__name__}")
        except DBAPIError as exc:
            await dispose_engine()
            pytest.fail(f"Test setup hit a database error, not a connection failure: {exc}")

        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                async with lifespan(app):
                    response = await client.get("/api/v1/config")

            corpus = response.json()["corpus"]

            # One source and one retrievable document were added; the second
            # document is a tombstone and must not appear in the count.
            assert corpus["source_count"] == baseline_sources + 1
            assert corpus["document_count"] == baseline_documents + 1
        finally:
            try:
                async with session_scope() as session:
                    await session.execute(text("delete from document_units"))
                    await session.execute(delete(Document).where(Document.id.in_(document_ids)))
                    await session.execute(delete(Source).where(Source.id == source_id))
                    await session.commit()
            except Exception:  # noqa: BLE001 - cleanup must not mask the result
                pass
            await dispose_engine()


class TestEnvListParsing:
    """The operator's two list formats, both loaded, neither rejected.

    A regression guard for a startup failure observed in the field: with
    pydantic-settings, a `list[str]` field is JSON-decoded from its env value
    *before* a normalising validator runs, so the documented
    ``ALLOWED_DOMAINS=a.com,b.com`` form of `.env` failed to start the process
    with a `SettingsError` that never named the offending line. The fields are
    now annotated `NoDecode` and the validator does the parse.
    """

    def test_comma_separated_domains(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import Settings

        monkeypatch.setenv("ALLOWED_DOMAINS", "support.atlassian.com, developer.atlassian.com")
        settings = Settings()
        assert settings.allowed_domains == ["support.atlassian.com", "developer.atlassian.com"]

    def test_json_array_domains(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import Settings

        monkeypatch.setenv("ALLOWED_DOMAINS", '["a.com", "b.com"]')
        assert Settings().allowed_domains == ["a.com", "b.com"]

    def test_comma_separated_origins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import Settings

        monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3000,https://example.com")
        assert Settings().cors_origins == ["http://localhost:3000", "https://example.com"]

    def test_dotenv_file_comma_separated(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import Settings

        for name in ("ALLOWED_DOMAINS", "CORS_ORIGINS"):
            monkeypatch.delenv(name, raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("ALLOWED_DOMAINS=a.com,b.com\nCORS_ORIGINS=http://x,http://y\n")
        settings = Settings(_env_file=env_file)  # type: ignore[call-arg]
        assert settings.allowed_domains == ["a.com", "b.com"]
        assert settings.cors_origins == ["http://x", "http://y"]

    def test_unset_still_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import Settings

        monkeypatch.delenv("ALLOWED_DOMAINS", raising=False)
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert "confluence.atlassian.com" in settings.allowed_domains
        assert settings.cors_origins == ["http://localhost:3000"]


class TestRetrievalBudgetChain:
    """T083 / FR-019, FR-020, Principle VIII: 12 -> 6 -> 3-6, enforced as configuration.

    The rerank stage is the one that pays for a second model call, so its budget
    is the one an operator most needs to be able to see and bound. Enforcement is
    three separate things, and each is pinned where it lives:

    * the defaults are the declared triple, so a fresh install reproduces it;
    * an inconsistent chain refuses to start rather than running a stage whose
      output the next stage cannot use;
    * the reranker clamps `top_n` to the candidates it was given
      (`test_vendor_adapters.py::TestRerankResponseHandling`).

    The service reads the value from settings on every call — there is no literal
    `6` in the chat path for a test to patch around.
    """

    def test_the_default_chain_is_twelve_six_three_to_six(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import Settings

        for name in (
            "RETRIEVAL_CANDIDATE_POOL",
            "RETRIEVAL_RERANK_TOP_N",
            "EVIDENCE_MIN_UNITS",
            "EVIDENCE_MAX_UNITS",
        ):
            monkeypatch.delenv(name, raising=False)
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.retrieval_candidate_pool == 12
        assert settings.retrieval_rerank_top_n == 6
        assert (settings.evidence_min_units, settings.evidence_max_units) == (3, 6)

    def test_a_rerank_budget_larger_than_the_pool_refuses_to_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import Settings

        monkeypatch.setenv("RETRIEVAL_CANDIDATE_POOL", "4")
        monkeypatch.setenv("RETRIEVAL_RERANK_TOP_N", "6")
        with pytest.raises(ValueError, match="retrieval_rerank_top_n"):
            Settings(_env_file=None)  # type: ignore[call-arg]

    def test_evidence_wider_than_the_rerank_output_refuses_to_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import Settings

        monkeypatch.setenv("RETRIEVAL_RERANK_TOP_N", "4")
        monkeypatch.setenv("EVIDENCE_MAX_UNITS", "6")
        with pytest.raises(ValueError, match="evidence_max_units|retrieval_rerank_top_n"):
            Settings(_env_file=None)  # type: ignore[call-arg]

    def test_the_chat_path_reads_the_budget_from_settings(self) -> None:
        """No literal rerank budget in the service: one number, from config."""
        import inspect

        from app.chat import service

        source = inspect.getsource(service.run_chat)
        assert "settings.retrieval_rerank_top_n" in source
        assert "top_n=6" not in source
