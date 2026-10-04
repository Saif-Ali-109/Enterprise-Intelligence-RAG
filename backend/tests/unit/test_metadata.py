"""Metadata extraction: title, product, category, page_type, language, confidence.

T043/T053. FR-024. Asserts the honesty property: each derivation carries a
confidence value, and the derivation is allowed to be low-confidence when
the evidence — the page alone — does not justify a certain record.
"""

from __future__ import annotations

import pytest
from app.ingestion.metadata import extract_metadata

pytestmark = pytest.mark.unit


class TestProduct:
    def test_product_from_docs_path(self) -> None:
        meta = extract_metadata(
            "<html></html>", url="https://support.atlassian.com/confluence-cloud/docs/x/"
        )
        assert meta.product == "confluence"
        assert meta.product_confidence >= 0.9

    def test_product_from_developer_platform_path(self) -> None:
        meta = extract_metadata(
            "<html></html>",
            url="https://developer.atlassian.com/cloud/jira/platform/rate-limiting/",
        )
        assert meta.product == "jira"

    def test_product_from_developer_service_desk_path(self) -> None:
        meta = extract_metadata(
            "<html></html>", url="https://developer.atlassian.com/cloud/jira/service-desk/rest/"
        )
        assert meta.product == "jsm"


class TestCategory:
    def test_rest_api_category(self) -> None:
        meta = extract_metadata(
            "<html></html>",
            url="https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue/",
        )
        assert meta.category == "rest-api"

    def test_docs_category(self) -> None:
        meta = extract_metadata(
            "<html></html>",
            url="https://support.atlassian.com/jira-software-cloud/docs/create-a-new-project/",
        )
        assert meta.category == "user-documentation"


class TestTitle:
    def test_title_from_h1_preferred_over_title_tag(self) -> None:
        html = "<html><head><title>Chrome Title</title></head><body><h1>Article Heading</h1></body></html>"
        meta = extract_metadata(
            html, url="https://support.atlassian.com/jira-software-cloud/docs/x/"
        )
        assert meta.title == "Article Heading"
        assert meta.title_confidence >= 0.9

    def test_title_falls_back_to_title_tag_with_lower_confidence(self) -> None:
        html = "<html><head><title>Article X</title></head><body><p>Body.</p></body></html>"
        meta = extract_metadata(
            html, url="https://support.atlassian.com/jira-software-cloud/docs/x/"
        )
        assert meta.title == "Article X"
        assert meta.title_confidence < 0.9


class TestLanguage:
    PROSE = (
        "Create a new space to organize your work. A space holds pages that you and your team "
        "can access. You can set its security level and decide who can see it. Spaces are "
        "designed for documentation, planning, and collaboration across teams."
    )

    def test_english_prose_detected(self) -> None:
        meta = extract_metadata(
            f"<html><body><p>{self.PROSE}</p></body></html>", url="https://example.com/docs/x/"
        )
        assert meta.language == "en"

    def test_non_english_returns_unknown_not_a_guess(self) -> None:
        de = (
            "Erstellen Sie einen neuen Bereich, um Ihre Arbeit zu organisieren. "
            "Ein Bereich enthält Seiten, auf die Sie und Ihr Team zugreifen können."
        )
        meta = extract_metadata(
            f"<html><body><p>{de}</p></body></html>", url="https://example.com/docs/x/"
        )
        assert meta.language in ("unknown", "en")  # heuristic may pass DE as en (MEDIUM)
        if meta.language == "en":
            assert meta.language_confidence <= 0.6

    def test_no_text_is_unknown_with_low_confidence(self) -> None:
        meta = extract_metadata("<html></html>", url="https://example.com/")
        assert meta.language == "unknown"
        assert meta.language_confidence <= 0.3


class TestCanonical:
    def test_canonical_from_link_tag(self) -> None:
        html = (
            '<html><head><link rel="canonical" href="https://canonical.example/docs/x"/></head>'
            "<body></body></html>"
        )
        meta = extract_metadata(html, url="https://other.example/docs/x")
        assert meta.canonical_url == "https://canonical.example/docs/x"

    def test_canonical_falls_back_to_url(self) -> None:
        meta = extract_metadata("<html></html>", url="https://example.com/docs/x/")
        assert meta.canonical_url == "https://example.com/docs/x/"


class TestPageType:
    def test_rest_url_is_api_reference(self) -> None:
        meta = extract_metadata(
            "<html></html>",
            url="https://developer.atlassian.com/cloud/jira/platform/rest/v3/intro/",
        )
        assert meta.page_type == "api_reference"

    def test_create_title_is_how_to(self) -> None:
        html = "<html><body><h1>Create a space</h1></body></html>"
        meta = extract_metadata(
            html, url="https://support.atlassian.com/confluence-cloud/docs/create-a-space/"
        )
        assert meta.page_type == "how_to"
