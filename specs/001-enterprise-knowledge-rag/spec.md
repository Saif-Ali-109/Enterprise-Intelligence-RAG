# Feature Specification: Enterprise Knowledge Intelligence RAG

**Feature Branch**: `001-enterprise-knowledge-rag`

**Created**: 2026-09-27

**Status**: Draft

**Input**: User description: "Enterprise Knowledge Intelligence RAG — build a production-quality, evidence-grounded enterprise knowledge retrieval system over publicly available Atlassian documentation (Jira, Confluence, Jira Service Management, Developer Platform). Ingest by URL rather than shipping a corpus; answer only from retrieved evidence; cite every substantive claim; refuse when evidence is thin; expose the retrieval pipeline; evaluate honestly. Publish to https://github.com/Saif-Ali-109/Enterprise-Intelligence-RAG. Use Groq as the default language model provider."

## Clarifications

### Session 2026-09-27

- Q: Which lifecycle states must a document pass through between being discovered and being removed, and what should happen to its previously indexed content if a re-crawl fails partway through? → A: Six states — discovered, crawling, processed, indexed, failed, deleted. `processed` means extracted and chunked; `indexed` means vectors are written. On mid-crawl failure, previously indexed content stays live and queryable, and only the pages that actually changed are superseded.
- Q: What should happen when two operations collide — for example a crawl is running on a source and the operator deletes a document from it, or two crawls of the same source are triggered at once? → A: One active crawl per source; a second crawl request is rejected with a clear message. Deletion always wins — a deleted document is tombstoned so an in-flight crawl cannot resurrect it.
- Q: What should a user see when the embedding or vector search service is unreachable while they are asking a question? → A: Answer with a clear service-unavailable message and a request id, and record the failure. Never fall back to answering without evidence, and never present a partial or ungrounded answer as if it succeeded.
- Q: What should count as a correct hit for the retrieval quality gates, given that pinning expected source URLs makes the dataset rot as Atlassian reorganises its docs? → A: Topic-based grading — record expected product, category and heading path rather than exact URLs, and mark a hit correct when the retrieved content covers the expected topic.
- Q: What accessibility standard must the dashboard meet, given this is a portfolio piece a reviewer will judge on presentation quality? → A: WCAG AA core — keyboard navigable throughout, visible focus, labelled controls, AA colour contrast, semantic headings, and screen-reader announcements for streamed progress.

### Session 2026-09-28

- Q: The numeric quality gates were set below the figures quoted in the original brief (Recall@5 0.80 vs 0.87, MRR 0.75 vs 0.81, faithfulness 0.90 vs 0.94), but those figures were never confirmed as measurements from a real run. Should the gates be raised to match them, kept lower, or made adjustable? → A: Make the gates adjustable by configuration, defaulting to the lower values. Raising gates to unverified figures risks a permanently failing dashboard; fixing them at lower values would prevent recalibration once real measurements exist. Thresholds must be configuration and MUST be recorded on every run so a result stays interpretable after a change. The two absolute-zero gates remain absolute and are not adjustable.
- Q: Which hosted embedding model should be committed to, given the size of a retrieval unit and the model's maximum input length? → A: `llama-text-embed-v2`. `multilingual-e5-large` caps at 512 tokens, which would silently truncate larger retrieval units and leave that unit cited as supporting claims drawn from text it never encoded. This is a correctness constraint, not a preference ranking. The choice remains configuration; only the default is fixed.
- Q: Which language model should serve grounded generation, and which should serve query classification? → A: A large model for generation and a small, fast model for classification. Classification is a cheap judgement that runs on every question and directly affects the response-time budget, so it must not consume the reasoning budget the generation path needs.
- Q: Should the persistence model follow the six tables sketched in the original brief, or the nine the design phase requires? → A: Nine. The additional tables are required by the absolute-zero gates and by the crawl-failure and deletion requirements; folding them away would remove the audit trail those requirements depend on.

## User Scenarios & Testing *(mandatory)*

