# Evaluation guide

The evaluation suite runs the curated question set through the same pipeline that answers a user's
question, grades the results, records every number against the run that produced it, and renders a
gate that is either `pass` or `fail` with no third option.

Two rules govern everything below:

1. **Every displayed number came from an actual run.** No hard-coded, estimated, seeded, or
   placeholder metric exists in this system, and the test suite asserts the absence structurally
   rather than by inspection.
2. **A failing gate is reported as failing.** There is no "marginal", no warning tint, no partial
   credit in the outcome field. A run that fails is a run that failed.

---

## 1. Measured results

The current run measures the pipeline rather than a broken harness, and it
**fails its gate**. Nothing here is illustrative.

### Run `227cb017` — 2026-10-13, dataset 1.3.0, fingerprint `f2a9fd38…`

165 documents, 1150 units. 7 answered, 23 refused, 2 raised.

| Metric | Measured | Threshold | Gate |
|---|---|---|---|
| `recall_at_5` | **0.500** | ≥ 0.80 | **fail** |
| `precision_at_5` | **0.269** | ≥ 0.60 | **fail** |
| `mrr` | **0.455** | ≥ 0.75 | **fail** |
| `cross_product_domain_coverage` | **0.667** | ≥ 0.80 | **fail** |
| `citation_validity` | 1.000 | ≥ 0.98 | pass |
| `claim_coverage` | **0.000** | ≥ 0.85 | **fail** |
| `faithfulness` | 0.586 | ≥ 0.90 | **fail** |
| `unsupported_refusal_rate` | 1.000 | = 1.00 | pass |
| `fabricated_fact_count` | **0** | = 0 | **pass** |
| `invalid_citation_count` | **0** | = 0 | **pass** |
| `p50_latency_ms` | 16 663 | — | reported |
| `p95_latency_ms` | **95 221** | ≤ 8000 | **fail** |
| `error_rate` | 0.062 | — | reported |

**Retrieval is no longer structurally zero.** It read 0.000 in every earlier run;
it now reads 0.500 — 13 hits across 26 graded questions — after three changes
whose effects are separable in the record below. It is still below the 0.80 gate,
and it is reported as failing.

### What moved, and why

**1. The grading condition was unsatisfiable.** The gold set names document
categories — `filters`, `spaces`, `page-permissions`, `issue-api`, `oauth`,
`boards`, `permissions`, `issue-types`, `notifications`, `automation`,
`projects`, `troubleshooting`, `issues-and-requests`, `request-management`,
`rate-limiting`, `search-api`, `service-desk-api` — and `DocumentCategory` has six
URL-derived values. Measured on the indexed corpus: `jira / user-documentation`
47, `jira / rest-api` 47, `confluence / user-documentation` 33, `jsm /
user-documentation` 25, and a handful across `root`, `developer`, `general`. **Not
one expected category was present**, so the condition could never be true.
Amended 2026-10-12 (FR-063): a hit needs the product and the heading path;
`expected_category` stays in the dataset as a descriptive label.

**2. The dataset's heading paths had rotted.** Atlassian renamed the pages the
gold set targets. Of 25 expected heading paths, 19 did not exist under that
wording in the live corpus. Dataset 1.3.0 repoints three against verified pages
and records the authored wording in each question's notes.

**3. A confident filter was cutting the corpus to one candidate.** From
`query_logs`:

```
question:       How do I create a new space in Confluence Cloud?
applied filter: {product: confluence, category: general}
retrieved:      1 candidate   reranked: 1   evidence: 1
```

The analyzer was confident and `general` is a real value, but 2 of 33 indexed
Confluence pages carry `category = general`. `should_widen()` fired only on *zero*
candidates, so one candidate — below the minimum evidence count of 3 — passed as
success. It now widens when the filtered pool is smaller than `evidence_min_units`.

**A fourth cause was found while verifying the third.** The same filter question
now refuses again, and the trace says why: the expected page *is* retrieved, with
heading path `Save your search as a filter` — but labelled `product=jsm`, because
that slug is published under both the Jira Service Management and the Jira
Software sections and the corpus had only the JSM copy. Indexing the Jira Software
version changed that question from **refused** to **answered with a citation** in
one step. The corpus now covers 165 documents.

### What still fails, and the honest reading

**Recall 0.500 against 0.80.** 13 of 26 graded questions hit. The 13 misses split
into two groups, and the split is legible in the run:

