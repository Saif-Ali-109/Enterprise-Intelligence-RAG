import { expect, test, type Page } from "@playwright/test";

/**
 * T170 / FR-057, SC-022: the semantics a screen reader depends on, asserted in a
 * browser rather than assumed from JSX.
 *
 * **axe cannot check any of this.** A `<div>` soup with the right colours and the
 * right `lang` passes every automated rule in `accessibility.spec.ts` and is
 * still unusable with a screen reader. What follows is the structural half of
 * accessibility: landmarks, heading order, names, table headers, and the two live
 * regions carrying the right politeness.
 *
 * **The failures these catch are invisible to the other tests here.** A skipped
 * heading level is a `h2` after an `h4`; a table without headers is a set of
 * `<td>`s; a landmark-less page is three `<section>`s. All of them render
 * perfectly. All of them cost a reader the ability to navigate.
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

const SOURCES = { items: [SOURCE], total: 1, limit: 25, offset: 0 };

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
  total: 2,
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

/** An answered question, for the states a page at rest never renders. */
const CANNED_ANSWER = [
  'event: query_received\ndata: {"request_id":"r1","question":"q","inspect":false}\n\n',
  'event: query_analyzed\ndata: {"request_id":"r1","detected_product":"jira","detected_category":"filters","intent":"how_to","entities":[],"classification_confidence":{"product":0.9,"category":0.9,"intent":0.9,"entities":0.5},"rewrite_queries":["q"],"applied_filters":{"product":"jira"},"filters_suppressed":false,"ambiguity_note":null}\n\n',
  'event: retrieval_started\ndata: {"request_id":"r1","candidate_pool":12,"query_count":1}\n\n',
  'event: retrieval_completed\ndata: {"request_id":"r1","candidates_retrieved":12,"queries_executed":[{"query":"q","returned":12,"top_score":0.83}]}\n\n',
  'event: reranking_completed\ndata: {"request_id":"r1","candidates_reranked":5,"rerank_model":"bge-reranker-v2-m3"}\n\n',
  'event: generation_started\ndata: {"request_id":"r1","evidence_count":5,"attempt":1}\n\n',
  'event: citation_validation\ndata: {"request_id":"r1","citations_valid":2,"citations_stripped":0,"citations_repaired":0,"rejected_identifiers":[]}\n\n',
  'event: answer_completed\ndata: {"request_id":"r1","outcome":"answered","answer":"Open the filter manager and choose Save as filter. [1]","citations":[{"rank":1,"document_id":"d1","unit_id":"u1","source_url":"https://support.atlassian.com/jira-cloud-administration/docs/filter-manager/","title":"Filter manager","product":"jira","category":"filters","heading_path":["Filter manager","Saving a filter"],"quote":"To save a filter…","retrieval_score":0.83,"rerank_score":0.91,"validation_state":"valid"},{"rank":2,"document_id":"d2","unit_id":"u2","source_url":"https://support.atlassian.com/jira-cloud-administration/docs/filter-sharing/","title":"Sharing a filter","product":"confluence","category":"filters","heading_path":["Sharing a filter"],"quote":"A saved filter…","retrieval_score":0.71,"rerank_score":0.74,"validation_state":"valid"}],"total_ms":1810}\n\n',
  "data: [DONE]\n\n",
].join("");

/** A refusal: the other terminal state. */
const CANNED_REFUSAL = [
  'event: query_received\ndata: {"request_id":"r2","question":"q","inspect":false}\n\n',
  'event: query_analyzed\ndata: {"request_id":"r2","detected_product":"jira","detected_category":"billing","intent":"pricing","entities":[],"classification_confidence":{"product":0.4,"category":0.3,"intent":0.6,"entities":0.1},"rewrite_queries":["q"],"applied_filters":{},"filters_suppressed":false,"ambiguity_note":null}\n\n',
  'event: retrieval_started\ndata: {"request_id":"r2","candidate_pool":12,"query_count":1}\n\n',
  'event: retrieval_completed\ndata: {"request_id":"r2","candidates_retrieved":12,"queries_executed":[{"query":"q","returned":12,"top_score":0.21}]}\n\n',
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
      if (/\/evaluations\/runs$/.test(path)) return { items: [], total: 0, limit: 20, offset: 0 };
      if (/\/documents\/[0-9a-f-]+$/.test(path)) return DOCUMENT;
      return null;
    })();
    if (body === null) {
      await route.fulfill({
        status: 404,
        json: { error: { code: "NOT_FOUND", message: "not stubbed" } },
      });
      return;
    }
    await route.fulfill({ json: body });
  });
}

