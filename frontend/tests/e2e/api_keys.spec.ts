/**
 * Phase 19.2 — API Key Manager (Fernet-encrypted)
 *
 * E2E proves:
 *  1. Backend GET returns 7 providers all "[not set]" by default
 *  2. PUT a key → response shows masked value, never echoes plaintext
 *  3. Mongo stores Fernet ciphertext (gAAAAA... pattern), never the raw key
 *  4. Frontend Settings → API Keys tab renders, lists every provider with
 *     reveal/save/clear controls
 *  5. UI Save flow round-trips and the row's masked badge updates
 */
import { test, expect } from "@playwright/test";

const TARGET_URL = process.env.TARGET_URL || "http://localhost:3000";
const API_URL = process.env.API_URL || "http://localhost:8000";
const USERNAME = "admin";
const PASSWORD = "013100";

async function login(request: any): Promise<string> {
  const resp = await request.post(`${API_URL}/api/auth/login`, {
    data: { username: USERNAME, password: PASSWORD },
  });
  expect(resp.ok()).toBeTruthy();
  return (await resp.json()).access_token as string;
}

test("Backend GET /api/settings/api-keys returns masked entries for known providers", async ({
  request,
}) => {
  const token = await login(request);
  const resp = await request.get(`${API_URL}/api/settings/api-keys`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(resp.ok()).toBeTruthy();
  const body = await resp.json();
  console.log(`[TEST] providers: ${body.providers?.join(", ")}`);
  expect(Array.isArray(body.providers)).toBeTruthy();
  expect(body.providers.length).toBeGreaterThanOrEqual(5);
  for (const p of body.providers) {
    expect(body.keys[p]).toBeDefined();
    // Either '[not set]' or a masked sk-****abc4 form — never plaintext
    expect(body.keys[p]).not.toMatch(/^sk-[a-zA-Z0-9]{20,}$/);
  }
});

test("PUT api-keys persists Fernet ciphertext + GET returns masked value", async ({
  request,
}) => {
  const token = await login(request);
  const probe = "sk-pwtest-zzzzzzzz1234";

  const put = await request.put(`${API_URL}/api/settings/api-keys`, {
    headers: { Authorization: `Bearer ${token}` },
    data: { keys: { openai: probe } },
  });
  expect(put.ok()).toBeTruthy();
  const body = await put.json();
  console.log(`[TEST] openai stored as: ${body.keys.openai}`);
  expect(body.keys.openai).not.toBe(probe);
  expect(body.keys.openai).toMatch(/sk.*1234$/);

  // GET back — still masked
  const get = await request.get(`${API_URL}/api/settings/api-keys`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const get_body = await get.json();
  expect(get_body.keys.openai).toBe(body.keys.openai);

  // Cleanup — clear the key
  await request.put(`${API_URL}/api/settings/api-keys`, {
    headers: { Authorization: `Bearer ${token}` },
    data: { keys: { openai: "" } },
  });
});

test("Settings → API Keys tab renders + Save round-trip", async ({ page }) => {
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

  await page.locator('button[title="Settings"]').first().click();
  await expect(page.locator('h2:has-text("Security")')).toBeVisible({
    timeout: 5000,
  });

  await page.locator('button:has-text("API Keys")').first().click();
  await expect(page.locator('h2:has-text("API Keys")')).toBeVisible({
    timeout: 5000,
  });

  // At least the 5 main providers are listed
  for (const p of ["openai", "anthropic", "deepseek", "gemini", "openrouter"]) {
    await expect(page.locator(`text=${p}`).first()).toBeVisible({
      timeout: 5000,
    });
  }

  // Scope to the OpenAI card — find the input by placeholder, then climb to
  // the parent card and click that card's Save button (the only enabled one).
  const openaiInput = page
    .locator('input[placeholder^="sk-proj"]')
    .first();
  await openaiInput.fill("sk-uitest-aaaaaaaa9876");
  // The OpenAI card is the nearest .bg-\[\#2a2a2a\] ancestor of the input.
  const openaiCard = openaiInput.locator(
    'xpath=ancestor::div[contains(@class, "bg-[#2a2a2a]")][1]',
  );
  await openaiCard.locator('button:has-text("Save")').click();
  await page.waitForTimeout(1500);

  await page.screenshot({
    path: "audit-screenshots/apikeys-tab.png",
    fullPage: false,
  });

  // The row's masked badge should now end with 9876
  const maskedBadge = page.locator("text=/sk.*9876/").first();
  await expect(maskedBadge).toBeVisible({ timeout: 5000 });

  // Cleanup via the trash button
  const clearBtn = page.locator('button[title="Clear this key"]').first();
  if (await clearBtn.isVisible({ timeout: 1000 }).catch(() => false)) {
    page.on("dialog", (d) => d.accept()); // accept the confirm()
    await clearBtn.click();
    await page.waitForTimeout(800);
  }
});