<!--
  IMPORTANT: User stories are PRIORITIZED as user journeys ordered by importance.
  Each user story is INDEPENDENTLY TESTABLE — implementing just ONE still
  delivers a viable MVP that demonstrates value.
-->

### User Story 1 - Get a Cited, Grounded Answer (Priority: P1)

As a knowledge seeker, I ask a question about a product's documentation and get a
concise, actionable answer in which every substantive claim is backed by a
citation that resolves to the original source page.

**Why this priority**: This is the entire product promise. Nothing else
matters if answers are ungrounded or unsourced.

**Independent Test**: Index a documentation page, ask one question about it, and
confirm the answer's claims each carry a citation whose link opens the original
page and whose quoted text actually appears in that page.

**Acceptance Scenarios**:

1. **Given** documentation has been indexed, **When** I ask a factual question about it, **Then** I receive a concise answer where each substantive claim carries a citation resolving to the original source page.
2. **Given** an answer cites several sources, **When** I inspect the citations, **Then** each shows its source title, product, section, relevance, and a working link to the original page.
3. **Given** two sources disagree, **When** I receive the answer, **Then** the disagreement is stated explicitly and both sources are cited rather than silently reconciled.

---

### User Story 2 - Be Told "Not Enough Evidence" Instead of a Guess (Priority: P1)

As a knowledge seeker, I ask something the documentation does not cover and get an
honest refusal that shows what was searched, rather than a confident fabrication.

**Why this priority**: The single most important trust behavior in a retrieval
system. A confident wrong answer is worse than no answer.

**Independent Test**: Ask a question entirely outside the indexed corpus and
confirm the system refuses, states what it searched, and does not present any
invented fact or citation.

**Acceptance Scenarios**:

1. **Given** a question unrelated to the indexed corpus, **When** I submit it, **Then** the system states it could not find enough evidence to answer reliably and names what it searched.
2. **Given** a refusal, **When** no relevant sources exist at all, **Then** the system shows no fabricated sources or links.
3. **Given** a refusal where weakly related sources do exist, **When** the response is shown, **Then** those sources are offered as leads, clearly marked as insufficient.
4. **Given** a borderline question, **When** the system is unsure which product domain applies, **Then** it reports low confidence and does not silently narrow the search to one domain.

---

### User Story 3 - Ingest Documentation From Public URLs (Priority: P1)

As a documentation operator, I register public documentation URLs, and the system
respectfully crawls, extracts, structurally chunks, and indexes them with full
provenance — without me ever handling raw page content or writing code.

**Why this priority**: Without ingestion there is nothing to retrieve, and the
legal/attribution obligations begin here.

**Independent Test**: Register one public documentation URL, let it crawl, and
confirm indexed content appears with correct title, product, category, source
link, and heading structure.

**Acceptance Scenarios**:

1. **Given** a public documentation URL within an allowed domain, **When** I register and crawl it, **Then** pages are extracted, split along their real structure, and indexed with title, product, category, and heading path.
2. **Given** a page whose content has not changed since the last crawl, **When** I refresh it, **Then** the system skips re-indexing and reports it as unchanged.
3. **Given** a page whose content has changed, **When** I refresh it, **Then** the previous version is superseded and its stale entries are removed rather than accumulating.
4. **Given** I delete an indexed document, **When** deletion finishes, **Then** its content is fully removed from both the index and the registry, and it can no longer be retrieved.
5. **Given** a URL outside the allowed domains, **When** I attempt to register it, **Then** the system refuses it and explains why.

---

### User Story 4 - Synthesize Across Product Domains (Priority: P2)

As a knowledge seeker, I ask a question that spans products — such as why a user
can see a linked page in one product but not another — and get an answer that
draws on evidence from each relevant domain.

**Why this priority**: This is the differentiator over a single-corpus chatbot
and the strongest demonstration that metadata-aware retrieval works.

**Independent Test**: Ask a cross-product question and confirm the answer draws on
evidence from at least two documentation domains and the interface reports how
many sources were used.

**Acceptance Scenarios**:

