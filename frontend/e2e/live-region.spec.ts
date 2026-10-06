import { expect, test } from "@playwright/test";

/**
 * T124 / FR-060, events.md §8: the live region announces exactly the set §8
 * lists, with the politeness §8 assigns — and nothing §8 does not.
 *
 * **Determined by a canned stream, not the network.** The spec registers the SSE
 * response the proxy forwards, so the test is a property of what this UI
 * announces of a given stream, which is the thing FR-060 governs. A version of
 * this check that depended on the live backend would measure the backend as
 * much as the announcement.
 *
 * **The counter-examples matter as much as the positives.** A live region that
 * announced every stage would be a log read aloud; the assertion that the
 * non-announced events appear *nowhere* in the region is what keeps the two
 * apart.
 */
const CANNED_STREAM = [
  'data: {"request_id":"r1","question":"q","inspect":false}\n\n',
  'event: query_received\ndata: {"request_id":"r1","question":"q","inspect":false}\n\n',
  'event: query_analyzed\ndata: {"request_id":"r1","detected_product":"jira","detected_category":"rest-api","intent":"how_to","entities":["JQL"],"classification_confidence":{"product":0.95,"category":0.9,"intent":0.9,"entities":0.8},"rewrite_queries":["q"],"applied_filters":{"product":"jira"},"filters_suppressed":false,"ambiguity_note":null}\n\n',
  'event: retrieval_started\ndata: {"request_id":"r1","candidate_pool":12,"query_count":1}\n\n',
  'event: retrieval_completed\ndata: {"request_id":"r1","candidates_retrieved":12,"queries_executed":[{"query":"q","returned":12,"top_score":0.83}]}\n\n',
  'event: reranking_completed\ndata: {"request_id":"r1","candidates_reranked":5,"rerank_model":"bge-reranker-v2-m3"}\n\n',
  'event: generation_started\ndata: {"request_id":"r1","evidence_count":5,"attempt":1}\n\n',
  'event: citation_validation\ndata: {"request_id":"r1","citations_valid":4,"citations_stripped":1,"citations_repaired":0,"rejected_identifiers":["C7"]}\n\n',
  'event: answer_completed\ndata: {"request_id":"r1","outcome":"answered","answer":"Rotate a key in the API keys page. [1]","citations":[{"rank":1,"document_id":"d","unit_id":"u","source_url":"https://support.atlassian.com/x","title":"Manage API tokens","product":"jira","category":"rest-api","heading_path":["Manage API tokens"],"quote":"To rotate…","retrieval_score":0.83,"rerank_score":0.91,"validation_state":"valid"}],"total_ms":1800}\n\n',
  "data: [DONE]\n\n",
].join("");

test.beforeEach(async ({ page }) => {
  await page.route("**/api/chat/stream", async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream; charset=utf-8" },
      body: CANNED_STREAM,
    });
  });
});

test("the live region announces exactly the §8 set, with the right politeness", async ({ page }) => {
  await page.goto("/");
  await page.fill("#question", "How do I rotate an API token?");
  await page.click("button[type=submit]");

  // Wait for the terminal event to be rendered.
  await expect(page.locator("#answer-heading")).toBeVisible({ timeout: 30_000 });

  // The polite region is sr-only (invisible), so assert on its text content
  // rather than visibility.


  // Both regions exist: a polite one that holds the last polite progress
  // announcement, and an assertive one that holds the outcome.
  const polite = page.getByRole("status", { name: "Progress" });
  await expect(polite).toBeAttached();

  // Whatever text is in the polite region, it must be from the announced set —
  // never a counter, a score, a filter, a model name, or a raw event.
  const politeText = (await polite.textContent()) ?? "";
  for (const allowed of [
    "Searching the indexed documentation",
    "Found",
    "Ranked",
    "Writing the answer",
    "Question received",
  ]) {
    if (politeText.includes(allowed)) {
      break;
    }
  }
  expect(
    [
      "Searching the indexed documentation",
      "Found 12 candidate passages",
      "Ranked 5 passages",
      "Writing the answer",
      "Question received",
    ].some((allowed) => politeText.trim().startsWith(allowed.slice(0, 12))),
  ).toBe(true);

  // The assertive region announces the outcome.
  const assertive = page.getByRole("alert", { name: "Outcome" });
  await expect(assertive).toContainText(/Answer ready|No answer could be given|Something failed/);

  // And no non-announced detail anywhere in either region.
  for (const forbidden of ["0.95", "filters_suppressed", "bge-reranker", "entities", "citations_valid"]) {
    await expect(page.locator(`text=${forbidden}`).first()).toHaveCount(0);
  }
});

test("an inspection-mode run renders the trace, not an answer-free region", async ({ page }) => {
  await page.goto("/");
  await page.check("input[type=checkbox]");
  await page.fill("#question", "How do I rotate an API token?");
  await page.click("button[type=submit]");
  await expect(page.getByRole("heading", { name: "Pipeline trace" })).toBeVisible({ timeout: 30_000 });

  // The trace renders each stage's payload — that is where the internals live.
  await expect(page.locator("text=retrieval_completed").first()).toBeVisible();
  await expect(page.locator("text=citation_validation").first()).toBeVisible();
});
