import { expect, test } from "@playwright/test";
import * as fs from "fs";

const BASE = process.env.BASE_URL || "http://localhost:3000";
const API = process.env.API_URL || "http://localhost:8000";
const TOKEN = process.env.TOKEN || "";
const ADMIN_USER = process.env.ADMIN_USER || process.env.DEFAULT_ADMIN_USERNAME || "admin";
const ADMIN_PASSWORD =
  process.env.ADMIN_PASSWORD || process.env.DEFAULT_ADMIN_PASSWORD || "";
const IMAGE_FIXTURE =
  process.env.IMAGE_FIXTURE ||
  "/Users/king/polymath_v3.3/20260518_polymath-logo_00004_.png";
const SCREENSHOTS = "test-results/query-plan-v2";
fs.mkdirSync(SCREENSHOTS, { recursive: true });

async function authenticate(page: import("@playwright/test").Page, request: import("@playwright/test").APIRequestContext) {
  let token = TOKEN;
  if (!token) {
    expect(
      ADMIN_PASSWORD,
      "Set TOKEN or ADMIN_PASSWORD/DEFAULT_ADMIN_PASSWORD for live UI tests",
    ).not.toBe("");
    const loginResponse = await request.post(`${API}/api/auth/login`, {
      data: { username: ADMIN_USER, password: ADMIN_PASSWORD },
    });
    expect(
      loginResponse.ok(),
      `login failed (${loginResponse.status()})`,
    ).toBeTruthy();
    token = String((await loginResponse.json()).access_token || "");
  }
  const meResponse = await request.get(`${API}/api/auth/me`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(meResponse.ok(), `auth/me failed (${meResponse.status()})`).toBeTruthy();
  const me = await meResponse.json();
  await page.addInitScript(
    ([token, user]) => {
      localStorage.setItem(
        "polymath-auth",
        JSON.stringify({ state: { token, user }, version: 0 }),
      );
    },
    [token, me] as const,
  );
}

test.use({ launchOptions: { slowMo: 0 } });

test("Corpus Manager exposes a terminal error and recovers on retry", async ({
  page,
  request,
}) => {
  await authenticate(page, request);
  const initialCorpora = page.waitForResponse(
    (response) => response.url().includes("/api/corpora") && response.ok(),
  );
  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  await initialCorpora;

  let corpusAttempts = 0;
  await page.route("**/api/corpora", async (route) => {
    corpusAttempts += 1;
    if (corpusAttempts === 1) {
      await route.abort("timedout");
      return;
    }
    await route.continue();
  });
  await page.locator('button[title="Corpus Manager"]').first().click();
  await expect(page.getByText(/Corpus load was cancelled|Failed to fetch/i)).toBeVisible();

  await page.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByText("markbuildsbrands_transcripts", { exact: true })).toBeVisible({
    timeout: 15_000,
  });
  expect(corpusAttempts).toBeGreaterThanOrEqual(2);
  await page.screenshot({ path: `${SCREENSHOTS}/corpus-manager-recovered.png` });
});

test("image attachment preview renders, stays fixed, and can be removed", async ({
  page,
  request,
}) => {
  await authenticate(page, request);
  const failedImages: string[] = [];
  page.on("response", (response) => {
    if (response.request().resourceType() === "image" && !response.ok()) {
      failedImages.push(`${response.status()} ${response.url()}`);
    }
  });

  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  const fileInput = page.locator('input[type="file"]').first();
  await fileInput.setInputFiles(IMAGE_FIXTURE);

  const preview = page.getByTestId("attachment-preview-image");
  await expect(preview).toBeVisible();
  const imageMetrics = await preview.evaluate((element: HTMLImageElement) => ({
    naturalWidth: element.naturalWidth,
    naturalHeight: element.naturalHeight,
    width: element.getBoundingClientRect().width,
    height: element.getBoundingClientRect().height,
    src: element.currentSrc,
  }));
  expect(imageMetrics.naturalWidth).toBeGreaterThan(0);
  expect(imageMetrics.naturalHeight).toBeGreaterThan(0);
  expect(imageMetrics.width).toBe(40);
  expect(imageMetrics.height).toBe(40);
  expect(imageMetrics.src.startsWith("blob:")).toBeTruthy();
  expect(failedImages).toEqual([]);

  await page.getByRole("button", { name: /Remove .*polymath-logo/i }).click();
  await expect(page.getByTestId("attachment-preview")).toHaveCount(0);

  let serializedRequest: Record<string, unknown> | null = null;
  await page.route("**/api/chat", async (route) => {
    serializedRequest = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body:
        'data: {"type":"sources","sources":[]}\n\n' +
        'data: {"type":"token","content":"image received"}\n\n' +
        'data: {"type":"done"}\n\n',
    });
  });
  await fileInput.setInputFiles(IMAGE_FIXTURE);
  await page.getByRole("textbox", { name: "Ask Polymath..." }).fill("Inspect this image");
  await page.getByRole("button", { name: "EXECUTE" }).click();
  await expect.poll(() => serializedRequest).not.toBeNull();
  const attachments = serializedRequest?.attachments as Array<Record<string, unknown>>;
  expect(attachments).toHaveLength(1);
  expect(attachments[0].kind).toBe("image");
  expect(attachments[0].mime_type).toBe("image/png");
  expect(String(attachments[0].content)).not.toMatch(/^data:/);
  expect(String(attachments[0].content).length).toBeGreaterThan(100);
  await expect(page.getByText(/image: .*polymath-logo/i)).toBeVisible();
  await page.screenshot({ path: `${SCREENSHOTS}/image-attachment-desktop.png` });
});

test("mobile image preview stays inside the composer without layout overflow", async ({
  page,
  request,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await authenticate(page, request);
  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  await page.locator('input[type="file"]').first().setInputFiles(IMAGE_FIXTURE);

  const previewCard = page.getByTestId("attachment-preview");
  await expect(previewCard).toBeVisible();
  const metrics = await previewCard.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return {
      left: rect.left,
      right: rect.right,
      viewportWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    };
  });
  expect(metrics.left).toBeGreaterThanOrEqual(0);
  expect(metrics.right).toBeLessThanOrEqual(metrics.viewportWidth);
  expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.viewportWidth);

  await page.locator('input[type="file"]').first().setInputFiles({
    name: "broken-preview.png",
    mimeType: "image/png",
    buffer: Buffer.from("not a valid image"),
  });
  await expect(page.locator('[title="Image preview failed"]')).toBeVisible();
  await page.screenshot({ path: `${SCREENSHOTS}/image-attachment-mobile-fallback.png` });
});
