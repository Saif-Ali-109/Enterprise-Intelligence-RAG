"""Stage-1 fetch with SSRF and robots enforcement.

T050 (fetcher half) and the fetch-side of T045. The discovery half of T045
— sitemap URLs passing the same robots gate — lives in test_discovery.py.

These tests are the contract that:
1. a disallowed path raises RobotsDisallowed and is **never fetched** by
   any other route — the transport mock is asserted uncalled;
2. SSRF refusal fires before robots and before any fetch;
3. a permitted URL returns the response and enforces the byte ceiling;
4. the shared client advertises an honest User-Agent.
"""

from __future__ import annotations

import httpx
import pytest
from app.core import security
from app.core.errors import RobotsDisallowed, SsrfBlocked, ValidationFailed
from app.ingestion.fetcher import fetch_page
from app.ingestion.robots import RobotsGate

pytestmark = pytest.mark.unit


class _TransportGuard:
    """A Counting MockTransport: the assertion target for 'never fetched'."""

    def __init__(self, body: bytes = b"<html></html>", status: int = 200) -> None:
        self.body = body
        self.status = status
        self.calls: list[str] = []

    def make(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(str(request.url))
            return httpx.Response(self.status, content=self.body)

        return httpx.MockTransport(handler)


class TestStageOneFetch:
    @pytest.mark.asyncio
    async def test_disallowed_path_is_never_fetched(self) -> None:
        guard = _TransportGuard()
        client = httpx.AsyncClient(transport=guard.make())
        with pytest.raises(RobotsDisallowed):
            await fetch_page(
                client,
                "https://support.atlassian.com/contact/form",
                allowed_domains=["support.atlassian.com"],
                robots_gate=lambda _url: False,
            )
        assert guard.calls == [], "the gate fetched a page it refused to fetch"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_ssrf_refusal_precedes_fetch_and_robots(self) -> None:
        guard = _TransportGuard()
        client = httpx.AsyncClient(transport=guard.make())
        with pytest.raises(SsrfBlocked):
            await fetch_page(
                client,
                "http://127.0.0.1/administration",
                allowed_domains=["support.atlassian.com"],
                robots_gate=lambda _url: True,
            )
        assert guard.calls == []
        await client.aclose()

    @pytest.mark.asyncio
    async def test_permitted_url_returns_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(security, "check_resolved", lambda *_a, **_k: None)
        guard = _TransportGuard(body=b"<html><body>ok</body></html>")
        client = httpx.AsyncClient(transport=guard.make())
        response = await fetch_page(
            client,
            "https://support.atlassian.com/jira-software-cloud/docs/access-a-project/",
            allowed_domains=["support.atlassian.com"],
            robots_gate=lambda _url: True,
        )
        assert response.is_success
        assert guard.calls == [
            "https://support.atlassian.com/jira-software-cloud/docs/access-a-project/"
        ]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_oversized_page_rejected_at_the_boundary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(security, "check_resolved", lambda *_a, **_k: None)
        guard = _TransportGuard(body=b"x" * 10)
        client = httpx.AsyncClient(transport=guard.make())
        with pytest.raises(ValidationFailed):
            await fetch_page(
                client,
                "https://support.atlassian.com/docs/big",
                allowed_domains=["support.atlassian.com"],
                robots_gate=lambda _url: True,
                max_bytes=5,
            )
        await client.aclose()

    @pytest.mark.asyncio
    async def test_unreachable_robots_stamps_down_the_fetch(self) -> None:
        # An origin whose robots fetch 500s: the primed gate denies, and the
        # one shared predicate then both hides the crawl URL from the
        # expensive SSRF check and protects it from transport.
        handler_calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            handler_calls["n"] += 1
            return httpx.Response(500, text="")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gate = RobotsGate(client)
        await gate.prime(["https://support.atlassian.com/docs/x"])
        assert gate.allows_sync("https://support.atlassian.com/docs/x") is False

        guard = _TransportGuard()
        client2 = httpx.AsyncClient(transport=guard.make())
        with pytest.raises(RobotsDisallowed):
            await fetch_page(
                client2,
                "https://support.atlassian.com/docs/x",
                allowed_domains=["support.atlassian.com"],
                robots_gate=gate.allows_sync,
            )
        assert guard.calls == []
        await client.aclose()
        await client2.aclose()


class TestHonestUserAgent:
    def test_user_agent_names_the_crawler(self) -> None:
        from app.core.config import get_settings

        ua = get_settings().crawl_user_agent
        assert "EnterpriseKnowledgeRAG" in ua
        assert "github.com" in ua
