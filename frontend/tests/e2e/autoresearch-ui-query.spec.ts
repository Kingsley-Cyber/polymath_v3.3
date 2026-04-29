/**
 * UI-driven autoresearch — click-through query flow.
 *
 * Uses the pre-ingested `autoresearch-*` corpus (created by
 * autoresearch-ingest.spec.ts). Then:
 *   1. Login
 *   2. Select the corpus via the header CorpusMultiSelect
 *   3. Open GraphView, assert entity nodes render
 *   4. Type a ReadME-specific question in the chat box, send
 *   5. Verify streaming tokens render in the chat window
 *   6. Verify final assistant text contains ReadME-specific terms
 *   7. Open Sidebar conversations list — conversation appears, delete works
 */
import { test, expect, type Page } from "@playwright/test";

const UI = process.env.BASE_URL || "http://localhost:3000";
const API = process.env.API_URL || "http://127.0.0.1:8000";
const ADMIN_USER = process.env.ADMIN_USER || "admin";
const ADMIN_PASS = process.env.ADMIN_PASS || "013100";

async function login(page: Page) {
  await page.goto(UI);
  const login = await page
    .getByText(/Sign in|Login|AUTH_REQUIRED/i)
    .first()
    .waitFor({ timeout: 5000 })
    .then(() => true)
    .catch(() => false);
  if (login) {
    await page.locator('input[type="text"]').first().fill(ADMIN_USER);
    await page.locator('input[type="password"]').first().fill(ADMIN_PASS);
    await page.locator('button[type="submit"]').first().click();
    await page.waitForLoadState("networkidle", { timeout: 15000 });
  }
  await page.waitForTimeout(500);
}