1. **Given** two linked products are indexed, **When** I ask why access differs between them, **Then** the answer draws on evidence from both products and cites each.
2. **Given** a multi-part question, **When** the system searches, **Then** it issues separate targeted searches per aspect rather than one undifferentiated search.
3. **Given** a single product fully answers the question, **When** I ask it, **Then** the system stays within that domain instead of padding with unrelated sources.

---

### User Story 5 - See How the Answer Was Reached (Priority: P2)

As a reviewer or evaluator, I can inspect the deterministic stages that produced an
answer — what was searched, what was filtered, what was retrieved, reranked, and
finally selected — without being shown the model's private reasoning.

**Why this priority**: Explainability is what makes a retrieval system reviewable
and is central to demonstrating engineering skill.

**Independent Test**: Enable the inspection mode on a query and confirm every
pipeline stage is visible with its counts and scores, and that no private model
reasoning appears anywhere.

**Acceptance Scenarios**:

1. **Given** inspection mode is enabled, **When** an answer completes, **Then** I see the analyzed query, applied filters, candidates found, candidates reranked, evidence selected, and final citations in order.
2. **Given** inspection mode is enabled or disabled, **When** any response is rendered, **Then** no private model reasoning or internal deliberation is shown.
3. **Given** I submit a question, **When** I choose live progress, **Then** I see progress updates arrive in a fixed, documented order ending with the completed answer.
4. **Given** a long-running answer, **When** progress is streaming, **Then** the interface reports progress rather than appearing frozen.

---

### User Story 6 - Measure Retrieval And Answer Quality Honestly (Priority: P2)

As a reviewer, I run a curated question set and see retrieval and answer quality
measured from that real run — and I can tell instantly if a number is missing
rather than invented.

**Why this priority**: An evaluation capability with fabricated numbers is worse
than none; measured quality is the project's proof of competence.

**Independent Test**: Run the evaluation, confirm the displayed metrics change to
match the run, and confirm the empty state shows no numbers before any run exists.

**Acceptance Scenarios**:

1. **Given** a curated question set exists, **When** I run an evaluation, **Then** retrieval quality, citation quality, groundedness, and latency metrics are computed from that run and displayed.
2. **Given** no evaluation has been run, **When** I open the evaluation view, **Then** I see an empty state explaining that no results exist yet, with no placeholder numbers.
3. **Given** an evaluation completes, **When** I view the results, **Then** each metric is traceable to the run that produced it and the run is timestamped.
4. **Given** a question has several valid answers, **When** it is scored, **Then** scoring accepts any valid answer meeting the stated expectations rather than requiring one exact string.
5. **Given** an evaluation run, **When** measured quality falls below the agreed thresholds, **Then** the result is reported as a failing gate rather than quietly displayed as a pass.

---

### User Story 7 - Explore And Curate The Indexed Corpus (Priority: P3)

As a documentation operator, I browse what is indexed, see its health and coverage,
and manage sources and configuration from a dashboard.

**Why this priority**: Needed for day-two operation and for demonstrating the
system honestly, but the product still delivers value without a polished console.

**Independent Test**: Open the corpus views and confirm source, document, and
configuration status are visible and actionable without touching a database.

**Acceptance Scenarios**:

1. **Given** sources are registered, **When** I open the source view, **Then** I see each source's identity, product, pages indexed, last crawl time, and status.
2. **Given** documents are indexed, **When** I open the document view, **Then** I can browse and filter them and open any one to its original page.
3. **Given** a source is no longer wanted, **When** I remove it, **Then** its indexed content is removed and it stops appearing in answers.
4. **Given** I open the configuration view, **Then** I see the current operational status and settings that affect retrieval, without exposing any secret value.

### Edge Cases

