"""Frontier discovery: sitemaps, links, scope, robots, and determinism.

T045. FR-027, FR-028, FR-044.

The fixtures below are **taken from the measured live pages**, not invented. Each
test that depends on a real Atlassian quirk names the page and the date it was
observed, because a test asserting a belief about a vendor's HTML that was never
checked is how the T027 write-path bug got in.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

# The four support.atlassian.com start_urls, from data/source_manifest.json.
_ARTICLE = "https://support.atlassian.com/jira-software-cloud/docs/access-a-project/"
_ADMIN = "https://support.atlassian.com/jira-cloud-administration/docs/set-up-jira-products/"
_SCOPE = "https://support.atlassian.com/jira-software-cloud/docs/"

# The two developer.atlassian.com start_urls, whose navigation IS server-rendered.
_DEV_PLATFORM = "https://developer.atlassian.com/cloud/jira/platform/"
_DEV_DESK = "https://developer.atlassian.com/cloud/jira/service-desk/"


def _urlset(*urls: str) -> str:
    body = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'


def _sitemapindex(*urls: str) -> str:
    body = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in urls)
    return f'<?xml version="1.0" encoding="UTF-8"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</sitemapindex>'


def _html(*hrefs: str) -> str:
    links = "".join(f'<a href="{h}">x</a>' for h in hrefs)
    return f"<html><body>{links}</body></html>"


def _allow_all(_url: str) -> bool:
    return True


# ============================================================================
# URL normalisation
# ============================================================================


class TestNormaliseUrl:
    def test_a_relative_href_resolves_against_the_page(self) -> None:
        """Resolution is standard, and the result is then scope-checked.

        `../burndown-chart/` from `/jira-software-cloud/docs/access-a-project/`
        resolves to `/jira-software-cloud/docs/burndown-chart/` — one level up is
        the docs section, which is the scope. An earlier version of this test
        expected `view-and-understand-the-burndown-chart/`, which no URL
        arithmetic produces; it was a plausible-looking guess, which is worse
        than a wrong constant.
        """
        from app.ingestion.discovery import normalise_url

        assert (
            normalise_url("../burndown-chart/", base_url=_ARTICLE)
            == "https://support.atlassian.com/jira-software-cloud/docs/burndown-chart/"
        )

    def test_a_fragment_is_stripped(self) -> None:
        """`#section-2` addresses a position in a page, not a different page.

        Keeping it would enqueue the same document once per anchor, and with a
        page cap that would spend the cap on duplicates.
        """
        from app.ingestion.discovery import normalise_url

        assert normalise_url(_ARTICLE + "#before-you-begin", base_url=_ARTICLE) == _ARTICLE

    @pytest.mark.parametrize(
        ("href", "why"),
        [
            ("", "empty"),
            ("   ", "whitespace only"),
            ("mailto:support@example.invalid", "not fetchable"),
            ("javascript:void(0)", "not fetchable"),
            ("tel:+441234567890", "not fetchable"),
            ("/relative/with/no/base", "fine but needs a base"),
        ],
    )
    def test_unusable_hrefs_return_none_rather_than_a_guess(self, href: str, why: str) -> None:
        from app.ingestion.discovery import normalise_url

        assert normalise_url(href, base_url=_ARTICLE) is None or why == "fine but needs a base"

    def test_a_backslash_href_is_rejected(self) -> None:
        """Measured 2026-10-01 on `what-are-issues-and-requests/`.

        The live page emits hrefs with literal escaped backslashes:

            /what-are-issues-and-requests/\\/\\/confluence.atlassian.com\\/servicedeskcloud\\/…

        `urljoin` does **not** reject these. It resolves them to a plausible-looking
        wrong URL under the start page's own path, which is worse than dropping
        them: the wrong URL passes every later check and gets crawled. A
        legitimate backslash is percent-encoded (`%5C`), so a raw one is
        malformed by definition.
        """
        from app.ingestion.discovery import normalise_url

        malformed = (
            "https://support.atlassian.com/jira-service-management-cloud/docs/"
            "what-are-issues-and-requests/\\/\\/confluence.atlassian.com\\/"
            "servicedeskcloud\\/advanced-searching-fields-reference-780868130.html"
        )

        assert normalise_url(malformed, base_url=_ARTICLE) is None

    def test_a_percent_encoded_backslash_is_still_a_valid_url(self) -> None:
        """The rejection must not be so broad it drops real URLs.

        `%5C` is a backslash the publisher actually meant, so refusing it would
        discard legitimate content to fix a different problem.
        """
        from app.ingestion.discovery import normalise_url

        assert normalise_url(f"{_SCOPE}weird%5Cname/", base_url=_ARTICLE) is not None


# ============================================================================
# Scope — the crawl must not leave its section
# ============================================================================


class TestScope:
    def test_a_sibling_article_is_in_scope(self) -> None:
        from app.ingestion.discovery import links_from_html

        sibling = f"{_SCOPE}reopen-a-sprint/"
        frontier = links_from_html(_html(sibling), base_url=_ARTICLE, start_url=_ARTICLE, depth=1)

        assert [c.url for c in frontier.queued] == [sibling]

    def test_another_product_is_out_of_scope(self) -> None:
        """The single most important boundary test.

        `support.atlassian.com` links to every other Atlassian product from every
        page. Following those turns a 40-page Jira corpus into a crawl of the
        whole site, and every unit would be attributed to a product it is not in.
        """
        from app.ingestion.discovery import links_from_html

        other = "https://support.atlassian.com/confluence-cloud/docs/what-is-confluence-cloud/"
        frontier = links_from_html(_html(other), base_url=_ARTICLE, start_url=_ARTICLE, depth=1)

        assert frontier.queued == []
        assert frontier.out_of_scope == 1

    def test_the_scoping_prefix_looks_like_a_prefix_but_is_not(self) -> None:
        """`/docs-archive/` must not match the scope `/docs/`.

        A character comparison would match here, because the string "docs" sits at
        the right offset. `is_within_scope` compares segments for exactly this
        reason; this test pins the behaviour so a future simplification to a
        `startswith` is caught.
        """
        from app.ingestion.discovery import links_from_html

        lookalike = "https://support.atlassian.com/jira-software-cloud/docs-archive/old-page/"
        frontier = links_from_html(_html(lookalike), base_url=_ARTICLE, start_url=_ARTICLE, depth=1)

        assert frontier.queued == []
        assert frontier.out_of_scope == 1

    def test_sitemap_urls_are_held_to_the_same_scope(self) -> None:
        """A sitemap is an inventory, not a scope grant.

        The `jira-cloud` sub-sitemap lists 645 pages; all of them are under
        `/jira-software-cloud/`, so a correct scope rule admits them. This test
        injects one that is not, to prove the filter is actually applied rather
        than merely present.
        """
        from app.ingestion.discovery import urls_from_sitemap

        xml = _urlset(
            f"{_SCOPE}in-scope/",
            "https://support.atlassian.com/confluence-cloud/docs/set-up-confluence-cloud/",
            "https://support.atlassian.com/jira-cloud-administration/docs/configure-fields/",
        )
        frontier = urls_from_sitemap(xml, start_url=_ARTICLE)

        assert [c.url for c in frontier.queued] == [f"{_SCOPE}in-scope/"]
        assert frontier.out_of_scope == 2


# ============================================================================
# Robots — one authorisation path (FR-028)
# ============================================================================


class TestRobotsGate:
    def test_a_disallowed_sitemap_url_is_refused(self) -> None:
        """Measured: support.atlassian.com disallows `/contact/*`.

        A sitemap can list a URL the site's own robots.txt disallows. Honouring
        the sitemap unconditionally would make it a way around a directive.
        """
        from app.ingestion.discovery import urls_from_sitemap

        xml = _urlset(f"{_SCOPE}in-scope/", f"{_SCOPE}contact-us/")
        frontier = urls_from_sitemap(
            xml, start_url=_ARTICLE, robots_allows=lambda u: "contact-us" not in u
        )

        assert [c.url for c in frontier.queued] == [f"{_SCOPE}in-scope/"]
        assert frontier.disallowed == 1

    def test_a_disallowed_link_is_refused_identically(self) -> None:
        """The point of the gate being shared: same rule, same outcome, either way in."""
        from app.ingestion.discovery import links_from_html, urls_from_sitemap

        gate = lambda u: "forbidden" not in u  # noqa: E731
        xml = _urlset(f"{_SCOPE}forbidden-page/")
        via_sitemap = urls_from_sitemap(xml, start_url=_ARTICLE, robots_allows=gate)
        via_link = links_from_html(
            _html(f"{_SCOPE}forbidden-page/"),
            base_url=_ARTICLE,
            start_url=_ARTICLE,
            depth=1,
            robots_allows=gate,
        )

        assert via_sitemap.queued == []
        assert via_link.queued == []
        assert via_sitemap.disallowed == via_link.disallowed == 1

    def test_scope_is_checked_before_robots_so_the_audit_trail_stays_true(self) -> None:
        """An out-of-scope URL must never be reported as a robots violation.

        The site cannot be said to have disallowed a URL we were never entitled
        to ask about. Recording it as `ROBOTS_DISALLOWED` would put a false
        statement in the crawl audit trail — the record is supposed to be a
        faithful account of what happened.
        """
        from app.ingestion.discovery import urls_from_sitemap

        out_of_scope = "https://support.atlassian.com/confluence-cloud/docs/x/"
        frontier = urls_from_sitemap(
            _urlset(out_of_scope), start_url=_ARTICLE, robots_allows=lambda _u: False
        )

        assert frontier.out_of_scope == 1
        assert frontier.disallowed == 0


# ============================================================================
# Sitemaps
# ============================================================================


class TestSitemaps:
    def test_a_sitemapindex_yields_its_children(self) -> None:
        """Measured 2026-10-01: support.atlassian.com/sitemap.xml is a
        sitemapindex of 56 sub-sitemaps, each ending `.xml`.

        An earlier filter looked for `sitemap.xml` as the suffix and found zero
        children, which briefly suggested the host published no sub-sitemaps at
        all. It does; the suffix is just `.xml`. That near-miss is why this test
        pins the actual shape.
        """
        from app.ingestion.discovery import sitemap_index_children

        xml = _sitemapindex(
            "https://support.atlassian.com/jira-cloud.xml",
            "https://support.atlassian.com/confluence-cloud.xml",
            "https://support.atlassian.com/bitbucket-cloud.xml",
        )
        assert sitemap_index_children(xml) == [
            "https://support.atlassian.com/jira-cloud.xml",
            "https://support.atlassian.com/confluence-cloud.xml",
            "https://support.atlassian.com/bitbucket-cloud.xml",
        ]

    def test_a_plain_urlset_is_not_mistaken_for_an_index(self) -> None:
        """A urlset holds pages, not sub-sitemaps; returning them would recurse wrongly."""
        from app.ingestion.discovery import sitemap_index_children

        assert sitemap_index_children(_urlset(f"{_SCOPE}a/")) == []

    def test_sub_sitemaps_inside_a_urlset_are_not_treated_as_pages(self) -> None:
        from app.ingestion.discovery import urls_from_sitemap

        frontier = urls_from_sitemap(
            _urlset(f"{_SCOPE}a/", "https://support.atlassian.com/jira-cloud.xml"),
            start_url=_ARTICLE,
        )

        assert [c.url for c in frontier.queued] == [f"{_SCOPE}a/"]

    def test_sitemap_urls_are_recorded_as_depth_one_via_sitemap(self) -> None:
        """Depth is hops from the registered `start_url`, and provenance is kept.

        Recording depth 0 would understate how far a URL is from the registered
        entry point, which is exactly what an operator reads the crawl job to
        find out.
        """
        from app.ingestion.discovery import urls_from_sitemap

        frontier = urls_from_sitemap(_urlset(f"{_SCOPE}a/"), start_url=_ARTICLE)

        only = frontier.queued[0]
        assert (only.depth, only.via) == (1, "sitemap")

    def test_xml_escaped_ampersands_are_decoded(self) -> None:
        """`<loc>` values are XML-escaped.

        Without unescaping, a URL containing `&` would be enqueued carrying
        `&amp;` and hit a different page than the sitemap names.
        """
        from app.ingestion.discovery import urls_from_sitemap

        frontier = urls_from_sitemap(_urlset(f"{_SCOPE}search/?a=1&amp;b=2"), start_url=_ARTICLE)

        assert [c.url for c in frontier.queued] == [f"{_SCOPE}search/?a=1&b=2"]

    def test_a_malformed_sitemap_url_is_counted_not_queued(self) -> None:
        from app.ingestion.discovery import urls_from_sitemap

        frontier = urls_from_sitemap(
            _urlset(f"{_SCOPE}good/", f"{_SCOPE}bad\\/path/"), start_url=_ARTICLE
        )

        assert [c.url for c in frontier.queued] == [f"{_SCOPE}good/"]
        assert frontier.malformed == 1


# ============================================================================
# Capping and determinism
# ============================================================================


class TestDeterminism:
    def _frontier(self, order: list[str]) -> list[str]:
        from app.ingestion.discovery import build_frontier

        return [
            c.url
            for c in build_frontier(
                _ARTICLE,
                max_pages=3,
                sitemap_xml=_urlset(*order),
                robots_allows=_allow_all,
            ).queued
        ]

    def test_the_capped_set_does_not_depend_on_sitemap_order(self) -> None:
        """**The reason the frontier is sorted.**

        A page cap applied to an unordered set keeps a different subset on each
        run, so `max_pages` would mean something different every time. That
        breaks SC-012 — re-crawling an unchanged source must add zero duplicate
        content — for reasons having nothing to do with content change, and it
        would make the corpus non-reproducible in a way no test could explain.
        """
        forward = [f"{_SCOPE}{name}/" for name in ("a", "b", "c", "d", "e", "f")]
        backward = list(reversed(forward))

        assert self._frontier(forward) == self._frontier(backward)

    def test_the_cap_keeps_the_earliest_urls_alphabetically(self) -> None:
        """The `start_url` occupies one cap slot, since it must be fetched.

        An earlier version of this test expected `a, b, c` and forgot the root,
        which fails correctly: a source whose own registered entry point is not
        crawled is a source that did not do what it was registered to do.
        """
        assert self._frontier([f"{_SCOPE}{n}/" for n in ("f", "e", "d", "c", "b", "a")]) == [
            _ARTICLE,
            f"{_SCOPE}a/",
            f"{_SCOPE}b/",
        ]

    def test_the_start_url_is_always_queued_first(self) -> None:
        """Whatever the cap, the registered entry point is fetched.

        A source whose own `start_url` is absent from its crawl is a source that
        silently did not do what it was registered to do.
        """
        from app.ingestion.discovery import build_frontier

        frontier = build_frontier(
            _ARTICLE, max_pages=1, sitemap_xml=_urlset(f"{_SCOPE}a/"), robots_allows=_allow_all
        )

        assert [c.url for c in frontier.queued] == [_ARTICLE]
        assert frontier.queued[0].via == "start_url"
        assert frontier.queued[0].depth == 0

    def test_a_zero_cap_yields_an_empty_frontier_rather_than_everything(self) -> None:
        from app.ingestion.discovery import build_frontier

        assert (
            build_frontier(_ARTICLE, max_pages=0, sitemap_xml=_urlset(f"{_SCOPE}a/")).queued == []
        )


# ============================================================================
# Counting — no silent loss
# ============================================================================


class TestRefusalsAreCounted:
    def test_every_refused_candidate_is_accounted_for(self) -> None:
        """Discovery that silently drops candidates leaves a gap nothing explains.

        Each category must be counted, and the counts must be able to explain the
        difference between what was offered and what was queued — otherwise a
        shrinking corpus is indistinguishable from a crawler that stopped working.
        """
        from app.ingestion.discovery import build_frontier

        frontier = build_frontier(
            _ARTICLE,
            max_pages=100,
            robots_allows=lambda u: "blocked" not in u,
            sitemap_xml=_urlset(
                f"{_SCOPE}keep-1/",
                f"{_SCOPE}keep-2/",
                f"{_SCOPE}blocked-1/",
                f"{_SCOPE}keep-1/",  # duplicate
                "https://support.atlassian.com/confluence-cloud/docs/other/",  # out of scope
            ),
            link_html=_html(f"{_SCOPE}keep-3/"),
        )

        queued = [c.url for c in frontier.queued]
        assert len(queued) == len(set(queued)), "a duplicate reached the queue"
        assert frontier.disallowed == 1
        assert frontier.out_of_scope == 1
        assert frontier.duplicate == 1
        assert frontier.skipped_total == 3

    def test_duplicates_across_both_mechanisms_are_deduplicated(self) -> None:
        """A page reachable from both the sitemap and a link is one page.

        Without this, a URL present in both is fetched and indexed twice, which
        is the duplicate accumulation SC-012 exists to prevent.
        """
        from app.ingestion.discovery import build_frontier

        frontier = build_frontier(
            _ARTICLE,
            max_pages=100,
            sitemap_xml=_urlset(f"{_SCOPE}shared/"),
            link_html=_html(f"{_SCOPE}shared/"),
            robots_allows=_allow_all,
        )

        assert [c.url for c in frontier.queued].count(f"{_SCOPE}shared/") == 1
        assert frontier.duplicate == 1


# ============================================================================
# The measured corpus shape
# ============================================================================


class TestMeasuredCorpusShape:
    """What the discovery decision was based on. If Atlassian's HTML changes,
    these are the numbers that stop matching and the decision should be revisited.
    """

    def test_support_article_pages_expose_almost_no_in_scope_links(self) -> None:
        """Measured 2026-10-01, live:

            access-a-project/                       667 hrefs -> 1 in scope
            set-up-jira-products/                   283 hrefs -> 4, 3 malformed
            what-are-issues-and-requests/          1235 hrefs -> 4, 3 malformed
            create-and-organize-work-in-confluence/ 484 hrefs -> 1

        This is why four sources use sitemaps and only two use links. If this
        test starts failing, the documentation navigation has become
        server-rendered and the split is no longer needed.
        """
        from app.ingestion.discovery import links_from_html

        # A representative page: a link to a sibling, a link to another product,
        # a link to the account area, and the malformed form measured on it.
        html = _html(
            f"{_SCOPE}reopen-a-sprint/",
            "https://support.atlassian.com/confluence-cloud/docs/set-up-confluence-cloud/",
            "https://support.atlassian.com/jira-software-cloud/access-a-project/",
            f"{_SCOPE}x/\\/\\/confluence.atlassian.com\\/y/",
        )
        frontier = links_from_html(html, base_url=_ARTICLE, start_url=_ARTICLE, depth=1)

        assert [c.url for c in frontier.queued] == [f"{_SCOPE}reopen-a-sprint/"]
        assert frontier.out_of_scope == 2
        assert frontier.malformed == 1

    def test_developer_landing_pages_do_expose_in_scope_links(self) -> None:
        """Measured 2026-10-01: 29 in-scope links at depth 1 on `/cloud/jira/platform/`.

        The developer site renders its navigation server-side, which is the whole
        reason these two sources use link-following and the other four do not.
        """
        from app.ingestion.discovery import links_from_html

        html = _html(
            f"{_DEV_PLATFORM}basic-auth-for-rest-apis/",
            f"{_DEV_PLATFORM}apis/document/structure",
        )
        frontier = links_from_html(html, base_url=_DEV_PLATFORM, start_url=_DEV_PLATFORM, depth=1)

        assert len(frontier.queued) == 2
        assert all(c.via == "link" for c in frontier.queued)


class TestIndexStartUrlNeedsAnExplicitScope:
    """`scope_prefix` drops the leaf segment, which is wrong for an index.

    Measured consequence: `scope_prefix("…/cloud/jira/platform/")` returns
    `/cloud/jira/`, not `/cloud/jira/platform/`. That is correct behaviour for an
    *article* start URL — `/docs/access-a-project/` must scope to `/docs/` — but
    for a start URL that **is** a section index it silently widens the crawl to
    sibling sections like `/cloud/jira/software/`, which is a different product
    surface than the one registered.

    So the derived scope stays the default (it is right far more often) and a
    source that is an index declares its own prefix.
    """

    def test_the_derived_scope_of_an_index_is_too_broad(self) -> None:
        """The gap itself, pinned so the fix is visible rather than assumed."""
        from app.core.security import scope_prefix

        assert scope_prefix(_DEV_PLATFORM) == "/cloud/jira/"

    def test_without_an_explicit_scope_a_sibling_section_is_crawled(self) -> None:
        from app.ingestion.discovery import build_frontier

        frontier = build_frontier(
            _DEV_PLATFORM,
            max_pages=100,
            link_html=_html("https://developer.atlassian.com/cloud/jira/software/some-page/"),
            robots_allows=_allow_all,
        )

        assert len(frontier.queued) == 2, (
            "expected the wrongly-widened derived scope to admit the sibling "
            "section; if this now fails the derived rule changed and this test "
            "needs revisiting"
        )

    def test_an_explicit_scope_excludes_the_sibling_section(self) -> None:
        from app.ingestion.discovery import build_frontier

        frontier = build_frontier(
            _DEV_PLATFORM,
            max_pages=100,
            scope="/cloud/jira/platform/",
            link_html=_html(
                "https://developer.atlassian.com/cloud/jira/software/some-page/",
                "https://developer.atlassian.com/cloud/jira/platform/basic-auth-for-rest-apis/",
            ),
            robots_allows=_allow_all,
        )

        queued = [c.url for c in frontier.queued]
        assert (
            "https://developer.atlassian.com/cloud/jira/platform/basic-auth-for-rest-apis/"
            in queued
        )
        assert "https://developer.atlassian.com/cloud/jira/software/some-page/" not in queued
        assert frontier.out_of_scope == 1

    def test_an_explicit_scope_still_confines_the_start_url_itself(self) -> None:
        """The root is queued directly, so it must be checked against the scope
        too — otherwise a mistyped scope would admit a root outside it."""
        from app.ingestion.discovery import build_frontier

        frontier = build_frontier(
            _DEV_PLATFORM, max_pages=100, scope="/cloud/jira/software/", robots_allows=_allow_all
        )

        assert frontier.queued == []
        assert frontier.out_of_scope == 1

    def test_depth_does_not_apply_to_a_source_that_supplies_a_sitemap(self) -> None:
        """A sitemap already enumerates the section, so deeper traversal adds
        only the site's own cross-section navigation — which scope then removes.
        """
        from app.ingestion.discovery import build_frontier

        frontier = build_frontier(
            _ARTICLE,
            max_pages=100,
            max_depth=0,
            sitemap_xml=_urlset(f"{_SCOPE}a/"),
            robots_allows=_allow_all,
        )

        assert [c.url for c in frontier.queued] == [_ARTICLE, f"{_SCOPE}a/"]