/** Heading levels in document order, as numbers. */
async function headingLevels(page: Page): Promise<number[]> {
  return page.evaluate(() =>
    Array.from(document.querySelectorAll("h1, h2, h3, h4, h5, h6")).map((h) =>
      Number(h.tagName.slice(1)),
    ),
  );
}

/**
 * No level is skipped going down.
 *
 * A jump from `h2` to `h4` tells a reader heading-mode navigation that a whole
 * level of the document is missing, and it usually means a heading was chosen for
 * its size rather than its place. Going *up* is fine and expected — a subsection
 * closing before its parent.
 */
function expectNoSkippedLevels(levels: number[], where: string) {
  for (let i = 1; i < levels.length; i += 1) {
    expect(
      levels[i] - levels[i - 1],
      `heading level jumped from h${levels[i - 1]} to h${levels[i]} on ${where}`,
    ).toBeLessThanOrEqual(1);
  }
}

test("every view declares its language", async ({ page }) => {
  for (const path of ["/", "/sources", "/documents", "/evaluations", "/settings"]) {
    await page.goto(path);
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
  }
});

test("every view has exactly one h1, and it names the view", async ({ page }) => {
  const expectations: Array<[string, RegExp]> = [
    ["/", /ask a question/i],
    ["/sources", /sources/i],
    ["/documents", /documents/i],
    ["/evaluations", /evaluation/i],
    ["/settings", /settings/i],
  ];

  for (const [path, name] of expectations) {
    await page.goto(path);
    await expect(page.locator("h1")).toHaveCount(1);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(name);
  }
});

test("the site name is a banner, not a second top-level heading", async ({ page }) => {
  // The shell's product name used to be an `h1` beside each view's own `h1`, so a
  // heading list offered the product name and the page name as equals.
  await page.goto("/sources");
  const h1s = await page.evaluate(() =>
    Array.from(document.querySelectorAll("h1")).map((h) => h.textContent?.trim()),
  );
  expect(h1s).toEqual(["Sources"]);
  await expect(page.getByText("Enterprise Knowledge Intelligence").first()).toBeAttached();
});

test("every view has main, and no second one", async ({ page }) => {
  for (const path of ["/", "/sources", "/documents", "/evaluations", "/settings"]) {
    await page.goto(path);
    await expect(page.getByRole("main")).toHaveCount(1);
  }
});

test("the disclaimer is present on every view, not only the ask page", async ({ page }) => {
  for (const path of ["/", "/sources", "/documents", "/evaluations", "/settings"]) {
    await page.goto(path);
    await expect(page.getByText(/not affiliated with, endorsed by, or sponsored by atlassian/i))
      .toBeVisible();
  }
});

test("heading levels never skip, on any view", async ({ page }) => {
  await stubCorpus(page);
  for (const path of ["/", "/sources", "/documents", "/evaluations", "/settings"]) {
    await page.goto(path);
    // A single heading is legitimate — `/documents` is a table with a caption and
    // a title, and inventing a section heading to satisfy a count would be
    // markup written for the test rather than for the reader.
    expect((await headingLevels(page)).length).toBeGreaterThanOrEqual(1);
    expectNoSkippedLevels(await headingLevels(page), path);
  }
});

/**
 * Heading order in the answered and refused states too.
 *
 * Both `h4`s this found were inside a card — the answer's per-product groups and
 * the crawl job's skip reasons — and neither renders on a page at rest. A
 * hierarchy checked only in the empty state is a hierarchy checked on the states
 * nobody looks at.
 */
test("heading levels never skip in the answered or refused state", async ({ page }) => {
  await stubCorpus(page);
  await page.route("**/api/chat/stream", async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream; charset=utf-8" },
      body: CANNED_ANSWER,
    });
  });

  await page.goto("/");
  await page.fill("#question", "How do I save a filter?");
  await page.click("button[type=submit]");
  await expect(page.locator("#answer-heading")).toBeVisible({ timeout: 30_000 });
  expectNoSkippedLevels(await headingLevels(page), "/ with an answer");

  await page.goto("/");
  await page.route("**/api/chat/stream", async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream; charset=utf-8" },
      body: CANNED_REFUSAL,
    });
  });
  await page.fill("#question", "How much is the Premium plan?");
  await page.click("button[type=submit]");
  await expect(page.getByRole("alert", { name: "Outcome" })).toContainText(/No answer/, {
    timeout: 30_000,
  });
  expectNoSkippedLevels(await headingLevels(page), "/ with a refusal");
});

