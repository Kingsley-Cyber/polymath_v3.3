/**
 * Live-trace "tabs" — TRUE frontend E2E.
 *
 * Unlike scripts/e2e_chat_test.py (which only asserts the backend SSE stream),
 * this drives the real React UI in a browser and asserts that the live process
 * timeline actually RENDERS: the per-step collapsible cards ("tabs") generate
 * while streaming, the running step is animated, finished steps auto-collapse,
 * and a finished card can be re-expanded. Backend correctness is out of scope
 * here — we assert what the user sees.
 *
 * Run:
 *   ADMIN_USER=admin ADMIN_PASSWORD=*** \
 *   BASE_URL=http://localhost:3000 API_URL=http://localhost:8000 \
 *   npx playwright test tests/e2e/live-trace-tabs.spec.ts --project=chromium --reporter=line
 */
import { test, expect, type Page } from "@playwright/test";
import * as fs from "fs";

const BASE = process.env.BASE_URL || "http://localhost:3000";
const API = process.env.API_URL || "http://localhost:8000";
const USER = process.env.ADMIN_USER || "admin";
const PASS = process.env.ADMIN_PASSWORD || "";
const CORPUS_NAME = process.env.CORPUS_NAME || "authentic_library";
const QUERY =
  process.env.PROBE_QUERY ||
  "What is NLP and how do the documents in this library describe it?";
const SHOT_DIR = "tab-screenshots/live-trace";

// Local viewport + kill the global 4.8s slowMo (meant for the audit specs).
// actionTimeout bounds every click/fill so a non-actionable element can never
// hang the whole test.
test.use({
  launchOptions: { slowMo: 0 },
  viewport: { width: 1440, height: 900 },
  actionTimeout: 15_000,
});

test.setTimeout(220_000);

fs.mkdirSync(SHOT_DIR, { recursive: true });

const report: string[] = [];
const log = (m: string) => {
  report.push(m);
  // eslint-disable-next-line no-console
  console.log(`[live-trace] ${m}`);
};