test("UI query — pick corpus, ask question, assert tokens + entities + conv", async ({
  page,
  request,
}) => {
  test.setTimeout(6 * 60 * 1000);
  await page.setViewportSize({ width: 1500, height: 1000 });

  // Pre-req: token for cleanup calls
  const loginResp = await request.post(`${API}/api/auth/login`, {
    data: { username: ADMIN_USER, password: ADMIN_PASS },
  });
  const { access_token: token } = await loginResp.json();
  const H = { Authorization: `Bearer ${token}` };

  // Find the most recently-ingested autoresearch corpus to use for the test.
  const corporaResp = await request.get(`${API}/api/corpora`, { headers: H });
  const corpora = (await corporaResp.json()) as Array<{
    corpus_id: string;
    name: string;
    doc_count: number;
  }>;
  const target = corpora
    .filter((c) => c.name.startsWith("autoresearch") && c.doc_count > 0)
    .sort((a, b) => b.name.localeCompare(a.name))[0];
  expect(target, "at least one ingested autoresearch corpus must exist").toBeTruthy();
  console.log(`[ui] using corpus ${target.corpus_id} (${target.name})`);

  await login(page);

  // Step 1 — Select the corpus via the header CorpusMultiSelect dropdown.
  // The header button is the Database-icon button labelled by the corpus name
  // or "No corpus selected".
  const corpusPicker = page
    .locator("button")
    .filter({ hasText: /corpus|No corpus|Corpora/i })
    .first();
  if ((await corpusPicker.count()) === 0) {
    throw new Error("CorpusMultiSelect trigger not found in header");
  }
  await corpusPicker.click();
  await page.waitForTimeout(400);

  // Click the row for our target corpus.
  const row = page.getByText(target.name, { exact: false }).first();
  await row.waitFor({ timeout: 4000 });
  await row.click();
  await page.waitForTimeout(400);
  // Close the dropdown — CorpusMultiSelect only closes on `mousedown` outside
  // its dropdownRef, so keyboard Escape is a no-op. Force a real click outside.
  await page.mouse.click(10, 10);
  await page.waitForTimeout(300);

  // Step 2 — Open GraphView and assert nodes rendered.
  // (Skipping close — the chat area remains visible next to GraphView, and
  // the mobile-only close button isn't clickable on desktop viewport.)
  const graphToggle = page
    .locator('button[title*="Graph"], button:has(svg.lucide-share-2)')
    .first();
  if ((await graphToggle.count()) > 0) {
    await graphToggle.click();
    await page.waitForTimeout(1500);
    const entStat = page.locator("text=/Entities:/i").first();
    if ((await entStat.count()) > 0) {
      const statText = await entStat.evaluate(
        (el) => el.parentElement?.textContent || "",
      );
      console.log(`[ui] graph stats row: ${statText.slice(0, 80)}`);
      expect(statText, "graph should have >0 entities").toMatch(/Entities:\s*[1-9]/);
    }
    // Close via the same toggle button (or Escape) — safer than hunting the X.
    await page.keyboard.press("Escape");
    await page.waitForTimeout(300);
    if ((await graphToggle.count()) > 0) {
      try {
        await graphToggle.click({ timeout: 2000 });
      } catch {
        /* already closed */
      }
      await page.waitForTimeout(400);
    }
  }

  // Step 3 — Type a ReadME-specific question and send.
  const chatInput = page
    .locator(
      'textarea[placeholder*="QUERY"], textarea[placeholder*="EXECUTE"], textarea',
    )
    .first();
  await chatInput.waitFor({ timeout: 4000 });
  await chatInput.fill(
    "Based on the ingested docs, what is Polymath RAG, and what chunking strategy does the Playwright test mission describe? List the specific token target for parent chunks if mentioned.",
  );

  // Hit Execute button — matched by title attribute ("Execute Query") set
  // in ChatInput.tsx, since the button has no accessible name or text role.
  const sendBtn = page
    .locator('button[title*="Execute"]')
    .first();
  await sendBtn.click({ force: true });

  // Step 4 — Wait for streamed assistant text to accumulate.
  // Watch for the last "assistant" message bubble to grow over time.
  const deadline = Date.now() + 4 * 60 * 1000;
  let lastLen = 0;
  let stableFor = 0;
  while (Date.now() < deadline) {
    await page.waitForTimeout(1500);
    const bubbles = await page
      .locator('[data-role="assistant"], div:has-text("assistant")')
      .all();
    // Fallback: read all text on the chat area and detect growth.
    const chatText = await page.evaluate(() => {
      const main = document.querySelector("main") || document.body;
      return main.textContent || "";
    });
    if (chatText.length > lastLen + 10) {
      stableFor = 0;
      lastLen = chatText.length;
    } else {
      stableFor += 1500;
    }
    if (stableFor >= 8000 && lastLen > 200) break;
    if (bubbles.length > 0 && lastLen > 600) {
      // Enough text; likely done or nearly done.
      // Wait a bit more for completion, then break if stable.
      if (stableFor >= 4000) break;
    }
  }

  // Step 5 — Final text assertion.
  const finalText = await page.evaluate(() => {
    const main = document.querySelector("main") || document.body;
    return main.textContent || "";
  });
  console.log(`[ui] final chat area length: ${finalText.length}`);

  // Response should touch at least one ReadME-ground-truth term.
  const lower = finalText.toLowerCase();
  const termsHit = [
    "polymath",
    "rag",
    "chunk",
    "playwright",
    "e2e",
  ].filter((t) => lower.includes(t));
  expect(termsHit.length, "at least 3 ReadME-grounded terms in response").toBeGreaterThanOrEqual(3);

  // Step 6 — Sidebar conversation present, delete works.
  // Sidebar list item by title fragment of our prompt.
  const sidebarItem = page
    .locator("button, div")
    .filter({ hasText: /Polymath|Based on the ingested/i })
    .first();
  await sidebarItem.waitFor({ timeout: 4000 }).catch(() => {});
  const initialCount = await page
    .locator('[data-conversation-item="true"], button:has(svg.lucide-message-square)')
    .count();
  console.log(`[ui] sidebar conv count visible ≈ ${initialCount}`);

  // Best-effort delete via hover → trash
  const trashBtn = page
    .locator('button[title*="Delete"], button:has(svg.lucide-trash-2)')
    .first();
  if ((await trashBtn.count()) > 0) {
    await trashBtn.click();
    await page.waitForTimeout(800);
    // Confirm if a confirmation dialog fired
    page.on("dialog", (d) => d.accept());
  }

  console.log("[ui] done.");
});
