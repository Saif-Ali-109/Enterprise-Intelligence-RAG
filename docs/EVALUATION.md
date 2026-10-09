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

This is the honest record of real runs against real corpora. Nothing here is illustrative, and
**the headline is a failure**: the absolute-zero gate does not currently hold.

| | |
|---|---|
| **Run id** | `fb88cffa-2aeb-48b3-994c-ddab89c63ff5` |
| **Dataset** | version `1.2.0`, fingerprint `68d76c26e6fc063c…` |
| **Started → finished** | 2026-10-09T18:32:27Z → 2026-10-09T18:39:18Z (6 min 51 s) |
| **Corpus** | 157 documents, 1100 units — 18 registered sources across four products |
| **Provider** | `openai/gpt-oss-120b` via Groq; rerank `bge-reranker-v2-m3` |
| **Outcome** | **`fail`** — 7 gates outside their thresholds, including the absolute zero |

### Metrics as measured

| Metric | Measured | Threshold | Gate |
|---|---|---|---|
| `recall_at_5` | **0.000** | ≥ 0.80 | **fail** |
| `precision_at_5` | **0.000** | ≥ 0.60 | **fail** |
| `mrr` | **0.000** | ≥ 0.75 | **fail** |
| `cross_product_domain_coverage` | 1.000 | ≥ 0.80 | pass |
| `citation_validity` | 1.000 | ≥ 0.98 | pass |
| `claim_coverage` | **0.000** | ≥ 0.85 | **fail** |
| `faithfulness` | **0.233** | ≥ 0.90 | **fail** |
| `unsupported_refusal_rate` | 1.000 | = 1.00 | pass |
| `fabricated_fact_count` | **3** | = 0 | **fail** |
| `invalid_citation_count` | **0** | = 0 | pass |
| `p50_latency_ms` | 8093 | — | reported |
| `p95_latency_ms` | **32 500** | ≤ 8000 | **fail** |
| `error_rate` | 0.156 | — | reported |

Six questions answered, 21 refused, 5 raised. Every one of the 21 refusals carried a stated
reason.

### The absolute zero is the finding that matters

`fabricated_fact_count = 3` is not a rounding artefact and not a metric that disagrees with the
runtime. It is three genuine, uncited, actionable claims:

> "To set the restrictions, open the Share dialog on the page, choose 'Restrict' and select
> whether you want to limit 'Can view' or 'Can edit' to specific users or groups."

That sentence carries no citation, in an answer that otherwise cites correctly. It is the most
useful sentence in the answer — a procedure a reader would follow — and nothing in the system
stopped it reaching a client.

**The prompt already promises this cannot happen.** It instructs the model to mark every claim and
states that an answer whose sentences carry no markers is refused. The generation step verifies
that the answer as a whole is `SUPPORTED` by the evidence, which it was; it never checks that each
claim is marked. So the promise lives in the prompt, the enforcement does not exist, and the
evaluation found the gap. Fixing it is a product decision with a measured cost, recorded in
[tasks.md](../specs/001-enterprise-knowledge-rag/tasks.md) and not resolved here: an earlier live
measurement found the model produces inline markers in 3 of 6 answers, so enforcing the promise
strictly would refuse roughly half of all answerable questions over a formatting habit, while
their citations resolve perfectly well.

### Retrieval: zero, and the dataset has drifted

`recall_at_5 = 0.000` after 157 documents and 1100 units — worse than the 0.036 the 19-document
corpus produced, and that is not a paradox. Recall is graded by whether a retrieved unit covers the
expected **product, category and heading path**, and Atlassian has renamed the pages the gold set
was written against. Measured against the live corpus: of the 25 expected heading paths, **6 are
present and 19 no longer exist under that wording** — "service space agents" is now "service
project agents", "Delete, restore, or purge a content item" is now "…a page". The gold set is
version 1.2.0 and its labels are stale; the metric is faithfully reporting that the expected
targets are not in the corpus.

That is a dataset problem, not a retrieval one, and it is **not fixed here** — editing the gold
set's expected labels to match today's corpus would be moving the goalposts to make a number look
better. It is stated so the number can be read correctly.

### What this run does establish

- **all four deliberately-unsupported questions were refused** (`unsupported_refusal_rate = 1.000`),
  including the version question that a previous corpus answered from a page breadcrumb;
- **zero invalid citations** — every citation the model issued resolved to evidence that was
  actually served (`citation_validity = 1.000`);
- cross-product coverage 1.000: every cross-product question consulted at least two products.

### What is wrong with the system, not the corpus

**`p95_latency_ms = 32 500`** against an 8000 ms target, with p50 at 8093 ms. Two causes, both
real: the corpus is now 1100 units rather than 149, and 5 of 32 questions raised rather than
answered because the provider's quota was exhausted mid-run. A rate-limited provider is an
environment fact, not a defect, and it is recorded as `error_rate` rather than averaged away.

### Earlier runs, for comparison

| Run | Corpus | Outcome | Notable |
|---|---|---|---|
| `dfa3a1fd` (2026-10-07) | 19 docs / 149 units | fail | All questions refused; no generation ran, so the absolute zeros were untested |
| `b6d452a3` (2026-10-09) | 19 docs / 149 units | fail | Same, inside the container image |
| `68350c4a` (2026-10-09) | 132 docs / 841 units | fail | **Navigation was still in the corpus**: one published answer asserted an API version, grounded in a breadcrumb containing the string `REST API v2`. Ten questions errored on provider rate limits. |
| `fb88cffa` (2026-10-09) | 157 docs / 1100 units | fail | After the navigation filter and the fingerprint fix. That version question now **refuses**. Three uncited claims remain — the finding above. |

The progression matters more than any single row: the absolute zeros looked green for two runs
simply because nothing was answered, and only became measurable once the corpus was large enough
for the pipeline to actually generate.

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