- **8 were refused**, so the pipeline found evidence and declined it — the gate,
  the verifier, or the uncited-claim check. `simple-jira-issue-types`,
  `perms-jira-project-role`, `simple-jira-board-default`, `perms-jsm-agent-permission`
  and four more. These are answer-quality questions, not retrieval ones.
- **3 were answered but missed**, which is the opposite problem:
  `api-jira-jql-search`, `api-jira-ratelimit`, `cross-jira-api-auth` — all three
  target `developer.atlassian.com`, where the REST reference pages extract into
  many small heading-scoped units and the expected section is not what surfaces.

**Cross-product coverage fell to 0.667** from 1.000. With fewer questions reaching
generation, a single missed cross-product question moves the ratio a long way; on
two questions of data it is not a measurement of anything. It is reported as
failing.

**Claim coverage 0.000 and faithfulness 0.586** over 7 answers. Claim coverage
expects each of a question's `topics` to appear in the answer text, and no answer
covered any. On 7 answers this says the answers are short and general, not that the
grading is wrong — but 7 answers is a thin basis for a quality verdict, and it is
the metric most in need of a larger corpus and more answerable questions.

**p95 95 221 ms against an 8000 ms target**, p50 16 663 ms. Generation dominates,
and the uncited-claim retry doubles it on every question the model answers without
inline markers — the cost of the enforcement that made the absolute zero hold. Two
questions raised `ProviderError`; a provider error is not retried by design.

### What is established

The **absolute-zero gates hold**: zero fabricated facts and zero invalid
citations, with 7 answers that could have violated them. `citation_validity` is
1.000 — every citation the model issued resolved to evidence that was actually
served. All four deliberately-unsupported questions are refused. Retrieval is
measured, and measured short of its gate.

### Earlier runs

| Run | Dataset | Corpus | Outcome | Notable |
|---|---|---|---|---|
| `dfa3a1fd` | 1.1.0 | 19 docs / 149 units | fail | All refused; nothing generated, so the absolute zeros were untested |
| `b6d452a3` | 1.2.0 | 19 docs / 149 units | fail | Same, inside the container image |
| `68350c4a` | 1.2.0 | 132 docs / 841 units | fail | Navigation still indexed: a published answer asserted an API version, grounded in a breadcrumb containing `REST API v2` |
| `fb88cffa` | 1.2.0 | 157 docs / 1100 units | fail | Navigation filter and fingerprint fix landed; three uncited claims — the absolute zero failed |
| `72a40bc7` | 1.3.0 | 163 docs / 1150 units | fail | Absolute zero passes for the first time; retrieval still 0.000 for the three structural reasons above |
| `3bbebca5` | 1.3.0 | 163 docs / 1150 units | fail | Provider quota exhausted, `error_rate = 0.625` — not a measurement of the pipeline |
| **`227cb017`** | **1.3.0** | **165 docs / 1150 units** | **fail** | **Retrieval measured: 0.500.** Absolute zeros hold; 7 gates fail |

---

## 2. Running the suite

### Through the API

```bash
# Start a run. Returns 202 with the run row; metrics are null while it runs.
curl -s -X POST http://localhost:8000/api/v1/evaluations/runs

# Poll it.
curl -s http://localhost:8000/api/v1/evaluations/runs/<run_id>

# The numbers, the thresholds they were judged against, and what failed.
curl -s http://localhost:8000/api/v1/evaluations/runs/<run_id> \
  | jq '{status, metrics, thresholds, gate_outcome, gate_failures}'

# The per-question detail behind the aggregates, including the absolute-zero counts.
curl -s "http://localhost:8000/api/v1/evaluations/runs/<run_id>/results?limit=50"

# The dataset itself, and the runs so far.
curl -s http://localhost:8000/api/v1/evaluations/dataset
curl -s "http://localhost:8000/api/v1/evaluations/runs?limit=20"
```

A run takes roughly 2–4 minutes for 32 questions, dominated by generation latency. One run at a
time: a second `POST` while one is in flight returns `409 CONFLICT`, because two writers over one
corpus and one threshold set would make a run's numbers incomparable to themselves.

### Through the dashboard

`http://localhost:3000/evaluations`. The same numbers, grouped by what they measure, each metric
cell labelled with the run id and timestamp that produced it. Before any run exists, the page says
so in words and renders no numeric quality value at all.

---

## 3. How the dataset is graded

`evaluation/golden_questions.json` holds 32 questions across eight categories and three difficulties.
The grading rule is the important part:

> **A retrieved result counts as a hit when its content covers the expected product, category, and
> heading path — never when its URL matches.**

There is deliberately no URL anywhere in the dataset, and `contracts/evaluation-dataset.schema.json`
sets `additionalProperties: false` with no URL field, so a pinned source cannot be introduced through
a new property either. The `evaluation_questions` table has no `source_url` column at all. Three
independent mechanisms, because the file and the database are loaded by different code.

The reason is that this corpus is ingested by URL from documentation the publisher reorganises
whenever they like. A dataset pinned to today's page structure would report a false regression the
first time a heading was renamed — and would report it as a *retrieval* regression when nothing about
retrieval had changed.

Heading-path coverage is order-sensitive but not contiguous: an expected path `["Filters", "Saving a
filter"]` is covered by `["Filters", "Managing a filter", "Saving a filter"]`, and not by the reverse
order.

**A question with several valid answers is not scored as a failure.** `expected.acceptable_alternatives`
records alternative correct phrasings, and `expected.notes` records them for a human grader. Claim
coverage is scored against `expected.topics`, each of which is one checkable assertion rather than a
topic label — that is what makes coverage falsifiable rather than a matter of taste.

**The unsupported bucket is real.** Four questions are deliberately unanswerable: plausible-sounding
questions about things this corpus genuinely does not document. They must be refused with zero
fabricated facts and zero fabricated citations. Their retrieval metrics are `null`, not `0.0` — a
retrieval score recorded there would drag the average down and misrepresent a correct refusal as a
retrieval failure.

### The metric definitions

| Metric | Definition | Why it is defined this way |
|---|---|---|
| `recall_at_5` | Share of questions whose target is covered within the top 5 reranked candidates | Binary per question: each question has one target |
| `precision_at_5` | Share of the top 5 that cover the target | Distinguishes "found it among noise" from "found a clean page" |
| `mrr` | Mean reciprocal rank of the first hit | Rank quality, not just presence |
| `cross_product_domain_coverage` | Share of cross-product questions that consulted ≥ 2 products | Read from the retrieval trace on answered runs and from `searched` on refused ones |
| `citation_validity` | valid ÷ (valid + stripped + rejected) | Every citation the answer issued, not every citation it kept |
| `claim_coverage` | Share of expected topics the answer text covers | The falsifiable one; each topic is one checkable assertion |
| `faithfulness` | Share of answer sentences grounded in a cited quote | A sentence counts as grounded when ≥ ⅓ of its content words appear in the cited passages **and** at least two of those shared words are five characters or longer |
| `unsupported_refusal_rate` | Share of unsupported questions that refused | Must be 1.0 |
| `fabricated_fact_count` | Answer sentences carrying no citation marker | A **count**. No configuration can move it |
| `invalid_citation_count` | Citations stripped or rejected during validation | A **count**. No configuration can move it |
| `p50` / `p95` | Nearest-rank percentiles of total request latency | `total_time`, not the last stage: it includes queueing |
| `error_rate` | Questions that raised rather than answered or refused | A provider outage should not be silently averaged away |

Both thresholds for `faithfulness` were calibrated against answers this system actually produced,
not chosen for convenience: a correctly cited paraphrase of a 32-word passage shares 0.42 of its
content words with it, so the original 0.5 threshold scored a faithful answer at zero. At ⅓ with the
distinctive-word condition, that answer scores 1.0 and a sentence about JVM heap sizes appended to
the same answer drags the ratio to 0.5.

---

## 4. Thresholds

Gates live in configuration, never in code:

```bash
EVAL_GATE_RECALL_AT_5=0.80            EVAL_GATE_PRECISION_AT_5=0.60
EVAL_GATE_MRR=0.75                    EVAL_GATE_CROSS_PRODUCT_COVERAGE=0.80
EVAL_GATE_CITATION_VALIDITY=0.98       EVAL_GATE_CLAIM_COVERAGE=0.85
EVAL_GATE_FAITHFULNESS=0.90            EVAL_GATE_UNSUPPORTED_REFUSAL_RATE=1.00
EVAL_GATE_P95_LATENCY_MS=8000
```

Every one of these is recorded on the run in `thresholds` **before** the run is judged, and a run
is never comparable across different thresholds. A raised threshold changes the outcome to `fail` and
names the gate — that is a test, not a hope:

```bash
# Raise a gate and re-run: the same corpus now fails a different gate, and the
# failure names the target and the measured value.
EVAL_GATE_RECALL_AT_5=0.99 make up
```