- **The corpus is empty or crawling is still in progress**: Questions return a clear "not enough evidence" state that distinguishes "nothing indexed yet" from "nothing relevant found", and never invents an answer.
- **The documentation site is unreachable, slow, or rate-limiting**: The crawl backs off and retries within limits, records the failure against the affected job, and continues with other pages instead of aborting everything.
- **The site disallows crawling a path**: That path is skipped and recorded as disallowed, not fetched by another route.
- **A page is mostly navigation, boilerplate, or an error page**: It is rejected at extraction as having insufficient main content rather than being indexed as noise.
- **A URL resolves to several addresses, including a private or internal one**: The request is refused before any connection is made.
- **Two pages have identical content**: The duplicate is detected and not indexed twice.
- **A page is enormous, or a chunk cannot be split cleanly**: Oversized content is split at the nearest safe structural boundary, and content that cannot form a useful chunk is recorded rather than silently dropped.
- **Documentation is updated between crawls**: Only the changed portion produces new content; unchanged content is not re-embedded or duplicated.
- **The model provider is unavailable, rate-limits, or returns malformed output**: The request fails with a clear, safe error; the system does not fall back to answering without evidence.
- **The embedding or vector search service is unavailable while a question is being asked**: The user receives a service-unavailable message with a request id, no answer is produced, and no weaker substitute retrieval is silently used in its place.
- **The model emits a citation identifier that was never retrieved**: It is stripped or repaired, and the response is never allowed to present it as a real source.
- **The model contradicts the evidence or answers from memory**: The answer is caught by the grounding check and either corrected or refused, with at most two attempts.
- **A question is ambiguous between two products**: The system searches both, states the ambiguity, and does not silently commit to one.
- **A question is extremely long, or submitted at high frequency**: It is rejected or slowed with a clear message rather than degrading the service.
- **A question arrives containing instructions meant to override the system's rules**: It is treated as a question to answer from evidence, never as an instruction to follow.
- **Every document under a source is deleted**: The source remains listed with zero pages rather than disappearing or erroring.
- **A crawl is already running for a source and another crawl is requested**: The second request is refused with a message naming the job already in progress, and the running crawl continues undisturbed.
- **A document is deleted while a crawl that would re-index it is still running**: The deletion stands, the in-flight crawl skips the document, and the content does not reappear in the index or in later answers.

## Requirements *(mandatory)*

### Functional Requirements

**Grounded answering and citations**

- **FR-001**: The system MUST derive every answer solely from evidence it retrieved and selected, and MUST NOT draw on the model's own knowledge of the subject.
- **FR-002**: The system MUST attach at least one citation to every substantive factual claim in an answer.
- **FR-003**: Citation identifiers MUST originate exclusively from retrieved evidence. The model MUST NOT be permitted to author, invent, or alter a citation identifier or a source link.
- **FR-004**: The system MUST validate citations deterministically, independent of the model, confirming that each cited item was actually retrieved, that its link matches the registry of record, and that it exists.
- **FR-005**: The system MUST reject or repair any citation that fails validation, and MUST verify that the answer text references no citation absent from the retrieved set.
- **FR-006**: The system MUST preserve conditional phrasing from the source and MUST NOT present a conditional instruction as unconditional.

**Refusal and uncertainty**

- **FR-007**: The system MUST enforce minimum evidence-quality thresholds and MUST refuse to answer when they are not met.
- **FR-008**: A refusal MUST state that the indexed documentation did not provide enough evidence, MUST report what was searched, and MUST offer any partially relevant sources as clearly-labelled leads.
- **FR-009**: The system MUST return an explicit "unknown" for product, category, or intent when its confidence is insufficient, and MUST NOT force a classification to fill the field.
- **FR-010**: The system MUST restrict or skip domain filtering when classification confidence is low, favouring a broader search over an unjustified narrowing.
- **FR-011**: The system MUST verify generated answers against the evidence and MUST cap regeneration attempts at two.

**Query understanding**

- **FR-012**: The system MUST analyze each question to identify the relevant product domain, category, intent, and key entities, with a confidence value for each.
- **FR-013**: The system MUST rewrite complex or multi-part questions into multiple targeted retrieval queries, including across product domains when the question spans them.
- **FR-014**: The system MUST support narrowing retrieval by product, category, page type, and language.

