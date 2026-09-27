<!--
SYNC IMPACT REPORT
==================
Version change: (unversioned template) -> 1.0.0
Bump type: MAJOR (initial ratification; no prior governed version existed)

Modified principles: none (all principles are newly instantiated)
  - Principle I   : template slot -> I. Grounded Answers, Deterministic Citations
  - Principle II  : template slot -> II. Refuse Rather Than Guess
  - Principle III : template slot -> III. Structure-Aware Ingestion and Traceability
  - Principle IV  : template slot -> IV. Corpus Independence and Legal Compliance
  - Principle V   : template slot -> V. Explainability Without Chain-of-Thought
  - Principle VI  : template slot -> VI. Honest Evaluation, No Fabricated Metrics
  - Principle VII : template slot -> VII. Provider-Neutral Interfaces, Env-Driven Config
  - Principle VIII: template slot -> VIII. Small Services and Clear Boundaries
  - Principle IX  : template slot -> IX. Security by Default
  - Principle X   : template slot -> X. Observable, Secret-Safe Operation
  - Principle XI  : template slot -> XI. Deliberate Scope and Incremental Delivery

Added sections:
  - Additional Constraints and Non-Functional Standards
  - Development Workflow, Testing, and Quality Gates
  - Project identity and independence statement (inside Governance)

Removed sections: none

Deferred items / TODO placeholders: none. All template placeholders were
resolved from the project specification. Application code, manifests, gold
dataset, documentation, and deployment files are NOT created by this command
and are tracked as deferred implementation intents.
-->

# Enterprise Knowledge Intelligence RAG Constitution

Project codename: `knowledge-intelligence`

This constitution governs the engineering of an evidence-grounded enterprise
knowledge retrieval system. It exists to prove that Retrieval-Augmented
Generation is a discipline of ingestion, retrieval quality, and verification
rather than a wiring exercise between an LLM and a vector database.

## Core Principles

### I. Grounded Answers, Deterministic Citations (NON-NEGOTIABLE)

Generated answers MUST be derived exclusively from evidence retrieved and
selected by the pipeline. Every substantive factual claim MUST carry at least
one citation resolvable to a retrieved chunk. Citation identifiers MUST be drawn
from retrieved context only; the LLM is never trusted to author, guess, or
mint a citation id or URL.

Citation validation MUST be a deterministic, testable component independent of
the model. It MUST verify that each cited chunk was actually retrieved, that the
source URL matches the database registry, that the chunk exists, and that the
answer text contains no unknown citation id. Invalid citations MUST be rejected
or repaired, never passed through.

Rationale: Traceability is the product. An unverifiable citation is worse than
no citation, because it converts a visible gap into a hidden defect.

### II. Refuse Rather Than Guess

When retrieval or rerank quality falls below configured thresholds, the system
MUST NOT answer confidently. It MUST return an explicit insufficient-evidence
response, stating what was searched and linking any partially relevant sources.

Classification and query analysis MUST return `null` rather than a forced guess
when confidence is insufficient. Metadata filters MUST be applied only when
analysis confidence clears a defined threshold. The system MUST preserve
conditional phrasing from source documentation and MUST explicitly surface
disagreement between documents rather than silently reconciling it.

Verification passes are bounded: a maximum of two generation attempts, with no
unbounded regeneration loops.

Rationale: A correct refusal is a successful outcome. Forced answers are the
primary failure mode in retrieval-augmented systems.

### III. Structure-Aware Ingestion and Traceability

Ingestion MUST progress through discrete, independently testable stages:
fetch, HTML validation, content extraction, cleaning, metadata extraction,
section detection, chunking, chunk validation, indexing, and registry
persistence.

Chunking MUST be structure-aware, deriving boundaries from headings,
subheadings, paragraphs, lists, tables, and code blocks. Blind fixed-character
splitting is prohibited. Code blocks MUST NOT be split mid-example where
avoidable. Every chunk MUST retain its heading path, page title, source URL,
product, and section.

