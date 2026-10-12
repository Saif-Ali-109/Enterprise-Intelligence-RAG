import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

/**
 * T168 / FR-059, SC-013: every view is operable by keyboard and exposes no
 * automated accessibility violation.
 *
 * **This is a real audit of the real views, with the API stubbed.** Stubbing
 * changes what is on the page, not how the page is built: axe analyses the
 * rendered DOM, and the DOM here is the production build's. A live corpus would
 * make the test's result depend on how many pages a crawl happened to index,
 * which is not what is being checked.
 *
 * **The rules are not filtered.** No `disableRules`, no tag exclusions, no
 * `.exclude()`. A filtered axe run reports the number the team chose to look at,
 * which is the number that makes an accessibility test decorative. Where a rule
 * was genuinely inapplicable to this application the fix belongs in the markup,
 * not in this file — and colour contrast in particular is checked against the
 * actual computed styles, so it cannot be talked out of a failure.
 *
 * **Every view is covered, including the states that are easy to forget**: an
 * answered question with citations, a refusal with leads, the empty evaluation
 * state, and a degraded dependency on the settings view. Those are the states a
 * real user meets, and they are where a missing label or a broken heading order
 * actually hides.
 */

const SOURCE = {
  id: "11111111-1111-4111-8111-111111111111",
  name: "Jira Cloud administration docs",
  start_url: "https://support.atlassian.com/jira-cloud-administration/",
  allowed_domains: ["support.atlassian.com"],
  product_domain: "jira",
  enabled: true,
  status: "active",
  page_count: 42,
  unit_count: 611,
  last_crawl_at: "2026-10-09T18:10:00Z",
  last_crawl_status: "completed",
  created_at: "2026-10-05T09:00:00Z",
};

const SOURCES = {
  items: [SOURCE],
  total: 1,
  limit: 25,
  offset: 0,
};

const DOCUMENT = {
  id: "33333333-3333-4333-8333-333333333333",
  source_id: SOURCE.id,
  url: "https://support.atlassian.com/jira-cloud-administration/docs/filter-manager/",
  canonical_url: "https://support.atlassian.com/jira-cloud-administration/docs/filter-manager/",
  title: "Filter manager",
  product: "jira",
  category: "filters",
  page_type: "documentation",
  language: "en",
  state: "indexed",
  state_detail: null,
  vectors_live: true,
  unit_count: 3,
  content_fingerprint: "abc123",
  source_modified_at: null,
  last_crawled_at: "2026-10-09T18:09:00Z",
  indexed_at: "2026-10-09T18:09:00Z",
  tombstoned_at: null,
};

const UNITS = {
  items: [
    {
      id: "u1",
      document_id: DOCUMENT.id,
      ordinal: 0,
      vector_id: `${DOCUMENT.id}#0000`,
      heading_path: ["Filter manager", "Saving a filter"],
      block_types: ["heading", "paragraph"],
      token_count: 742,
      overlap_tokens: 0,
      text_fingerprint: "fp0",
    },
    {
      id: "u2",
      document_id: DOCUMENT.id,
      ordinal: 1,
      vector_id: `${DOCUMENT.id}#0001`,
      heading_path: ["Filter manager", "Sharing a filter"],
      block_types: ["paragraph"],
      token_count: 610,
      overlap_tokens: 50,
      text_fingerprint: "fp1",
    },
  ],
  total: 3,
  limit: 100,
  offset: 0,
};

const CONFIG = {
  retrieval: {
    candidate_pool: 12,
    rerank_top_n: 6,
    evidence_min: 3,
    evidence_max: 6,
    filters_enabled: true,
  },
  generation: {
    provider: "groq",
    model: "openai/gpt-oss-120b",
    classification_model: "openai/gpt-oss-20b",
    max_attempts: 2,
    reasoning_exposed: false,
  },
  index: {
    name: "enterprise-knowledge",
    namespace: "atlassian-public",
    embed_model: "llama-text-embed-v2",
    dimension_source: "service",
    rerank_model: "bge-reranker-v2-m3",
  },
  corpus: {
    source_count: 1,
    document_count: 42,
    unit_count: 611,
    allowed_domains: ["support.atlassian.com"],
  },
  secrets: {
    pinecone_api_key: { configured: true },
    groq_api_key: { configured: true },
    database_url: { configured: true },
  },
};

