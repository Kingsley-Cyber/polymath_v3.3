/**
 * Diagnostic: does the reasoning trace actually stream during synthesis?
 * Fires a query and watches the full stream (retrieval → synthesis), reporting
 * whether the "Reasoning trace" panel ever appears and how much text it shows.
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
const SHOT = "tab-screenshots/reasoning";

test.use({
  launchOptions: { slowMo: 0 },
  viewport: { width: 1440, height: 900 },
  actionTimeout: 15_000,
});
test.setTimeout(200_000);
fs.mkdirSync(SHOT, { recursive: true });

test("observe reasoning trace through synthesis", async ({ page, request }) => {
  const lr = await request.post(`${API}/api/auth/login`, {
    data: { username: USER, password: PASS },
  });
  expect(lr.ok(), `login failed (${lr.status()})`).toBeTruthy();
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

  const reasoningTitle = page.getByText("Reasoning trace", { exact: false });
  let reasoningSeen = false;
  let reasoningChars = 0;
  let answerSeen = false;

  const deadline = Date.now() + 110_000;
  while (Date.now() < deadline) {
    if (!reasoningSeen && (await reasoningTitle.count()) > 0) {
      reasoningSeen = true;
      await page.screenshot({ path: `${SHOT}/reasoning-visible.png` });
    }
    if (reasoningSeen) {
      const txt = await page
        .locator(".pm-live-scroll-panel")
        .first()
        .innerText()
        .catch(() => "");
      reasoningChars = Math.max(reasoningChars, txt.trim().length);
    }
    const ans = await page
      .locator(".synthesis-body")
      .first()
      .innerText()
      .catch(() => "");
    if (ans && ans.replace(/\s/g, "").length > 60) {
      answerSeen = true;
      break;
    }
    await page.waitForTimeout(600);
  }

  await page.screenshot({ path: `${SHOT}/final.png`, fullPage: true });
  console.log(
    `[reasoning] reasoningSeen=${reasoningSeen} reasoningChars=${reasoningChars} answerSeen=${answerSeen}`,
  );
});
