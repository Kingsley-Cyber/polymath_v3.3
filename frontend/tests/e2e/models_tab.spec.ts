import { test, expect } from "@playwright/test";

const TARGET_URL = process.env.TARGET_URL || "http://localhost:3000";
const USERNAME = "admin";
const PASSWORD = "013100";

test("Settings → Models tab renders + shows installed list or reachable error", async ({
  page,
}) => {
  page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(TARGET_URL);
  const needLogin = await page
    .locator("text=AUTH_REQUIRED")
    .first()
    .isVisible({ timeout: 5000 })
    .catch(() => false);
  if (needLogin) {
    await page.locator('input[type="text"]').fill(USERNAME);
    await page.locator('input[type="password"]').fill(PASSWORD);
    await page.locator('button[type="submit"]').click();
  }
  await page.waitForSelector("text=> touch new_node.md", { timeout: 20000 });

  // Open Settings
  await page.locator('button[title="Settings"]').first().click();
  await expect(page.locator('h2:has-text("Security")')).toBeVisible({
    timeout: 5000,
  });

  // Click the Models tab
  const modelsTab = page.locator('button:has-text("Models")').first();
  await modelsTab.click();

  // Header renders
  await expect(page.locator('h2:has-text("Models")')).toBeVisible({
    timeout: 5000,
  });

  // Pull form elements present
  await expect(page.locator('input[placeholder="model:tag"]')).toBeVisible();
  await expect(page.locator('button:has-text("Pull")').first()).toBeVisible();

  // Installed section header present
  await expect(
    page.locator("text=/Installed \\(\\d+\\)/").first(),
  ).toBeVisible({ timeout: 10000 });

  await page.waitForTimeout(1000);
  await page.screenshot({
    path: "audit-screenshots/07-models-tab.png",
    fullPage: false,
  });

  // Log the installed count or error
  const countText =
    (await page.locator("text=/Installed \\(\\d+\\)/").textContent()) || "";
  console.log(`[TEST] ${countText.trim()}`);

  const errorVisible = await page
    .locator("text=/Cannot reach Ollama|HTTP 503/")
    .first()
    .isVisible()
    .catch(() => false);
  console.log(
    `[TEST] Ollama reachable: ${!errorVisible} (503 shown = Ollama container down; host Ollama still works via alt config)`,
  );
});
