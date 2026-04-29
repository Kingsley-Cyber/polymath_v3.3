import { test, expect } from "@playwright/test";

// Smoke test for the Phase 17.4 post-fix: Settings modal opens, Agentic
// toggle flips state, PUT /api/settings persists, reload hydrates back to
// the persisted value.

const TARGET_URL = process.env.TARGET_URL || "http://localhost:3000";
const USERNAME = "admin";
const PASSWORD = "013100";

test("Settings modal: Agentic toggle persists across reload", async ({
  page,
}) => {
  const apiCalls: { url: string; status: number; method: string }[] = [];
  page.on("response", async (resp) => {
    const url = resp.url();
    if (url.includes("/api/settings")) {
      apiCalls.push({
        url: url.split("/api")[1],
        status: resp.status(),
        method: resp.request().method(),
      });
    }
  });
  page.on("console", (msg) => {
    if (msg.type() === "error" || msg.type() === "warning") {
      console.log(`[BROWSER ${msg.type().toUpperCase()}] ${msg.text()}`);
    }
  });
  page.on("pageerror", (err) => {
    console.log(`[PAGE ERROR] ${err.message}`);
  });

  // ── 1. Login ──────────────────────────────────────────────────────────
  await page.goto(TARGET_URL);
  const isLogin = await page
    .locator("text=AUTH_REQUIRED")
    .first()
    .isVisible({ timeout: 8000 })
    .catch(() => false);

  if (isLogin) {
    await page.locator('input[type="text"]').fill(USERNAME);
    await page.locator('input[type="password"]').fill(PASSWORD);
    await page.locator('button[type="submit"]').click();
  }

  // Wait for the app shell
  await expect(page.locator("text=> touch new_node.md")).toBeVisible({
    timeout: 20000,
  });

  // ── 2. Open Settings ──────────────────────────────────────────────────
  await page.locator('button[title="Settings"]').first().click();

  // Settings modal header should be visible
  await expect(page.locator('h2:has-text("Security")')).toBeVisible({
    timeout: 5000,
  });

  // ── 3. Navigate to Retrieval & Agent tab ──────────────────────────────
  await page.locator('button:has-text("Retrieval & Agent")').first().click();
  await expect(
    page.locator('h2:has-text("Retrieval & Agent")'),
  ).toBeVisible({ timeout: 5000 });

  // ── 4. Capture initial Agentic toggle state, flip it, capture new state
  const toggle = page.locator('button[aria-label="Toggle agentic mode"]');
  await expect(toggle).toBeVisible();
  const beforeClass = (await toggle.getAttribute("class")) || "";
  const wasOn = beforeClass.includes("bg-amber-500");
  console.log(`[TEST] Agentic toggle before flip: ${wasOn ? "ON" : "OFF"}`);

  await toggle.click();

  // Give the PUT a moment
  await page.waitForTimeout(1500);

  const afterClass = (await toggle.getAttribute("class")) || "";
  const isOn = afterClass.includes("bg-amber-500");
  console.log(`[TEST] Agentic toggle after flip: ${isOn ? "ON" : "OFF"}`);
  expect(isOn).toBe(!wasOn);

  // ── 5. Verify GET + PUT fired and returned 200 ────────────────────────
  const getCalls = apiCalls.filter((c) => c.method === "GET");
  const putCalls = apiCalls.filter((c) => c.method === "PUT");
  console.log(`[TEST] GET /api/settings calls:`, getCalls);
  console.log(`[TEST] PUT /api/settings calls:`, putCalls);
  expect(getCalls.length).toBeGreaterThan(0);
  for (const c of getCalls) expect(c.status).toBe(200);
  expect(putCalls.length).toBeGreaterThan(0);
  for (const c of putCalls) expect(c.status).toBe(200);

  // ── 6. Reload and verify persistence ──────────────────────────────────
  await page.reload();

  const isLogin2 = await page
    .locator("text=AUTH_REQUIRED")
    .first()
    .isVisible({ timeout: 3000 })
    .catch(() => false);
  if (isLogin2) {
    await page.locator('input[type="text"]').fill(USERNAME);
    await page.locator('input[type="password"]').fill(PASSWORD);
    await page.locator('button[type="submit"]').click();
  }
  await expect(page.locator("text=> touch new_node.md")).toBeVisible({
    timeout: 20000,
  });

  await page.locator('button[title="Settings"]').first().click();
  await expect(page.locator('h2:has-text("Security")')).toBeVisible({
    timeout: 5000,
  });
  await page.locator('button:has-text("Retrieval & Agent")').first().click();
  await expect(
    page.locator('h2:has-text("Retrieval & Agent")'),
  ).toBeVisible({ timeout: 5000 });

  const toggle2 = page.locator('button[aria-label="Toggle agentic mode"]');
  const reloadClass = (await toggle2.getAttribute("class")) || "";
  const persistedOn = reloadClass.includes("bg-amber-500");
  console.log(
    `[TEST] Agentic toggle after reload: ${persistedOn ? "ON" : "OFF"} (expected ${!wasOn ? "ON" : "OFF"})`,
  );
  expect(persistedOn).toBe(!wasOn);

  console.log("[TEST] ✔ Toggle persists across reload — settings fix verified");
});
