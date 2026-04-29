// Screenshot the new Modal Runtime Connection block in the Models tab.
import { test } from "@playwright/test";

const TARGET_URL = process.env.BASE_URL || "http://localhost:3000";
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD || "013100";
const SHOT_DIR = "tab-screenshots";

test("capture Modal runtime UI", async ({ page }) => {
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
        break;
      }
    } catch {
      /* noop */
    }
  }
  await page.waitForTimeout(500);

  await page.getByRole("button", { name: /^Models$/ }).first().click();
  await page.waitForTimeout(600);

  // Scroll the "Runtime Connection" heading into view.
  const runtime = page.getByText(/Runtime Connection/i).first();
  await runtime.scrollIntoViewIfNeeded();
  await page.waitForTimeout(400);

  await page.screenshot({
    path: `${SHOT_DIR}/05-modal-runtime.png`,
    fullPage: false,
  });
  console.log("saved 05-modal-runtime.png");
});
