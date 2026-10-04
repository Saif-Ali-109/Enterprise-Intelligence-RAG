"""Robots gate: parsing, matching, caching, conservative-unreachable.

T049. The gate is the single authorisation path for sitemap- and
link-derived URLs alike, so its matching rules and its failure modes are
pinned here — a behaviour change in `parse_robots` must fail a test, not
silently widen or narrow what the crawler may fetch.
"""

from __future__ import annotations

import httpx
import pytest
from app.ingestion.robots import RobotsGate, parse_robots

pytestmark = pytest.mark.unit


class TestParseAndMatch:
    def test_single_star_group_disallows(self) -> None:
        policy = parse_robots(
            "User-agent: *\nDisallow: /contact/*\nDisallow: /jira-service-desk-cloud-deprecated/*\n"
        )
        assert policy.allows("/contact/form") is False
        assert policy.allows("/jira-service-desk-cloud-deprecated/x") is False
        assert policy.allows("/jira-software-cloud/docs/access-a-project/") is True

    def test_allow_beats_disallow_on_tie(self) -> None:
        policy = parse_robots("User-agent: *\nDisallow: /private/*\nAllow: /private/public\n")
        # Allow and Disallow match the same length: explicit Allow wins.
        assert policy.allows("/private/public") is True
        assert policy.allows("/private/secret") is False

    def test_longest_specific_rule_wins(self) -> None:
        policy = parse_robots("User-agent: *\nDisallow: /*\nAllow: /docs/\n")
        assert policy.allows("/docs/page") is True
        assert policy.allows("/other") is False

    def test_wildcard_and_end_anchor(self) -> None:
        policy = parse_robots("User-agent: *\nDisallow: /*.php$\n")
        assert policy.allows("/index.php") is False
        assert policy.allows("/docs/index.html") is True

    def test_document_with_only_allow_rules(self) -> None:
        policy = parse_robots("User-agent: *\nAllow: /\n")
        assert policy.allows("/anything") is True

    def test_empty_disallow_means_no_restriction(self) -> None:
        policy = parse_robots("User-agent: *\nDisallow:\n")
        assert policy.allows("/anything") is True

    def test_named_agent_group_does_not_apply_to_us(self) -> None:
        # A directive aimed at another named bot must not suddenly govern us.
        policy = parse_robots(
            "User-agent: atlassian-bot\nDisallow: /*\n\nUser-agent: *\nAllow: /\n"
        )
        assert policy.allows("/docs/page") is True

    def test_our_agent_group_overrides_star(self) -> None:
        policy = parse_robots(
            "User-agent: *\nDisallow: /a/\n\nUser-agent: EnterpriseKnowledgeRAG\nAllow: /a/\n"
        )
        assert policy.allows("/a/page") is True

    def test_disallow_without_leading_slash_is_normalised(self) -> None:
        # Measured 2026-10-01 on developer.atlassian.com: the directive is
        # printed as `Disallow: platform/...` with no leading slash. Treating
        # it as written would match no request path; the gate prepends the
        # slash so the author's intent holds.
        policy = parse_robots(
            "User-agent: *\nDisallow: platform/forge/ui-kit-components/uik1_all\n"
        )
        assert policy.allows("/platform/forge/ui-kit-components/uik1_all/index") is False

    def test_crawl_delay_read_from_matching_group(self) -> None:
        policy = parse_robots("User-agent: *\nCrawl-delay: 8\n")
        assert policy.crawl_delay_seconds == 8.0

    def test_crawl_delay_unparseable_is_absent(self) -> None:
        policy = parse_robots("User-agent: *\nCrawl-delay: soon-ish\n")
        assert policy.crawl_delay_seconds is None


class _FakeOriginClient:
    """An httpx client whose robots.txt response each origin is canned with."""

    def __init__(self, routes: dict[str, httpx.Response | Exception]) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            route = routes.get(str(request.url))
            if route is None:
                return httpx.Response(404, text="")
            if isinstance(route, Exception):
                raise route
            return route

        self._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.calls: list[str] = []

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    async def aclose(self) -> None:
        await self._client.aclose()


class TestGate:
    @pytest.mark.asyncio
    async def test_404_means_allowed(self) -> None:
        fake = _FakeOriginClient({})
        gate = RobotsGate(fake.client)
        await gate.prime(["https://support.atlassian.com/docs/x"])
        assert gate.allows_sync("https://support.atlassian.com/docs/x") is True
        await fake.aclose()

    @pytest.mark.asyncio
    async def test_5xx_means_denied(self) -> None:
        fake = _FakeOriginClient({"https://a.example/robots.txt": httpx.Response(503, text="down")})
        gate = RobotsGate(fake.client)
        await gate.prime(["https://a.example/docs/x"])
        assert gate.allows_sync("https://a.example/docs/x") is False
        await fake.aclose()

    @pytest.mark.asyncio
    async def test_network_error_means_denied(self) -> None:
        fake = _FakeOriginClient({"https://a.example/robots.txt": httpx.ConnectError("refused")})
        gate = RobotsGate(fake.client)
        await gate.prime(["https://a.example/docs/x"])
        assert gate.allows_sync("https://a.example/docs/x") is False
        await fake.aclose()

    @pytest.mark.asyncio
    async def test_unprimed_origin_is_denied(self) -> None:
        fake = _FakeOriginClient({})
        gate = RobotsGate(fake.client)
        await gate.prime(["https://a.example/docs/x"])
        # A different, unprimed origin must fail closed, not open.
        assert gate.allows_sync("https://b.example/docs/x") is False
        await fake.aclose()

    @pytest.mark.asyncio
    async def test_prime_caches_each_origin_once(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, text="User-agent: *\nDisallow: /x/\n")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gate = RobotsGate(client)
        await gate.prime([f"https://a.example/docs/{i}" for i in range(5)])
        assert calls["n"] == 1
        assert gate.allows_sync("https://a.example/x/y") is False
        assert gate.allows_sync("https://a.example/ok") is True
        await client.aclose()
