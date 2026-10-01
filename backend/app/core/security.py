"""The SSRF guard, and the input bounds.

FR-043, FR-044, SC-014. The requirement is narrow and the failure it prevents
is severe: this system fetches URLs supplied by an operator, and a fetch
service that will dial `169.254.169.254` on request is a credential-exfiltration
primitive regardless of what the rest of the system does.

Four rules, in the order they are applied:

1. **Scheme first.** Parse, then require `http` or `https`. `file://`,
   `gopher://`, and `dict://` are refused before any host resolution, because
   resolving a host is itself a network operation.
2. **Allowlist by host, exact match.** The host must be one of
   `ALLOWED_DOMAINS`, compared case-insensitively against the *parsed* host.
   Substring matching is how `support.atlassian.com.evil.test` gets in.
3. **Resolve, then check every address.** A hostname that resolves to
   `127.0.0.1` or `10.0.0.1` is refused. This is the check that matters, and it
   is why the guard is a separate step from the allowlist: an allowlist of real
   hostnames would normally make resolution unnecessary, but a compromised or
   misconfigured DNS response can point an allowlisted name anywhere, so both
   checks run and the resolved address is the one that binds.
4. **Port restricted.** Only 80 and 443, so an allowlisted host cannot be used
   to reach an internal service that happens to be listening on another port.

Everything here is synchronous and pure except `assert_resolves_public`, which
is the only part that touches DNS. That split is deliberate: 100% of the
classification logic is testable without a network, which is what T040 asserts
against a fixed corpus of hostile targets.
"""

from __future__ import annotations

import ipaddress
import socket
from enum import StrEnum
from typing import Final
from urllib.parse import unquote, urlsplit

# ============================================================================
# Refusal reasons
# ============================================================================


class RefusalReason(StrEnum):
    """Why a URL was refused. Enumerated, so it is safe to return to a client.

    A category, never the address. Naming the offending address in a response
    tells whoever probed the endpoint which internal ranges are in use, which is
    the reconnaissance this guard exists to prevent. The address goes in the
    log, keyed by request id.
    """

    NOT_ABSOLUTE = "not_absolute"
    NON_WEB_SCHEME = "non_web_scheme"
    MALFORMED = "malformed"
    NO_HOST = "no_host"
    NOT_ALLOWLISTED = "not_allowlisted"
    LOOPBACK = "loopback"
    PRIVATE_RANGE = "private_range"
    LINK_LOCAL = "link_local"
    CLOUD_METADATA = "cloud_metadata"
    RESERVED = "reserved"
    MULTICAST = "multicast"
    UNSPECIFIED = "unspecified"
    DISALLOWED_PORT = "disallowed_port"
    EMBEDDED_CREDENTIALS = "embedded_credentials"
    UNRESOLVABLE = "unresolvable"


#: The cloud instance-metadata address. Checked as a named case rather than
#: falling out of "link-local", because it is the single most valuable target
#: for this class of attack and it deserves to be unambiguous in a log.
#:
#: 169.254.169.254 is covered by the link-local rule too, so this is not a
#: separate defence — it is a separate LABEL, so an operator reading a log can
#: see at a glance that an attempt was made on the metadata service.
CLOUD_METADATA_ADDRESSES: Final[frozenset[str]] = frozenset(
    {
        "169.254.169.254",  # AWS, Azure, GCP, OpenStack, DigitalOcean
        "169.254.170.2",  # AWS ECS task metadata
        "100.100.100.200",  # Alibaba Cloud
        "192.0.0.192",  # Oracle Cloud
        "fd00:ec2::254",  # AWS IMDSv6
    }
)

#: Hostnames that resolve inside a network regardless of DNS. Refused by name,
#: before resolution, because resolving them is a wasted network call and on
#: some resolvers a leak.
INTERNAL_HOSTNAMES: Final[frozenset[str]] = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        # Kubernetes and Docker service discovery. A containerised backend
        # reaching the database is normal; a *crawl* reaching the database is
        # not, and both would resolve.
        "kubernetes",
        "kubernetes.default",
        "kubernetes.default.svc",
        "host.docker.internal",
        "gateway.docker.internal",
    }
)

ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})
ALLOWED_PORTS: Final[frozenset[int]] = frozenset({80, 443})


# ============================================================================
# Address classification
# ============================================================================


