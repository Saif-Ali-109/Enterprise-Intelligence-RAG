# Data source policy

What this system collects, what it never collects, what it never redistributes, how it crawls,
and what deletion means. Written to be read by someone deciding whether to run it, not only by
someone operating it.

Independent technical demonstration. Not affiliated with, sponsored by, or endorsed by Atlassian
or any documentation publisher. This repository contains no third-party page content.

---

## 1. The corpus is exactly what you register

Answers come from one place: pages retrieved from sources you registered by URL. Nothing else is
searched, nothing else can support a claim, and no general web search exists anywhere in this
system.

A source is one documentation section with a start URL and a domain allowlist. The crawler reads
that page's sitemap and its links, obeys the site's `robots.txt`, and indexes what it finds within
the configured page and depth caps.

The default allowlist is Atlassian's three documentation domains, because that is what this
demonstration indexes. It is configuration, not a hard-coded assumption, and it is enforced
per-request rather than per-source (see §4).

## 2. What is collected

| Stored | Where | Why |
|---|---|---|
| Source registrations: name, start URL, allowlist, crawl caps | Postgres `sources` | You configured it; the console shows it |
| Page provenance: URL, canonical URL, title, product, category, page type, language | Postgres `documents` | A citation must name its page |
| Content fingerprint per page, per unit | Postgres `documents`, `document_units` | Change detection without keeping the text |
| Structure per unit: ordinal, heading path, block types, token count, overlap | Postgres `document_units` | Reconstructing how a page was chunked |
| Lifecycle state per page: indexed, stale, superseded, deleted, failed | Postgres `documents` | What is searchable, and why something is not |
| Questions asked, the outcome, and the counts behind it | Postgres `query_logs` | A complaint has to be investigable |
| The evidence behind an answer, as a bounded quote | Postgres `citations` | A reader can check a claim without re-fetching |
| Embedded unit text | Pinecone vector index | Retrieval reads it back one unit at a time |
| Crawl job counters and errors | Postgres `crawl_jobs` | Why a crawl indexed fewer pages than it found |

## 3. What is never collected

**Page text is never written to this application's database.** The registry holds provenance and
structure; the vector store holds text. Retrieval reads a unit's text at query time and returns
evidence to the model, and that text goes to the provider and back, but it is not persisted here.
This is enforced by design, not by convention: the only third-party text in the database is the
bounded `citations.quote` span supporting a specific claim.

**No page content in version control.** `scripts/check_repo_hygiene.sh` fails the build on
third-party page text and on credential-shaped strings in tracked files. Run by `make check`.

**No credentials anywhere but the environment.** Secrets are read from the environment at
runtime, are never baked into an image layer or a compose file, never returned by any endpoint
(`SecretPresence` reports `configured: true|false` and has no field a value could occupy), and never
logged. `tests/contract/test_config_no_secrets.py` sweeps every endpoint with sentinel values and
asserts the structural absence as well.

**No model reasoning, ever.** If the provider returns a reasoning channel it is discarded at the
adapter boundary before anything else sees it. `tests/contract/test_no_reasoning.py` walks every
event payload, every response field, and every log record for a key matching
`reasoning|thought|chain_of_thought|deliberation`, including under `inspect: true` and at DEBUG
logging.

**No user identity.** There is no session, no account, no cookie. The application has no notion of
who is asking.

**No telemetry beyond what debugging and evaluation require.** There is no analytics SDK, no
third-party script, and no outbound call except to the two configured vendors and the pages you
registered. Client-side code talks only to this application's own routes.

## 4. Crawling conduct

The crawler is a guest on someone else's infrastructure. The defaults are chosen accordingly.

**A domain allowlist is enforced per request.** A registered start URL does not grant the right to
fetch anything on that host — the crawler re-checks each resolved URL against the configured
domains, and refuses anything outside them.

**Server-side request forgery is refused structurally.** Loopback addresses, private and
link-local ranges, the cloud metadata endpoint (`169.254.169.254`), and non-HTTP schemes are
rejected before a connection is opened. Redirects are followed only within the allowlist.

**`robots.txt` is obeyed.** A disallowed path is skipped, recorded as `robots_disallowed` in the
job's `skipped_reasons`, and counted separately from pages that were read — so "we did not read it"
is never confused with "we read it and it had not changed".

**Requests are paced and bounded.** A per-source `delay_seconds` sits between page fetches (default
1.0 s), every request has a timeout, responses over `CRAWL_MAX_BYTES` are refused, and the crawler
identifies itself with a configurable `User-Agent` rather than pretending to be a browser.

**Superseded pages are superseded, not silently replaced.** A page whose fingerprint is unchanged
is not re-embedded. A page that shrank has its surplus units tombstoned, so a citation can never
resolve to text that no longer exists.

## 5. Deletion

Deleting a document removes it from every layer, and the removal is verifiable:

1. the registry row moves to `deleted` with `vectors_live: false` and a `state_detail`;
2. its vectors are removed from the Pinecone namespace;
3. its units stop being retrievable, so no future answer can cite it;
4. the document remains browsable in `/documents` with its state visible — a tombstone is
   evidence, not clutter.

The console keeps the source registration visible even when every document under it is deleted. A
crawler still pointed at a source you cannot see is worse than an empty table.

`query_logs` and `citations` are **audit records and are not deleted by a document delete**. They
hold ids, counts, scores, and bounded quotes, not page text. Retention is bounded by
`QUERY_LOG_RETENTION_DAYS` (see §6), and the audit row for a question is what makes a claim about
that answer checkable at all.

## 6. Retention

| Data | Retention |
|---|---|
| Sources, documents, units | Until you delete them. The application never expires a page on its own. |
| Crawl jobs | Until you delete them. |
| `query_logs`, `citations` | Bounded by `QUERY_LOG_RETENTION_DAYS`. |
| `evaluation_runs`, `evaluation_results` | Kept. A run is the provenance of a metric; deleting it would make a displayed number untraceable. |
| Vectors in Pinecone | Removed with their document. |

`pipeline_trace` is written **only when `inspect: true`**, because a trace is per-request
operator-facing detail and a permanent copy of one for every question is a retention decision
nobody made. Questions themselves are retained on a bounded schedule rather than indefinitely
(Constitution X).

## 7. What this demonstration is not

- **Not affiliated with Atlassian.** No sponsorship, endorsement, or review. Every URL is fetched as
  a member of the public web would fetch it.
- **Not a mirror.** Pages are not copied into this repository. The only copy of a page's text is
  the embedding in the configured Pinecone index, which is a retrieval artefact rather than a
  collection.
- **Not redistributing anyone's content.** No page text, no bulk export, no archive. A citation is a
  link plus a bounded supporting span, which is the form the law and the publisher's terms both
  expect.
- **Not scraping what it was not given.** Only registered domains, only pages reachable from them,
  only within the caps you set, and only where `robots.txt` permits.

## 8. Running it against someone else's documentation

Register a source you have the right to index, keep the caps conservative, and read
[`../specs/001-enterprise-knowledge-rag/quickstart.md`](../specs/001-enterprise-knowledge-rag/quickstart.md)
scenario V2 before you register anything. If a publisher asks for a source to be removed, deleting
the source removes its pages from the registry and the vector index; the remaining audit rows name
the source by id and hold no page text.

## Related

- [Architecture](ARCHITECTURE.md) — how the pipeline is built.
- [Evaluation guide](EVALUATION.md) — how quality is measured and how a failing gate is reported.
- Constitution IV (Corpus Independence and Legal Compliance) and IX (Security by Default) in
  [`.specify/memory/constitution.md`](../.specify/memory/constitution.md).