const HEALTH = {
  status: "ok",
  version: "1.0.0",
  dependencies: {
    database: { status: "ok", detail: null, latency_ms: 3 },
    vector_store: { status: "ok", detail: null, latency_ms: 88 },
    language_model: { status: "ok", detail: null, latency_ms: 410 },
  },
};

const JOBS = {
  items: [
    {
      id: "j1",
      source_id: SOURCE.id,
      target_url: SOURCE.start_url,
      status: "completed",
      requested_at: "2026-10-09T18:05:00Z",
      started_at: "2026-10-09T18:05:01Z",
      finished_at: "2026-10-09T18:10:00Z",
      counters: { discovered: 42, processed: 42, unchanged: 0, skipped: 0, failed: 0 },
      skipped_reasons: null,
      error_code: null,
      error_message: null,
    },
  ],
  total: 1,
  limit: 20,
  offset: 0,
};

const EMPTY_RUNS = { items: [], total: 0, limit: 20, offset: 0 };

const RUN = {
  id: "b6d452a3-0072-429a-b794-64b519ea06af",
  status: "completed",
  dataset_version: "1.2.0",
  dataset_fingerprint: "68d76c26e6fc063c",
  started_at: "2026-10-09T17:24:37Z",
  finished_at: "2026-10-09T17:26:47Z",
  metrics: {
    recall_at_5: 0.0,
    precision_at_5: 0.0,
    mrr: 0.0,
    cross_product_domain_coverage: 1.0,
    citation_validity: null,
    claim_coverage: null,
    faithfulness: null,
    unsupported_refusal_rate: 1.0,
    fabricated_fact_count: 0,
    invalid_citation_count: 0,
    p50_latency_ms: 3311,
    p95_latency_ms: 8578,
    error_rate: 0.0,
  },
  thresholds: {
    recall_at_5: 0.8,
    precision_at_5: 0.6,
    mrr: 0.75,
    cross_product_domain_coverage: 0.8,
    citation_validity: 0.98,
    claim_coverage: 0.85,
    faithfulness: 0.9,
    unsupported_refusal_rate: 1.0,
    p95_latency_ms: 8000,
  },
  gate_outcome: "fail",
  gate_failures: [
    { gate: "recall_at_5", target: 0.8, actual: 0.0 },
    { gate: "p95_latency_ms", target: 8000, actual: 8578 },
  ],
  question_count: 32,
};

/** An answered question: the state with the most interactive and text content. */
const ANSWERED = [
  'data: {"request_id":"r1","question":"How do I save a filter?","inspect":false}\n\n',
  'event: query_received\ndata: {"request_id":"r1","question":"How do I save a filter?","inspect":false}\n\n',
  'event: query_analyzed\ndata: {"request_id":"r1","detected_product":"jira","detected_category":"filters","intent":"how_to","entities":["filter"],"classification_confidence":{"product":0.95,"category":0.9,"intent":0.9,"entities":0.8},"rewrite_queries":["How do I save a filter?"],"applied_filters":{"product":"jira","category":"filters"},"filters_suppressed":false,"ambiguity_note":null}\n\n',
  'event: retrieval_started\ndata: {"request_id":"r1","candidate_pool":12,"query_count":1}\n\n',
  'event: retrieval_completed\ndata: {"request_id":"r1","candidates_retrieved":12,"queries_executed":[{"query":"How do I save a filter?","returned":12,"top_score":0.83}]}\n\n',
  'event: reranking_completed\ndata: {"request_id":"r1","candidates_reranked":5,"rerank_model":"bge-reranker-v2-m3"}\n\n',
  'event: generation_started\ndata: {"request_id":"r1","evidence_count":5,"attempt":1}\n\n',
  'event: citation_validation\ndata: {"request_id":"r1","citations_valid":2,"citations_stripped":0,"citations_repaired":0,"rejected_identifiers":[]}\n\n',
  'event: answer_completed\ndata: {"request_id":"r1","outcome":"answered","answer":"Open the filter manager and choose Save as filter. A saved filter can be run again by name from the same screen. [1]","citations":[{"rank":1,"document_id":"d1","unit_id":"u1","source_url":"https://support.atlassian.com/jira-cloud-administration/docs/filter-manager/","title":"Filter manager","product":"jira","category":"filters","heading_path":["Filter manager","Saving a filter"],"quote":"To save a filter, open the filter manager…","retrieval_score":0.83,"rerank_score":0.91,"validation_state":"valid"},{"rank":2,"document_id":"d2","unit_id":"u2","source_url":"https://support.atlassian.com/jira-cloud-administration/docs/filter-sharing/","title":"Sharing a filter","product":"jira","category":"filters","heading_path":["Sharing a filter"],"quote":"A saved filter can be reused…","retrieval_score":0.71,"rerank_score":0.74,"validation_state":"valid"}],"total_ms":1810}\n\n',
  "data: [DONE]\n\n",
].join("");