/**
 * The in-flight crawl notice, which has its own heading structure.
 *
 * The skip-reasons heading inside the crawl panel is corrected to the same level
 * but is not asserted here: the panel renders while a crawl is in flight, and
 * skip reasons only exist once it has finished, so a stub combining the two would
 * be a state no run can reach.
 */
test("heading levels never skip while a crawl is in flight", async ({ page }) => {
  await stubCorpus(page);
  await page.route("**/api/v1/crawl-jobs**", async (route) => {
    await route.fulfill({
      json: {
        ...JOBS,
        items: [
          {
            ...JOBS.items[0],
            status: "running",
            finished_at: null,
            skipped_reasons: null,
            counters: { discovered: 42, processed: 17, unchanged: 0, skipped: 0, failed: 0 },
          },
        ],
      },
    });
  });

  await page.goto("/");
  await expect(page.getByText(/crawl/i).first()).toBeVisible();
  expectNoSkippedLevels(await headingLevels(page), "/ with a crawl in flight");
});

test("the question box is labelled, not merely placeheld", async ({ page }) => {
  await stubCorpus(page);
  await page.goto("/");

  const box = page.locator("#question");
  // The accessible name, however it is provided. A placeholder is not a label:
  // it vanishes on the first keystroke and is not reliably announced.
  const name = await box.evaluate((el) => {
    const id = el.getAttribute("id");
    const explicit = id ? document.querySelector(`label[for="${id}"]`) : null;
    const wrapping = el.closest("label");
    return (
      explicit?.textContent ??
      wrapping?.textContent ??
      el.getAttribute("aria-label") ??
      el.getAttribute("aria-labelledby")
    );
  });
  expect(name?.trim().length ?? 0).toBeGreaterThan(0);

  // And the character counter is attached, so the limit is announced rather than
  // being a second floating paragraph.
  await expect(box).toHaveAttribute("aria-describedby", "question-help");
});

test("the inspect toggle is a real checkbox with a name", async ({ page }) => {
  await stubCorpus(page);
  await page.goto("/");

  const toggle = page.locator('input[type="checkbox"]').first();
  await expect(toggle).toBeAttached();
  const labelled = await toggle.evaluate((el) => {
    const id = el.getAttribute("id");
    return Boolean(
      (id && document.querySelector(`label[for="${id}"]`)) ||
        el.closest("label") ||
        el.getAttribute("aria-label") ||
        el.getAttribute("aria-labelledby"),
    );
  });
  expect(labelled).toBe(true);
});

test("both live regions exist, are persistent, and are distinct", async ({ page }) => {
  await stubCorpus(page);
  await page.goto("/");

  const polite = page.getByRole("status", { name: "Progress" });
  const assertive = page.getByRole("alert", { name: "Outcome" });

  await expect(polite).toBeAttached();
  await expect(assertive).toBeAttached();

  // Two persistent regions, not one whose politeness flips. A region that
  // changes `aria-live` between runs is announced inconsistently — some screen
  // readers register the live-ness at insertion, not per update.
  const attributes = await page.evaluate(() =>
    Array.from(document.querySelectorAll('[role="status"], [role="alert"]')).map((el) => ({
      role: el.getAttribute("role"),
      live: el.getAttribute("aria-live"),
    })),
  );
  expect(attributes.some((a) => a.role === "status")).toBe(true);
  expect(attributes.some((a) => a.role === "alert")).toBe(true);
});

test("the sources table has a caption and header cells", async ({ page }) => {
  await stubCorpus(page);
  await page.goto("/sources");

  const table = page.getByRole("table", { name: /registered sources/i });
  await expect(table).toBeVisible();
  // A named table is navigable by name; a caption is how a table gets one.
  await expect(table.locator("caption")).toHaveCount(1);
  await expect(table.locator("th")).not.toHaveCount(0);
});

