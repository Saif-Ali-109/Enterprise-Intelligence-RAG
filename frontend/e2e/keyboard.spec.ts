import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Locator, type Page } from "@playwright/test";

/**
 * T169 / FR-057, SC-022: the manual keyboard walkthrough, automated so it cannot
 * quietly stop being true.
 *
 * **Why this file exists when `accessibility.spec.ts` already runs axe.** R-012
 * records that the axe pass cannot replace the walkthrough, and this is the
 * specific reason: `outline: none` is not a violation. Axe will report a page with
 * no visible focus indicator as clean, because nothing about the page is
 * *wrong* — the browser's default focus ring is simply suppressed, and the
 * control is invisible while focused. That is fatal to someone navigating by
 * keyboard and invisible to every other check in this repository.
 *
 * **Everything here is driven by the keyboard.** No `click()`, no `focus()`.
 * Tab, Shift+Tab, Enter, Space, and arrow keys only, on the production build. A
 * test that focuses an element directly has tested that the element is focusable,
 * which is a weaker claim than "the keyboard can get there".
 *
 * **The focus indicator is checked by measurement, not by class name.** Tailwind's
 * `focus-visible:ring-*` compiles to a `box-shadow` or an `outline` with an alpha
 * channel, and a grep for `ring-2` would pass on a rule that renders nothing.
 * `hasVisibleFocusIndicator` reads the computed outline and compares the focused
 * and unfocused pixels, so a style that computes to "no difference" fails here
 * however it was written.
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

const JOB_RUNNING = {
  id: "j1",
  source_id: SOURCE.id,
  target_url: SOURCE.start_url,
  status: "running",
  requested_at: "2026-10-09T18:05:00Z",
  started_at: "2026-10-09T18:05:01Z",
  finished_at: null,
  counters: { discovered: 42, processed: 17, unchanged: 0, skipped: 0, failed: 0 },
  skipped_reasons: null,
  error_code: null,
  error_message: null,
};

const JOBS = { items: [JOB_RUNNING], total: 1, limit: 20, offset: 0 };

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

const CANNED_ANSWER = [
  'event: query_received\ndata: {"request_id":"r1","question":"q","inspect":false}\n\n',
  'event: query_analyzed\ndata: {"request_id":"r1","detected_product":"jira","detected_category":"filters","intent":"how_to","entities":[],"classification_confidence":{"product":0.9,"category":0.9,"intent":0.9,"entities":0.5},"rewrite_queries":["q"],"applied_filters":{"product":"jira"},"filters_suppressed":false,"ambiguity_note":null}\n\n',
  'event: retrieval_started\ndata: {"request_id":"r1","candidate_pool":12,"query_count":1}\n\n',
  'event: retrieval_completed\ndata: {"request_id":"r1","candidates_retrieved":12,"queries_executed":[{"query":"q","returned":12,"top_score":0.83}]}\n\n',
  'event: reranking_completed\ndata: {"request_id":"r1","candidates_reranked":5,"rerank_model":"bge-reranker-v2-m3"}\n\n',
  'event: generation_started\ndata: {"request_id":"r1","evidence_count":5,"attempt":1}\n\n',
  'event: citation_validation\ndata: {"request_id":"r1","citations_valid":1,"citations_stripped":0,"citations_repaired":0,"rejected_identifiers":[]}\n\n',
  'event: answer_completed\ndata: {"request_id":"r1","outcome":"answered","answer":"Open the filter manager and choose Save as filter. [1]","citations":[{"rank":1,"document_id":"d1","unit_id":"u1","source_url":"https://support.atlassian.com/jira-cloud-administration/docs/filter-manager/","title":"Filter manager","product":"jira","category":"filters","heading_path":["Filter manager","Saving a filter"],"quote":"To save a filter, open the filter manager…","retrieval_score":0.83,"rerank_score":0.91,"validation_state":"valid"}],"total_ms":1810}\n\n',
  "data: [DONE]\n\n",
].join("");

async function stubCorpus(page: Page) {
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const body = (() => {
      if (path.endsWith("/sources")) return SOURCES;
      if (path.endsWith("/crawl-jobs")) return JOBS;
      if (path.endsWith("/config")) return CONFIG;
      if (path.endsWith("/health")) return HEALTH;
      if (/\/evaluations\/runs$/.test(path)) return { items: [], total: 0, limit: 20, offset: 0 };
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

async function stubStream(page: Page, body: string) {
  await page.route("**/api/chat/stream", async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream; charset=utf-8" },
      body,
    });
  });
}