**Retrieval quality and evidence selection**

- **FR-015**: The system MUST split content into retrievable units along its real document structure — headings, paragraphs, lists, tables, and code examples — and MUST NOT split on a fixed character count.
- **FR-016**: Retrievable units SHOULD target a comparable size to a substantial paragraph of prose with deliberate overlap between neighbours, so that a fact near a boundary remains retrievable.
- **FR-017**: The system MUST NOT split a code example mid-example when a safe boundary is available.
- **FR-018**: Each retrievable unit MUST retain its source link, page title, product, category, and full heading path.
- **FR-019**: The system MUST rank retrieved candidates with a relevance model distinct from initial retrieval, and MUST keep that stage swappable without altering the pipeline.
- **FR-020**: The system MUST assemble a focused evidence set of roughly three to six units, and MUST NOT place the entire candidate pool into the answer step.
- **FR-021**: Evidence selection MUST consider semantic relevance, rerank strength, source diversity, near-duplicate content, heading relevance, and metadata match.
- **FR-022**: The system MUST prefer fewer, better-spread sources over several near-identical passages from the same page.

**Ingestion and provenance**

- **FR-023**: The system MUST ingest documentation by referencing public URLs, and MUST NOT require or ship a pre-collected copy of the documentation.
- **FR-024**: Each ingested page MUST record its source link, canonical link, title, product, category, page type, language, content fingerprint, crawl time, and version information where detectable.
- **FR-025**: The ingestion process MUST progress through discrete, independently verifiable stages: retrieval, content-type validation, main-content extraction, cleaning, metadata extraction, section detection, chunking, chunk validation, indexing, and registry persistence.
- **FR-026**: The system MUST detect unchanged content by fingerprint and MUST skip re-processing it, and MUST reconcile superseded entries when content changes, so that repeated crawls never accumulate duplicates.
- **FR-027**: Crawling MUST begin only from explicitly registered starting points and MUST be bounded by page count, traversal depth, and per-request delay.
- **FR-028**: Crawling MUST respect the target site's stated crawling directives, MUST identify itself honestly, and MUST apply request timeouts and bounded retries with backoff.
- **FR-029**: The system MUST reject pages whose extracted main content is too small or too boilerplate-heavy to be worth indexing.

**Corpus management**

- **FR-030**: The system MUST present a registry of registered sources showing identity, product, pages indexed, last crawl time, and status, and MUST allow an operator to register, re-crawl, disable, and remove a source.
- **FR-031**: Removing a source or document MUST remove its content from both the search index and the registry, and its content MUST no longer be retrievable.
- **FR-032**: The system MUST NOT persist scraped third-party page content in version control. Only source references, manifests, and application-generated metadata belong in version control.

**Explainability and progress**

- **FR-033**: The system MUST expose deterministic pipeline metadata for each answered question: the analyzed question, detected domain and category, rewritten queries, applied filters, candidates retrieved, candidates reranked, evidence selected, and final citations.
- **FR-034**: The system MUST NOT expose private model reasoning or internal deliberation in any response, event, or view.
- **FR-035**: The system MUST stream progress updates in a fixed documented order, covering question received, question analyzed, retrieval started, retrieval completed, reranking completed, generation started, citation validation, and answer completed.

**Evaluation**