def classify_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> RefusalReason | None:
    """Classify a resolved address. `None` means acceptable.

    Ordering is deliberate. The most specific test comes first, so the reason
    recorded is the one an operator would act on: an attempt on the metadata
    service is labelled `cloud_metadata`, not `link_local`, and an operator
    reading the log does not have to know that 169.254.0.0/16 happens to cover
    it.
    """
    if str(ip) in CLOUD_METADATA_ADDRESSES:
        return RefusalReason.CLOUD_METADATA

    if ip.is_unspecified:
        return RefusalReason.UNSPECIFIED

    if ip.is_loopback:
        return RefusalReason.LOOPBACK

    if ip.is_link_local:
        return RefusalReason.LINK_LOCAL

    if ip.is_multicast:
        return RefusalReason.MULTICAST

    if ip.is_private:
        # `is_private` in the stdlib covers RFC 1918, RFC 4193 unique-local,
        # and the 100.64.0.0/10 carrier-grade NAT range. All are internal.
        return RefusalReason.PRIVATE_RANGE

    if ip.is_reserved:
        return RefusalReason.RESERVED

    return None


# ============================================================================
# URL validation
# ============================================================================


class UrlCheck:
    """The outcome of a check. Either usable, or refused with a reason."""

    __slots__ = ("reason", "host", "port", "scheme")

    def __init__(
        self,
        *,
        reason: RefusalReason | None = None,
        host: str | None = None,
        port: int | None = None,
        scheme: str | None = None,
    ) -> None:
        self.reason = reason
        self.host = host
        self.port = port
        self.scheme = scheme

    @property
    def ok(self) -> bool:
        return self.reason is None

    def __repr__(self) -> str:
        if self.ok:
            return f"UrlCheck(ok, {self.scheme}://{self.host}:{self.port})"
        return f"UrlCheck(refused, {self.reason})"


def normalise_host(host: str) -> str:
    """Lower-case and strip a trailing dot. `example.com.` == `example.com`.

    A trailing dot is the same host in DNS terms and a different string for
    every exact-match check, which makes it a trivially available bypass of a
    naive allowlist. Removing it closes that.
    """
    candidate = host.strip().lower()
    if candidate.endswith("."):
        candidate = candidate.rstrip(".")
    return candidate


def check_url(
    url: str,
    *,
    allowed_domains: list[str] | frozenset[str],
) -> UrlCheck:
    """Static validation of a URL. No network access.

    Checks, in order: parseable, absolute, web scheme, no embedded credentials,
    port, host present, not an internal name, allowlisted. Resolution is
    deliberately NOT done here — see `assert_resolves_public`, which the caller
    runs so it can be done once and its result reused for the connection.
    """
    if not url or not isinstance(url, str):
        return UrlCheck(reason=RefusalReason.MALFORMED)

    # A control character in a URL is how a "safe" string becomes a different
    # request once a HTTP client normalises it. Refused rather than stripped:
    # a URL needing repair is a URL whose repair is a guess.
    if any(char in url for char in ("\n", "\r", "\t", "\x00", " ")):
        return UrlCheck(reason=RefusalReason.MALFORMED)

    try:
        parts = urlsplit(url)
    except ValueError:
        return UrlCheck(reason=RefusalReason.MALFORMED)

    scheme = parts.scheme.lower()
    if not scheme:
        return UrlCheck(reason=RefusalReason.NOT_ABSOLUTE)
    if scheme not in ALLOWED_SCHEMES:
        return UrlCheck(reason=RefusalReason.NON_WEB_SCHEME)

    # `http://user:pass@host` — the credentials would go into an Authorization
    # header against a page the operator did not intend to authenticate to.
    if parts.username is not None or parts.password is not None:
        return UrlCheck(reason=RefusalReason.EMBEDDED_CREDENTIALS)

    try:
        port = parts.port
    except ValueError:
        # Out-of-range or non-numeric. A port the stdlib cannot parse is a port
        # no client should be guessing at.
        return UrlCheck(reason=RefusalReason.MALFORMED, scheme=scheme)

    if port is not None and port not in ALLOWED_PORTS:
        return UrlCheck(reason=RefusalReason.DISALLOWED_PORT, scheme=scheme, port=port)

    try:
        raw_host = parts.hostname
    except ValueError:
        return UrlCheck(reason=RefusalReason.MALFORMED, scheme=scheme)

    if not raw_host:
        return UrlCheck(reason=RefusalReason.NO_HOST, scheme=scheme)

    host = normalise_host(raw_host)

    if host in INTERNAL_HOSTNAMES:
        return UrlCheck(reason=RefusalReason.NOT_ALLOWLISTED, host=host, scheme=scheme)

    # A literal IP is checked directly, before the allowlist. An allowlisted
    # hostname will not resolve to a private address in normal operation, but a
    # literal one has no such protection, and `127.0.0.1` must be refused for
    # being loopback rather than for not being in a list.
    try:
        literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None = ipaddress.ip_address(
            host.strip("[]")
        )
    except ValueError:
        # A name, not a literal. The allowlist and the resolver decide what to
        # do with it; a literal that resolves nowhere is the case this block
        # exists for, since it has no DNS answer to inspect.
        literal = None

    if literal is not None:
        reason = classify_ip(literal)
        if reason is not None:
            return UrlCheck(reason=reason, host=host, port=port, scheme=scheme)

    normalised_allowlist = {normalise_host(domain) for domain in allowed_domains}
    if host not in normalised_allowlist:
        return UrlCheck(reason=RefusalReason.NOT_ALLOWLISTED, host=host, port=port, scheme=scheme)

    return UrlCheck(
        host=host,
        port=port or (443 if scheme == "https" else 80),
        scheme=scheme,
    )


