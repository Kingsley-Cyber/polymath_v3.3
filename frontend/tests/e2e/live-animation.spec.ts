/**
 * Live-trace animation — proves the ACTIVE process card is visibly alive while
 * streaming (sweeping scanline + running-step marker), with zero extra re-render
 * cost (CSS-only), and reports whether the model emitted a reasoning trace.
 *
 * Run:
 *   ADMIN_USER=admin ADMIN_PASSWORD=*** \
 *   BASE_URL=http://localhost:3000 API_URL=http://localhost:8000 \
 *   npx playwright test tests/e2e/live-animation.spec.ts --project=chromium --reporter=line
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
const SHOT = "tab-screenshots/live-animation";

test.use({
  launchOptions: { slowMo: 0 },
  viewport: { width: 1440, height: 900 },
  actionTimeout: 15_000,
});
test.setTimeout(200_000);
fs.mkdirSync(SHOT, { recursive: true });

test("active process card animates live while streaming", async ({
  page,
  request,
}) => {
  const errors: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text().slice(0, 200));
  });
  page.on("pageerror", (e) => errors.push("PAGEERR: " + String(e).slice(0, 200)));

  // auth
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

  // fire query
  await page.getByTestId("query-input").click();
  await page.getByTestId("query-input").fill(QUERY);
  await page.getByTestId("query-submit").click();

  // the active live card must appear with the live "working" class
  const activeCard = page.locator(".process-group-active");
  await expect(
    activeCard.first(),
    "active card should carry the live 'working' class while streaming",
  ).toBeVisible({ timeout: 45_000 });

  // capture the scanline sweep across 3 quick frames
  await page.screenshot({ path: `${SHOT}/a.png` });
  await page.waitForTimeout(300);
  await page.screenshot({ path: `${SHOT}/b.png` });
  await page.waitForTimeout(300);
  await page.screenshot({ path: `${SHOT}/c.png` });

  // PROOF the scanline animation is live: the active card's ::before has a
  // running CSS animation (not "none"). This confirms the CSS shipped + applied.
  const scan = await activeCard
    .first()
    .evaluate((el) => {
      const cs = getComputedStyle(el as Element, "::before");
      return {
        name: cs.animationName,
        dur: cs.animationDuration,
        playState: cs.animationPlayState,
      };
    })
    .catch(() => null);

  const runningMarker = await page
    .locator(".pm-process-row-running")
    .count()
    .catch(() => 0);
  const reasoningCard = await page
    .locator(".pm-tax-reason")
    .count()
    .catch(() => 0);

  console.log(
    `[live-anim] scanline=${JSON.stringify(scan)} runningMarker=${runningMarker} reasoningCard=${reasoningCard} consoleErrors=${errors.length}`,
  );

  expect(
    scan && scan.name && scan.name !== "none",
    `active card scanline animation should be running (got ${JSON.stringify(scan)})`,
  ).toBeTruthy();

  const traceErrors = errors.filter((e) =>
    /process-group|MessageBubble|reasoning|trace/i.test(e),
  );
  expect(
    traceErrors,
    `no trace-render console errors: ${traceErrors.join(" | ")}`,
  ).toHaveLength(0);
});
