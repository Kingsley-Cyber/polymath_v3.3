import { existsSync } from "node:fs";
import { chromium } from "@playwright/test";

const BASE_URL = process.env.BASE_URL || "http://127.0.0.1:4174/";
const CHROME_EXECUTABLE_PATH =
  process.env.CHROME_EXECUTABLE_PATH ||
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

const viewports = [
  ["phone", 390, 844],
  ["tablet", 820, 1180],
  ["desktop", 1440, 900],
];

const ingestionConfig = {
  extraction_engine: "cloud",
  extraction_models: [],
  summary_models: [],
  use_neo4j: true,
  chunk_summarization: true,
  embed_mode: "local",
  models_linked: false,
};

const readiness = {
  corpus_id: "test-corpus",
  status: "needs_repair",
  computed_at: "2026-07-12T10:00:00Z",
  source: "durable",
  blocking: ["2 documents need graph promotion"],
  next_actions: [
    {
      id: "graph",
      label: "Run graph jobs",
      lane: "graph",
      severity: "warning",
      reason: "Promote two queryable documents into Neo4j.",
      count: 2,
    },
  ],
  documents: {
    total: 76,
    queryable: 74,
    fully_enriched: 68,
    verified: 68,
    failed: 2,
    coverage: 0.97,
    fully_enriched_coverage: 0.89,
    excluded_total: 3,
    lexicon_ready: 72,
    lexicon_pending: 4,
    stage_counts: {},
  },
  chunks: { total: 56996, docs_with_chunks: 76 },
  summaries: {
    parent_total: 9453,
    parent_done: 9199,
    parent_missing: 254,
    parent_coverage: 0.97,
    retrieval_parent_total: 9453,
    retrieval_parent_done: 9199,
    retrieval_parent_missing: 254,
    retrieval_parent_coverage: 0.97,
    body_parent_total: 9453,
    body_parent_done: 9199,
    body_parent_missing: 254,
    body_parent_coverage: 0.97,
    document_total: 76,
    document_done: 72,
    document_missing: 4,
    document_coverage: 0.95,
    document_synced_done: 70,
    document_mismatch: 2,
    document_profile_done: 72,
    document_tree_done: 70,
    summary_tree_index_ready: 70,
    summary_tree_index_pending: 2,
  },
  graph: {
    required: true,
    promoted: 74,
    pending: 2,
    failed_docs: 0,
    failed_chunks: 0,
    failure_docs: 0,
    failure_rows: 0,
    stale_failure_docs: 0,
    stale_failure_rows: 0,
    reconciled_stale_failure_docs: 0,
    reconciled_stale_failure_rows: 0,
    orphaned_failure_docs: 0,
  },
  repair: {
    active_runs: 0,
    source_parse_jobs_pending: 0,
    document_pipeline_jobs_pending: 0,
    extraction_jobs_pending: 0,
    extraction_jobs_failed: 0,
    summary_jobs_pending: 4,
    summary_jobs_waiting_dependencies: 0,
    graph_promotion_jobs: { queued: 2 },
  },
  pressure: { status: "normal", backpressure: {} },
};

const corpus = {
  corpus_id: "test-corpus",
  name: "ecommerce_AI_FILM_SCHOOL_with_a_long_name",
  description: "Deep graph ingest for ecommerce PDFs and cinematic research.",
  doc_count: 79,
  ready_doc_count: 74,
  chunk_count: 56996,
  status: "ready",
  default_ingestion_config: ingestionConfig,
  readiness,
};

const documents = Array.from({ length: 8 }, (_, index) => ({
  doc_id: `doc-${index}`,
  corpus_id: "test-corpus",
  filename:
    index === 0
      ? "Michael Rabiger, Mick Hurbis-Cherrier - Directing - Film Techniques and Aesthetics (2020).md"
      : `Film research source ${index + 1}.md`,
  source_path: `/ingest-source/film/${index + 1}.md`,
  source_mime: "text/markdown",
  source_tier: "primary",
  chunk_count: 120 + index,
  parent_chunks: [],
  ingestion_config: ingestionConfig,
  write_state: {
    mongo_written: true,
    qdrant_written: true,
    neo4j_written: index > 1,
    verified: index > 1,
  },
}));

const json = (body) => ({
  status: 200,
  contentType: "application/json",
  body: JSON.stringify(body),
});

async function setupRoutes(page) {
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/auth/me") {
      return route.fulfill(json({ id: "qa", username: "qa", created_at: "2026-01-01T00:00:00Z" }));
    }
    if (path === "/api/corpora") return route.fulfill(json([corpus]));
    if (path === "/api/corpora/test-corpus/documents") return route.fulfill(json(documents));
    if (path.startsWith("/api/ingest/batches") || path.endsWith("/ingest-batches")) {
      return route.fulfill(json([]));
    }
    if (path === "/api/conversations") return route.fulfill(json([]));
    if (path === "/api/tools" || path === "/api/skills") return route.fulfill(json([]));
    if (path === "/api/models") {
      return route.fulfill(json({ chat_models: [], embedding_models: [], default_model: "", default_embedding_model: "" }));
    }
    if (path === "/api/settings/models") return route.fulfill(json({ query_model_pool: [] }));
    if (path === "/api/settings") return route.fulfill(json({ settings: { chat: {}, retrieval: {}, infrastructure: {}, extraction: { endpoints: [] } } }));
    return route.fulfill(json({}));
  });
}

async function box(page, testId) {
  return page.locator(`[data-testid="${testId}"]`).evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return {
      left: rect.left,
      right: rect.right,
      width: rect.width,
      scrollWidth: element.scrollWidth,
      clientWidth: element.clientWidth,
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
    const page = await browser.newPage({ viewport: { width, height } });
    await setupRoutes(page);
    await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
    await page.evaluate(() => {
      localStorage.setItem(
        "polymath-auth",
        JSON.stringify({ state: { token: "qa-token", user: { id: "qa", username: "qa" } }, version: 0 }),
      );
    });
    await page.reload({ waitUntil: "domcontentloaded" });
    const navToggle = page.locator('[aria-label="Open navigation"]');
    if (await navToggle.count()) await navToggle.click();
    await page.click('[data-testid="sidebar-db-btn"]');
    await page.waitForSelector('[data-testid="corpus-manager-dialog"]');
    const manager = await box(page, "corpus-manager-dialog");
    await page.screenshot({ path: `/tmp/polymath-corpus-manager-${name}.png`, fullPage: true });

    await page.getByRole("button", { name: `Open ${corpus.name}` }).click();
    await page.waitForSelector('[data-testid="corpus-detail"]');
    const detail = await box(page, "corpus-detail");
    const files = await box(page, "corpus-file-dashboard");
    await page.screenshot({ path: `/tmp/polymath-corpus-dashboard-${name}-default.png`, fullPage: true });

    await page.click('[data-testid="advanced-jobs-toggle"]');
    const detailAfterAdvanced = await box(page, "corpus-detail");
    await page.screenshot({ path: `/tmp/polymath-corpus-dashboard-${name}.png`, fullPage: true });

    const inFrame = [manager, detail, files, detailAfterAdvanced].every(
      (value) => value.left >= -1 && value.right <= width + 1 && value.scrollWidth <= value.clientWidth + 1,
    );
    results.push({ name, width, height, inFrame, manager, detail, files, detailAfterAdvanced });
    await page.close();
  }

  await browser.close();
  const failures = results.filter((result) => !result.inFrame);
  console.log(JSON.stringify({ baseUrl: BASE_URL, failures, results }, null, 2));
  if (failures.length) process.exit(1);
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
