import { existsSync } from "node:fs";
import { chromium } from "@playwright/test";

const BASE_URL = process.env.BASE_URL || "http://127.0.0.1:5173/";
const CHROME_EXECUTABLE_PATH =
  process.env.CHROME_EXECUTABLE_PATH ||
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

const viewports = [
  ["mobile-small", 320, 568],
  ["mobile-360x800", 360, 800],
  ["iphone-13-mini-ish", 375, 812],
  ["iphone-13", 390, 844],
  ["pixel-ish", 393, 873],
  ["large-phone", 412, 915],
  ["mobile-landscape", 844, 390],
  ["tablet-ipad", 768, 1024],
  ["tablet-modern", 810, 1080],
  ["tablet-large", 820, 1180],
  ["tablet-landscape", 1024, 768],
  ["desktop-1280x720", 1280, 720],
  ["laptop-1366x768", 1366, 768],
  ["laptop-1536x864", 1536, 864],
  ["desktop-1440x900", 1440, 900],
  ["desktop-1920x1080", 1920, 1080],
];

const globalSettings = {
  settings: {
    infrastructure: {
      mongodb_url: "",
      qdrant_url: "",
      neo4j_uri: "",
      neo4j_user: "",
      neo4j_password: "",
      litellm_base_url: "",
      litellm_master_key: "",
      ollama_base_url: "",
      redis_url: "",
      embedder_url: "",
      reranker_url: "",
      modal_enabled: false,
      modal_embedder_url: "",
      auth: {
        auth_secret_key: "",
        auth_algorithm: "HS256",
        auth_token_expire_days: 30,
      },
    },
    chat: {
      default_chat_model: "deepseek-chat",
      max_context_tokens: 32000,
      max_completion_tokens: 4096,
      temperature: 0.2,
      top_p: 0.9,
      agentic_mode_enabled: false,
      agentic_model: "",
      default_reasoning_mode: "none",
      reasoning_blend: [],
      hyde_model: "",
      query_profile: "balanced",
    },
    retrieval: {
      default_tier: "qdrant_mongo_graph",
      top_k_child: 20,
      top_k_summary: 12,
      reranker_model: "",
      rerank_top_n: 8,
      rerank_enabled: true,
      similarity_threshold: 0,
      max_corpora_per_query: 0,
      neo4j_expansion_cap: 100,
      final_top_k: 8,
      fact_seed_limit: 12,
      vector_child_chunks: 20,
      vector_summaries: 12,
      vector_final_sources: 8,
      vector_reranker: true,
      hybrid_child_chunks: 20,
      hybrid_summaries: 12,
      hybrid_final_sources: 8,
      hybrid_reranker: true,
      graph_child_chunks: 20,
      graph_summaries: 12,
      graph_fact_seeds: 12,
      graph_expansion: 100,
      graph_final_sources: 8,
      graph_reranker: true,
      graph_query_seed_entities: 10,
      graph_query_max_hops: 2,
      graph_query_node_limit: 80,
    },
    modal: {
      gpu_tier: "L4",
      min_containers: 0,
      max_containers: 1,
      idle_timeout_seconds: 300,
      concurrency_per_container: 1,
      app_name: "",
      model_id: "",
      use_auth: false,
      enabled: false,
      embedder_url: "",
      workspace: "",
    },
    extraction: { endpoints: [] },
  },
};

const modelsConfig = {
  query_model_pool: [
    {
      entry_id: "deepseek-chat",
      provider: "deepseek",
      model_name: "deepseek-chat",
      label: "DeepSeek Chat",
      enabled: true,
      source: "cloud",
      base_url: null,
      api_key_ciphertext: "[set]",
      created_at: "2026-01-01T00:00:00Z",
    },
  ],
  hyde: { default_enabled: false, pool_entry_id: null },
  agentic: { default_enabled: false, pool_entry_id: null },
  reasoning: { default_enabled: false, pool_entry_id: null },
  utility: { default_enabled: false, pool_entry_id: null },
  graph_query: { pool_entry_id: null },
};

const json = (body) => ({
  status: 200,
  contentType: "application/json",
  body: JSON.stringify(body),
});

async function setupRoutes(page) {
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/auth/me") {
      return route.fulfill(
        json({
          id: "responsive-test",
          username: "responsive-test",
          created_at: "2026-01-01T00:00:00Z",
        }),
      );
    }
    if (path === "/api/conversations") return route.fulfill(json([]));
    if (path === "/api/corpora") {
      return route.fulfill(
        json([
          {
            corpus_id: "authentic_library",
            name: "Authentic Library",
            doc_count: 498,
            status: "ready",
          },
          {
            corpus_id: "long_corpus",
            name: "Very Long Corpus Name That Should Still Truncate Cleanly",
            doc_count: 12,
            status: "ready",
          },
        ]),
      );
    }
    if (path === "/api/tools") {
      return route.fulfill(
        json([
          {
            id: "web_search",
            name: "Web Search",
            description: "Search web",
            enabled: true,
            code: "",
            created_at: "",
            updated_at: "",
          },
        ]),
      );
    }
    if (path === "/api/skills") {
      return route.fulfill(
        json([
          {
            id: "grounding",
            name: "Grounding",
            description: "Ground answers",
            prompt: "",
            enabled: true,
            created_at: "",
            updated_at: "",
          },
        ]),
      );
    }
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
      return route.fulfill(json(modelsConfig));
    }
    if (path === "/api/settings") {
      return route.fulfill(json(globalSettings));
    }
    return route.fulfill(json({}));
  });
}

