import { test } from "@playwright/test";

const TARGET_URL = process.env.TARGET_URL || "http://localhost:3000";

test("Capture Corpus Manager open state + create form + detail view", async ({
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
    await page.locator('input[type="text"]').fill("admin");
    await page.locator('input[type="password"]').fill("013100");
    await page.locator('button[type="submit"]').click();
  }
  await page.waitForSelector("text=> touch new_node.md", { timeout: 20000 });

  // Open Corpus Manager
  await page.locator('button[title="Corpus Manager"]').first().click();
  await page.waitForTimeout(800);
  await page.screenshot({
    path: "audit-screenshots/corpus-01-list.png",
    fullPage: false,
  });

  // Open Create Form
  const createBtn = page.locator('button:has-text("New Corpus")').first();
  if (await createBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
    await createBtn.click();
    await page.waitForTimeout(600);
    await page.screenshot({
      path: "audit-screenshots/corpus-02-create-form.png",
      fullPage: false,
    });
  }

  // Count visible corpora
  const listRows = await page.locator("text=/CORPUS/").count();
  console.log(`[AUDIT] CORPUS text occurrences in modal: ${listRows}`);

  // Describe modal bounding + background
  const modal = page.locator('div.bg-\\[\\#242424\\]').first();
  const box = await modal.boundingBox();
  console.log(
    `[AUDIT] Modal box: w=${box?.width?.toFixed(0)} h=${box?.height?.toFixed(0)} x=${box?.x?.toFixed(0)} y=${box?.y?.toFixed(0)}`,
  );
});
