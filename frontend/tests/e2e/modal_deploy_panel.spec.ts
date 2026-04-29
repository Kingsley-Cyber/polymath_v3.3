/**
 * Phase M2 — Modal Deploy Panel
 *
 * Verifies:
 *  1. Backend GET /api/settings returns a `modal` section with gpu_tier + max_containers
 *  2. PUT persists changes (round-trip via settings.modal)
 *  3. UI Models tab renders the new Modal Cloud Deployment panel with GPU cards + slider
 *  4. Deploy command template reflects the selected values
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

test("Backend exposes + accepts modal section on /api/settings", async ({
  request,
}) => {
  const token = await login(request);

  const get = await request.get(`${API_URL}/api/settings`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(get.ok()).toBeTruthy();
  const body = await get.json();
  console.log("[TEST] modal section:", body.settings.modal);
  expect(body.settings.modal).toBeDefined();
  expect(body.settings.modal.gpu_tier).toBeDefined();
  expect(body.settings.modal.max_containers).toBeGreaterThan(0);

  // Round-trip a change
  const put = await request.put(`${API_URL}/api/settings`, {
    headers: { Authorization: `Bearer ${token}` },
    data: {
      modal: {
        ...body.settings.modal,
        gpu_tier: "L40S",
        max_containers: 42,
      },
    },
  });
  expect(put.ok()).toBeTruthy();
  const after = await put.json();
  expect(after.settings.modal.gpu_tier).toBe("L40S");
  expect(after.settings.modal.max_containers).toBe(42);

  // Restore defaults
  await request.put(`${API_URL}/api/settings`, {
    headers: { Authorization: `Bearer ${token}` },
    data: {
      modal: {
        ...body.settings.modal,
      },
    },
  });
});

test("Settings → Models tab shows ModalDeployPanel with GPU cards", async ({
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

  await page.locator('button[title="Settings"]').first().click();
  await expect(page.locator('h2:has-text("Security")')).toBeVisible({
    timeout: 5000,
  });

  await page.locator('button:has-text("Models")').first().click();
  await expect(page.locator('h2:has-text("Models")')).toBeVisible({
    timeout: 5000,
  });

  // Panel header + every GPU tier button
  await expect(
    page.locator("text=/Modal Cloud Deployment/"),
  ).toBeVisible({ timeout: 5000 });
  for (const tier of ["T4", "L4", "A10G", "L40S", "A100", "H100"]) {
    await expect(
      page.locator(`button:has-text("${tier}")`).first(),
    ).toBeVisible({ timeout: 3000 });
  }

  // Slider + deploy command block
  await expect(
    page.locator("text=/Max Concurrent Containers/"),
  ).toBeVisible();
  await expect(
    page.locator("text=/modal deploy modal_embedder.py/"),
  ).toBeVisible();
  await expect(
    page.locator("text=/modal token set/"),
  ).toBeVisible();

  await page.screenshot({
    path: "audit-screenshots/modal-deploy-panel.png",
    fullPage: false,
  });

  // Switch GPU to L40S and verify deploy command updates
  await page.locator("button:has-text('L40S')").first().click();
  await page.waitForTimeout(300);
  await expect(
    page.locator("text=/POLYMATH_GPU='L40S'/"),
  ).toBeVisible();
  console.log("[TEST] deploy command reflects L40S selection");
});