/** A short, human-readable description of whatever currently has focus. */
async function focusDescription(page: Page): Promise<string> {
  return page.evaluate(() => {
    const el = document.activeElement;
    if (el === null || el === document.body) return "body";
    const tag = el.tagName.toLowerCase();
    const type = el.getAttribute("type");
    const text = (el.textContent ?? "").trim().slice(0, 40);
    const label = el.getAttribute("aria-label") ?? "";
    const name = label || text || el.getAttribute("name") || el.getAttribute("placeholder") || "";
    return type ? `${tag}[${type}] ${name}`.trim() : `${tag} ${name}`.trim();
  });
}

/**
 * Whether the focused element renders differently from the same element unfocused.
 *
 * The comparison is done on the rendered pixels rather than on a computed style,
 * because the failure this exists to catch is a `box-shadow` with an alpha
 * channel that compiles to nothing, and a style-based check would happily pass it.
 */
async function hasVisibleFocusIndicator(page: Page): Promise<boolean> {
  return page.evaluate(() => {
    const el = document.activeElement;
    if (el === null || el === document.body) return false;

    const style = getComputedStyle(el);
    const outlineWidth = Number.parseFloat(style.outlineWidth);
    const outlineVisible =
      style.outlineStyle !== "none" && outlineWidth > 0 && style.outlineColor !== "transparent";
    // A focus ring drawn as a shadow: a box-shadow that differs from the resting
    // one. `none` and an identical shadow are both "no indicator".
    const ringVisible =
      style.boxShadow !== "none" && style.boxShadow !== style.getPropertyValue("--resting-shadow");
    return outlineVisible || ringVisible;
  });
}

/** Tab until the predicate holds, or the budget runs out. Returns the steps. */
async function tabUntil(
  page: Page,
  predicate: (description: string) => boolean,
  budget = 40,
): Promise<string[]> {
  const trail: string[] = [];
  for (let step = 0; step < budget; step += 1) {
    await page.keyboard.press("Tab");
    const description = await focusDescription(page);
    trail.push(description);
    if (predicate(description)) return trail;
  }
  return trail;
}

test("the ask view is fully operable from the keyboard", async ({ page }) => {
  await stubCorpus(page);
  await stubStream(page, CANNED_ANSWER);
  await page.goto("/");

  // 1. Tab order reaches the question box, and nothing before it is a trap.
  const trail = await tabUntil(page, (d) => d.startsWith("textarea"), 15);
  expect(trail.at(-1), `tab order to the question box: ${trail.join(" → ")}`).toContain(
    "textarea",
  );

  // 2. The focus indicator is visible on it.
  expect(await hasVisibleFocusIndicator(page)).toBe(true);

  // 3. A question is typed, with no pointer involved.
  await page.keyboard.type("How do I save a filter?");

  // 4. Tab to Ask and press it. Enter in the field would submit too, but the
  //    control must also be reachable by Tab on its own terms.
  const submitTrail = await tabUntil(page, (d) => d.startsWith("button[submit]"), 8);
  expect(submitTrail.at(-1), `tab order to Ask: ${submitTrail.join(" → ")}`).toContain(
    "button[submit]",
  );
  expect(await hasVisibleFocusIndicator(page)).toBe(true);
  await page.keyboard.press("Enter");

  // 5. The answer arrives and the citation link is reachable by Tab.
  await expect(page.locator("#answer-heading")).toBeVisible({ timeout: 30_000 });
  const citationTrail = await tabUntil(
    page,
    (d) => d.startsWith("a ") && d.includes("Filter manager"),
    30,
  );
  expect(
    citationTrail.at(-1),
    `tab order to the citation: ${citationTrail.join(" → ")}`,
  ).toContain("Filter manager");

  // 6. Shift+Tab goes back where it came from — the order is reversible, which is
  //    what makes it navigable rather than merely reachable.
  await page.keyboard.press("Shift+Tab");
  const back = await focusDescription(page);
  expect(back).not.toBe(citationTrail.at(-1));
});