Chunk size and overlap targets are approximate, expressed in tokens, not
characters. Every ingested page MUST record source URL, canonical URL, title,
product, category, page type, language, content hash, crawl timestamp, and
version information when detectable. PostgreSQL stores registry state, chunk
records, crawl jobs, query logs, citations, and evaluation questions; Pinecone
remains the sole vector retrieval system.

Content hashing MUST drive versioning: unchanged content is never re-indexed,
changed content supersedes the prior version, and superseded vectors are
reconciled and removed. Documents MUST NOT be duplicated on every crawl.

Rationale: Retrieval quality is bounded by ingestion quality. Provenance
metadata is what makes evidence auditable months after ingestion.

### IV. Corpus Independence and Legal Compliance

The system ingests publicly accessible documentation by URL and MUST NOT ship,
commit, redistribute, or package scraped third-party page content as a
dataset. Only source URLs, manifests, and generated application metadata belong
in version control.

Crawling MUST be respectful: explicit starting URLs, domain allowlist, same-domain
restriction, `robots.txt` compliance, configured delay between requests, request
timeouts, retry with exponential backoff, descriptive user agent, page and depth
caps. The crawler MUST NOT attempt to bypass access controls, defeat
`robots.txt`, or reach private or authenticated content.

The system MUST NOT claim affiliation with, sponsorship by, or endorsement by
Atlassian or any source publisher, MUST NOT present third-party logos as its own,
and MUST NOT imply it is an official product. Every surfaced source MUST link
back to the original URL and carry attribution. Users MUST be able to delete
indexed content, which removes both registry rows and vectors. The
independence disclaimer MUST be present in the README and the user interface.

Rationale: Legality and trust are architectural concerns. Retrofitting
attribution and deletion after ingestion does not scale.

### V. Explainability Without Chain-of-Thought

The system MUST expose deterministic pipeline metadata: detected product and
category, rewritten retrieval queries, applied filters, retrieved candidate
count, reranked results, selected evidence, scores, and final citations.

The system MUST NOT expose hidden reasoning, internal chain-of-thought, or
unverifiable model deliberation. Developer and debug modes surface pipeline
state only. This constraint binds API responses, SSE event payloads, and the
user interface alike.

Rationale: Explainability of a retrieval system comes from its pipeline trace,
not from model introspection, which is neither stable nor trustworthy.

### VI. Honest Evaluation, No Fabricated Metrics

Reported quality metrics MUST be computed from an actual evaluation run.
Fabricating, hard-coding, seeding, or estimating displayed retrieval or
generation metrics is prohibited. The evaluation dashboard MUST reflect the
most recent real run, including its timestamp and dataset identity, and MUST
present no numbers when no run exists.

Evaluation MUST cover retrieval metrics (recall, precision, mean reciprocal
rank, normalized discounted cumulative gain where appropriate), generation
metrics (citation correctness, citation completeness, answer relevance,
faithfulness, groundedness), and system metrics (total, retrieval, and
generation latency, token usage, error rate).

The gold dataset MUST support multiple valid answers. Reference answers MUST be
expressed as notes and acceptable source URLs rather than a single exact string,
so that valid variation is not scored as failure.

Rationale: The portfolio value of an evaluation system is entirely contingent on
its numbers being real.

### VII. Provider-Neutral Interfaces, Environment-Driven Configuration

LLM access MUST be expressed through a provider interface with at least one
OpenAI-compatible implementation, and the architecture MUST accommodate other
providers without rewriting the retrieval pipeline. Reranking MUST likewise sit
behind a reranker interface so the implementation is swappable. Provider
selection, model choice, embedding model, index name, namespace, and thresholds
MUST be environment-driven.

Vector dimensionality MUST NOT be hard-coded when integrated embedding
configuration supplies it. Credentials MUST come from the environment and MUST
NOT be hard-coded in source. Feature code MUST stay inside the pipeline when
an official vendor API changes; the implementation adapts to current vendor
documentation rather than preserving obsolete API patterns.

Rationale: Portability and replaceability are what keep an integration honest as
vendor APIs evolve.

### VIII. Small Services and Clear Boundaries

Modules MUST remain small, typed, independently testable, and separated by clear
concerns. Giant service classes, everything-in-one modules, and untyped
dict-passing between layers are prohibited. Dependencies MUST be injected at
boundaries so each stage can be exercised in isolation.

