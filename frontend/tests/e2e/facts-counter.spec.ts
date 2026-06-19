/**
 * Deterministic FACTS counter in the RetrievalBadge context panel.
 * Fires a graph-tier query, expands the badge, and asserts the panel shows a
 * "facts" row with a real number (and that the old "tool loop" row is gone).
 */
import { test, expect } from "@playwright/test";
import * as fs from "fs";

const BASE = process.env.BASE_URL || "http://localhost:3000";
const API = process.env.API_URL || "http://localhost:8000";
const USER = process.env.ADMIN_USER || "admin";
const PASS = process.env.ADMIN_PASSWORD || "";
const QUERY =
  process.env.PROBE_QUERY ||
  "what is nlp and how does it assist in model fine tuning";
const SHOT = "tab-screenshots/facts";

test.use({ viewport: { width: 1440, height: 900 }, actionTimeout: 20_000 });
test.setTimeout(220_000);
fs.mkdirSync(SHOT, { recursive: true });

test("retrieval badge shows deterministic facts counter", async ({
  page,
  request,
}) => {
  const lr = await request.post(`${API}/api/auth/login`, {
    data: { username: USER, password: PASS },
  });
  expect(lr.ok(), `login ${lr.status()}`).toBeTruthy();
  const token = (await lr.json()).access_token as string;
  const me = await (
    await request.get(`${API}/api/auth/me`, {
      headers: { Authorization: `Bearer ${token}` },
    })
  ).json();
  await page.addInitScript(
    ([t, u]) =>
      localStorage.setItem(
        "polymath-auth",
        JSON.stringify({ state: { token: t, user: u }, version: 0 }),
      ),
    [token, me] as const,
  );

  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("query-input")).toBeVisible({ timeout: 25_000 });
  await page.getByTestId("query-input").click();
  await page.getByTestId("query-input").fill(QUERY);
  await page.getByTestId("query-submit").click();

  // The badge renders on the assistant message once the run completes.
  const badge = page.locator('[data-testid="source-citations"]').last();
  await expect(badge, "retrieval badge should appear").toBeVisible({
    timeout: 160_000,
  });
  await badge.scrollIntoViewIfNeeded();
  await badge.locator("button").first().click();

  const panel = badge.locator(".process-group").first();
  await expect(panel).toBeVisible();
  await page.waitForTimeout(250);
  await badge.scrollIntoViewIfNeeded();
  await badge.screenshot({ path: `${SHOT}/panel.png` });

  const panelText = (await panel.innerText()).toLowerCase();
  // Parse the facts value shown in the panel (the label "facts" then a number).
  const m = panelText.match(/facts\s*\n?\s*(\d+|—)/);
  const factsShown = m ? m[1] : "(not found)";
  console.log(
    `[facts] facts_row=${factsShown} hasFactsLabel=${panelText.includes("facts")} hasToolLoop=${panelText.includes("tool loop")}`,
  );

  expect(panelText, "panel should have a facts row").toContain("facts");
  expect(panelText, "tool loop row should be replaced").not.toContain(
    "tool loop",
  );
  expect(factsShown, "facts value should be a real number").toMatch(/^\d+$/);
});
