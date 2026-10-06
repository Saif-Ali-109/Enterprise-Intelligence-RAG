import { defineConfig, devices } from "@playwright/test";

/**
 * The e2e harness. Deliberately minimal: these specs exist to prove browser
 * behaviours (live-region announcements, inspect rendering) that a jsdom
 * snapshot or a type check cannot see, not to replace the contract tests.
 *
 * The server under test is the production build (`npm run start`) so the harness
 * answers with the same React runtime the container ships. `BACKEND_BASE_URL` is
 * satisfied by the canned SSE body most specs register, so e2e does not depend on
 * a live backend.
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  fullyParallel: false,
  retries: 0,
  use: {
    baseURL: "http://127.0.0.1:3100",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "npm run start -- --port 3100",
    url: "http://127.0.0.1:3100",
    reuseExistingServer: true,
    env: { BACKEND_BASE_URL: "http://127.0.0.1:8000" },
    timeout: 120_000,
  },
});
