# Enterprise Knowledge Intelligence RAG

An evidence-grounded RAG system over publicly available Atlassian documentation. It ingests pages by
URL at run time, answers only from retrieved evidence, cites every substantive claim with a link back
to the original page, and refuses when the evidence is thin. Citations are validated in code, never
trusted to the model.

**Independent technical demonstration.** Not affiliated with, sponsored by, or endorsed by Atlassian
or any documentation publisher. No third-party page content is stored in this repository. MIT
licensed — see [LICENSE](LICENSE).

---

## What it does, and what it refuses to do

Ask a question about the documentation you have indexed. The system searches only the pages you
registered, reranks the candidates, selects evidence, and either answers with a citation for every
substantive claim or refuses and says what it could not support.

It will not answer from memory, from a URL the model produced, or from a page you did not register.
A thin answer with visible citations is the failure mode this system is built to avoid.

```bash
curl -s -X POST localhost:8000/api/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"question": "How do I create a JQL filter?"}' | jq
```

---

## Running it

Requires Docker, and two API keys: **Groq** (generation) and **Pinecone** (retrieval).

```bash
cp .env.example .env      # then set GROQ_API_KEY and PINECONE_API_KEY
make up
```

| | |
|---|---|
| UI | http://localhost:3000 |
| API | http://localhost:8000/api/v1 |
| Health | http://localhost:8000/api/v1/health |

The backend exits at startup naming any missing key — deliberately, rather than starting degraded.
`make logs` follows the stack; `make down` stops it and keeps the database.

### Seed a corpus before anything is answerable

The system answers only from pages you have indexed. Register a source and its first crawl starts
immediately:

```bash
curl -s -X POST localhost:8000/api/v1/sources \
  -H 'Content-Type: application/json' \
  -d '{"name": "Atlassian support knowledge base",
       "start_url": "https://support.atlassian.com/",
       "product_domain": "jira",
       "max_pages": 25, "max_depth": 2, "delay_seconds": 0.4}'
```

Watch it at http://localhost:3000/sources, where each source shows live page and unit counts and
every crawl job shows what was discovered, read, skipped, and failed.

### Development without Docker

```bash
make venv && docker compose up -d postgres && make migrate
make dev-backend      # uvicorn with reload on :8000
make dev-frontend     # Next dev server on :3000
```

---

## The demonstration walkthrough

Five views, in the order a reviewer should meet them.

1. **`/` — ask a question.** Submit one and watch eight ordered progress events stream back
   (`query_received` → … → `answer_completed`). The answer carries numbered citations; click one and
   it resolves to the source section. Ask something the corpus cannot support and you get a refusal
   with the reason and the best passages found — not a confident wrong answer.
2. **`/sources` — the corpus console.** Every registered source with its product, live page and unit
   counts, last crawl time and status, and per-source actions. The crawl history below answers *why*
   a crawl indexed fewer pages than it discovered, with skip reasons broken out.
3. **`/documents` → a document — provenance.** Browse and filter what is indexed; every row links to
   the page it came from. The detail view shows product, category, page type, language, fingerprint,
   lifecycle state, and the unit inspector: the ordinals, heading paths, and token sizes the chunker
   produced. That last table is the direct visual check that page reconstruction worked.
4. **`/evaluations` — measured quality.** Start a run and watch it execute the 32-question gold set
   through the same pipeline a user hits. The metrics are grouped by what they measure, every number
   carries the run id and timestamp that produced it, and the gate renders as `pass` or `fail` with
   each failing gate named. Before any run exists the page says so and shows no number at all.
5. **`/settings` — what this instance is.** Effective configuration, counted corpus, dependency
   health reported independently, and secret **presence** only.

### Measured results

The current evaluation runs **fail their gates**, and are reported that way.

**Passing:** zero fabricated facts and zero invalid citations — the absolute-zero
gates, which now hold against answers rather than against a corpus too thin to
produce any; all four deliberately-unsupported questions refused; citation
validity 1.000; cross-product coverage 1.000.

**Failing:** recall@5 / precision@5 / MRR, claim coverage, and p95 latency.

The retrieval zeros are structural, not a ranking result, and each cause was
measured: the gold set expected document categories the system's six-value
vocabulary cannot produce; the publisher had renamed 19 of the 25 expected
heading paths; and a confident filter was cutting the Confluence corpus from 33
pages to a single candidate. The first two are fixed (FR-063 amended, dataset
1.3.0 audited) and the third is fixed (a filter that leaves less than the
minimum evidence count now widens). **None of the three fixes has yet had a clean
run to measure it** — the provider quota was exhausted — so the retrieval gates
have not been shown to pass, and this README does not claim they do.

The numbers, the evidence for each cause, and what remains unmeasured are in
[docs/EVALUATION.md](docs/EVALUATION.md) §1.

---

## Documentation

| | |
|---|---|
| [Architecture](docs/ARCHITECTURE.md) | How the pipeline is built and why |
| [Data source policy](docs/DATA_SOURCE_POLICY.md) | What is collected, what never is, crawling conduct, deletion |
| [Evaluation guide](docs/EVALUATION.md) | Running the suite, the metrics, reading a failing gate |
| [API reference](docs/API.md) | Generated from the contract, which is normative |
| [Quickstart](specs/001-enterprise-knowledge-rag/quickstart.md) | The fourteen validation scenarios |
| [Specification](specs/001-enterprise-knowledge-rag/spec.md) | Requirements and acceptance criteria |

---

## Checks

```bash
make check              # data validation, hygiene, API-doc freshness, lint, mypy, unit + contract
make test-all           # everything, including integration and e2e
```

The integration suite runs against the same development database the application uses, so a test run
empties a corpus that took a crawl to build. Re-register your sources afterwards.

---

## Layout

```
backend/     FastAPI application, retrieval and ingestion pipeline, evaluation runner
frontend/    Next.js UI: ask, sources, documents, evaluation, settings
evaluation/  The curated 32-question gold set
specs/       Specification, plan, tasks, data model, and the normative contracts
docs/        Architecture, policy, evaluation guide, generated API reference
```

MIT licensed. Independent demonstration; not affiliated with Atlassian.