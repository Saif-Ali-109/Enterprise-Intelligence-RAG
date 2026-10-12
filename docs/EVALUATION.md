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

Two runs, and the reason the second one cannot answer the question the first one
raised. Nothing here is illustrative.

### Run `3bbebca5` — 2026-10-12, dataset 1.3.0, fingerprint `f2a9fd38…`

163 documents, 1150 units. **`error_rate = 0.625`**, and that number governs
everything else in the row.

| Metric | Measured | Threshold | Gate |
|---|---|---|---|
| `recall_at_5` | 0.000 | ≥ 0.80 | **fail** |
| `precision_at_5` | 0.000 | ≥ 0.60 | **fail** |
| `mrr` | 0.000 | ≥ 0.75 | **fail** |
| `cross_product_domain_coverage` | 1.000 | ≥ 0.80 | pass |
| `citation_validity` | `null` | ≥ 0.98 | not applicable |
| `claim_coverage` | `null` | ≥ 0.85 | not applicable |
| `faithfulness` | `null` | ≥ 0.90 | not applicable |
| `unsupported_refusal_rate` | 1.000 | = 1.00 | pass |
| `fabricated_fact_count` | **0** | = 0 | **pass** |
| `invalid_citation_count` | **0** | = 0 | **pass** |
| `p50_latency_ms` | 127 864 | — | reported |
| `p95_latency_ms` | **139 022** | ≤ 8000 | **fail** |
| `error_rate` | **0.625** | — | reported |

Zero questions were answered. Nineteen of the twenty errors were
`ProviderRateLimited` — the Groq free-tier quota, exhausted after a day of
evaluation runs — and each of those questions waited out the runner's retry
backoff, which is where the 128-second median comes from. **This run measures a
throttled provider, not the pipeline**, and its `recall_at_5 = 0.000` is a
statement about 11 graded questions out of 32, not about retrieval.

### Run `72a40bc7` — 2026-10-12, dataset 1.3.0, before the FR-063 amendment

163 documents, 1150 units. `error_rate = 0.156`, four questions answered.

| Metric | Measured | Threshold | Gate |
|---|---|---|---|
| `recall_at_5` / `precision_at_5` / `mrr` | 0.000 | 0.80 / 0.60 / 0.75 | **fail** |
| `cross_product_domain_coverage` | 1.000 | ≥ 0.80 | pass |
| `citation_validity` | 1.000 | ≥ 0.98 | pass |
| `claim_coverage` | 0.000 | ≥ 0.85 | **fail** |
| `faithfulness` | 0.500 | ≥ 0.90 | **fail** |
| `unsupported_refusal_rate` | 1.000 | = 1.00 | pass |
| `fabricated_fact_count` | **0** | = 0 | **pass** |
| `invalid_citation_count` | **0** | = 0 | **pass** |
| `p50_latency_ms` | 20 289 | — | reported |
| `p95_latency_ms` | 77 175 | ≤ 8000 | **fail** |
| `error_rate` | 0.156 | — | reported |

**The absolute zeros hold.** That is the first run in which they were tested
against answers rather than against a corpus too thin to produce any.

### Why retrieval read 0.000, measured rather than inferred

Three separate causes, found in this order, each established from the registry
and `query_logs` rather than from a hypothesis.

**1. The gold set's categories were outside the system's vocabulary.** The dataset
names `filters`, `spaces`, `page-permissions`, `issue-api`, `oauth`, `boards`,
`permissions`, `issue-types`, `notifications`, `automation`, `projects`,
`troubleshooting`, `issues-and-requests`, `request-management`, `rate-limiting`,
`search-api`, `service-desk-api` for 21 of its 25 questions. `DocumentCategory`
has six URL-derived values. Measured on 164 indexed documents: `jira /
user-documentation` 47, `jira / rest-api` 47, `confluence / user-documentation`
33, `jsm / user-documentation` 25, and eight pages across `root`, `developer` and
`general`. **Not one expected category was present**, so the grading condition
was unsatisfiable by construction.

Amended 2026-10-12 (FR-063): a hit requires the product and the heading path;
`expected_category` stays in the dataset as a descriptive label. The dataset's
own 2026-10-02 audit had re-derived heading paths from real pages and never
applied the same treatment to `category`.

**2. The dataset's heading paths had rotted.** Atlassian renamed the pages the
gold set targets. Of 25 expected heading paths, 19 did not exist under that
wording in the live corpus — "service space agents" is now "service project
agents", "Delete, restore, or purge a content item" is now "…a page". Dataset
1.3.0 repoints three and audits the rest; 22 of 25 are now present.

**3. A confident filter was cutting the corpus to one candidate.** From
`query_logs`, live:

```
question:       How do I create a new space in Confluence Cloud?
applied filter: {product: confluence, category: general}
retrieved:      1 candidate   reranked: 1   evidence: 1
```

The analyzer was confident, and `general` is a real category value — but 2 of 33
indexed Confluence pages carry `category = general`. The filter cut a 33-page
section to a single candidate, which cannot fill `evidence_min_units = 3`, so the
answer was gone before the reranker saw it. `should_widen()` fired only on *zero*
candidates, so one candidate passed as success. Fixed 2026-10-12: a filtered pool
smaller than `evidence_min_units` now widens once.

**This last fix is verified by unit test and measurement, not by a live run** —
the provider quota is exhausted, and the analyzer's classification needs the
provider before retrieval can be reached with a real analysis.

### What is established, and what is not

**Established.** The absolute-zero gates hold on a corpus that produces answers.
Citation validity is 1.000 — every citation the model issued resolved to evidence
that was actually served. All four deliberately-unsupported questions are refused.
Cross-product coverage is 1.000. Retrieval's zero was structural, and the three
causes are named with the measurements that found them.

**Not established.** Whether the FR-063 amendment and the widening fix actually
raise recall. Both landed after `72a40bc7`, and `3bbebca5` was too degraded by
provider throttling to measure them. **The honest statement is that the retrieval
gates have not yet had a clean run**, and the next one should be started when the
provider quota is available.

### Earlier runs

| Run | Dataset | Corpus | Outcome | Notable |
|---|---|---|---|---|
| `dfa3a1fd` | 1.1.0 | 19 docs / 149 units | fail | All questions refused; nothing generated, so the absolute zeros were untested |
| `b6d452a3` | 1.2.0 | 19 docs / 149 units | fail | Same, inside the container image |
| `68350c4a` | 1.2.0 | 132 docs / 841 units | fail | Navigation still in the corpus: a published answer asserted an API version, grounded in a breadcrumb containing `REST API v2` |
| `fb88cffa` | 1.2.0 | 157 docs / 1100 units | fail | Navigation filter and fingerprint fix landed; that version question now refuses. Three uncited claims — the absolute zero failed |
| `72a40bc7` | 1.3.0 | 163 docs / 1150 units | fail | **Absolute zero passes.** Retrieval still 0.000 for the three structural reasons above |
| `3bbebca5` | 1.3.0 | 163 docs / 1150 units | fail | Provider quota exhausted; `error_rate = 0.625`. Not a measurement of the pipeline |

The progression is the point: the absolute zeros looked green for two runs only
because nothing was answered, and the retrieval zeros were structural from the
first run.

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