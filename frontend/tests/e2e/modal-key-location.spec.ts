// Screenshot just the Modal row in the API Keys tab so the user sees
// exactly where to paste their token.
import { test } from "@playwright/test";

const TARGET_URL = process.env.BASE_URL || "http://localhost:3000";
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD || "013100";
const SHOT_DIR = "tab-screenshots";

test("locate Modal key in API Keys tab", async ({ page }) => {
  await page.goto(TARGET_URL);

  // Login if needed
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

  // Open Settings — try several likely triggers
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
      /* fall through */
    }
  }
  await page.waitForTimeout(500);

  // Click API Keys tab
  await page
    .getByRole("button", { name: /API Keys/i })
    .first()
    .click();
  await page.waitForTimeout(400);

  // Scroll the Modal row into view
  const modalRow = page.getByText(/Modal Proxy Token/i).first();
  await modalRow.scrollIntoViewIfNeeded();
  await page.waitForTimeout(400);
  await page.screenshot({
    path: `${SHOT_DIR}/04-modal-key-row.png`,
    fullPage: false,
  });
  console.log("saved 04-modal-key-row.png");
});
