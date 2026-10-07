import { expect, test, type Page } from "@playwright/test";

/**
 * The corpus console (T155–T161), asserted in a browser.
 *
 * These views exist so an operator can answer four questions about their own
 * corpus: what is registered, what is searchable, what a citation resolves to,
 * and whether the services are up. Each question is a test below, and each is
 * asserted against a stubbed API — what is under test is what the console
 * *shows*, while the corpus API's own promises live in
 * `test_corpus_views.py`.
 */

const SOURCE = {
  id: "11111111-1111-4111-8111-111111111111",
  name: "Jira Cloud administration",
  start_url: "https://support.atlassian.com/jira-cloud-administration/docs/",
  allowed_domains: ["support.atlassian.com"],
  product_domain: "jira",
  enabled: true,
  status: "active",
  page_count: 2,
  unit_count: 6,
  last_crawl_at: "2026-10-06T09:00:00Z",
  last_crawl_status: "completed",
  created_at: "2026-10-05T09:00:00Z",
};

/** A source whose pages are all gone: it must stay listed (edge case 16). */
const EMPTY_SOURCE = {
  ...SOURCE,
  id: "22222222-2222-4222-8222-222222222222",
  name: "Retired section",
  page_count: 0,
  unit_count: 0,
  last_crawl_at: null,
  last_crawl_status: null,
};

const SOURCES = { items: [SOURCE, EMPTY_SOURCE], total: 2, limit: 25, offset: 0 };

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
  last_crawled_at: "2026-10-06T08:59:00Z",
  indexed_at: "2026-10-06T09:00:00Z",
  tombstoned_at: null,
  created_at: "2026-10-05T09:00:00Z",
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
    name: "atlassian-public",
    namespace: "atlassian-public",
    embed_model: "llama-text-embed-v2",
    dimension_source: "service",
    rerank_model: "bge-reranker-v2-m3",
  },
  corpus: {
    source_count: 2,
    document_count: 2,
    unit_count: 6,
    allowed_domains: ["support.atlassian.com"],
  },
  secrets: {
    pinecone_api_key: { configured: true },
    groq_api_key: { configured: true },
    database_url: { configured: true },
  },
};

const HEALTH = {
  status: "degraded",
  version: "1.0.0",
  dependencies: {
    database: { status: "ok", detail: null, latency_ms: 3 },
    vector_store: { status: "unavailable", detail: "the index did not respond", latency_ms: null },
    language_model: { status: "ok", detail: null, latency_ms: 410 },
  },
};

const JOBS = {
  items: [
    {
      id: "j1",
      source_id: SOURCE.id,
      target_url: SOURCE.start_url,
      status: "completed_with_errors",
      requested_at: "2026-10-06T08:55:00Z",
      started_at: "2026-10-06T08:55:01Z",
      finished_at: "2026-10-06T08:59:00Z",
      counters: { discovered: 5, processed: 2, unchanged: 1, skipped: 1, failed: 2 },
      skipped_reasons: { robots_disallowed: 1 },
      error_code: null,
      error_message: null,
    },
  ],
  total: 1,
  limit: 20,
  offset: 0,
};

/** One route, one switch: see evaluations.spec.ts for why. */
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

test("the sources console lists identity, counts, and last crawl", async ({ page }) => {
  await stubCorpus(page);
  await page.goto("/sources");

  const table = page.getByRole("table", { name: /Registered sources/i });
  await expect(table).toBeVisible();
  await expect(table.getByRole("cell", { name: "Jira Cloud administration" })).toBeVisible();
  // `.first()`: both rows are jira products, and this asserts the column is
  // populated rather than which row it belongs to.
  await expect(table.getByRole("cell", { name: "jira" }).first()).toBeVisible();

  // A source with no pages is still a row, and says "never crawled" rather
  // than claiming a successful crawl.
  await expect(table.getByRole("cell", { name: "Retired section" })).toBeVisible();
  await expect(table.getByText("never crawled")).toBeVisible();
});

test("removing a source asks first", async ({ page }) => {
  await stubCorpus(page);
  await page.goto("/sources");
  const row = page.getByRole("row", { name: /Jira Cloud administration/ });
  await row.getByRole("button", { name: "Remove" }).click();
  await expect(page.getByText("Stop indexing this source?")).toBeVisible();
  await row.getByRole("button", { name: "Keep" }).click();
  await expect(page.getByText("Stop indexing this source?")).toBeHidden();
});

test("crawl history shows per-job counters and error detail", async ({ page }) => {
  await stubCorpus(page);
  await page.goto("/sources");
  const table = page.getByRole("table", { name: /Crawl jobs/i });
  await expect(table).toBeVisible();
  await expect(table.getByRole("cell", { name: "completed_with_errors" })).toBeVisible();
  await expect(table.getByText("robots_disallowed: 1")).toBeVisible();
});

test("the documents browser lists provenance and links to each origin", async ({ page }) => {
  await stubCorpus(page);
  await page.goto("/documents");
  await expect(page.getByRole("cell", { name: "Filter manager" })).toBeVisible();
  await expect(
    page.getByRole("link", { name: DOCUMENT.url }),
  ).toHaveAttribute("href", DOCUMENT.url);
  await expect(page.getByRole("link", { name: "Inspect" })).toHaveAttribute(
    "href",
    `/documents/${DOCUMENT.id}`,
  );
});

test("the unit inspector shows ordinals, heading paths, and token sizes", async ({ page }) => {
  await stubCorpus(page);
  await page.goto(`/documents/${DOCUMENT.id}`);

  await expect(page.getByRole("heading", { name: "Filter manager" })).toBeVisible();
  await expect(page.getByText("abc123")).toBeVisible();
  const units = page.getByRole("table", { name: /units in ordinal order/i });
  await expect(units).toBeVisible();
  await expect(units.getByText("Filter manager › Saving a filter")).toBeVisible();
  await expect(units.getByText("742")).toBeVisible();
  await expect(units.getByText("610")).toBeVisible();
});

test("the settings view counts the corpus and reports secrets as presence", async ({ page }) => {
  await stubCorpus(page);
  await page.goto("/settings");

  // Config: counted corpus, effective values, presence-only secrets.
  await expect(page.getByRole("heading", { name: "Corpus (counted from live rows)" })).toBeVisible();
  await expect(page.getByText("llama-text-embed-v2")).toBeVisible();
  // `.last()`: the settings page wraps each panel in a page-level section, so
  // the filter matches the wrapper and the panel it wraps.
  const secrets = page
    .locator("section")
    .filter({ has: page.getByRole("heading", { name: /Secrets/ }) })
    .last();
  await expect(secrets).toContainText("configured");
  const secretsText = (await secrets.textContent()) ?? "";
  expect(secretsText).not.toMatch(/pcsk|gsk_/i);
  await expect(page.getByText("false", { exact: true }).first()).toBeVisible();
});

test("health reports each dependency independently", async ({ page }) => {
  await stubCorpus(page);
  await page.goto("/settings");

  await expect(page.getByText("Overall status:")).toBeVisible();
  await expect(page.getByText("unavailable")).toBeVisible();
  await expect(page.getByText("the index did not respond")).toBeVisible();
  await expect(page.getByText("latency: not measured")).toBeVisible();
});