test("live process-timeline tabs render, stream, and collapse in the real UI", async ({
  page,
  request,
}) => {
  const consoleErrors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text().slice(0, 300));
  });
  page.on("pageerror", (err) => consoleErrors.push(`PAGEERROR: ${String(err).slice(0, 300)}`));

  // ── 1. Authenticate via API, inject token+user into the store's localStorage
  // (skips the login form; keeps the secret out of the DOM / test source).
  const loginResp = await request.post(`${API}/api/auth/login`, {
    data: { username: USER, password: PASS },
  });
  expect(loginResp.ok(), `login failed (${loginResp.status()})`).toBeTruthy();
  const token = (await loginResp.json()).access_token as string;
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
  log(`authenticated as ${me?.username ?? USER}`);

  // ── 2. Load the app authenticated.
  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  await expect(
    page.getByTestId("query-input"),
    "chat composer should render when authenticated (not stuck on login)",
  ).toBeVisible({ timeout: 25_000 });
  await page.waitForTimeout(800); // let models/corpora sync
  await page.screenshot({ path: `${SHOT_DIR}/01-loaded.png`, fullPage: false });

  // ── 3. Corpus scope. Best-effort pick of the target corpus through the real
  // selector; if the selector isn't actionable we just proceed with the default
  // "[ALL CORPORA]" scope (which already covers the single corpus on this
  // instance). Either way the query runs against real indexed data.
  try {
    await page.getByTestId("corpus-multi-select").click({ timeout: 6000, force: true });
    const opt = page.getByRole("button", { name: new RegExp(CORPUS_NAME, "i") });
    await opt.first().click({ timeout: 5000 });
    await page.keyboard.press("Escape");
    log(`corpus selector reads: "${(await page.getByTestId("corpus-multi-select").innerText()).trim()}"`);
  } catch (e) {
    log(`corpus pick skipped → using ALL CORPORA default (${String(e).split("\n")[0].slice(0, 90)})`);
    await page.keyboard.press("Escape").catch(() => {});
  }

  // ── 4. Send a query.
  await page.getByTestId("query-input").click();
  await page.getByTestId("query-input").fill(QUERY);
  await page.getByTestId("query-submit").click();
  const tSubmit = Date.now();
  log(`submitted query: "${QUERY}"`);

  // No blank screen on fire: the taxonomy must paint almost immediately.
  await page.waitForTimeout(180);
  await page.screenshot({ path: `${SHOT_DIR}/02a-just-fired.png`, fullPage: false });

  // ── 5. ASSERT: the live trace cards ("tabs") appear while streaming.
  const card = page.locator(".process-group");
  try {
    await expect(card.first()).toBeVisible({ timeout: 45_000 });
  } catch (e) {
    await page.screenshot({ path: `${SHOT_DIR}/ERR-no-cards.png`, fullPage: true });
    const txt = (await page.getByTestId("response-panel").innerText().catch(() => "")).slice(0, 600);
    throw Object.assign(
      new Error(
        `No .process-group cards rendered within 45s of submit. Panel text:\n${txt}\nconsoleErrors=${consoleErrors.join(" | ")}\n(${e})`,
      ),
      { cause: e },
    );
  }
  log(`first trace card appeared after ${((Date.now() - tSubmit) / 1000).toFixed(1)}s`);

  // ── ORDER: the taxonomy (process cards) must render ABOVE the answer/draft.
  {
    const cardBox = await card.first().boundingBox();
    const draft = page
      .locator(".pm-live-answer-draft, .message-assistant .message-text")
      .first();
    const draftBox = await draft.boundingBox().catch(() => null);
    if (cardBox && draftBox) {
      log(`order check: taxonomy.y=${Math.round(cardBox.y)} answer.y=${Math.round(draftBox.y)}`);
      expect(
        cardBox.y,
        "taxonomy (process cards) must render ABOVE the answer bubble",
      ).toBeLessThan(draftBox.y);
    } else {
      log(`order check skipped (cardBox=${!!cardBox} draftBox=${!!draftBox})`);
    }
  }

  // Sample the live stream: prove MULTIPLE distinct step cards generate over
  // time (a real waterfall), that the running step is animated (.shiny-text),
  // and that the GEN/USE/WWW/RES badge taxonomy is present. Capture a mid-stream
  // screenshot at the richest moment seen.
  let maxCards = 0;
  let sawShiny = false;
  let sawActive = false;
  let sawPaintedCard = false; // a card with a real (non-zero) bounding box
  const badges = new Set<string>();
  let burst = 0;
  const sampleUntil = Date.now() + 160_000;
  let stableSig = "";
  let stableTicks = 0;

  const scanBadges = async () => {
    for (const b of await page.locator(".process-group .pm-tax-badge").allInnerTexts().catch(() => [] as string[])) {
      // Taxonomy badges render as "<EXE>" / "<GEN>" / "<RSN>" / "<WWW>" / "<TOOL>".
      const m = b.trim().toUpperCase().match(/^<?(EXE|WWW|TOOL|RSN|GEN|RES|WRN|INF)>?$/);
      if (m) badges.add(m[1]);
    }
  };

  while (Date.now() < sampleUntil) {
    const n = await card.count();
    maxCards = Math.max(maxCards, n);
    const active = await page.locator(".process-group-active").count();
    if (active > 0) sawActive = true;
    if (await page.locator(".pm-process-title.shiny-text").count()) sawShiny = true;
    await scanBadges();

    // Prove the leading card is actually painted (visible, non-zero size) — not
    // just present in the DOM. This distinguishes a real render from a ghost.
    if (n > 0) {
      const box = await card.first().boundingBox().catch(() => null);
      if (box && box.width > 40 && box.height > 8) {
        sawPaintedCard = true;
        // Burst-capture the live waterfall while it's on screen.
        if (active > 0 && burst < 6) {
          burst += 1;
          await page.screenshot({ path: `${SHOT_DIR}/02-stream-${String(burst).padStart(2, "0")}.png`, fullPage: false });
          if (burst === 1)
            log(`live card painted @ cards=${n} active=${active} box=${Math.round(box.width)}x${Math.round(box.height)}`);
        }
      }
    }

    // Completion: no running card + answer length stable for ~4 ticks.
    const ansLen = (await page.getByTestId("response-panel").innerText().catch(() => "")).length;
    const sig = `${n}|${ansLen}`;
    if (sig === stableSig) stableTicks += 1;
    else {
      stableSig = sig;
      stableTicks = 0;
    }
    if (active === 0 && n >= 1 && stableTicks >= 4) break;

    await page.waitForTimeout(active > 0 ? 500 : 1000);
  }
  await scanBadges();
  log(
    `stream done. maxCards=${maxCards} sawActive=${sawActive} sawShiny=${sawShiny} paintedCard=${sawPaintedCard} liveShots=${burst} badges=[${[...badges].join(",")}]`,
  );

  // ── Hard assertions on the FRONTEND trace UI (the user's actual ask).
  expect(maxCards, "expected multiple live step cards (a waterfall, not one burst)").toBeGreaterThanOrEqual(2);
  expect(sawActive || sawShiny, "expected a running/animated step during streaming").toBeTruthy();
  expect(sawPaintedCard, "live trace card must actually paint (non-zero box), not just exist in the DOM").toBeTruthy();
  expect([...badges].length, `expected GEN/USE/RES-style badges to render; got [${[...badges].join(",")}]`).toBeGreaterThan(0);

  // ── 6. Final state: answer + collapsible cards.
  await page.waitForTimeout(1200);
  await page.screenshot({ path: `${SHOT_DIR}/03-final.png`, fullPage: true });

  const finalCards = await card.count();
  expect(finalCards, "trace cards should persist after completion").toBeGreaterThanOrEqual(1);

  // Collapse + re-expand: a finished card header toggles aria-expanded.
  const headers = page.locator(".process-group-header");
  const hCount = await headers.count();
  let toggled = false;
  for (let i = 0; i < hCount; i += 1) {
    const h = headers.nth(i);
    const before = await h.getAttribute("aria-expanded");
    if (before === "false") {
      await h.click();
      await page.waitForTimeout(300);
      const after = await h.getAttribute("aria-expanded");
      if (after === "true") {
        toggled = true;
        log(`re-expanded a collapsed card (aria-expanded false→true)`);
        await page.screenshot({ path: `${SHOT_DIR}/04-reexpanded.png`, fullPage: false });
        break;
      }
    }
  }
  expect(
    toggled,
    "expected at least one auto-collapsed card that re-expands on click",
  ).toBeTruthy();

  // ── Soft / informational: answer text + sources (model may be think-only).
  const answerText = (await page.getByTestId("response-panel").innerText()).replace(/\s+/g, " ").trim();
  const answerLen = answerText.length;
  const sources = await page.getByTestId("source-citations").count();
  log(`answer_chars≈${answerLen} sources_panel=${sources}`);
  if (answerLen < 120) log(`WARN: answer looks short/empty (model may be thinking-only)`);
  if (sources === 0) log(`WARN: no source-citations panel rendered`);

  // ── No client crashes.
  if (consoleErrors.length) log(`console errors: ${consoleErrors.slice(0, 8).join(" | ")}`);
  const fatal = consoleErrors.filter((e) =>
    /Minified React error|Cannot read|is not a function|Unexpected|TypeError/.test(e),
  );
  expect(fatal, `fatal client errors: ${fatal.join(" | ")}`).toHaveLength(0);

  log("RESULT: live-trace tabs render, stream, and collapse correctly in the real UI.");
  fs.writeFileSync(`${SHOT_DIR}/report.txt`, report.join("\n"));
});

// keep Page import used for clarity in helpers if extended later
void (null as unknown as Page);