Framework-by-default composition is prohibited where it would obscure the
actual retrieval mechanics. Vendor operations MUST NOT be hidden behind
excessive abstraction layers. Unnecessary microservices are prohibited; the
system remains a modular monolith. Async I/O, structured logging, and typed
interfaces are defaults for network and pipeline work.

Rationale: The point of the portfolio is to make the RAG mechanics legible.
Indirection that hides Pinecone, chunking, or reranking defeats the exercise.

### IX. Security by Default

All secrets MUST be environment-based. API keys MUST NOT reach the frontend or
appear in logs, error payloads, or version control. CORS origins MUST be
explicitly configured rather than wildcarded.

User input and ingestion URLs MUST be validated, with maximum query length and
maximum ingestion size enforced. The crawler MUST defend against server-side
request forgery: it MUST refuse loopback, link-local metadata endpoints such as
`169.254.169.254`, private network ranges, non-HTTP schemes, and arbitrary
internal addresses, and MUST operate against a configured domain allowlist.
Requests MUST have bounded timeouts. Rate limiting MUST be applied. Extracted
HTML MUST be sanitized before rendering.

Stack traces MUST NOT be exposed in production responses. Errors MUST use the
consistent structured envelope carrying an error code, a safe message, and a
request id.

Rationale: An ingestion feature that fetches user-supplied URLs is a network
attack surface unless SSRF defenses are built in from the first commit.

### X. Observable, Secret-Safe Operation

Every query and ingestion run MUST be traceable via a request id, and MUST log
retrieval count, selected chunk count, latency breakdown, provider, model, token
usage when available, and errors. Logging MUST be structured rather than
free-form prose.

Logs MUST NOT contain secrets. Sensitive user queries MUST NOT be persisted
indefinitely; retention MUST be bounded and documented. Collection and exposure
of telemetry MUST be limited to what the debugging and evaluation workflows
genuinely require.

Rationale: Without trace metadata, a retrieval regression is undiagnosable, and
over-collection converts an observability feature into a liability.

### XI. Deliberate Scope and Incremental Delivery

The system MUST NOT pursue whole-corpus ingestion. The demonstration target is
on the order of 50 to 100 source pages, 500 to 1500 chunks, and at least 30
evaluation questions. Quality MUST be preferred over volume, and corpus
expansion is a deliberate decision rather than a default.

Delivery MUST follow the established phase order: scaffolding, database, vector
store, crawler, extraction, chunking, indexing, retrieval, query analysis,
reranking, evidence selection, generation, citation validation, streaming,
frontend, evaluation, observability, security hardening, containerization, and
documentation. Phase order MAY NOT be reordered to front-load visible UI over
retrieval correctness. Cloud-hosted vector infrastructure MUST NOT be replaced
with a local fake for convenience.

Rationale: A bounded corpus is what makes evaluation meaningful. Unbounded
ingestion produces unmeasurable quality and unbounded legal exposure.

## Additional Constraints and Non-Functional Standards

**Mandated stack.** Python 3.12 or newer, FastAPI, Pydantic v2, SQLAlchemy 2.x,
Alembic, PostgreSQL, httpx, selectolax or BeautifulSoup, trafilatura or an
equivalent main-content extractor, the Pinecone Python SDK, pytest, and
structured logging. Frontend: Next.js, TypeScript, React, Tailwind CSS, a
component system such as shadcn/ui, and TanStack Query. Infrastructure: Docker
with Docker Compose and a PostgreSQL service; the vector store stays
cloud-hosted.

**Vector store configuration.** Index `enterprise-knowledge`, serverless, cosine
similarity, integrated embedding where supported. Namespace strategy begins at
`atlassian-public` and remains forward-compatible with per-tenant namespaces. One
index per document is prohibited. Record ids follow the structured form
document id, hash, chunk index so traceability is mechanical.

**Metadata discipline.** Every record carries document id, chunk id, source url,
title, product, category, document type, content hash, crawl timestamp,
language, section, heading path, chunk index, and total chunks. Metadata MUST
remain compact; oversized fields are truncated or relocated, never embedded
wholesale.