### The two absolute zeros

`fabricated_fact_count` and `invalid_citation_count` **have no configuration variable**. Check:

```bash
grep -E 'FABRICAT|INVALID_CITATION|GATE.*ZERO' .env    # no output — by design
```

An absolute zero that can be configured is not a zero. They are counted and compared to `0`, they
appear in `gate_failures` when non-zero, and a `thresholds` block that omits them is correct rather
than incomplete.

---

## 5. Reading a failing gate

A gate failure is a fact with three parts: the gate, the target, and the measured value. The API
returns all three:

```json
"gate_failures": [
  {"gate": "recall_at_5", "target": 0.8,  "actual": 0.0},
  {"gate": "p95_latency_ms", "target": 8000.0, "actual": 32500.0}
]
```

Read it in this order:

1. **Is it the corpus or the pipeline?** The run row carries `config_snapshot`, `dataset_version`,
   `dataset_fingerprint`, and the corpus document and unit counts. A retrieval number that collapses
   after a source was re-registered is a corpus fact, not a retrieval regression. Check what changed
   before you change anything.
2. **Is a metric `null` because nothing applied, or because it went missing?** `null` means *not
   applicable to any question* — no answer was produced, so there was nothing to grade. It is never a
   substituted zero, and never a failure of its own.
3. **Did the absolute zeros hold?** `fabricated_fact_count` and `invalid_citation_count` at `0` are
   the two gates that must never move. Everything else is a tuning question; these are correctness.
4. **Look at the per-question rows.** `evaluation_results` gives one row per question with its own
   counts, so an aggregate can be traced to the questions that produced it.

Then reproduce it: same dataset fingerprint, same `config_snapshot`, same corpus counts. A number you
cannot reproduce is a number you should not report.

---

## 6. What the tests assert about all this

| Test | Property |
|---|---|
| `tests/unit/test_metrics_provenance.py` | Every quality number lives inside `metrics`, which is `null` until a run finishes. The metric field set is exactly the contract's twelve. There is no field a placeholder could be rendered into. |
| `tests/unit/test_gate_thresholds.py` | A raised threshold names its failing gate. Latency is the only at-most gate. The two zero gates fail with no configuration to tune them. A metric missing from a run is `null`, not a synthetic zero. |
| `tests/unit/test_topic_grading.py` | A unit covers a target by product, category, and heading path — never by URL. Order-sensitive, not contiguous. Unsupported questions are `None`, not `0.0`. |
| `tests/integration/test_metrics_respond.py` | **The tripwire.** A deliberately degraded pipeline moves five measured metrics; if any does not move, the test fails. A metric that cannot tell an answering pipeline from a refusing one is decorative. |
| `tests/unit/test_navigation_filter.py` | The blocks that made the corpus unusable are not indexed, and prose the extractor cut mid-sentence still is |
| `tests/unit/test_fingerprint_tracks_policy.py` | Changing how units are built invalidates the corpus that was built the old way |
| `tests/integration/test_eval_empty_state.py` | With no run: totals are `0`, metrics are `null`, and no endpoint renders a quality number. |
| `tests/integration/test_eval_run_bookkeeping.py` | Each question gets its own request id and its own audit row, and the run reaches a terminal status with real metrics. |

---

## 7. If a run fails to start

`POST /evaluations/runs` loads and schema-validates the dataset *before* the first question, and
refuses the run if the file is missing, unreadable, or invalid — a broken dataset should not be
discovered after thirty-two answers have been generated. The log names the reason.

Both files resolve against `PROJECT_ROOT` (the repository root in a checkout, `/app` in the
container image) and can be overridden individually:

```bash
EVAL_DATASET_PATH=/data/golden_questions.json
EVAL_DATASET_SCHEMA_PATH=/data/evaluation-dataset.schema.json
```

The dataset's `dataset_fingerprint` is a SHA-256 of its canonicalised content, recorded on every
run. Two runs are comparable only when the fingerprints match — `dataset_version` is a human label
that two different files can share.

---

## Related

- [Architecture](ARCHITECTURE.md) — the pipeline the suite measures.
- [Data source policy](DATA_SOURCE_POLICY.md) — what the corpus is and what it never is.
- [API reference](API.md) — generated from the contract.
- Quickstart scenarios V11 and V12 in
  [`../specs/001-enterprise-knowledge-rag/quickstart.md`](../specs/001-enterprise-knowledge-rag/quickstart.md).