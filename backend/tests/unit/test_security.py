"""SSRF guard: every forbidden class of target is refused before connecting.

T040. FR-044, SC-014 (quickstart V1). The static check is intentionally
pure — no DNS lookup is needed to refuse `http://169.254.169.254/` — so a
target in each of the five categories must be refused by `check_url`
alone. Asserting the *categories* rather than individual strings keeps the
suite a policy statement: a new loopback spelling added later falls out of
the classification tests rather than being forgotten.
"""

from __future__ import annotations

import pytest
from app.core.security import CLOUD_METADATA_ADDRESSES, check_url

pytestmark = pytest.mark.unit

ALLOWED = ["support.atlassian.com"]


def _reason(url: str):
    result = check_url(url, allowed_domains=ALLOWED)
    return result.reason


class TestForbiddenClassesRefused:
    def test_loopback_ipv4(self) -> None:
        assert _reason("http://127.0.0.1/admin") is not None
        assert _reason("http://127.0.0.2/x") is not None
        assert _reason("http://127.255.0.1/x") is not None

    def test_loopback_ipv6(self) -> None:
        assert _reason("http://[::1]/x") is not None

    def test_private_ranges(self) -> None:
        assert _reason("http://10.0.0.5/") is not None
        assert _reason("http://172.16.0.1/") is not None
        assert _reason("http://172.31.255.255/") is not None
        assert _reason("http://192.168.1.1/") is not None

    def test_link_local(self) -> None:
        assert _reason("http://169.254.0.1/") is not None
        assert _reason("http://169.254.169.254/latest/meta-data/") is not None

    def test_metadata_service_explicit(self) -> None:
        for address in CLOUD_METADATA_ADDRESSES:
            # A hostname form that we have forbidden must also fail as an IP.
            assert check_url(f"http://{address}/x", allowed_domains=ALLOWED).reason is not None

    def test_non_web_schemes(self) -> None:
        assert _reason("file:///etc/passwd") is not None
        assert _reason("ftp://example.com/x") is not None
        assert _reason("gopher://example.com/x") is not None
        assert _reason("mailto:user@example.com") is not None

    def test_internal_hostnames_refused(self) -> None:
        assert _reason("http://localhost/x") is not None
        assert _reason("http://localhost./x") is not None

    def test_embedded_credentials_refused(self) -> None:
        assert _reason("http://user:pw@support.atlassian.com/x") is not None

    def test_disallowed_ports_refused(self) -> None:
        assert _reason("http://support.atlassian.com:22/x") is not None

    def test_allowed_public_url_passes_static_check(self) -> None:
        assert (
            check_url("https://support.atlassian.com/docs/x", allowed_domains=ALLOWED).reason
            is None
        )