- **FR-036**: The system MUST compute every displayed quality metric from an actual evaluation run. Hard-coded, estimated, seeded, or placeholder metric values are prohibited.
- **FR-037**: When no evaluation run exists, the system MUST display an empty state and MUST NOT display any numeric quality value.
- **FR-038**: The system MUST measure retrieval quality, answer quality, and system performance, covering at minimum: recall, precision, mean reciprocal rank, citation correctness, citation completeness, answer relevance, faithfulness, groundedness, total and stage-level latency, and error rate.
- **FR-039**: The system MUST provide a curated question set of at least 30 questions spanning simple lookup, multi-part, cross-product, troubleshooting, permissions, programming-interface, ambiguous, and deliberately unsupported questions, with easy, medium, and hard difficulty.
- **FR-040**: Expected answers MUST be expressed as topic expectations and notes so that multiple valid answers are not scored as failures.
- **FR-063**: Retrieval correctness MUST be graded by topic coverage — the expected product, category, and heading path recorded for each question — and MUST NOT depend on exact source URLs, so that publisher reorganisations do not produce false regressions.
- **FR-064**: A retrieved result MUST count as a correct hit when its content covers the expected topic, and MUST NOT require the result to come from one predetermined page.
- **FR-041**: The system MUST report whether a run met the agreed quality thresholds, and MUST report a failing run as a failure.
- **FR-065**: Quality thresholds MUST be supplied by configuration and MUST be recorded on every evaluation run alongside its results, so that a recorded outcome remains interpretable after a threshold is changed. Thresholds MUST NOT be derived from, adjusted to, or selected in light of a run's own results.
- **FR-066**: The absolute-zero requirements — zero fabricated facts and zero invalid citation identifiers — MUST NOT be made adjustable by configuration, and MUST NOT be subject to a threshold at all.

**Security and operational safety**

- **FR-042**: All secrets MUST be supplied by the environment, MUST NOT reach the browser interface, and MUST NOT appear in logs, stored records, or version control.
- **FR-043**: The system MUST validate and bound all user-supplied input, including maximum question length and maximum ingestion size, and MUST reject non-conforming input with a clear message.
- **FR-044**: The crawler MUST refuse, before opening any connection, any target that is not an allowed public web address — including loopback, private network ranges, link-local and metadata-service addresses, non-web schemes, and internal hostnames.
- **FR-045**: The system MUST rate-limit inbound requests and MUST bound all outbound requests with timeouts.
- **FR-046**: The system MUST sanitize retrieved and rendered content before display, and MUST sanitize operator-supplied URLs.
- **FR-047**: Failures MUST return a consistent structured error carrying a stable error code, a safe human-readable message, and a request identifier. Internal details and stack traces MUST NOT be exposed.
- **FR-048**: The system MUST record, per request, a request identifier, the question, candidate and selected-evidence counts, stage-level latency, the provider and model used, token usage where available, and any error — while excluding secrets and applying bounded retention to stored questions.

**Accessibility**

- **FR-057**: All user-facing views MUST be operable entirely by keyboard, with a visible focus indicator on every interactive element and no keyboard traps.
- **FR-058**: All interactive controls MUST have programmatically determinable accessible names, and page structure MUST use semantic headings in a correct hierarchy.
- **FR-059**: Text and meaningful non-text elements MUST meet AA contrast ratios, and colour MUST NOT be the sole means of conveying state.
- **FR-060**: Streamed progress updates MUST be announced to assistive technology through a live region, so a screen-reader user is not left without feedback while an answer is being generated.

**Degraded dependency behaviour**

- **FR-061**: If the embedding or vector search service is unreachable, the system MUST return a clear service-unavailable failure carrying a request identifier, MUST record the failure, and MUST NOT return any answer.
- **FR-062**: The system MUST NOT substitute keyword-only matching, a cached prior answer, or any other degraded retrieval for a failed semantic search, and MUST NOT present a partial result as though it were a complete one.

**Independence and attribution**

- **FR-049**: The system MUST be presented as an independent technical demonstration. It MUST NOT claim affiliation with, sponsorship by, or endorsement by Atlassian or any documentation publisher, and MUST NOT be named or presented as an official product of any third party.
- **FR-050**: Every source shown to a user MUST link back to the original page and MUST carry attribution to its publisher.
- **FR-051**: The project MUST ship a published licence and an explicit data-source policy describing what is and is not collected, retained, and redistributed.
- **FR-052**: The system MUST be operable in a single-command local environment, and MUST provide automated tests covering the chunker, address validation, metadata extraction, citation validation, question analysis, filter construction, content fingerprinting, crawler security controls, and the answer verifier.
- **FR-053**: The system MUST track every document's processing state through an explicit lifecycle of discovered, crawling, processed, indexed, failed, and deleted, and MUST distinguish content that has been extracted and chunked from content whose vectors have been written.
- **FR-054**: When a re-crawl fails partway through, the system MUST leave the document's previously indexed content live and queryable, MUST supersede only the pages that actually changed, and MUST record the failure against the affected crawl job rather than discarding the whole document.
- **FR-055**: The system MUST permit at most one in-progress crawl per source. A crawl request for a source that is already being crawled MUST be rejected with a clear message identifying the running job, and MUST NOT silently queue or run concurrently.
- **FR-056**: Deletion MUST take precedence over in-flight ingestion. A deleted document MUST be tombstoned such that an in-flight crawl cannot re-create or resurrect it, and its content MUST remain unretrievable once deletion completes.

