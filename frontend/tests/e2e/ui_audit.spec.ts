import { test } from "@playwright/test";

const TARGET_URL = process.env.TARGET_URL || "http://localhost:3000";
const USERNAME = "admin";
const PASSWORD = "013100";

test("UI audit: capture current state", async ({ page }) => {
  page.setViewportSize({ width: 1440, height: 900 });

  await page.goto(TARGET_URL);
  const loginVisible = await page
    .locator("text=AUTH_REQUIRED")
    .first()
    .isVisible({ timeout: 5000 })
    .catch(() => false);
  if (loginVisible) {
    await page.locator('input[type="text"]').fill(USERNAME);
    await page.locator('input[type="password"]').fill(PASSWORD);
    await page.locator('button[type="submit"]').click();
  }
  await page.waitForSelector("text=> touch new_node.md", { timeout: 20000 });
  await page.waitForTimeout(800);

  // 1) Main app shell
  await page.screenshot({ path: "audit-screenshots/01-main.png", fullPage: false });

  // 2) Top header zoom
  const header = page.locator("header").first();
  await header.screenshot({ path: "audit-screenshots/02-header.png" });
  const hbox = await header.boundingBox();
  console.log(`[AUDIT] Header height: ${hbox?.height}px width: ${hbox?.width}px`);

  // 3) Sidebar (where corpus selector + settings live)
  await page.screenshot({ path: "audit-screenshots/03-sidebar.png", clip: { x: 0, y: 0, width: 320, height: 900 } });

  // 4) Chat input area
  const chatInputArea = page.locator('textarea[placeholder*="EXECUTE"]').first();
  if (await chatInputArea.isVisible()) {
    const cbox = await chatInputArea.boundingBox();
    console.log(`[AUDIT] Chat textarea: ${cbox?.width}x${cbox?.height}px at x=${cbox?.x}`);
    const chatContainer = page.locator('div:has(> textarea[placeholder*="EXECUTE"])').first();
    await chatContainer.screenshot({ path: "audit-screenshots/04-chatinput.png" });
  }

  // 5) Open Corpus Manager
  const corpusBtn = page.locator('button[title="Corpus Manager"]');
  if (await corpusBtn.first().isVisible()) {
    await corpusBtn.first().click();
    await page.waitForTimeout(800);
    await page.screenshot({ path: "audit-screenshots/05-corpus-manager.png", fullPage: false });
    // close
    await page.keyboard.press("Escape");
    await page.waitForTimeout(400);
  }

  // 6) Open Settings
  const settingsBtn = page.locator('button[title="Settings"]').first();
  await settingsBtn.click();
  await page.waitForTimeout(800);
  await page.screenshot({ path: "audit-screenshots/06-settings.png", fullPage: false });
  await page.keyboard.press("Escape");
  await page.waitForTimeout(400);

  // 7) Look for the agentic button/badge
  const agenticBadge = page.locator('text=/AGENTIC/i').first();
  if (await agenticBadge.isVisible({ timeout: 2000 }).catch(() => false)) {
    const text = await agenticBadge.textContent();
    const cls = await agenticBadge.getAttribute("class");
    console.log(`[AUDIT] Agentic badge text: "${text}"`);
    console.log(`[AUDIT] Agentic badge class: ${cls?.slice(0, 200)}`);
  } else {
    console.log("[AUDIT] No AGENTIC badge currently visible");
  }

  // 8) Look for "agentic" anywhere in chat header area
  const agenticAny = page.locator('text=/agentic/i');
  const count = await agenticAny.count();
  console.log(`[AUDIT] 'agentic' text occurrences in DOM: ${count}`);
  for (let i = 0; i < Math.min(count, 5); i++) {
    const t = await agenticAny.nth(i).textContent();
    const v = await agenticAny.nth(i).isVisible();
    console.log(`  [${i}] visible=${v} text="${t?.trim()?.slice(0, 80)}"`);
  }

  console.log("[AUDIT] Done — screenshots saved to frontend/audit-screenshots/");
});
