import { test } from "@playwright/test";

const TARGET_URL = process.env.BASE_URL || "http://localhost:3000";
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD || "013100";
const SHOT_DIR = "tab-screenshots/audit";

test("capture Model Pool tab + chat dropdown", async ({ page }) => {
  await page.setViewportSize({ width: 1400, height: 1000 });
  await page.goto(TARGET_URL);

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

  // Chat dropdown first — show the new Model Pool section at top
  const modelBtn = page.locator('button[title*="chat model" i]').first();
  if (await modelBtn.count()) {
    await modelBtn.click();
    await page.waitForTimeout(400);
    await page.screenshot({
      path: `${SHOT_DIR}/10-dropdown-with-pool.png`,
      fullPage: false,
    });
    await page.keyboard.press("Escape");
    await page.waitForTimeout(200);
  }

  // Settings → Model Pool
  const triggers = [
    page.getByRole("button", { name: /settings/i }),
    page.locator("button:has(svg.lucide-settings-2)"),
    page.locator("button:has(svg.lucide-settings)"),
  ];
  for (const t of triggers) {
    try {
      if (await t.count()) {
        await t.first().click({ timeout: 2000 });
        break;
      }
    } catch {
      /* noop */
    }
  }
  await page.waitForTimeout(500);

  await page.getByRole("button", { name: /Model Pool/i }).first().click();
  await page.waitForTimeout(600);
  await page.screenshot({
    path: `${SHOT_DIR}/11-model-pool-tab.png`,
    fullPage: true,
  });
  console.log("saved 10-dropdown-with-pool.png + 11-model-pool-tab.png");
});