/** A refusal: the other terminal state, with leads rather than citations. */
const REFUSED = [
  'data: {"request_id":"r2","question":"How much is the Premium plan?","inspect":false}\n\n',
  'event: query_received\ndata: {"request_id":"r2","question":"How much is the Premium plan?","inspect":false}\n\n',
  'event: query_analyzed\ndata: {"request_id":"r2","detected_product":"jira","detected_category":"billing","intent":"pricing","entities":[],"classification_confidence":{"product":0.4,"category":0.3,"intent":0.6,"entities":0.1},"rewrite_queries":["How much is the Premium plan?"],"applied_filters":{},"filters_suppressed":false,"ambiguity_note":null}\n\n',
  'event: retrieval_started\ndata: {"request_id":"r2","candidate_pool":12,"query_count":1}\n\n',
  'event: retrieval_completed\ndata: {"request_id":"r2","candidates_retrieved":12,"queries_executed":[{"query":"How much is the Premium plan?","returned":12,"top_score":0.21}]}\n\n',
  'event: reranking_completed\ndata: {"request_id":"r2","candidates_reranked":6,"rerank_model":"bge-reranker-v2-m3"}\n\n',
  'event: generation_started\ndata: {"request_id":"r2","evidence_count":0,"attempt":1}\n\n',
  'event: citation_validation\ndata: {"request_id":"r2","citations_valid":0,"citations_stripped":0,"citations_repaired":0,"rejected_identifiers":[]}\n\n',
  'event: answer_completed\ndata: {"request_id":"r2","outcome":"refused","refusal_reason":"INSUFFICIENT_EVIDENCE","answer":null,"citations":[],"leads":[{"source_url":"https://support.atlassian.com/jira-cloud-administration/docs/filter-manager/","title":"Filter manager","heading_path":["Filter manager"],"quote":"","relevance":0.21,"insufficient":true}],"total_ms":940}\n\n',
  "data: [DONE]\n\n",
].join("");

async function stubCorpus(page: Page) {
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const body = (() => {
      if (path.endsWith("/sources")) return SOURCES;
      if (path.endsWith("/documents")) return { items: [DOCUMENT], total: 1, limit: 25, offset: 0 };
      if (path.endsWith("/units")) return UNITS;
      if (path.endsWith("/crawl-jobs")) return JOBS;
      if (path.endsWith("/config")) return CONFIG;
      if (path.endsWith("/health")) return HEALTH;
      if (/\/evaluations\/runs$/.test(path)) return { items: [RUN], total: 1, limit: 20, offset: 0 };
      if (/\/evaluations\/dataset/.test(path)) {
        return { dataset_version: "1.2.0", dataset_fingerprint: "68d76c26", question_count: 32 };
      }
      if (/\/documents\/[0-9a-f-]+$/.test(path)) return DOCUMENT;
      return null;
    })();
    if (body === null) {
      await route.fulfill({ status: 404, json: { error: { code: "NOT_FOUND", message: "not stubbed" } } });
      return;
    }
    await route.fulfill({ json: body });
  });
}

