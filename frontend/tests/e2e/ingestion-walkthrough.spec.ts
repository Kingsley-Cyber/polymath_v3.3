// End-to-end walkthrough: cloud GPU (Modal) + summary + entity extraction setup.
// Captures every critical UI state the user hits. Does NOT persist data
// (form filled, not submitted) so the flow is repeatable.
//
// Run:  BASE_URL=http://localhost:3000 npx playwright test ingestion-walkthrough.spec.ts
import { test } from "@playwright/test";

const TARGET_URL = process.env.BASE_URL || "http://localhost:3000";
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD || "013100";
const SHOT_DIR = "tab-screenshots/walkthrough";

async function openSettings(page: import("@playwright/test").Page) {
  const triggers = [
    page.getByRole("button", { name: /settings/i }),
    page.locator("button:has(svg.lucide-settings-2)"),
    page.locator("button:has(svg.lucide-settings)"),
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

async function clickTab(
  page: import("@playwright/test").Page,
  label: RegExp,
) {
  await page.getByRole("button", { name: label }).first().click();
  await page.waitForTimeout(500);
}

async function shot(
  page: import("@playwright/test").Page,
  file: string,
  opts?: { full?: boolean },
) {
  await page.screenshot({
    path: `${SHOT_DIR}/${file}`,
    fullPage: opts?.full ?? false,
  });
  console.log(`  → ${file}`);
}

test("cloud GPU + summary + extraction setup walkthrough", async ({
  page,
}) => {
  test.setTimeout(180_000);
  await page.setViewportSize({ width: 1400, height: 1000 });

  console.log("STEP 0: Initial load + login");
  await page.goto(TARGET_URL);
  const needsLogin = await page
    .getByText(/AUTH_REQUIRED|Sign in|Login/i)
    .first()
    .waitFor({ timeout: 5000 })
    .then(() => true)
    .catch(() => false);
  if (needsLogin) {
    await shot(page, "00a-login-screen.png");
    await page.locator('input[type="text"]').first().fill("admin");
    await page.locator('input[type="password"]').first().fill(ADMIN_PASSWORD);
    await page.locator('button[type="submit"]').first().click();
    await page.waitForLoadState("networkidle", { timeout: 10000 });
  }
  await page.waitForTimeout(800);
  await shot(page, "00b-app-loaded.png");

  console.log("STEP 1: Open Settings → Models → Modal Runtime Connection");
  await openSettings(page);
  await page.waitForTimeout(500);
  await clickTab(page, /^Models$/);
  await page
    .getByText(/Runtime Connection/i)
    .first()
    .scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  await shot(page, "01-modal-runtime-connection.png");

  // Fill Modal URL + enable (but don't save — we're just showing the flow)
  console.log("STEP 2: Enable Modal + paste URL");
  const enableCheckbox = page
    .getByLabel(/Enable Modal cloud embedder/i)
    .first();
  if (await enableCheckbox.count()) {
    await enableCheckbox.check();
  }
  const urlInput = page
    .locator('input[placeholder*="modal.run" i]')
    .first();
  if (await urlInput.count()) {
    await urlInput.fill(
      "https://demo--polymath-embedder-qwen3.modal.run",
    );
  }
  await page.waitForTimeout(300);
  await shot(page, "02-modal-filled.png");

  console.log("STEP 3: Settings → API Keys");
  await clickTab(page, /API Keys/i);
  await shot(page, "03-api-keys-top.png", { full: true });

  console.log("STEP 4: Settings → Model Pool (migrated entries)");
  await clickTab(page, /Model Pool/i);
  await shot(page, "04-model-pool.png", { full: true });

  console.log("STEP 5: Open the Add-Model form");
  const addBtn = page
    .getByRole("button", { name: /Add Model to Pool/i })
    .first();
  if (await addBtn.count()) {
    await addBtn.click();
    await page.waitForTimeout(500);
    // Pick a preset to autofill (Kimi/Moonshot)
    const presetSelect = page.locator('select').first();
    if (await presetSelect.count()) {
      await presetSelect.selectOption({ label: /moonshot|kimi/i }).catch(() => {});
    }
    await page.waitForTimeout(300);
    await shot(page, "05-pool-add-form.png", { full: true });
    // Close the form
    await page
      .getByRole("button", { name: /^Cancel$/ })
      .first()
      .click()
      .catch(() => {});
    await page.waitForTimeout(300);
  }

  console.log("STEP 6: Close Settings, open Corpus Manager");
  await page.keyboard.press("Escape");
  await page.waitForTimeout(400);
  // Close all modals — sometimes esc only closes the inner one
  await page.keyboard.press("Escape").catch(() => {});
  await page.waitForTimeout(400);

  // Corpus Manager trigger: database icon in the sidebar header
  const dbBtn = page
    .locator("button:has(svg.lucide-database)")
    .first();
  if (await dbBtn.count()) {
    await dbBtn.click({ timeout: 3000 });
  } else {
    // Fallback — try text
    await page
      .getByRole("button", { name: /corpus/i })
      .first()
      .click()
      .catch(() => {});
  }
  await page.waitForTimeout(800);
  await shot(page, "06-corpus-manager.png");

  console.log("STEP 7: Open New Corpus form");
  const newCorpusBtn = page
    .getByRole("button", { name: /New Corpus/i })
    .first();
  if (await newCorpusBtn.count()) {
    await newCorpusBtn.click();
    await page.waitForTimeout(500);
  }
  await shot(page, "07-new-corpus-form-top.png");

  console.log("STEP 8: Fill name + enable toggles");
  const nameInput = page.locator('input[placeholder*="corpus_name" i]').first();
  if (await nameInput.count()) {
    await nameInput.fill("cloud-gpu-walkthrough-demo");
  }
  // Enable use_neo4j + chunk_summarization
  const neo4jToggle = page.getByLabel(/use_neo4j/i).first();
  if (await neo4jToggle.count()) {
    await neo4jToggle.check();
  }
  const summaryToggle = page.getByLabel(/chunk_summarization/i).first();
  if (await summaryToggle.count()) {
    await summaryToggle.check();
  }
  await page.waitForTimeout(300);
  await shot(page, "08-corpus-toggles-enabled.png");

  console.log("STEP 9: Scroll to Ingestion Models section");
  const ingestionModelsHeader = page.getByText(/Ingestion Models/i).first();
  if (await ingestionModelsHeader.count()) {
    await ingestionModelsHeader.scrollIntoViewIfNeeded();
    await page.waitForTimeout(300);
  }
  await shot(page, "09-ingestion-models-card.png");

  console.log("STEP 10: Scroll to Schema (Ontology-Lite) section");
  const schemaHeader = page.getByText(/Schema \(Ontology-Lite\)/i).first();
  if (await schemaHeader.count()) {
    await schemaHeader.scrollIntoViewIfNeeded();
    await page.waitForTimeout(300);
  }
  await shot(page, "10-schema-ontology-section.png");

  // Fill schema entities + predicates to show the populated state
  const entityTextarea = page
    .locator("textarea")
    .filter({ hasText: "" })
    .first();
  if (await entityTextarea.count()) {
    await entityTextarea.fill("Person\nOrganization\nProject\nConcept");
  }
  const textareas = page.locator("textarea");
  const count = await textareas.count();
  if (count >= 2) {
    await textareas.nth(1).fill("works_at\nbuilt\ndepends_on\nfounded");
  }
  await page.waitForTimeout(300);
  await shot(page, "11-schema-populated.png");

  console.log("STEP 11: Cancel the form (don't mutate state)");
  const cancelBtn = page
    .getByRole("button", { name: /^Cancel$/ })
    .first();
  if (await cancelBtn.count()) {
    await cancelBtn.click().catch(() => {});
    await page.waitForTimeout(300);
  }

  console.log("STEP 12: Close Corpus Manager");
  await page.keyboard.press("Escape");
  await page.waitForTimeout(400);
  await shot(page, "12-back-to-chat.png");

  console.log("DONE.");
});
