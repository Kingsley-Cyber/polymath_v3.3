/**
 * RAG Pipeline E2E Test Suite
 * Tests: upload 2 docs → pipeline → DB verification → query
 * Files: Architecture_Feasibility_Report.docx + Product Overview.txt
 */

import { test, expect, type Page } from "@playwright/test";
import path from "path";
import { fileURLToPath } from "url";

const BASE_URL = process.env.BASE_URL || "http://localhost:5174";
const API_URL = "http://localhost:8000";
const ADMIN_USER = "admin";
const ADMIN_PASS = "013100";

// ESM-safe __dirname equivalent
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Test files from TEST/Autoresearch/ directory (3 levels up from frontend/tests/e2e/)
const TEST_FILE_1 = path.resolve(
  __dirname,
  "../../../TEST/Autoresearch/Architecture_Feasibility_Report.docx",
);
const TEST_FILE_2 = path.resolve(
  __dirname,
  "../../../TEST/Autoresearch/Product Overview.txt",
);

// Shared state across serial tests
let jwtToken = "";
let testCorpusId = "";

// ── helpers ───────────────────────────────────────────────────────────────────

async function getAuthToken(): Promise<string> {
  const resp = await fetch(`${API_URL}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: ADMIN_USER, password: ADMIN_PASS }),
  });
  const data = (await resp.json()) as { access_token?: string };
  return data.access_token ?? "";
}

/** Inject the JWT into Zustand persisted auth store so the page auto-logs in. */
async function injectAuth(page: Page, token: string) {
  await page.addInitScript(
    ({ t }) => {
      const state = JSON.stringify({
        state: { token: t, user: { username: "admin" }, isAuthenticated: true, error: null },
        version: 0,
      });
      localStorage.setItem("polymath-auth", state);
    },
    { t: token },
  );
}

/** Inject a giant red cursor that follows the mouse so the human can see clicks. */
async function injectVisibleCursor(page: Page) {
  await page.addInitScript(() => {
    const install = () => {
      if (document.getElementById("pw-cursor")) return;
      const cursor = document.createElement("div");
      cursor.id = "pw-cursor";
      cursor.style.cssText = [
        "position:fixed",
        "top:0",
        "left:0",
        "width:24px",
        "height:24px",
        "border-radius:50%",
        "background:rgba(255,0,0,0.55)",
        "border:3px solid red",
        "box-shadow:0 0 12px 4px rgba(255,0,0,0.7)",
        "pointer-events:none",
        "z-index:2147483647",
        "transition:transform 60ms linear",
        "transform:translate(-12px,-12px)",
      ].join(";");
      document.documentElement.appendChild(cursor);

      const move = (e: MouseEvent) => {
        cursor.style.transform = `translate(${e.clientX - 12}px, ${e.clientY - 12}px)`;
      };
      const flash = () => {
        cursor.style.background = "rgba(255,255,0,0.9)";
        cursor.style.transform += " scale(1.6)";
        setTimeout(() => {
          cursor.style.background = "rgba(255,0,0,0.55)";
        }, 200);
      };
      document.addEventListener("mousemove", move, true);
      document.addEventListener("click", flash, true);
      document.addEventListener("mousedown", flash, true);
    };

    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", install);
    } else {
      install();
    }
  });
}

async function goHome(page: Page) {
  await injectAuth(page, jwtToken);
  await injectVisibleCursor(page);
  await page.goto(BASE_URL);
  // Wait for main app (chat input) — login screen should be bypassed
  await expect(page.locator('[data-testid="query-input"]')).toBeVisible({
    timeout: 15_000,
  });
}

// ── tests ─────────────────────────────────────────────────────────────────────

test.describe.serial("RAG Pipeline — Full Upload + Query Flow", () => {
  // Login once before all tests
  test.beforeAll(async () => {
    jwtToken = await getAuthToken();
    if (!jwtToken) throw new Error("Failed to obtain auth token from API");
  });

  // ── TEST 1 ─────────────────────────────────────────────────────────────────
  test("TEST 1 — app loads and core UI elements are present", async ({
    page,
  }) => {
    await goHome(page);

    await expect(page.locator('[data-testid="upload-zone"]')).toBeVisible();
    await expect(
      page.locator('[data-testid="collection-selector"]').first(),
    ).toBeVisible();
    await expect(page.locator('[data-testid="query-input"]')).toBeVisible();
    await expect(page.locator('[data-testid="query-submit"]')).toBeVisible();
  });

  // ── TEST 2 ─────────────────────────────────────────────────────────────────
  test("TEST 2 — create corpus and upload File 1 (Architecture_Feasibility_Report.docx)", async ({
    page,
  }) => {
    test.setTimeout(480_000); // DOCX pipeline + 4x slowMo clicks
    await goHome(page);

    // Open Corpus Manager via sidebar DB button
    await page.locator('[data-testid="sidebar-db-btn"]').click();
    await expect(page.locator("text=Corpus Manager")).toBeVisible({
      timeout: 5_000,
    });

    // Click "New Corpus" to open create form
    await page.locator('[data-testid="create-corpus-btn"]').click();
    await expect(
      page.locator('[data-testid="corpus-name-input"]'),
    ).toBeVisible({ timeout: 5_000 });

    // Fill name and submit
    const corpusName = `E2E_Test_${Date.now()}`;
    await page.locator('[data-testid="corpus-name-input"]').fill(corpusName);
    await page.locator('[data-testid="corpus-create-submit"]').click();

    // UX: after Create, the app auto-drills into CorpusDetail — "+ Ingest" should appear
    await expect(page.locator("text=+ Ingest")).toBeVisible({ timeout: 10_000 });

    // Upload File 1 via hidden file input
    const fileInput = page.locator('[data-testid="corpus-file-input"]');
    await fileInput.setInputFiles(TEST_FILE_1);

    // Upload overlay should appear with "INGESTING DOCUMENT" heading
    await expect(page.locator('[data-testid="upload-overlay"]')).toBeVisible({
      timeout: 10_000,
    });

    // Wait for upload overlay to disappear (pipeline done)
    await expect(page.locator('[data-testid="upload-overlay"]')).toBeHidden({
      timeout: 240_000,
    });

    // Document row with pipeline-status should now exist
    await expect(
      page.locator('[data-testid="pipeline-status"]').first(),
    ).toContainText(/COMPLETE|PARTIAL/i, { timeout: 10_000 });

    // Retrieve the corpus ID via API for later tests
    const resp = await page.evaluate(
      async ({ apiUrl, token }) => {
        const r = await fetch(`${apiUrl}/api/corpora`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        return r.json();
      },
      { apiUrl: API_URL, token: jwtToken },
    );
    const corpora = resp as Array<{ corpus_id: string; name: string }>;
    const found = corpora.find((c) => c.name === corpusName);
    if (found) testCorpusId = found.corpus_id;
    expect(testCorpusId).not.toBe("");
  });

  // ── TEST 3 ─────────────────────────────────────────────────────────────────
  test("TEST 3 — upload File 2 (Product Overview.txt) to same corpus", async ({
    page,
  }) => {
    test.setTimeout(420_000); // txt pipeline + 4x slowMo clicks
    expect(testCorpusId).not.toBe(""); // must have been set by TEST 2

    await goHome(page);

    // Re-open Corpus Manager
    await page.locator('[data-testid="sidebar-db-btn"]').click();
    await expect(page.locator("text=Corpus Manager")).toBeVisible({
      timeout: 5_000,
    });

    // Open button is always visible now (no hover needed)
    await page.locator('[data-testid="corpus-browse-btn"]').first().click();

    await expect(page.locator("text=+ Ingest")).toBeVisible({ timeout: 8_000 });

    // Upload File 2
    const fileInput = page.locator('[data-testid="corpus-file-input"]');
    await fileInput.setInputFiles(TEST_FILE_2);

    // Upload overlay appears
    await expect(page.locator('[data-testid="upload-overlay"]')).toBeVisible({
      timeout: 10_000,
    });

    // Overlay disappears = pipeline done
    await expect(page.locator('[data-testid="upload-overlay"]')).toBeHidden({
      timeout: 240_000,
    });

    // Both docs should now have pipeline-status showing COMPLETE/PARTIAL
    await expect(
      page.locator('[data-testid="pipeline-status"]').last(),
    ).toContainText(/COMPLETE|PARTIAL/i, { timeout: 10_000 });
  });

  // ── TEST 4 ─────────────────────────────────────────────────────────────────
  test("TEST 4 — both files show pipeline completion in CorpusDetail UI", async ({
    page,
  }) => {
    expect(testCorpusId).not.toBe("");

    await goHome(page);

    await page.locator('[data-testid="sidebar-db-btn"]').click();
    await expect(page.locator("text=Corpus Manager")).toBeVisible({
      timeout: 5_000,
    });

    // Click Open (always visible now)
    await page.locator('[data-testid="corpus-browse-btn"]').first().click();

    await expect(page.locator("text=+ Ingest")).toBeVisible({ timeout: 8_000 });

    // Expect at least 2 documents in the list
    await expect(page.locator('[data-testid="pipeline-status"]')).toHaveCount(
      2,
      { timeout: 15_000 },
    );

    // All pipeline statuses must be COMPLETE or PARTIAL
    const statuses = page.locator('[data-testid="pipeline-status"]');
    const count = await statuses.count();
    expect(count).toBeGreaterThanOrEqual(2);

    for (let i = 0; i < count; i++) {
      const text = await statuses.nth(i).textContent();
      expect(text).toMatch(/COMPLETE|PARTIAL/i);
    }
  });

  // ── TEST 5 ─────────────────────────────────────────────────────────────────
  test("TEST 5 — database verification: both files embedded in Qdrant via API", async ({
    page,
  }) => {
    expect(testCorpusId).not.toBe("");

    // Load blank page to avoid CORS issues with page.evaluate
    await page.goto("about:blank");

    const docs = await page.evaluate(
      async ({ apiUrl, token }) => {
        const r = await fetch(`${apiUrl}/api/documents?limit=50`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        return r.json();
      },
      { apiUrl: API_URL, token: jwtToken },
    );

    const docList = docs as Array<{
      doc_id: string;
      filename: string;
      chunk_count: number;
      embedded: boolean;
    }>;

    // Find Architecture doc
    const doc1 = docList.find(
      (d) =>
        d.filename?.toLowerCase().includes("architecture") ||
        d.filename?.toLowerCase().includes("feasibility"),
    );
    // Find Product Overview doc
    const doc2 = docList.find(
      (d) =>
        d.filename?.toLowerCase().includes("product") ||
        d.filename?.toLowerCase().includes("overview"),
    );

    expect(doc1).toBeDefined();
    expect(doc1!.chunk_count).toBeGreaterThan(0);
    expect(doc1!.embedded).toBe(true);

    expect(doc2).toBeDefined();
    expect(doc2!.chunk_count).toBeGreaterThan(0);
    expect(doc2!.embedded).toBe(true);
  });

  // ── TEST 6 ─────────────────────────────────────────────────────────────────
  test("TEST 6 — query returns non-empty grounded response", async ({
    page,
  }) => {
    await goHome(page);

    // Open corpus multi-select and pick the test corpus
    await page.locator('[data-testid="corpus-multi-select"]').click();
    // Wait for dropdown to appear then select the E2E corpus
    await page.waitForTimeout(500);
    const corpusBtn = page
      .locator('button')
      .filter({ hasText: /E2E_Test/i })
      .first();
    if (await corpusBtn.isVisible({ timeout: 2_000 }).catch(() => false)) {
      await corpusBtn.click();
    }
    // Dismiss dropdown
    await page.keyboard.press("Escape");

    // Type and submit query
    await page
      .locator('[data-testid="query-input"]')
      .fill(
        "What are the key architecture components and technical feasibility findings?",
      );
    await page.locator('[data-testid="query-submit"]').click();

    // Wait for the response panel to have content (streaming completes)
    await expect(page.locator('[data-testid="response-panel"]')).not.toBeEmpty(
      { timeout: 90_000 },
    );

    const responseText = await page
      .locator('[data-testid="response-panel"]')
      .textContent();
    expect(responseText?.trim().length).toBeGreaterThan(50);
  });

  // ── TEST 7 ─────────────────────────────────────────────────────────────────
  test("TEST 7 — Qdrant collections populated; hierarchical if available", async ({
    page,
  }) => {
    // Verify Qdrant collections have vectors
    await page.goto("about:blank");

    const qdrantResp = await page.evaluate(async () => {
      const r = await fetch("http://localhost:6333/collections");
      return r.json();
    });

    const collections = (
      qdrantResp as { result: { collections: Array<{ name: string }> } }
    ).result.collections;
    const names = collections.map((c) => c.name);

    expect(names).toContain("polymath_naive");

    // Check naive collection has points
    const naiveInfo = await page.evaluate(async () => {
      const r = await fetch("http://localhost:6333/collections/polymath_naive");
      return r.json();
    });
    const pointCount = (
      naiveInfo as { result?: { points_count?: number } }
    ).result?.points_count;
    expect(typeof pointCount).toBe("number");
    expect(pointCount).toBeGreaterThan(0);

    // Bonus: run a graph-mode query if hrag collection has data
    if (names.includes("polymath_hrag")) {
      const hragInfo = await page.evaluate(async () => {
        const r = await fetch(
          "http://localhost:6333/collections/polymath_hrag",
        );
        return r.json();
      });
      const hragPoints = (
        hragInfo as { result?: { points_count?: number } }
      ).result?.points_count;

      if (hragPoints && hragPoints > 0) {
        await goHome(page);
        // Toggle Graph mode on
        const graphToggle = page
          .locator("button")
          .filter({ hasText: /Graph/i })
          .first();
        if (
          await graphToggle.isVisible({ timeout: 2_000 }).catch(() => false)
        ) {
          await graphToggle.click();
        }
        await page
          .locator('[data-testid="query-input"]')
          .fill(
            "What are the main technical components described in the documents?",
          );
        await page.locator('[data-testid="query-submit"]').click();
        await expect(
          page.locator('[data-testid="response-panel"]'),
        ).not.toBeEmpty({ timeout: 90_000 });
      } else {
        console.log("⚠ polymath_hrag has no vectors — skipping graph query");
      }
    } else {
      console.log("⚠ polymath_hrag collection not found — skipping");
    }
  });
});
