// Full-page screenshots of Models / Custom Models / API Keys + chat dropdown,
// plus any visible cross-links between them. Used to audit integration gaps.
import { test } from "@playwright/test";

const TARGET_URL = process.env.BASE_URL || "http://localhost:3000";
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD || "013100";
const SHOT_DIR = "tab-screenshots/audit";

async function openSettings(page: import("@playwright/test").Page) {
  const triggers = [
    page.getByRole("button", { name: /settings/i }),
    page.locator('[aria-label*="settings" i]'),
    page.locator('[title*="settings" i]'),
    page.locator("button:has(svg.lucide-settings)"),
    page.locator("button:has(svg.lucide-settings-2)"),
  ];
  for (const t of triggers) {
    try {
      if (await t.count()) {
        await t.first().click({ timeout: 2000 });
        return;
      }
    } catch {
      /* noop */
    }
  }
}

async function snapTab(
  page: import("@playwright/test").Page,
  tabLabel: RegExp,
  file: string,
) {
  await page.getByRole("button", { name: tabLabel }).first().click();
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${SHOT_DIR}/${file}`, fullPage: true });
  console.log(`saved ${file}`);
}

test("audit three tabs + dropdown", async ({ page }) => {
  await page.setViewportSize({ width: 1400, height: 900 });
  await page.goto(TARGET_URL);

  // Login
  const needsLogin = await page
    .getByText(/AUTH_REQUIRED|Sign in|Login/i)
    .first()
    .waitFor({ timeout: 5000 })
    .then(() => true)
    .catch(() => false);
  if (needsLogin) {
    await page.locator('input[type="text"]').first().fill("admin");
    await page.locator('input[type="password"]').first().fill(ADMIN_PASSWORD);
    await page.locator('button[type="submit"]').first().click();
    await page.waitForLoadState("networkidle", { timeout: 10000 });
  }
  await page.waitForTimeout(800);

  // ── 1. Chat-time model dropdown (shows Custom Profiles + discovered) ──
  // The ModelSelector sits in the header. Click its button (the sparkles icon).
  const modelBtn = page
    .locator('button[title*="chat model" i]')
    .first();
  if (await modelBtn.count()) {
    await modelBtn.click();
    await page.waitForTimeout(300);
    await page.screenshot({
      path: `${SHOT_DIR}/00-chat-dropdown.png`,
      fullPage: false,
    });
    // Close by pressing Escape
    await page.keyboard.press("Escape");
    await page.waitForTimeout(200);
  }

  // ── 2. Open Settings ──
  await openSettings(page);
  await page.waitForTimeout(500);

  // ── 3. Each tab ──
  await snapTab(page, /^Models$/, "01-models-full.png");
  await snapTab(page, /Custom Models/i, "02-custom-models-full.png");
  await snapTab(page, /API Keys/i, "03-api-keys-full.png");
});
