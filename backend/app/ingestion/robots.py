"""The robots.txt gate: one authorisation path for every crawl decision.

T049. FR-028, FR-027, R-015.

## The one authorisation path

Every crawl decision about fetching a URL passes through this gate — whether
the URL came from the frontier's link mechanism or its sitemap mechanism. A
sitemap is not an authorisation (`support.atlassian.com/robots.txt` disallows
`/contact/*` and a sitemap may still list it). Two authorisation paths would
mean a URL is judged twice by differently-coded rules and the divergence
would never be reconciled; the audit trail needs a single answer to "was this
URL allowed to be fetched?"

## Measured inventory (2026-10-01)

Fetched and parsed the real files:

| host | disallows | crawl-delay | notes |
|---|---|---|---|
| `support.atlassian.com` | `/contact/*`, `/jira-service-desk-cloud-deprecated/*` | none | single `*` group, declares sitemap |
| `developer.atlassian.com` | one without a leading slash, `platform/forge/ui-kit-components/uik1_all` | none | `*` plus a named `atlassian-bot` group |
| `confluence.atlassian.com` | — | **60** | excluded from the manifest |

No allowlisted host except the excluded one states a `crawl-delay`. The two
gates that *both* need to agree are why the directive handling below is
conservative by default: the declared delay, when present, is honoured via
`RobotsGate.crawl_delay`, and an absent or unparseable robots file never adds
coverage silently.

## Unreachable is deny

A robots file can be absent or unreachable, and the two cases do not mean the
same thing:

- **404/410** — the site publishes no rules; the allow-by-default reading of
  the protocol applies. The URL is permitted.
- **5xx, timeout, or a fetch exception** — the site *has* rules and we could
  not read them. Fetching anyway would argue "I could not tell whether this
  was disallowed, so I did it" — the exact loop that makes a crawler
  unwelcome. Everything from that origin is refused until the file can be
  read again.

## Rule matching

Paths are matched as prefixes after converting `*` (any run) and `$` (end)
markers; ties are broken by path length, with an explicit `Allow` winning over
an equally long `Disallow` (RFC 9309 §2.2.1). A `Disallow` path that does not
begin with `/` is normalised by prepending one — measured instance:
`developer.atlassian.com` prints `platform/forge/...` without a slash;
treating that as a plain prefix would match nothing, and failing to notice
would make a real directive vanish.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

import httpx

from app.core.logging import get_logger, redact

_log = get_logger("ingestion.robots")

#: The product token of settings.crawl_user_agent, matched case-insensitively.
_AGENT_TOKEN: Final[str] = "EnterpriseKnowledgeRAG"


def _rule_matches(path: str, rule: str) -> bool:
    """Prefix match with `*` and `$` markers, the shape of every measured rule.

    `*` stands for any run of characters including zero, and `$` for end of
    path. Since a path either begins the rule or it does not, prefix matching
    followed by the wildcard translation is the whole grammar these directives
    use; a bespoke function rather than `re` because the two wildcards are the
    only operators and an `re.escape`-then-replace ordering is how that code
    turns a literal `*` into a literal asterisk and back again, silently
    matching the wrong set.
    """
    if rule.endswith("$"):
        anchored = True
        rule = rule[:-1]
    else:
        anchored = False
    path_key = path if path.startswith("/") else "/" + path
    if "*" not in rule:
        # No wildcards: the common case, a plain prefix. The module is on the
        # hot path of every candidate URL, and a prefix check is the cheap
        # form of the general rule. `$` turns the prefix test into equality.
        return path_key == rule if anchored else path_key.startswith(rule)
    # General case: split on `*`, escape each literal, and rejoin with `.*`.
    # Anchoring at the start always applies (a robots rule is a prefix of the
    # request path); `$` makes the whole pattern account for the whole path.
    segments = rule.split("*")
    pattern = ".*".join(re.escape(segment) for segment in segments)
    rx = re.compile("^" + pattern + ("$" if anchored else ""))
    return bool(rx.match(path_key))


@dataclass(frozen=True)
class _Group:
    user_agents: tuple[str, ...]
    allow: tuple[str, ...]
    disallow: tuple[str, ...]
    crawl_delay: float | None


@dataclass(frozen=True)
class RobotsPolicy:
    """One host's parsed robots.txt.

    Frozen because the policy is replaced as a unit when a fetch succeeds;
    in-place mutation would let two callers hold the same object while one
    is mid-decision.
    """

    allow_rules: tuple[str, ...] = ()
    disallow_rules: tuple[str, ...] = ()
    crawl_delay_seconds: float | None = None
    raw: str = ""

    def allows(self, path: str) -> bool:
        """Whether `path` on this host may be fetched.

        Longest matching rule wins; a tie is an Allow. An empty path is the
        whole site, and Disallow:"" means no restriction — both produce
        "allow".
        """
        best: tuple[int, bool] | None = None
        for rule in self.allow_rules:
            if rule and _rule_matches(path, rule):
                length = len(rule.replace("*", "").rstrip("$"))
                candidate = (length, True)
                if (
                    best is None
                    or candidate[0] > best[0]
                    or (candidate[0] == best[0] and candidate[1])
                ):
                    best = candidate
        for rule in self.disallow_rules:
            if rule and _rule_matches(path, rule):
                length = len(rule.replace("*", "").rstrip("$"))
                candidate = (length, False)
                if best is None or candidate[0] > best[0]:
                    best = candidate
                elif candidate[0] == best[0] and not best[1]:
                    best = candidate
        if best is None:
            return True
        return best[1]


def _normalise_rule(path: str) -> str:
    """One real deviation from the textbook grammar is normalised here.

    Measured on `developer.atlassian.com` 2026-10-01: the directive is
    printed without its leading slash (`platform/forge/...` rather than
    `/platform/forge/...`). Left alone, a prefix match against a request
    path that always starts with `/` matches nothing and the directive goes
    unhonoured. Prepending the slash makes it match what the author meant.
    The reverse deviation (rule text with two slashes) is a publisher bug
    this parser does not repair.
    """
    return path if path.startswith("/") or path in ("*", "$") else "/" + path


def parse_robots(text: str) -> RobotsPolicy:
    """Directives → one policy, keyed to this crawler's agent token.

    Group selection: a group whose `User-Agent` list names our agent token
    wins; failing that, a `*` group governs; failing that, the file says
    nothing about us and every rule list is empty (treated as fully
    permitted). A tok|<...> block after the first non-UA line is not a new
    group — the RFC attaches subsequent lines to the most recent UA line(s).
    """
    groups: list[_Group] = []
    current_uas: list[str] = []
    current_allow: list[str] = []
    current_disallow: list[str] = []
    current_delay: float | None = None
    seen_directive = False

    def _flush() -> None:
        nonlocal current_uas, current_allow, current_disallow, current_delay, seen_directive
        if current_uas or current_allow or current_disallow or current_delay is not None:
            groups.append(
                _Group(
                    user_agents=tuple(current_uas),
                    allow=tuple(current_allow),
                    disallow=tuple(current_disallow),
                    crawl_delay=current_delay,
                )
            )
        current_uas, current_allow, current_disallow = [], [], []
        current_delay = None
        seen_directive = False

    for line in text.splitlines():
        # Strip inline comments and CA/CR artifacts; directive grammar is
        # `Field: value` with the name matched case-insensitively.
        if "#" in line:
            line = line[: line.index("#")]
        line = line.strip()
        if not line or ":" not in line:
            continue
        name, _, value = line.partition(":")
        name = name.strip().lower()
        value = value.strip()

        if name == "user-agent":
            if seen_directive:
                _flush()
            current_uas.append(value)
        elif name in ("allow", "disallow"):
            seen_directive = True
            if not value:
                # Empty Disallow means no restriction for this group; record it
                # so the group is not dropped if it is the only directive.
                (current_allow if name == "allow" else current_disallow).append("")
            elif name == "allow":
                current_allow.append(_normalise_rule(value))
            else:
                current_disallow.append(_normalise_rule(value))
        elif name == "crawl-delay":
            seen_directive = True
            try:
                current_delay = float(value)
            except ValueError:
                # An unparseable delay must not crash the crawl: the
                # configured politeness delay still applies, and this file
                # simply adds nothing.
                continue
        elif name == "sitemap":
            # The manifest registers sitemaps explicitly; we parse the file
            # for allow/disallow, not for discovery.
            continue
        else:
            continue
        seen_directive = seen_directive or name in ("allow", "disallow", "crawl-delay")

    _flush()

    specific = [
        g
        for g in groups
        if any(_AGENT_TOKEN.lower() in ua.lower() or "all" == ua.lower() for ua in g.user_agents)
    ]
    wild = [g for g in groups if any(ua == "*" for ua in g.user_agents)]
    governing = specific if specific else (wild if wild else [])
    if not governing:
        return RobotsPolicy(raw=text)

    allow_rules: list[str] = []
    disallow_rules: list[str] = []
    delays: list[float] = []
    for group in governing:
        allow_rules.extend(group.allow)
        disallow_rules.extend(group.disallow)
        if group.crawl_delay is not None:
            delays.append(group.crawl_delay)
    return RobotsPolicy(
        allow_rules=tuple(sorted(allow_rules)),
        disallow_rules=tuple(sorted(disallow_rules)),
        crawl_delay_seconds=max(delays) if delays else None,
        raw=text,
    )


#: Outcome of trying to read a host's robots file. The two are different
#: conversations: one says "no rules here", the other says "I know rules
#: existed and refused to tell you them".
_ALLOW_ALL = RobotsPolicy(raw="404: no robots file")
_DENY_ALL = RobotsPolicy(disallow_rules=("/",), raw="unreachable: treating as disallow")


class RobotsGate:
    """Per-origin cache of parsed robots files.

    `prime` reads the candidate hosts once (the frontier is sorted, so this
    is deterministic too); `allows_sync` then answers from the cache, which
    is why `build_frontier`'s synchronous `robots_allows` callable can be
    wired to the real gate without a second async boundary. A URL on an
    origin the gate was not primed with is **denied**: the frontier only
    yields URLs on manifest hosts, and a missing prime is a wiring bug that
    must produce a loud refusal, not an open door.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._cache: dict[str, RobotsPolicy] = {}

    async def prime(self, urls: list[str]) -> None:
        origins = sorted({_origin(url) for url in urls})
        for origin in origins:
            policy = await self._read(origin)
            self._cache[origin] = policy

    async def _read(self, origin: str) -> RobotsPolicy:
        try:
            response = await self._client.get(f"{origin}/robots.txt")
        except Exception as exc:  # noqa: BLE001
            # A network failure reading robots/text is a "can't read the
            # rules" event; conservatively deny everything from this origin.
            _log.warning(
                "robots.txt unreachable", extra={"origin": origin, "error_type": type(exc).__name__}
            )
            return _DENY_ALL
        if response.status_code == 404 or response.status_code == 410:
            return _ALLOW_ALL
        if not response.is_success:
            _log.warning(
                "robots.txt returned non-success",
                extra={"origin": origin, "status": response.status_code},
            )
            return _DENY_ALL
        try:
            return parse_robots(response.text)
        except Exception as exc:  # noqa: BLE001
            # A robots file whose structure the parser cannot survive is an
            # unreadable file: deny rather than let a broken file widen the
            # crawl.
            _log.warning(
                "robots.txt failed to parse",
                extra={"origin": origin, "error_type": type(exc).__name__},
            )
            return _DENY_ALL

    def allows_sync(self, url: str) -> bool:
        origin = _origin(url)
        policy = self._cache.get(origin)
        if policy is None:
            _log.warning(
                "robots gate was not primed for this origin",
                extra={"origin": origin, "url": redact(url)},
            )
            return False
        path = urlsplit(url).path or "/"
        return policy.allows(path)

    def crawl_delay(self, url: str) -> float | None:
        policy = self._cache.get(_origin(url))
        return policy.crawl_delay_seconds if policy is not None else None


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


__all__ = [
    "RobotsGate",
    "RobotsPolicy",
    "parse_robots",
]