### Key Entities

- **Source**: A registered public documentation entry point. Attributes: name, address, product domain, enabled state, page and chunk counts, last crawl time, status.
- **Document**: One ingested page. Attributes: source and canonical address, title, product, category, page type, language, content fingerprint, source-modified and crawl timestamps, derived-unit count. Lifecycle: moves through `discovered` (registered but not fetched) → `crawling` (fetch in progress) → `processed` (main content extracted and split into units) → `indexed` (vectors written and retrievable), with `failed` (fetch or processing errored) and `deleted` (removed by an operator) reachable from any state. A document at `indexed` remains live and queryable; a re-crawl that fails partway returns the document to `failed` while leaving its prior indexed content retrievable.
- **Document Unit**: A retrievable, structure-derived segment of a document. Attributes: owning document, index reference, ordinal, text fingerprint, heading path, size in tokens.
- **Crawl Job**: One crawl run against a source. Attributes: target address, status, start and finish times, error detail, pages discovered, pages processed.
- **Question Analysis**: The structured interpretation of a user question. Attributes: original question, normalized and rewritten queries, detected product, category, intent, entities, per-field confidence.
- **Retrieved Candidate**: A unit returned by initial retrieval. Attributes: unit reference, similarity score, product, category, heading path.
- **Reranked Candidate**: A candidate with a relevance-model score. Attributes: candidate reference, rerank score, rank.
- **Selected Evidence**: The final focused evidence set. Attributes: unit reference, selection rationale scores, section, excerpt.
- **Query Log**: An audit record of one answered question. Attributes: question, rewritten query, detected domain and category, retrieved and reranked counts, answer, confidence, latency.
- **Citation**: A validated evidence reference attached to an answer. Attributes: owning query log, document and unit references, source address, rank, retrieval and rerank scores.
- **Evaluation Question**: A curated assessment item. Attributes: question text, expected product, expected category, expected heading path, difficulty, category, answer notes. Graded by topic coverage rather than by exact source URL, so publisher reorganisations do not invalidate the dataset.
- **Evaluation Run**: One execution of the assessment suite. Attributes: dataset identity, timestamp, per-question results, aggregate metrics, threshold outcome.

## Success Criteria *(mandatory)*

### Measurable Outcomes

**How thresholds are read.** SC-001 through SC-007 are **configuration values**. Each states a
default; the operative threshold is whatever is configured when the run executes, and the
value in force is recorded on the run so a result stays interpretable after a change. Raising
a threshold is legitimate once a real run justifies it, and lowering one to make a run pass
is a governance failure, not a tuning decision.

**SC-008 and SC-009 are not thresholds.** They are absolute zeros, stated as absolutes
deliberately. There is no configuration under which a fabricated fact or an invalid citation
identifier is acceptable, so no value makes them pass more easily.

**Retrieval quality gates (measured on the curated question set)**

- **SC-001**: Recall at 5 reaches at least the configured threshold — default 0.80.
- **SC-002**: Mean reciprocal rank reaches at least the configured threshold — default 0.75.
- **SC-003**: Precision at 5 reaches at least the configured threshold — default 0.70.
- **SC-004**: At least the configured proportion of multi-part and cross-product questions — default 90% — retrieve evidence from every domain the question spans.

**Answer quality gates**