**Retrieval budget.** Initial dense retrieval takes 12 candidates. Reranking
narrows to 5. Final generation context is 3 to 6 chunks selected on evidence
quality. Dumping the full candidate pool into the prompt is prohibited.
Evidence selection MUST weigh semantic relevance, rerank score, source
diversity, duplication, heading relevance, and metadata match, and MUST avoid
redundantly returning several chunks from the same page when fewer suffice.

**Interface surface.** The documented endpoint set is treated as a contract:
chat, streaming chat, source registry, document inventory, and evaluation
run and retrieval. Streaming uses server-sent events with a fixed, ordered event
vocabulary. User-facing surfaces include chat, sources, documents, evaluations,
and settings, each with an evidence panel and a developer mode.

**Error contract.** Failures return the structured error envelope with a stable
error code, a human-readable message, and the request id, never a stack trace.

**Readability bar.** The repository MUST be immediately understandable to a
reviewer as the work of someone who can build and evaluate a real retrieval
system, not merely connect an LLM to a vector database.

## Development Workflow, Testing, and Quality Gates

**Staged delivery.** Work proceeds in the phase order fixed by Principle XI. Each
phase lands with its own tests before the next begins.

**Required test tiers.**
Unit coverage MUST include the chunker, URL validator, metadata extraction,
citation validator, query analyzer, retrieval filter construction, content
hashing, crawler security controls, and the answer verifier. Integration
coverage MUST include PostgreSQL, Pinecone, the ingestion pipeline, and the
retrieval pipeline. End-to-end coverage MUST exercise question to retrieval to
generation to citation validation to response.

**Definition of done.** No phase is complete until the system starts under
Docker Compose, migrations apply, the index initializes, sources register, the
crawler runs, documents extract, chunks generate and index, queries retrieve
relevant chunks, metadata filtering and reranking work, answers are grounded,
citations validate, unsupported questions are refused, streaming works, the
frontend functions, and the evaluation suite runs with computed metrics.

**Compliance review.** Every change is reviewed against the Core Principles
before merge. Any deviation MUST be justified in the change description or
amended into this constitution first. Added complexity MUST be justified against
Principle VIII. Any new dependency MUST be justified against Principle VII.

**Quality is not optional.** "It works" is insufficient. Architectural clarity,
retrieval quality, explainability, evaluation, interface polish, security,
maintainability, realism of the enterprise use case, and demonstrability are
reviewed as first-class criteria.

## Governance

**Independence statement.** Enterprise Knowledge Intelligence RAG is an
independent technical demonstration. It is not affiliated with, sponsored by, or
endorsed by Atlassian or any documentation publisher. It is presented as an
independent RAG demonstration powered by publicly available documentation, and
MUST NOT be named or worded as an official product of any third party.

**Precedence.** This constitution supersedes all other project practices,
conventions, and informal preferences. Where a plan, spec, task list, or code
comment conflicts with it, this document governs and the conflict is a defect to
be corrected, not an exception to be granted.

**Amendment procedure.** Amendments MUST be proposed as an explicit change to
this file with a stated rationale. Each amendment MUST record the version change,
the principles added, modified, or removed, the sections added or removed, and
any deferred items, as a sync impact record. Amendments take effect on
ratification and apply to all subsequent work.

**Versioning policy.** Versions follow semantic versioning applied to
governance itself.
MAJOR increments on backward-incompatible governance changes, including removal
of a principle or redefinition of its meaning.
MINOR increments when a principle or section is added, or existing guidance is
materially expanded.
PATCH increments for clarifications, wording fixes, and non-semantic
refinements.
Where the bump type is ambiguous, the rationale MUST be stated before
ratifying.

**Compliance review expectations.** All pull requests MUST be reviewed for
constitution compliance. Unjustified complexity is a review failure. RAG quality
regressions, fabricated metrics, committed corpus content, leaked secrets, and
unvalidated citations are blocking defects regardless of feature completeness.

**Version**: 1.0.0 | **Ratified**: 2026-09-27 | **Last Amended**: 2026-09-27