test("data cells are marked as headers with scope", async ({ page }) => {
  await stubCorpus(page);
  await page.goto("/documents");

  const table = page.getByRole("table").first();
  await expect(table).toBeVisible();
  const scopes = await table.evaluate((t) =>
    Array.from(t.querySelectorAll("th")).map((th) => th.getAttribute("scope")),
  );
  expect(scopes.length).toBeGreaterThan(0);
  // Every column header is scoped: `col` for the column it titles. An unscoped
  // `th` is read as a row header, which makes a multi-column table read wrong.
  expect(scopes.every((scope) => scope === "col")).toBe(true);
});

test("every link has a discernible name", async ({ page }) => {
  await stubCorpus(page);
  for (const path of ["/", "/sources", "/documents", "/evaluations", "/settings"]) {
    await page.goto(path);
    const nameless = await page.evaluate(() =>
      Array.from(document.querySelectorAll("a"))
        .filter((a) => (a.textContent ?? "").trim() === "" && !a.getAttribute("aria-label"))
        .map((a) => a.getAttribute("href")),
    );
    expect(nameless, `links with no accessible name on ${path}`).toEqual([]);
  }
});

/**
 * The view's `h1` is its first heading, and comes before its content.
 *
 * A page whose title renders below the table it titles is structurally correct
 * and practically unusable: a reader arriving with heading navigation lands on
 * the table before they are told what page they are on.
 */
test("each view's h1 comes first, above its content", async ({ page }) => {
  await stubCorpus(page);

  for (const path of ["/", "/sources", "/documents", "/evaluations", "/settings"]) {
    await page.goto(path);
    const placement = await page.evaluate(() => {
      const h1 = document.querySelector("h1");
      if (h1 === null) return null;
      const main = document.querySelector("main");
      const inMain = h1 === main || main?.contains(h1) === true;
      // Where the h1 sits relative to the first table or form on the page.
      const content = document.querySelector("table, form");
      return {
        inMain,
        headingFirst:
          content === null ||
          (h1.compareDocumentPosition(content) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0,
      };
    });

    expect(placement, `no h1 on ${path}`).not.toBeNull();
    expect(placement?.inMain, `the h1 on ${path} is outside <main>`).toBe(true);
    expect(placement?.headingFirst, `the h1 on ${path} renders after its content`).toBe(true);
  }
});

test("the document detail names its document with a heading", async ({ page }) => {
  await stubCorpus(page);
  await page.goto(`/documents/${DOCUMENT.id}`);

  await expect(page.getByRole("heading", { name: DOCUMENT.title })).toBeVisible();
  // The origin link is a link, not a button-looking div, and it says where it goes.
  const origin = page.getByRole("link", { name: new RegExp(DOCUMENT.url.slice(8, 30)) }).first();
  await expect(origin).toHaveAttribute("href", DOCUMENT.url);
});

test("the settings view describes each secret's presence without a value", async ({ page }) => {
  await stubCorpus(page);
  await page.goto("/settings");

  // The semantic claim is that a secret is reported as present or absent and
  // never as a value: the definition list term names the setting, the
  // description says whether it is configured.
  const configured = page.getByText(/configured/i).first();
  await expect(configured).toBeVisible();
  await expect(page.locator("body")).not.toContainText(/sk_/);
});

test("Ask is disabled while there is no question, and enables as soon as there is", async ({
  page,
}) => {
  await stubCorpus(page);
  await page.goto("/");

  // A disabled submit button with no explanation is a dead end for a keyboard
  // user: they can focus it, they can see it, and pressing it does nothing. The
  // states below are what make the disabling legible.
  const submit = page.locator("button[type=submit]");
  await expect(submit).toBeVisible();
  await expect(submit).toBeDisabled();

  // Whitespace is not a question.
  await page.fill("#question", "   ");
  await expect(submit).toBeDisabled();

  await page.fill("#question", "How do I save a filter?");
  await expect(submit).toBeEnabled();

  // And it goes back: clearing the box disables it again rather than leaving a
  // control that submits nothing.
  await page.fill("#question", "");
  await expect(submit).toBeDisabled();
});

test("the question limit is stated before it is reached", async ({ page }) => {
  await stubCorpus(page);
  await page.goto("/");

  const help = page.locator("#question-help");
  await expect(help).toContainText(/2000|limit/i);
  // The limit is announced as part of the field, not only drawn next to it.
  await expect(page.locator("#question")).toHaveAttribute("aria-describedby", "question-help");
});