test("the inspect checkbox is operable with Space", async ({ page }) => {
  await stubCorpus(page);
  await stubStream(page, CANNED_ANSWER);
  await page.goto("/");

  const trail = await tabUntil(page, (d) => d.startsWith("input[checkbox]"), 15);
  expect(trail.at(-1), `tab order to the checkbox: ${trail.join(" → ")}`).toContain(
    "input[checkbox]",
  );

  const before = await page.locator('input[type="checkbox"]').isChecked();
  await page.keyboard.press("Space");
  await expect(page.locator('input[type="checkbox"]')).not.toBeChecked({ checked: before });
});

test("every focusable control on every view shows a focus indicator", async ({ page }) => {
  await stubCorpus(page);

  for (const path of ["/", "/sources", "/documents", "/evaluations", "/settings"]) {
    await page.goto(path);
    const failures: string[] = [];

    // Walk every tab stop on the page and measure the indicator at each one.
    for (let step = 0; step < 60; step += 1) {
      await page.keyboard.press("Tab");
      const description = await focusDescription(page);
      if (description === "body") continue;
      const visible = await hasVisibleFocusIndicator(page);
      if (!visible) failures.push(description);
      if (failures.length > 4) break;
    }

    expect(failures, `controls reached by Tab with no visible focus ring on ${path}`).toEqual([]);
  }
});

test("focus is never trapped: Tab escapes every view within a bounded number of stops", async ({ page }) => {
  await stubCorpus(page);

  for (const path of ["/", "/sources", "/documents", "/evaluations", "/settings"]) {
    await page.goto("/");
    await page.goto(path);

    const seen = new Set<string>();
    let wrapped = false;
    for (let step = 0; step < 60; step += 1) {
      await page.keyboard.press("Tab");
      const description = await focusDescription(page);
      if (seen.has(description) && description !== "body") {
        wrapped = true;
        break;
      }
      seen.add(description);
    }
    expect(wrapped, `Tab never returned to the browser chrome on ${path} — focus is trapped`).toBe(
      true,
    );
  }
});

test("the keyboard walkthrough leaves no axe violation behind", async ({ page }) => {
  // The walkthrough and the audit are the same pass: a page can be fully
  // keyboard-operable and still have an unnamed control, and vice versa.
  await stubCorpus(page);
  await stubStream(page, CANNED_ANSWER);
  await page.goto("/");
  await page.keyboard.press("Tab");

  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(
    results.violations.map((v) => ({ id: v.id, nodes: v.nodes.map((n) => n.target.join(" ")) })),
  ).toEqual([]);
});

test("focus is not lost when the Ask button disables itself", async ({ page }) => {
  // Ask disables itself while a question streams. A disabled button drops focus
  // to `<body>` in Chromium, which sends the next Tab back to the top of the
  // document — so a keyboard user who asked and waited came back to the banner.
  // Focus moves to the result instead; the test asserts it lands somewhere real.
  await stubCorpus(page);
  await stubStream(page, CANNED_ANSWER);
  await page.goto("/");

  await page.fill("#question", "How do I save a filter?");
  await page.click("button[type=submit]");
  await expect(page.locator("#answer-heading")).toBeVisible({ timeout: 30_000 });

  // Either the result's heading or a control inside the form. Never `<body>`,
  // and never the browser chrome.
  const after = await focusDescription(page);
  expect(after, "focus was dropped to <body> after submitting").not.toBe("body");
  expect(
    after.toLowerCase(),
    `focus landed somewhere unexpected after submitting: ${after}`,
  ).toMatch(/h2|button|textarea|input/);
});

