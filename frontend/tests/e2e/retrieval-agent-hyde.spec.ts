import { test } from "@playwright/test";

const TARGET_URL = process.env.BASE_URL || "http://localhost:3000";
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD || "013100";
const SHOT_DIR = "tab-screenshots/audit";

test("Retrieval & Agent + Retrieval tabs — hunt for HyDE", async ({ page }) => {
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
  await page.waitForTimeout(600);

  // Open Settings
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

  for (const [label, file] of [
    [/Retrieval & Agent/i, "04-retrieval-agent.png"],
    [/^Retrieval$/, "05-retrieval.png"],
    [/^Ingestion$/, "06-ingestion.png"],
    [/General/i, "07-general.png"],
  ] as Array<[RegExp, string]>) {
    try {
      await page.getByRole("button", { name: label }).first().click();
      await page.waitForTimeout(500);
      await page.screenshot({ path: `${SHOT_DIR}/${file}`, fullPage: true });
      console.log(`saved ${file}`);
    } catch (e) {
      console.log(`skipped ${file}: ${e}`);
    }
  }
});