# ============================================================================
# Resolution
# ============================================================================


def resolve_addresses(
    host: str, *, port: int = 443
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve a hostname to every address it maps to.

    Every address, not the first. A name with both an A record pointing
    publicly and an AAAA record pointing at `::1` is a real bypass, and
    resolving once and taking the first answer is a coin flip on which one the
    client picks.
    """
    try:
        # AI_ADDRCONFIG: only addresses this host could actually use. Without
        # it, a name with only AAAA records resolves to a placeholder on a
        # host with no IPv6 route.
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP, flags=socket.AI_ADDRCONFIG)
    except socket.gaierror:
        return []

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        # `sockaddr` is a fixed-length tuple by type but is empty for some
        # address families, so the emptiness check is real rather than
        # defensive. `sockaddr[0]` is the address for INET/INET6 and a *path*
        # for a unix socket, hence the str() — the set holds strings, and
        # `str()` is also what makes the dedup compare like with like instead of
        # relying on `ip_address` accepting both forms.
        raw = str(sockaddr[0]) if sockaddr else ""
        if not raw or raw in seen:
            continue
        seen.add(raw)
        try:
            addresses.append(ipaddress.ip_address(raw))
        except ValueError:
            # A unix-socket path, or anything else that is not an address. Not
            # an error: this function is called with `AI_ADDRCONFIG` for TCP, so
            # reaching here means the resolver returned something unexpected
            # and the safe reading is "no usable address".
            continue
    return addresses


def check_resolved(
    host: str,
    *,
    port: int = 443,
) -> RefusalReason | None:
    """Resolve a host and classify every address. `None` means acceptable.

    A host that does not resolve is NOT accepted. An unreachable name could be
    a typo, or it could be a name that resolves only on the attacker's network.
    Refusing it means the crawl records an honest failure instead of a
    mysterious one.
    """
    addresses = resolve_addresses(host, port=port)
    if not addresses:
        return RefusalReason.UNRESOLVABLE

    # Check ALL of them and report the FIRST refusal. Reporting only the
    # address the client would have happened to use is a weaker check, and the
    # order of preference is not ours to control.
    for address in addresses:
        reason = classify_ip(address)
        if reason is not None:
            return reason

    return None


def assert_fetchable(
    url: str,
    *,
    allowed_domains: list[str] | frozenset[str],
    resolve: bool = True,
) -> UrlCheck:
    """Full pre-flight: static checks, then resolution.

    Raises `SsrfBlocked` — imported lazily to keep `errors` and `security`
    from importing each other at module scope.

    `resolve=False` exists for the tests that assert the static half in
    isolation, and for a caller that has already resolved and wants to reuse
    the result rather than pay for a second lookup.
    """
    from app.core.errors import SsrfBlocked

    check = check_url(url, allowed_domains=allowed_domains)
    # `ok` is the single authority on "passed", and it is defined as
    # `reason is None`. Narrowing on `not check.ok` alone would leave mypy
    # unable to prove the pairing, so the `reason is None` case is handled
    # explicitly rather than asserted away with a cast: a future edit that
    # decoupled the two would then fall into this branch instead of raising on
    # `None.value`.
    if check.reason is not None:
        raise SsrfBlocked(reason=check.reason.value)

    if resolve and check.host is not None:
        reason = check_resolved(check.host, port=check.port or 443)
        if reason is not None:
            raise SsrfBlocked(reason=reason.value)

    return check


def scope_prefix(start_url: str) -> str:
    """The directory containing the start URL — the crawl's scope, as a path.

    The start URL names a *page*, so the scope is its containing directory, not
    its own path. For `/jira-software-cloud/docs/access-a-project/` that is
    `/jira-software-cloud/docs/`, which is the documentation section — the
    intent recorded in data/source_manifest.json. Including the leaf segment
    instead would confine the crawl to that one page plus its descendants, and a
    documentation section is precisely what a breadth-first crawl from a single
    page URL cannot discover on its own.

    Returned separately from `is_within_scope` because it is worth recording:
    the effective scope prefix is written to the crawl job, so "why did the
    crawler visit this page" is answerable from the audit trail rather than by
    re-deriving the rule.
    """
    try:
        parts = urlsplit(start_url)
    except ValueError:
        return "/"

    segments = [segment for segment in parts.path.split("/") if segment]
    if not segments:
        return "/"

    return "/" + "/".join(unquote(segment) for segment in segments[:-1]) + "/"


def is_within_scope(url: str, *, start_url: str) -> bool:
    """Whether a discovered link stays inside its source's crawl scope.

    Scope is the start URL's host AND its containing directory (see
    `scope_prefix`). Depth-2 from
    `/jira-software-cloud/docs/access-a-project/` therefore walks that
    documentation section rather than the whole of support.atlassian.com, which
    is the difference between a coherent 40-page corpus and a crawl that
    follows the site's own navigation into nine unrelated products.

    The prefix is compared on path SEGMENTS, not characters. A character
    comparison would let `/jira-software-cloud/docs-archive/` match the prefix
    `/jira-software-cloud/docs`, because the string "docs" is present at the
    right offset. Segment comparison cannot be fooled that way.
    """
    try:
        candidate = urlsplit(url)
        origin = urlsplit(start_url)
    except ValueError:
        return False

    if normalise_host(candidate.hostname or "") != normalise_host(origin.hostname or ""):
        return False

    origin_segments = [segment for segment in origin.path.split("/") if segment][:-1]
    candidate_segments = [segment for segment in candidate.path.split("/") if segment]

    if len(candidate_segments) < len(origin_segments):
        return False

    # Compare after percent-decoding, so `/docs%2Fadmin` cannot smuggle a
    # segment past the comparison and then be decoded into one.
    #
    # Indexed over `range(len(origin_segments))` rather than `zip`-ed. An earlier
    # version used `zip(origin_segments, candidate_segments, strict=True)` on the
    # reasoning that the guard above established "at least as many segments", so
    # "the lengths already agree". That inference was false: `strict=True`
    # requires *equal* lengths, and a candidate DEEPER than the scope prefix is
    # the normal case, not an edge case — every article under `/docs/` is one
    # segment deeper than `/docs/`. So `is_within_scope` raised `ValueError` on
    # essentially every real URL instead of returning a verdict. It was latent
    # because no test had yet exercised it (T045).
    #
    # Indexing keeps the guard load-bearing in the way the comment above wanted:
    # if the length guard were removed or weakened, `candidate_segments[index]`
    # raises `IndexError` rather than silently truncating the comparison and
    # reporting a match.
    return all(
        unquote(origin_segments[index]) == unquote(candidate_segments[index])
        for index in range(len(origin_segments))
    )


# ============================================================================
# Input bounds
# ============================================================================


def enforce_max_length(value: str, *, limit: int, field: str) -> None:
    """Reject over-long input at the boundary (FR-043).

    Raises `ValidationFailed` with the FIELD NAME, never the value. Echoing
    the input back is how a 200KB "question" becomes a 200KB error body, and
    how unescaped user text reaches a response (contracts/README.md, `details`).
    """
    from app.core.errors import ValidationFailed

    if len(value) > limit:
        raise ValidationFailed(details={"field": field, "limit": limit})


def enforce_max_bytes(payload: bytes, *, limit: int, field: str) -> None:
    """Reject an over-large payload at the boundary (FR-043)."""
    from app.core.errors import ValidationFailed

    if len(payload) > limit:
        raise ValidationFailed(details={"field": field, "limit": limit})


__all__ = [
    "ALLOWED_PORTS",
    "ALLOWED_SCHEMES",
    "CLOUD_METADATA_ADDRESSES",
    "INTERNAL_HOSTNAMES",
    "RefusalReason",
    "UrlCheck",
    "assert_fetchable",
    "check_resolved",
    "check_url",
    "classify_ip",
    "enforce_max_bytes",
    "enforce_max_length",
    "is_within_scope",
    "normalise_host",
    "resolve_addresses",
    "scope_prefix",
]