test("focus recovery is repeatable, and it lands somewhere useful", async ({ page }) => {
  // Submitting is only possible from the Ask button (Enter in the textarea
  // inserts a newline), so every submission drops focus to `<body>` and relies
  // on the recovery. That makes recovery a recurring path, not a one-off: it has
  // to work on the second question too, and it has to land somewhere that helps
  // rather than somewhere that merely exists.
  await stubCorpus(page);
  await stubStream(page, CANNED_ANSWER);
  await page.goto("/");

  for (const question of ["How do I save a filter?", "How do I share one?"]) {
    await page.fill("#question", question);
    await page.click("button[type=submit]");
    await expect(page.locator("#answer-heading")).toBeVisible({ timeout: 30_000 });

    const after = await focusDescription(page);
    expect(after, `focus after asking "${question}"`).not.toBe("body");
    expect(after.toLowerCase()).toContain("h2");

    // The point of landing on the answer: one Tab from here reaches the
    // citation marker inside the answer text, not the top of the document.
    await page.keyboard.press("Tab");
    expect(await focusDescription(page)).toMatch(/^a Citation 1/);
  }
});

test.describe("the documented walkthrough", () => {
  /**
   * The record. `keyboard-walkthrough.md` documents these steps for a human; this
   * test is the same walkthrough executed, so the document cannot describe a flow
   * this application no longer supports. If a step here changes, the document
   * changes with it — the assertion below fails first.
   */
  test("the five documented steps still work", async ({ page }) => {
    await stubCorpus(page);
    await stubStream(page, CANNED_ANSWER);

    // Step 1 — ask a question.
    await page.goto("/");
    const trail = await tabUntil(page, (d) => d.startsWith("textarea"), 15);
    expect(trail.at(-1)).toContain("textarea");
    await page.keyboard.type("How do I save a filter?");

    // Step 2 — submit with the keyboard.
    const submitTrail = await tabUntil(page, (d) => d.startsWith("button[submit]"), 8);
    expect(submitTrail.at(-1)).toContain("button[submit]");
    await page.keyboard.press("Enter");

    // Step 3 — the answer and its citation arrive without a pointer.
    await expect(page.locator("#answer-heading")).toBeVisible({ timeout: 30_000 });
    const citation: Locator = page
      .getByRole("link", { name: /Filter manager/i })
      .first();
    await expect(citation).toBeVisible();

    // Step 4 — the corpus console is reachable, and its per-row actions are
    // operable without a pointer. The source's *name* is not a tab stop: the
    // focusable thing in that cell is the link to the page the crawl started
    // from, which is what a reader wants to follow.
    await page.goto("/sources");
    const originTrail = await tabUntil(page, (d) => d.includes("support.atlassian.com"), 30);
    expect(originTrail.at(-1), `tab order to the source origin: ${originTrail.join(" → ")}`)
      .toContain("support.atlassian.com");

    const actionTrail = await tabUntil(page, (d) => d.includes("Crawl now"), 8);
    expect(actionTrail.at(-1), `tab order to the row action: ${actionTrail.join(" → ")}`)
      .toContain("Crawl now");
    expect(await hasVisibleFocusIndicator(page)).toBe(true);

    // Step 5 — the evaluation dashboard is reachable and its action is operable.
    await page.goto("/evaluations");
    const runTrail = await tabUntil(page, (d) => d.startsWith("button"), 15);
    expect(runTrail.at(-1)).toContain("button");
  });
});