async function stubStream(page: Page, body: string) {
  await page.route("**/api/chat/stream", async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream; charset=utf-8" },
      body,
    });
  });
}

/**
 * Run axe and assert a clean result.
 *
 * The failure message names the rule and the offending nodes rather than dumping
 * a violation count, because "3 violations" does not tell anyone what to fix and
 * a test that fails without naming the element gets disabled.
 */
async function expectNoViolations(page: Page, where: string) {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();

  const violations = results.violations.map((violation) => ({
    id: violation.id,
    impact: violation.impact,
    help: violation.help,
    // axe's own explanation carries the measured contrast ratio and the expected
    // threshold. Without it a colour-contrast failure says only "it failed",
    // which is not enough to fix and is therefore how these get disabled.
    detail: violation.nodes[0]?.failureSummary?.split("\n").slice(1).join(" ").trim() ?? "",
    nodes: violation.nodes.slice(0, 3).map((node) => node.target.join(" ")),
  }));

  expect(violations, `accessibility violations on ${where}`).toEqual([]);
}

/**
 * Run the whole set of view checks in one colour scheme.
 *
 * Both schemes matter: the tokens are declared per scheme in `globals.css`, and
 * a defect there is invisible in the other scheme. This was not theoretical —
 * `@theme` inside a media query emitted an unconditional second `:root`, so
 * every light-scheme visitor was served the dark palette, and the axe pass
 * caught the stock light-mode status colours drawn on top of it.
 */
const SCHEMES = ["light", "dark"] as const;

