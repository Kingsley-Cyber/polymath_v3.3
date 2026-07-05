/**
 * Library panel redesign — TRUE frontend E2E.
 *
 * Drives the real React UI: open Corpus Manager → open the live-batch corpus →
 * assert the redesigned library half:
 *   1. rows render PARSED book titles (never the raw libgen filename)
 *   2. READY / FAILED sections render with counts
 *   3. batch failures that never became documents (chunker timeouts) appear
 *      in FAILED with a plain-English reason — so the library's failure count
 *      finally agrees with the batch header
 *   4. the filter narrows rows by title / author
 *
 * Run:
 *   TOKEN=<jwt>  (or ADMIN_USER/ADMIN_PASSWORD) \
 *   BASE_URL=http://localhost:3000 API_URL=http://localhost:8000 \
 *   CORPUS_NAME=authentic_library_v2 \
 *   npx playwright test tests/e2e/library-panel.spec.ts --project=chromium --reporter=line
 */
import { test, expect } from "@playwright/test";
import * as fs from "fs";
import { parseBookMeta } from "../../src/lib/label-utils";

const BASE = process.env.BASE_URL || "http://localhost:3000";
const API = process.env.API_URL || "http://localhost:8000";
const TOKEN = process.env.TOKEN || "";
const USER = process.env.ADMIN_USER || "admin";
const PASS = process.env.ADMIN_PASSWORD || "";
const CORPUS_NAME = process.env.CORPUS_NAME || "authentic_library_v2";
const SHOT = "tab-screenshots/library-panel";

test.use({
  launchOptions: { slowMo: 0 },
  viewport: { width: 1440, height: 900 },
  actionTimeout: 20_000,
});
test.setTimeout(180_000);
fs.mkdirSync(SHOT, { recursive: true });

test("library panel renders parsed names, sections, reasons, and filter", async ({
  page,
  request,
}) => {
  const pageErrors: string[] = [];
  page.on("pageerror", (e) => pageErrors.push(String(e).slice(0, 300)));

  // ── auth: minted TOKEN preferred (admin password is often stale), else login
  let token = TOKEN;
  if (!token) {
    const lr = await request.post(`${API}/api/auth/login`, {
      data: { username: USER, password: PASS },
    });
    expect(lr.ok(), `login failed (${lr.status()}) — pass TOKEN instead`).toBeTruthy();
    token = (await lr.json()).access_token as string;
  }
  const auth = { headers: { Authorization: `Bearer ${token}` } };
  const me = await (await request.get(`${API}/api/auth/me`, auth)).json();
  await page.addInitScript(
    ([t, u]) => {
      localStorage.setItem(
        "polymath-auth",
        JSON.stringify({ state: { token: t, user: u }, version: 0 }),
      );
    },
    [token, me] as const,
  );

  // ── ground truth from the API: a completed doc whose parsed title differs
  // from its raw filename (that difference is exactly what we're shipping).
  const corpora = (await (await request.get(`${API}/api/corpora`, auth)).json()) as Array<{
    corpus_id: string;
    name: string;
  }>;
  const corpus = corpora.find((c) => c.name === CORPUS_NAME);
  expect(corpus, `corpus "${CORPUS_NAME}" must exist`).toBeTruthy();

  const docs = (await (
    await request.get(
      `${API}/api/corpora/${corpus!.corpus_id}/documents?limit=100&offset=0`,
      auth,
    )
  ).json()) as Array<{
    filename?: string;
    write_state: {
      mongo_written?: boolean;
      qdrant_written?: boolean;
      neo4j_written?: boolean;
      verified?: boolean | null;
    };
  }>;
  const completed = docs.filter(
    (d) =>
      d.filename &&
      d.write_state?.mongo_written &&
      d.write_state?.qdrant_written &&
      d.write_state?.neo4j_written &&
      d.write_state?.verified !== false,
  );
  const candidates = completed.map((d) => ({
    raw: d.filename as string,
    meta: parseBookMeta(d.filename as string),
  }));
  // Prefer a doc with a parsed author; else any doc whose title was transformed.
  const probe =
    candidates.find((c) => c.meta.author && c.meta.title !== c.raw) ||
    candidates.find(
      (c) => c.meta.title && c.meta.title !== c.raw.replace(/\.\w+$/, ""),
    );
  expect(probe, "need one completed doc whose parsed title differs from raw").toBeTruthy();
  console.log(
    `[library-e2e] probe doc: "${probe!.meta.title}"${probe!.meta.author ? ` — ${probe!.meta.author}` : ""} (raw: ${probe!.raw.slice(0, 60)})`,
  );

  // ── open the app → Corpus Manager → the corpus detail
  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  await page.locator('button[title="Corpus Manager"]').first().click();
  const card = page
    .locator("div")
    .filter({ has: page.getByText(CORPUS_NAME, { exact: true }) })
    .filter({ has: page.getByTestId("corpus-browse-btn") })
    .last();
  await card.getByTestId("corpus-browse-btn").first().click();

  const panel = page.getByTestId("library-panel");
  await expect(panel, "library panel should render").toBeVisible({ timeout: 15_000 });

  // ── 1+2: sections with counts, parsed title visible, raw filename absent
  await expect(page.getByText("READY", { exact: true })).toBeVisible();
  await expect(page.getByText("FAILED", { exact: true })).toBeVisible();

  const probeRow = panel.getByText(probe!.meta.title, { exact: true }).first();
  await expect(probeRow, "parsed book title should render in the library").toBeVisible({
    timeout: 15_000,
  });
  await expect(
    panel.getByText(probe!.raw, { exact: true }),
    "raw libgen filename must NOT render as row text (tooltip only)",
  ).toHaveCount(0);

  // ── 3: batch-only failures surface with a plain-English reason
  // (chunker-timeout books never created documents; they used to be invisible)
  await expect(
    panel.getByText(/chunking timed out/i).first(),
    "chunker-timeout batch failures should appear in FAILED with a readable reason",
  ).toBeVisible({ timeout: 20_000 });

  await page.screenshot({ path: `${SHOT}/01-library.png`, fullPage: false });

  // ── 4: the filter narrows to the probe row, and a nonsense query empties it
  const filter = page.getByTestId("library-filter");
  await filter.fill(probe!.meta.title.slice(0, 14));
  await expect(probeRow).toBeVisible();
  await filter.fill("zzz_no_such_book_zzz");
  await expect(panel.getByText("[NO_MATCHES]").first()).toBeVisible();
  await filter.fill("");
  await page.screenshot({ path: `${SHOT}/02-filtered.png`, fullPage: false });

  expect(pageErrors, `page errors: ${pageErrors.join(" | ")}`).toHaveLength(0);
});
