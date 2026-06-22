import { existsSync } from "node:fs";
import { chromium } from "@playwright/test";

const BASE_URL = process.env.BASE_URL || "http://127.0.0.1:5173/";
const CHROME_EXECUTABLE_PATH =
  process.env.CHROME_EXECUTABLE_PATH ||
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

const json = (body) => ({
  status: 200,
  contentType: "application/json",
  body: JSON.stringify(body),
});

async function setupRoutes(page) {
  await page.route("**/api/**", (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;

    if (path === "/api/auth/me") {
      return route.fulfill(
        json({
          id: "chat-sidebar-smoke",
          username: "chat-sidebar-smoke",
          created_at: "2026-01-01T00:00:00Z",
        }),
      );
    }
    if (path === "/api/conversations" && request.method() === "POST") {
      return route.fulfill(json({ id: "smoke-created-chat" }));
    }
    if (path === "/api/conversations") return route.fulfill(json([]));
    if (path === "/api/corpora") return route.fulfill(json([]));
    if (path === "/api/tools") return route.fulfill(json([]));
    if (path === "/api/skills") return route.fulfill(json([]));
    if (path === "/api/models") {
      return route.fulfill(
        json({
          chat_models: [
            {
              id: "deepseek-chat",
              name: "DeepSeek Chat",
              provider: "deepseek",
              source: "mock",
              type: "chat",
            },
          ],
          embedding_models: [],
          default_model: "deepseek-chat",
          default_embedding_model: "",
        }),
      );
    }
    if (path === "/api/settings/models") {
      return route.fulfill(
        json({
          query_model_pool: [],
          hyde: { default_enabled: false, pool_entry_id: null },
          agentic: { default_enabled: false, pool_entry_id: null },
          reasoning: { default_enabled: false, pool_entry_id: null },
          utility: { default_enabled: false, pool_entry_id: null },
          graph_query: { pool_entry_id: null },
        }),
      );
    }
    if (path === "/api/settings") {
      return route.fulfill(
        json({
          settings: {
            chat: { default_chat_model: "deepseek-chat" },
            retrieval: { default_tier: "qdrant_mongo_graph" },
          },
        }),
      );
    }
    return route.fulfill(json({}));
  });
}

function parseAlpha(color) {
  const rgba = color.match(/rgba?\(([^)]+)\)/i);
  if (rgba) {
    const parts = rgba[1].split(/[ ,/]+/).filter(Boolean);
    return parts.length >= 4 ? Number(parts[3]) : 1;
  }
  const slash = color.match(/\/\s*([0-9.]+)\s*\)?$/);
  if (slash) return Number(slash[1]);
  return color.includes("transparent") ? 0 : 1;
}

async function authedPage(browser, width, height) {
  const page = await browser.newPage({
    viewport: { width, height },
    isMobile: width < 768,
    hasTouch: width < 1024,
  });
  await setupRoutes(page);
  await page.goto(BASE_URL, { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.evaluate(() => {
    localStorage.clear();
    localStorage.setItem(
      "polymath-auth",
      JSON.stringify({
        state: {
          token: "mock-token",
          user: {
            id: "chat-sidebar-smoke",
            username: "chat-sidebar-smoke",
            created_at: "2026-01-01T00:00:00Z",
          },
        },
        version: 0,
      }),
    );
  });
  await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 });
  await page.waitForSelector("#chat-sidebar", { timeout: 15000 });
  return page;
}

async function run() {
  const launchOptions = existsSync(CHROME_EXECUTABLE_PATH)
    ? { executablePath: CHROME_EXECUTABLE_PATH }
    : {};
  const browser = await chromium.launch({ headless: true, ...launchOptions });

  const mobile = await authedPage(browser, 390, 900);
  await mobile
    .getByRole("button", { name: /Open navigation|Collapse navigation/ })
    .first()
    .click();
  await mobile.waitForTimeout(250);

  const mobileState = await mobile.evaluate(() => {
    const aside = document.querySelector("#chat-sidebar");
    const backdrop = document.querySelector(".pm-chat-backdrop");
    const asideRect = aside?.getBoundingClientRect();
    const backdropStyle = backdrop ? getComputedStyle(backdrop) : null;
    return {
      aside: asideRect
        ? { x: asideRect.x, width: asideRect.width, height: asideRect.height }
        : null,
      backdropBackground: backdropStyle?.backgroundColor ?? null,
    };
  });
  const newChatBox = await mobile
    .getByRole("button", { name: "Create new chat" })
    .boundingBox();
  const collapseBox = await mobile
    .getByRole("button", { name: "Collapse navigation" })
    .first()
    .boundingBox();
  const backdropAlpha = parseAlpha(mobileState.backdropBackground ?? "");

  await mobile.getByRole("button", { name: "Create new chat" }).click();
  await mobile.waitForTimeout(150);
  await mobile
    .getByRole("button", { name: "Collapse navigation" })
    .first()
    .click();
  await mobile.waitForTimeout(250);
  const mobileClosedBox = await mobile.locator("#chat-sidebar").boundingBox();
  await mobile.close();

  if (!mobileState.aside || mobileState.aside.x > 1 || mobileState.aside.width < 300) {
    throw new Error(`mobile sidebar failed to open: ${JSON.stringify(mobileState.aside)}`);
  }
  if (!newChatBox || newChatBox.width < 240) {
    throw new Error(`new chat button is not reliably clickable: ${JSON.stringify(newChatBox)}`);
  }
  if (!collapseBox || collapseBox.width < 20) {
    throw new Error(`collapse button is not reliably clickable: ${JSON.stringify(collapseBox)}`);
  }
  if (backdropAlpha >= 0.75) {
    throw new Error(`mobile backdrop is too opaque: ${mobileState.backdropBackground}`);
  }
  if (!mobileClosedBox || mobileClosedBox.x > -250) {
    throw new Error(`mobile sidebar failed to collapse: ${JSON.stringify(mobileClosedBox)}`);
  }

  const desktop = await authedPage(browser, 1280, 800);
  await desktop.getByLabel("UI Protocol color scheme").first().selectOption("nord");
  await desktop.waitForTimeout(150);
  const themeClass = await desktop.evaluate(() => document.documentElement.className);
  await desktop
    .getByRole("button", { name: /Collapse navigation|Open navigation/ })
    .first()
    .click();
  await desktop.waitForTimeout(200);
  const desktopClosedBox = await desktop.locator("#chat-sidebar").boundingBox();
  await desktop
    .getByRole("button", { name: /Collapse navigation|Open navigation/ })
    .first()
    .click();
  await desktop.waitForTimeout(200);
  const desktopOpenBox = await desktop.locator("#chat-sidebar").boundingBox();
  await desktop.close();
  await browser.close();

  if (!themeClass.includes("theme-nord")) {
    throw new Error(`UI protocol did not switch to nord: ${themeClass}`);
  }
  if (!desktopClosedBox || desktopClosedBox.width !== 0) {
    throw new Error(`desktop sidebar failed to collapse: ${JSON.stringify(desktopClosedBox)}`);
  }
  if (!desktopOpenBox || desktopOpenBox.width < 250) {
    throw new Error(`desktop sidebar failed to reopen: ${JSON.stringify(desktopOpenBox)}`);
  }

  console.log(
    JSON.stringify(
      {
        baseUrl: BASE_URL,
        ok: true,
        mobile: {
          backdropAlpha,
          newChatBox,
          collapseBox,
          closedBox: mobileClosedBox,
        },
        desktop: {
          themeClass,
          closedBox: desktopClosedBox,
          openBox: desktopOpenBox,
        },
      },
      null,
      2,
    ),
  );
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