function isInFrame(box, viewport) {
  return (
    box &&
    box.x >= -1 &&
    box.y >= -1 &&
    box.right <= viewport.width + 1 &&
    box.bottom <= viewport.height + 1
  );
}

async function popoverBox(page, text) {
  await page.waitForSelector(`text=${text}`, { timeout: 5000 });
  return page
    .locator(`text=${text}`)
    .locator(
      'xpath=ancestor::div[contains(@class, "fixed") or contains(@class, "absolute")][1]',
    )
    .evaluate((el) => {
      const rect = el.getBoundingClientRect();
      const styles = getComputedStyle(el);
      return {
        x: Math.round(rect.x),
        y: Math.round(rect.y),
        width: Math.round(rect.width),
        height: Math.round(rect.height),
        right: Math.round(rect.right),
        bottom: Math.round(rect.bottom),
        position: styles.position,
      };
    });
}

async function run() {
  const launchOptions = existsSync(CHROME_EXECUTABLE_PATH)
    ? { executablePath: CHROME_EXECUTABLE_PATH }
    : {};
  const browser = await chromium.launch({ headless: true, ...launchOptions });
  const results = [];

  for (const [name, width, height] of viewports) {
    const page = await browser.newPage({
      viewport: { width, height },
      isMobile: width < 768,
      hasTouch: width < 1024,
    });
    await setupRoutes(page);

    await page.goto(BASE_URL, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.evaluate(() => localStorage.clear());
    await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForSelector('input[autocomplete="username"]', {
      timeout: 15000,
    });

    const login = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
      username: document
        .querySelector('input[autocomplete="username"]')
        ?.getAttribute("placeholder"),
      password: document
        .querySelector('input[autocomplete="current-password"]')
        ?.getAttribute("placeholder"),
    }));

    await page.evaluate(() => {
      localStorage.clear();
      localStorage.setItem(
        "polymath-auth",
        JSON.stringify({
          state: {
            token: "mock-token",
            user: {
              id: "responsive-test",
              username: "responsive-test",
              created_at: "2026-01-01T00:00:00Z",
            },
          },
          version: 0,
        }),
      );
    });
    await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForSelector('[data-testid="chat-context-toggle"]', {
      timeout: 15000,
    });
    await page.waitForSelector('[data-testid="model-selector-toggle"]', {
      timeout: 15000,
    });

    const scroll = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
      bodyScrollWidth: document.body.scrollWidth,
    }));

    const boxes = {};
    await page.click('[data-testid="chat-context-toggle"]');
    boxes.context = await popoverBox(page, "Query Context");
    await page.waitForSelector('[data-testid="context-corpus-list"]', {
      timeout: 5000,
    });
    await page.waitForSelector('[data-testid="context-query-speed"]', {
      timeout: 5000,
    });
    boxes.corpus = await page
      .locator('[data-testid="context-corpus-list"]')
      .evaluate((el) => {
        const rect = el.getBoundingClientRect();
        return {
          x: Math.round(rect.x),
          y: Math.round(rect.y),
          width: Math.round(rect.width),
          height: Math.round(rect.height),
          right: Math.round(rect.right),
          bottom: Math.round(rect.bottom),
        };
      });
    await page.locator('[data-testid="context-query-speed"]').scrollIntoViewIfNeeded();
    boxes.speed = await page
      .locator('[data-testid="context-query-speed"]')
      .evaluate((el) => {
        const rect = el.getBoundingClientRect();
        return {
          x: Math.round(rect.x),
          y: Math.round(rect.y),
          width: Math.round(rect.width),
          height: Math.round(rect.height),
          right: Math.round(rect.right),
          bottom: Math.round(rect.bottom),
        };
      });
    await page.mouse.click(2, Math.min(height - 2, 300));

    await page.click('[data-testid="model-selector-toggle"]');
    boxes.model = await popoverBox(page, "Select Engine");
    await page.mouse.click(2, Math.min(height - 2, 300));

    await page.click('[data-testid="composer-features-toggle"]');
    await page.locator("button").filter({ hasText: /\[ITEMS:/ }).click();
    boxes.items = await popoverBox(page, "Tools (0)");

    const viewport = { width, height };
    results.push({
      name,
      width,
      height,
      loginOk:
        login.scrollWidth <= login.innerWidth &&
        login.username === "username" &&
        login.password === "password",
      appNoHOverflow:
        scroll.scrollWidth <= scroll.innerWidth &&
        scroll.bodyScrollWidth <= scroll.innerWidth,
      inFrame: {
        context: isInFrame(boxes.context, viewport),
        corpus: isInFrame(boxes.corpus, viewport),
        speed: isInFrame(boxes.speed, viewport),
        model: isInFrame(boxes.model, viewport),
        items: isInFrame(boxes.items, viewport),
      },
      boxes,
      scroll,
    });
    await page.close();
  }

  await browser.close();

  const failures = results.filter(
    (result) =>
      !result.loginOk ||
      !result.appNoHOverflow ||
      !Object.values(result.inFrame).every(Boolean),
  );

  console.log(
    JSON.stringify(
      {
        baseUrl: BASE_URL,
        total: results.length,
        failures,
        summary: results.map((result) => ({
          name: result.name,
          ok:
            result.loginOk &&
            result.appNoHOverflow &&
            Object.values(result.inFrame).every(Boolean),
          inFrame: result.inFrame,
          scroll: result.scroll,
        })),
      },
      null,
      2,
    ),
  );

  if (failures.length > 0) process.exit(1);
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
