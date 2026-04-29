import { test } from "@playwright/test";

const TARGET_URL = process.env.TARGET_URL || "http://localhost:3000";
const USERNAME = "admin";
const PASSWORD = "013100";

test("Dropdown audit: capture each ToggleBar + header dropdown open state", async ({
  page,
}) => {
  page.setViewportSize({ width: 1440, height: 900 });

  await page.goto(TARGET_URL);
  const login = await page
    .locator("text=AUTH_REQUIRED")
    .first()
    .isVisible({ timeout: 5000 })
    .catch(() => false);
  if (login) {
    await page.locator('input[type="text"]').fill(USERNAME);
    await page.locator('input[type="password"]').fill(PASSWORD);
    await page.locator('button[type="submit"]').click();
  }
  await page.waitForSelector("text=> touch new_node.md", { timeout: 20000 });
  await page.waitForTimeout(800);

  // Dump every button text so we know what to target
  const allButtons = await page.locator("button").all();
  console.log(`[INFO] Found ${allButtons.length} buttons total`);
  for (let i = 0; i < allButtons.length; i++) {
    const t = (await allButtons[i].textContent())?.trim() || "";
    const title = await allButtons[i].getAttribute("title");
    if (t.length > 0 && t.length < 80) {
      const box = await allButtons[i].boundingBox();
      console.log(
        `  [btn ${i}] y=${box?.y?.toFixed(0)} text="${t}" title="${title || ""}"`,
      );
    }
  }

  async function captureByIndex(buttonIndex: number, label: string) {
    const allBtns = await page.locator("button").all();
    if (buttonIndex >= allBtns.length) {
      console.log(`[SKIP] ${label} index ${buttonIndex} out of range`);
      return;
    }
    const btn = allBtns[buttonIndex];
    const text = (await btn.textContent())?.trim() || "";
    const box = await btn.boundingBox();
    console.log(
      `[CAPTURE] ${label} — "${text.slice(0, 40)}" at y=${box?.y?.toFixed(0)}`,
    );
    try {
      await btn.click({ timeout: 3000 });
    } catch {
      console.log(`[SKIP] ${label} click failed`);
      return;
    }
    await page.waitForTimeout(500);
    await page.screenshot({
      path: `audit-screenshots/dropdown-${label}.png`,
      fullPage: false,
    });
    await page.mouse.click(5, 5);
    await page.waitForTimeout(300);
  }

  // After dumping we can pick by known text fragments
  const btnByText = async (fragment: RegExp) => {
    const all = await page.locator("button").all();
    for (let i = 0; i < all.length; i++) {
      const t = (await all[i].textContent())?.trim() || "";
      if (fragment.test(t)) return i;
    }
    return -1;
  };

  const toolsIdx = await btnByText(/TOOLS/);
  const reasonIdx = await btnByText(/REASON/);
  const speedIdx = await btnByText(/SPEED/);
  const tierIdx = await btnByText(/HYBRID|TIER|VECTOR|GRAPH-AUGMENTED/);
  const corporaIdx = await btnByText(/ALL CORPORA|CORPORA/);
  const collsIdx = await btnByText(/ALL_COLLS|COLLS/);
  const modelIdx = await btnByText(/LLAMA|DEEPSEEK|GPT|CLAUDE|OLLAMA/);

  console.log(
    `[INDEXES] tools=${toolsIdx} reason=${reasonIdx} speed=${speedIdx} tier=${tierIdx} corpora=${corporaIdx} colls=${collsIdx} model=${modelIdx}`,
  );

  if (corporaIdx >= 0) await captureByIndex(corporaIdx, "01-header-corpora");
  if (collsIdx >= 0) await captureByIndex(collsIdx, "02-header-collections");
  if (modelIdx >= 0) await captureByIndex(modelIdx, "03-header-model");
  if (toolsIdx >= 0) await captureByIndex(toolsIdx, "04-togglebar-tools");
  if (reasonIdx >= 0) await captureByIndex(reasonIdx, "05-togglebar-reason");
  if (speedIdx >= 0) await captureByIndex(speedIdx, "06-togglebar-speed");
  if (tierIdx >= 0) await captureByIndex(tierIdx, "07-togglebar-tier");

  console.log("[DROPDOWN AUDIT] done");
});
