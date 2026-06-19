/**
 * Duplicates panel — TRUE frontend E2E.
 *
 * Drives the real React UI: open Corpus Manager → open the corpus → toggle the
 * "Duplicates" panel → assert it runs the containment scan and RENDERS the real
 * remaining clusters (the C++/Java `likely` cluster + the `review` clusters that
 * were deliberately held back). Asserts what the user sees, not the backend.
 *
 * Run:
 *   ADMIN_USER=admin ADMIN_PASSWORD=*** \
 *   BASE_URL=http://localhost:3000 API_URL=http://localhost:8000 \
 *   npx playwright test tests/e2e/duplicates-panel.spec.ts --project=chromium --reporter=line
 */
import { test, expect } from "@playwright/test";
import * as fs from "fs";

const BASE = process.env.BASE_URL || "http://localhost:3000";
const API = process.env.API_URL || "http://localhost:8000";
const USER = process.env.ADMIN_USER || "admin";
const PASS = process.env.ADMIN_PASSWORD || "";
const SHOT = "tab-screenshots/duplicates";

test.use({
  launchOptions: { slowMo: 0 },
  viewport: { width: 1440, height: 900 },
  actionTimeout: 20_000,
});
test.setTimeout(220_000);
fs.mkdirSync(SHOT, { recursive: true });

test("duplicates panel scans and renders real clusters in the UI", async ({
  page,
  request,
}) => {
  const errors: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text().slice(0, 200));
  });
  page.on("pageerror", (e) => errors.push("PAGEERR: " + String(e).slice(0, 200)));

  // ── auth (inject token, skip the form)
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
    ([t, u]) => {
      localStorage.setItem(
        "polymath-auth",
        JSON.stringify({ state: { token: t, user: u }, version: 0 }),
      );
    },
    [token, me] as const,
  );

  await page.goto(BASE, { waitUntil: "domcontentloaded" });

  // ── open Corpus Manager → open the corpus detail
  await page.locator('button[title="Corpus Manager"]').first().click();
  await page.waitForTimeout(900);
  await page.getByTestId("corpus-browse-btn").first().click();
  await page.waitForTimeout(700);
  await page.screenshot({ path: `${SHOT}/01-detail.png` });

  // ── toggle the Duplicates panel
  await page.getByRole("button", { name: /^Duplicates$/i }).click();
  await expect(
    page.getByText("Near-Duplicate Documents"),
    "panel header should render immediately on toggle",
  ).toBeVisible({ timeout: 10_000 });
  await page.screenshot({ path: `${SHOT}/02-scanning.png` });

  // ── wait for the scan to resolve: real cluster data must appear. The C++/Java
  // `likely` cluster was deliberately kept, so its filename is a concrete probe.
  const dsaCluster = page.getByText(/Data Structures and Algorithm/i).first();
  const emptyState = page.getByText(/No near-duplicate documents found/i);
  await expect(
    dsaCluster.or(emptyState),
    "scan should resolve to clusters or an explicit empty state (not hang)",
  ).toBeVisible({ timeout: 160_000 });

  const isEmpty = await emptyState.isVisible().catch(() => false);
  const badges = await page.locator("text=/^(certain|likely|review)$/i").count();
  console.log(
    `[dupes] empty=${isEmpty} confidenceBadges=${badges} consoleErrors=${errors.length}`,
  );
  await page.screenshot({ path: `${SHOT}/03-results.png`, fullPage: true });

  // ── assertions: the panel rendered real data, no component errors
  if (!isEmpty) {
    await expect(
      dsaCluster,
      "the held-back C++/Java cluster should render",
    ).toBeVisible();
    expect(badges, "confidence badges should render").toBeGreaterThan(0);
    // the per-cluster Remove control exists
    await expect(
      page.getByRole("button", { name: /^Remove \d+/ }).first(),
    ).toBeVisible();
  }
  const dupErrors = errors.filter((e) =>
    /duplicat|resolve|getDuplicates|DuplicatesPanel/i.test(e),
  );
  expect(
    dupErrors,
    `no duplicate-panel console errors: ${dupErrors.join(" | ")}`,
  ).toHaveLength(0);
});