- **SC-005**: At least the configured proportion of citations are valid — default 90% — resolving to a genuinely retrieved item whose text supports the claim.
- **SC-006**: At least the configured proportion of substantive claims in answers are supported by at least one citation — default 85%.
- **SC-007**: Answers score at least the configured faithfulness threshold — default 0.90.
- **SC-008**: 100% of questions in the deliberately-unsupported category are refused, with zero fabricated facts and zero fabricated citations.
- **SC-009**: Zero invalid or unknown citation identifiers survive into a response across a full evaluation run.

**Corpus and operations**

- **SC-010**: An operator can register a documentation source and see indexed content with correct title, domain, category, and source link, without writing any code.
- **SC-011**: A 50 to 100 page corpus yields 500 to 1500 retrievable units, each traceable to its original page and heading path.
- **SC-012**: Re-crawling an unchanged source adds zero duplicate content.
- **SC-013**: Deleting a document or source makes its content unreachable in subsequent answers.
- **SC-014**: 100% of a defined set of internal-network and disallowed-scheme targets are refused before any connection is attempted.
- **SC-015**: Zero third-party page contents, and zero secrets, appear in version control.

**User-facing performance**

- **SC-016**: A user sees the first progress update within 2 seconds of submitting a question, and never experiences an unexplained pause longer than 10 seconds.
- **SC-017**: A complete answer, including citations and evidence, is delivered within 8 seconds for at least 95% of questions.
- **SC-018**: 100% of answers display their citations, and 100% of citation links resolve to the original publisher page.

**Demonstrability**

- **SC-019**: The published repository contains an architecture overview, a data-source policy, an evaluation guide, an interface reference, and an explicit statement that the project is not affiliated with, sponsored by, or endorsed by Atlassian.
- **SC-020**: A reviewer can reproduce the full demonstration — start the system, see indexed sources, ask a simple question, inspect its evidence, ask a cross-product question, enable inspection mode, and run an evaluation — in under 15 minutes, following only the published instructions.
- **SC-021**: Every interactive control in every view is reachable and operable by keyboard alone, with a visible focus indicator, and no view traps keyboard focus.
- **SC-022**: An automated accessibility check of the built interface reports no serious or critical violations, and all text meets AA contrast ratios.

## Assumptions

- The target documentation is publicly accessible, permits crawling of the specific paths registered, and states its crawling directives. Exact paths and their current availability are verified before the first crawl; paths that disallow crawling are excluded rather than worked around.
- The project's published repository is `https://github.com/Saif-Ali-109/Enterprise-Intelligence-RAG`, currently empty, and is the destination for this work.
- The project is published under the MIT licence.
- Groq is the default language model provider, reached through a provider-neutral interface so that another provider can be substituted by configuration alone without altering the retrieval pipeline. A Groq API key and model choice are supplied by the environment at run time.
- Hosted embedding and vector-retrieval services are available to the project by subscription, including at least one integrated-embedding model; the specific model is selected from what the account actually offers at implementation time rather than fixed in advance, and vector dimensionality is read from service configuration rather than hard-coded.
- The demonstration corpus is deliberately bounded at 50 to 100 pages. Quality is preferred over volume; broader ingestion is a separate, later decision.
- The evaluation question set is authored by the project owner. Each question records its expected product, category, and heading path as topic expectations, never a single canonical answer string, and is not pinned to specific source URLs — so the dataset survives the publisher reorganising its documentation.
- The interface is a single-operator research and demonstration tool. There are no end-user accounts, roles, or per-tenant isolation in this release; namespace structure is forward-compatible with multi-tenancy but multi-tenancy is out of scope.
- The interface is served over plain web transport in local development; transport encryption and identity management are deployment concerns outside this release.
- Network access to the documentation publisher, the language model provider, and the hosted retrieval service is available in the development and demonstration environment.
- Out of scope for this release: authoring or editing documentation, writing actions back to any external system, non-English source corpora, real-time or live-data retrieval, and automated ingestion beyond manually registered sources.
