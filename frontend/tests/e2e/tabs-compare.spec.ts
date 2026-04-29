// Walk through Models / Custom Models / API Keys tabs and screenshot each.
// Run: BASE_URL=http://localhost:3000 npx playwright test tabs-compare.spec.ts
import { test } from "@playwright/test";

const TARGET_URL = process.env.BASE_URL || "http://localhost:3000";
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD || "013100";

// Resolve relative to project root (playwright cwd = frontend/).
const SHOT_DIR = "tab-screenshots";

async function openSettings(page: import("@playwright/test").Page) {
  // Try every likely trigger in priority order; first one that matches wins.
  const triggers = [
    page.getByRole("button", { name: /settings/i }),
    page.locator('[aria-label*="settings" i]').first(),
    page.locator('[title*="settings" i]').first(),
    page.locator("button").filter({ hasText: /SETTINGS/ }).first(),
    page.locator("button:has(svg.lucide-settings)").first(),
    page.locator("button:has(svg.lucide-settings-2)").first(),
  ];
  for (const t of triggers) {
    try {
      if (await t.count()) {
        await t.first().click({ timeout: 2000 });
        return;
      }
    } catch {
      /* fall through */
    }
  }
  throw new Error("Could not find a Settings trigger button.");
}

test("capture Models / Custom Models / API Keys tabs", async ({ page }) => {
  page.on("console", (m) =>
    console.log(`[browser ${m.type()}] ${m.text()}`),
  );
  page.on("pageerror", (e) => console.log(`[browser ERROR] ${e.message}`));

  await page.goto(TARGET_URL);

  // Login if we land on the auth view.
  const maybeAuth = page
    .getByText(/AUTH_REQUIRED|Sign in|Login/i)
    .first()
    .waitFor({ timeout: 5000 })
    .then(() => true)
    .catch(() => false);
  if (await maybeAuth) {
    await page.locator('input[type="text"]').first().fill("admin");
    await page.locator('input[type="password"]').first().fill(ADMIN_PASSWORD);
    await page.locator('button[type="submit"]').first().click();
    await page.waitForLoadState("networkidle", { timeout: 10000 });
  }

  // Give the app a beat to settle.
  await page.waitForTimeout(800);

  await openSettings(page);
  await page.waitForTimeout(400);

  // Full-panel screenshot of whatever tab opens first.
  await page.screenshot({
    path: `${SHOT_DIR}/00-settings-default.png`,
    fullPage: true,
  });

  // Click each of the three tabs in turn.
  const tabs: Array<{ label: RegExp; file: string }> = [
    { label: /^Models$/, file: "01-models.png" },
    { label: /Custom Models/i, file: "02-custom-models.png" },
    { label: /API Keys/i, file: "03-api-keys.png" },
  ];

  for (const { label, file } of tabs) {
    const target = page.getByRole("button", { name: label }).first();
    await target.scrollIntoViewIfNeeded();
    await target.click();
    await page.waitForTimeout(500);
    await page.screenshot({
      path: `${SHOT_DIR}/${file}`,
      fullPage: true,
    });
    console.log(`saved ${file}`);
  }
});