for (const colorScheme of SCHEMES) {
  test.describe(`in the ${colorScheme} colour scheme`, () => {
    test.use({ colorScheme });

    test("the ask view is clean before a question is submitted", async ({ page }) => {
      await stubCorpus(page);
      await page.goto("/");
      await expect(page.locator("#question")).toBeVisible();
      await expectNoViolations(page, `/ (${colorScheme})`);
    });

    test("an answered question with two citations is clean", async ({ page }) => {
      await stubCorpus(page);
      await stubStream(page, ANSWERED);

      await page.goto("/");
      await page.fill("#question", "How do I save a filter?");
      await page.click("button[type=submit]");
      await expect(page.locator("#answer-heading")).toBeVisible({ timeout: 30_000 });

      await expectNoViolations(page, `/ with an answer and two citations (${colorScheme})`);
    });

    test("a refusal with leads is clean", async ({ page }) => {
      await stubCorpus(page);
      await stubStream(page, REFUSED);

      await page.goto("/");
      await page.fill("#question", "How much is the Premium plan?");
      await page.click("button[type=submit]");
      await expect(page.getByRole("alert", { name: "Outcome" })).toContainText(/No answer/, {
        timeout: 30_000,
      });

      await expectNoViolations(page, `/ with a refusal and leads (${colorScheme})`);
    });

    test("the sources console is clean", async ({ page }) => {
      await stubCorpus(page);
      await page.goto("/sources");
      await expect(page.getByRole("table", { name: /Registered sources/i })).toBeVisible();
      await expectNoViolations(page, `/sources (${colorScheme})`);
    });

    test("the documents browser is clean", async ({ page }) => {
      await stubCorpus(page);
      await page.goto("/documents");
      await expect(page.getByRole("table")).toBeVisible();
      await expectNoViolations(page, `/documents (${colorScheme})`);
    });

    test("the document detail and unit inspector are clean", async ({ page }) => {
      await stubCorpus(page);
      await page.goto(`/documents/${DOCUMENT.id}`);
      await expect(page.getByRole("heading", { name: DOCUMENT.title })).toBeVisible();
      await expectNoViolations(page, `/documents/[id] (${colorScheme})`);
    });

    test("the evaluation dashboard with a failing gate is clean", async ({ page }) => {
      await stubCorpus(page);
      await page.goto("/evaluations");
      await expect(page.getByText(/fail/i).first()).toBeVisible();
      await expectNoViolations(page, `/evaluations with a run (${colorScheme})`);
    });

    test("the empty evaluation state is clean", async ({ page }) => {
      await stubCorpus(page);
      await page.route("**/api/v1/evaluations/runs**", async (route) => {
        const path = new URL(route.request().url()).pathname;
        if (path.endsWith("/evaluations/runs")) {
          await route.fulfill({ json: EMPTY_RUNS });
          return;
        }
        await route.fallback();
      });

      await page.goto("/evaluations");
      await expect(
        page.getByRole("heading", { name: /no evaluation has been run/i }),
      ).toBeVisible();
      // The empty state must also render nothing that looks like a measurement
      // (FR-037) — asserted here because this is the state where that mistake
      // would hide, and a11y and honesty are checked in the same pass.
      await expect(page.locator("text=/\\b0\\s*%/")).toHaveCount(0);
      await expectNoViolations(page, `/evaluations with no run (${colorScheme})`);
    });

    test("the settings view, with dependencies reported, is clean", async ({ page }) => {
      await stubCorpus(page);
      await page.goto("/settings");
      await expect(page.getByRole("heading", { name: /configuration/i }).first()).toBeVisible();
      await expectNoViolations(page, `/settings (${colorScheme})`);
    });

    /**
     * Error states carry the most status colour and the most `role="alert"`, and
     * they are the states no happy-path pass ever renders. A registry that fails
     * to load is a normal Tuesday, so it gets the same audit as the loaded page.
     */
    test("the error state of the sources console is clean", async ({ page }) => {
      await page.route("**/api/v1/**", async (route) => {
        const path = new URL(route.request().url()).pathname;
        if (path.endsWith("/sources")) {
          await route.fulfill({
            status: 503,
            json: {
              error: {
                code: "VECTOR_SERVICE_UNAVAILABLE",
                message: "The vector service is unreachable.",
                request_id: "0f8d2a1c-9b44-4c7d-8e55-6d3a1b2c4e90",
                details: null,
              },
            },
          });
          return;
        }
        await route.fallback();
      });
      await page.route("**/api/v1/sources**", (route) => route.fallback());

      await page.goto("/sources");
      await expect(page.getByRole("alert").first()).toBeVisible();
      await expectNoViolations(page, `/sources with a failed load (${colorScheme})`);
    });

    test("the inspection trace view is clean", async ({ page }) => {
      await stubCorpus(page);
      await stubStream(page, ANSWERED);

      await page.goto("/");
      await page.check("input[type=checkbox]");
      await page.fill("#question", "How do I save a filter?");
      await page.click("button[type=submit]");
      await expect(page.getByRole("heading", { name: "Pipeline trace" })).toBeVisible({
        timeout: 30_000,
      });

      await expectNoViolations(page, `/ with the inspection trace (${colorScheme})`);
    });
  });
}

/**
 * The scheme the tokens declare is the scheme the browser gets.
 *
 * This is the assertion that would have caught the `@theme`-inside-a-media-query
 * defect directly, rather than through three downstream contrast failures: a
 * light-scheme browser must render a light background and dark text.
 */
/**
 * Perceptual lightness of a computed colour, 0 (black) to 1 (white).
 *
 * Chromium serialises `oklch()`/`lab()` back in that notation rather than as
 * `rgb()`, and the two number the lightness differently — `oklch` L is 0…1 while
 * `lab` L is 0…100. Reading the first number out of either as if it were a red
 * channel made this test fail against a correct light scheme, which is the
 * failure mode of a test written to match the bug it found.
 */
function lightness(value: string): number {
  const first = Number(value.match(/-?\d*\.?\d+/)?.[0] ?? "0");
  if (/^oklch/i.test(value)) return first;
  if (/^lab/i.test(value)) return first / 100;
  return first / 255;
}

test("the declared colour scheme is the one the browser is given", async ({ page }) => {
  await page.goto("/settings");
  const resolved = await page.evaluate(() => ({
    dark: matchMedia("(prefers-color-scheme: dark)").matches,
    background: getComputedStyle(document.body).backgroundColor,
    foreground: getComputedStyle(document.body).color,
  }));

  expect(lightness(resolved.background) < 0.5).toBe(resolved.dark);
  expect(lightness(resolved.foreground) > 0.5).toBe(resolved.dark);
});