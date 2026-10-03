"""Content-type validation and boilerplate rejection.

T051. FR-029. The cleaner is the gate that keeps a stub, an error page, or
a JSON payload from becoming a searchable "document" — the failure mode
that pollutes retrieval with one-sentence "main content".
"""

from __future__ import annotations

import pytest
from app.ingestion.cleaner import (
    ContentRejected,
    assert_html_content_type,
    assess_boilerplate,
    clean,
)

pytestmark = pytest.mark.unit


class TestContentType:
    def test_html_accepted(self) -> None:
        assert_html_content_type("text/html; charset=utf-8")
        assert_html_content_type("application/xhtml+xml")

    def test_missing_rejected(self) -> None:
        with pytest.raises(ContentRejected) as exc:
            assert_html_content_type(None)
        assert exc.value.reason == "missing_content_type"

    def test_non_html_rejected(self) -> None:
        with pytest.raises(ContentRejected) as exc:
            assert_html_content_type("application/json")
        assert exc.value.reason == "non_html_content_type"
        with pytest.raises(ContentRejected):
            assert_html_content_type("application/pdf")


class TestBoilerplate:
    HEALTHY = (
        "Create a new space to organize your work. Every space has a unique key and can hold "
        "many pages. You can control who sees the pages inside, and you can move pages between "
        "spaces. A space can also be archived when the work inside it is finished, and its "
        "pages remain searchable to those with access. Archived spaces can be restored at "
        "any time from the spaces directory."
    ) * 3

    def test_healthy_page_passes(self) -> None:
        result = clean(self.HEALTHY)
        assert result.words > 0

    def test_too_small_rejected(self) -> None:
        with pytest.raises(ContentRejected) as exc:
            assess_boilerplate("Hello world")
        assert exc.value.reason == "too_small"

    def test_error_page_rejected(self) -> None:
        text = (
            "Page not found. The page you are looking for does not exist. Please check the URL and try again. "
            * 5
        )
        with pytest.raises(ContentRejected) as exc:
            assess_boilerplate(text)
        assert exc.value.reason == "error_page"

    def test_boilerplate_heavy_rejected(self) -> None:
        text = ("self help please contact support self help please contact support " * 20).strip()
        with pytest.raises(ContentRejected) as exc:
            assess_boilerplate(text)
        assert exc.value.reason == "boilerplate_heavy"
