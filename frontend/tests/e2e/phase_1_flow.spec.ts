import { test, expect } from "@playwright/test";

const TARGET_URL = process.env.TARGET_URL || "http://localhost:3000";

test.describe("Polymath RAG Phase 1 Smoke Test", () => {
  test("Executes full 8-step conversation flow", async ({ page }) => {
    // Add console listener to help debug frontend errors during the test
    page.on("console", (msg) => {
      console.log(
        `[Browser Console] ${msg.type().toUpperCase()}: ${msg.text()}`,
      );
    });

    page.on("pageerror", (err) => {
      console.log(`[Browser Error]: ${err.message}`);
    });

    console.log("1. Initial Load");
    await page.goto(TARGET_URL);

    console.log("1.5 Handle Authentication");
    const viewState = await Promise.race([
      page
        .waitForSelector("text=AUTH_REQUIRED", { timeout: 15000 })
        .then(() => "login"),
      page
        .waitForSelector("text=> touch new_node.md", { timeout: 15000 })
        .then(() => "app"),
    ]).catch(() => "timeout");

    console.log(`View state detected: ${viewState}`);

    if (viewState === "login") {
      await page.locator('input[type="text"]').fill("admin");
      await page.locator('input[type="password"]').fill("changeme");
      await page.locator('button[type="submit"]').click();
    }

    console.log("Wait for application to load");
    await expect(page.locator("text=> touch new_node.md")).toBeVisible({
      timeout: 15000,
    });

    console.log("2. Type + Send");
    const inputField = page.locator('textarea[placeholder*="EXECUTE QUERY"]');
    await inputField.fill("Who built the Great Pyramids?");
    await page.locator('button:has-text("EXECUTE")').first().click();

    console.log("3. Wait for streaming response to finish");
    // Wait for the state to transition to EXECUTING, then back to IDLE
    await expect(page.locator("text=STATE: EXECUTING"))
      .toBeVisible({ timeout: 5000 })
      .catch(() => console.log("Did not see EXECUTING state"));
    await expect(page.locator("text=STATE: IDLE")).toBeVisible({
      timeout: 60000,
    });

    console.log("Check if the chat response is in the DOM");
    const messages = page.locator(".message-assistant");
    await expect(messages.first()).toBeVisible({ timeout: 15000 });

    console.log("4. Follow-up");
    await inputField.fill("When exactly?");
    await page.locator('button:has-text("EXECUTE")').first().click();

    console.log("Wait for the second message to stream and complete");
    await expect(page.locator("text=STATE: EXECUTING"))
      .toBeVisible({ timeout: 5000 })
      .catch(() => console.log("Did not see EXECUTING state 2"));
    await expect(page.locator("text=STATE: IDLE")).toBeVisible({
      timeout: 60000,
    });
    await expect(messages.nth(1)).toBeVisible({ timeout: 15000 });

    console.log("5. Second conversation");
    await page.locator("text=> touch new_node.md").click();
    await expect(page.locator("text=> Start a conversation.")).toBeVisible({
      timeout: 15000,
    });

    await inputField.fill("What is quantum mechanics?");
    await page.locator('button:has-text("EXECUTE")').first().click();

    await expect(page.locator("text=STATE: IDLE")).toBeVisible({
      timeout: 60000,
    });
    await expect(messages.first()).toBeVisible({ timeout: 15000 });

    console.log("6. Switch Back");
    // The sidebar titles replace spaces with underscores and append .md
    const firstConv = page
      .locator("button")
      .filter({ hasText: /who_built/i })
      .first();
    await firstConv.click();

    console.log("We expect the 2 previous messages to load");
    await expect(messages).toHaveCount(2, { timeout: 15000 });

    console.log("7. Refresh Page");
    await page.reload();

    console.log("Handle authentication again on refresh if required");
    const refreshState = await Promise.race([
      page
        .waitForSelector("text=AUTH_REQUIRED", { timeout: 10000 })
        .then(() => "login"),
      page
        .waitForSelector("text=> touch new_node.md", { timeout: 10000 })
        .then(() => "app"),
    ]).catch(() => "timeout");

    if (refreshState === "login") {
      await page.locator('input[type="text"]').fill("admin");
      await page.locator('input[type="password"]').fill("changeme");
      await page.locator('button[type="submit"]').click();
    }

    console.log("Wait for sidebar to load and select the active conversation");
    await expect(firstConv).toBeVisible({ timeout: 15000 });
    await firstConv.click();
    await expect(messages).toHaveCount(2, { timeout: 15000 });

    console.log("8. Delete");
    // Set up the dialog handler BEFORE triggering it
    page.once("dialog", (dialog) => dialog.accept());

    await firstConv.hover();
    await page.locator(".lucide-trash-2").first().click();

    await expect(firstConv).not.toBeVisible({ timeout: 15000 });
    console.log("Test Complete");
  });
});
