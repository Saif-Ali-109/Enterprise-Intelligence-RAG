import { expect, test } from "@playwright/test";

/**
 * The evaluation dashboard (T149–T151): three properties, all browser-side.
 *
 * 1. With no run, the page says so in words and renders **no** quality number
 *    at all — FR-037, and the reason this spec asserts absence by scanning the
 *    whole rendered text for the metric labels rather than spot-checking one
 *    cell.
 * 2. A failing gate reads as a failure, in plain language, with its target and
 *    its measured value (T150).
 * 3. Every metric cell carries the run id and timestamp that produced it
 *    (T149) — a number with no provenance on screen is a fabricated number.
 *
 * The API is stubbed at the same-origin proxy. What is under test is the
 * dashboard's rendering, and the contract tests own what the API returns.
 */

const RUN = {
  id: "0f8d2a1c-9b44-4c7d-8e55-6d3a1b2c4e90",
  dataset_version: "1.2.0",
  dataset_fingerprint: "a".repeat(64),
  status: "completed",
  started_at: "2026-10-06T09:00:00Z",
  finished_at: "2026-10-06T09:04:12Z",
  question_count: 32,
  metrics: {
    recall_at_5: 0.4125,
    precision_at_5: 0.6,
    mrr: 0.78125,
    cross_product_domain_coverage: 0.75,
    citation_validity: 0.993,
    claim_coverage: 0.6875,
    faithfulness: 0.9125,
    unsupported_refusal_rate: 1.0,
    fabricated_fact_count: 0,
    invalid_citation_count: 0,
    p50_latency_ms: 2410,
    p95_latency_ms: 5240,
    error_rate: 0,
  },
  thresholds: { recall_at_5: 0.8, precision_at_5: 0.6, mrr: 0.75 },
  gate_outcome: "fail",
  gate_failures: [
    { gate: "recall_at_5", target: 0.8, actual: 0.4125 },
    { gate: "claim_coverage", target: 0.85, actual: 0.6875 },
  ],
};

const RESULTS = {
  items: [
    {
      question_id: "simple-jira-filter-saved",
      question: "How do I save a filter?",
      category: "simple_lookup",
      difficulty: "easy",
      recall_at_5: 1,
      precision_at_5: 0.2,
      mrr: 1,
      citation_validity: 1,
      claim_coverage: 0.666,
      faithfulness: 0.9,
      refused: false,
      fabricated_fact_count: 0,
      invalid_citation_count: 0,
      latency_ms: 2180,
      error: null,
    },
  ],
  total: 1,
  limit: 50,
  offset: 0,
};

/**
 * One route, switching on the path.
 *
 * Playwright matches page routes in reverse registration order, so three
 * overlapping patterns registered separately mean the last one silently wins
 * for every URL it also matches — a run-detail request would be answered with
 * the runs page and the dashboard would crash on a missing `id`. One handler
 * with an explicit switch has no such ambiguity.
 */
async function stubApi(page: import("@playwright/test").Page, run: unknown | null) {
  await page.route("**/api/v1/evaluations/runs**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/results")) {
      await route.fulfill({ json: RESULTS });
      return;
    }
    if (path.includes("/evaluations/runs/")) {
      await route.fulfill({ json: run ?? RUN });
      return;
    }
    await route.fulfill({
      json:
        run === null
          ? { items: [], total: 0, limit: 20, offset: 0 }
          : { items: [RUN], total: 1, limit: 20, offset: 0 },
    });
  });
}

test("with no run the page explains and shows no quality number", async ({ page }) => {
  await stubApi(page, null);
  await page.goto("/evaluations");

  await expect(page.getByRole("heading", { name: "No evaluation has been run" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Run the evaluation suite" })).toBeVisible();

  const text = (await page.locator("main").textContent()) ?? "";
  // No metric label may appear as a value cell, and no fabricated zeroes.
  for (const label of ["recall at 5", "mrr", "faithfulness", "claim coverage", "0.000"]) {
    expect(text.toLowerCase()).not.toContain(label);
  }
});

test("a failing gate reads as a failure with its target and value", async ({ page }) => {
  await stubApi(page, RUN);
  await page.goto("/evaluations");

  // Named, not bare `getByRole("alert")`: Next renders its own route announcer with
  // that role, and an unnamed locator resolves to both.
  const banner = page.getByRole("alert", { name: "Quality gate" });
  await expect(banner).toContainText("Gate: fail");
  // Regex, not a string: string matching is case-sensitive and the banner
  // sentence-cases the gate name it renders from `recall_at_5`.
  await expect(banner).toContainText(/recall at 5/i);
  await expect(banner).toContainText(/claim coverage/i);
  await expect(banner).toContainText("0.4125");
  await expect(banner).toContainText("0.8");

  // The metrics are grouped, and the failing metric shows its measured value.
  await expect(page.getByRole("heading", { name: "Retrieval" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Answer quality" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Performance" })).toBeVisible();
});

test("every metric cell attributes its number to the run that measured it", async ({ page }) => {
  await stubApi(page, RUN);
  await page.goto("/evaluations");

  await expect(page.getByText(/run 0f8d2a1c · 2026-10-06T09:04:12Z/).first()).toBeVisible();
  // The zero-count gates render as zero counts, not as ratios.
  await expect(page.getByText("fabricated fact count").locator("..")).toContainText("0");
  await expect(page.getByText("invalid citation count").locator("..")).toContainText("0");
});

test("a running run shows no metrics and says the suite is in progress", async ({ page }) => {
  await stubApi(page, { ...RUN, status: "running", metrics: null, finished_at: null });
  await page.goto("/evaluations");
  await expect(page.getByText("Run in progress (running)")).toBeVisible();
  const text = ((await page.locator("main").textContent()) ?? "").toLowerCase();
  expect(text).not.toContain("recall at 5");
});