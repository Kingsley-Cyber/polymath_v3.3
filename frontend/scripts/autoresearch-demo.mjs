// Playwright headed UI demo — every action is a real button click or form fill.
// No backend API shortcuts. Red cursor overlay + slow-mo so every step is visible.
//
// Run:  node C:/Users/Sammb/Downloads/Polymath_v3.3/frontend/scripts/autoresearch-demo.mjs

import { chromium } from '@playwright/test';

const FRONTEND = 'http://localhost:3000';
const USERNAME = 'dev';
const PASSWORD = 'devpass-throwaway-2026';

const CORPUS_NAME = 'Autoresearch';
const FILES = [
  'C:/Users/Sammb/Downloads/Polymath_v3.3/TEST/Autoresearch/Product Overview.txt',
  'C:/Users/Sammb/Downloads/Polymath_v3.3/TEST/Autoresearch/Architecture_Feasibility_Report.docx',
];

const KEY_A = process.env.DEEPSEEK_KEY_A || '';
const KEY_B = process.env.DEEPSEEK_KEY_B || '';
const CONCURRENCY = 33;

if (!KEY_A || !KEY_B) {
  throw new Error('Set DEEPSEEK_KEY_A and DEEPSEEK_KEY_B before running autoresearch-demo.mjs');
}

// -------------------------------------------------------------------------

(async () => {
  console.log('[1] launching headed Chromium with red cursor overlay…');
  const browser = await chromium.launch({
    headless: false,
    slowMo: 350,
    args: ['--window-size=1440,900', '--window-position=60,40'],
  });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();

  // Inject red cursor overlay that follows synthetic Playwright mouse events.
  await page.addInitScript(() => {
    const install = () => {
      if (document.getElementById('__pw_cursor')) return;
      const c = document.createElement('div');
      c.id = '__pw_cursor';
      c.style.cssText = `
        position: fixed; top: -100px; left: -100px;
        width: 22px; height: 22px;
        background: radial-gradient(circle, rgba(255,30,30,0.95) 0%, rgba(255,30,30,0.25) 70%, transparent 100%);
        border: 2px solid #ff1e1e;
        border-radius: 50%;
        pointer-events: none;
        z-index: 2147483647;
        transform: translate(-50%, -50%);
        box-shadow: 0 0 12px rgba(255,30,30,0.8);
        transition: box-shadow 0.15s ease;
      `;
      document.documentElement.appendChild(c);
      const move = (x, y) => { c.style.left = x + 'px'; c.style.top = y + 'px'; };
      document.addEventListener('mousemove', (e) => move(e.clientX, e.clientY), true);
      document.addEventListener('click', (e) => {
        move(e.clientX, e.clientY);
        c.style.boxShadow = '0 0 28px 10px rgba(255,30,30,1)';
        setTimeout(() => { c.style.boxShadow = '0 0 12px rgba(255,30,30,0.8)'; }, 220);
      }, true);
    };
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', install);
    } else {
      install();
    }
  });

  // -- LOGIN via UI -------------------------------------------------------
  console.log('[2] navigating and logging in via UI…');
  await page.goto(FRONTEND);
  await page.waitForSelector('input[placeholder="admin"]', { timeout: 15000 });
  await page.fill('input[placeholder="admin"]', USERNAME);
  await page.fill('input[type="password"]', PASSWORD);
  await page.click('button[type="submit"]');

  // -- OPEN CORPUS MANAGER -----------------------------------------------
  console.log('[3] opening Corpus Manager…');
  await page.waitForSelector('[data-testid="sidebar-db-btn"]', { timeout: 15000 });
  await page.click('[data-testid="sidebar-db-btn"]');
  await page.waitForTimeout(600);

  // -- CLICK NEW CORPUS ---------------------------------------------------
  console.log('[4] clicking "+ New Corpus"…');
  await page.click('[data-testid="create-corpus-btn"]');
  await page.waitForSelector('[data-testid="corpus-name-input"]', { timeout: 8000 });

  // -- FILL NAME + DESCRIPTION -------------------------------------------
  console.log('[5] filling name + description…');
  await page.fill('[data-testid="corpus-name-input"]', CORPUS_NAME);
  await page.fill('input[placeholder="description (optional)"]', 'Playwright demo — Deep mode, two DeepSeek pools @ 33 concurrency');

  // -- SELECT DEEP PRESET ------------------------------------------------
  console.log('[6] selecting Deep preset…');
  // Radio input id is `${idPrefix}-preset-${key}` with idPrefix="create"
  await page.click('label[for="create-preset-deep"]');
  await page.waitForTimeout(300);

  // -- UNCHECK "Reuse Summary pool" so Extraction becomes independent ----
  console.log('[7] unchecking "Reuse Summary pool for Extraction"…');
  // The checkbox is the input inside the label containing that text
  const reuseCheckbox = page.locator('label', { hasText: 'Reuse Summary pool for Extraction' }).locator('input[type="checkbox"]');
  if (await reuseCheckbox.isChecked()) {
    await reuseCheckbox.click();
    await page.waitForTimeout(300);
  }

  // -- FILL SUMMARY POOL (Ghost A) ---------------------------------------
  // Summary card = first pool editor in the ingestion models block.
  // Each pool's editor has: <select>, two text inputs, one number input,
  // one password input, and an Add button with title="Add to pool (or press Enter)".
  console.log('[8] configuring Ghost A (Summary) pool: deepseek · concurrency 33 · key #1…');
  const summaryCard = page.locator('div', { hasText: /Summary Models \(GHOST A\)/i }).last();
  await summaryCard.locator('select').selectOption('deepseek');
  await page.waitForTimeout(300);
  await summaryCard.locator('input[placeholder="base_url (blank = default)"]').fill('https://api.deepseek.com/v1');
  await summaryCard.locator('input[placeholder="model (required)"]').fill('deepseek/deepseek-chat');
  const summaryConc = summaryCard.locator('input[type="number"]');
  await summaryConc.click({ clickCount: 3 });
  await summaryConc.fill(String(CONCURRENCY));
  await summaryCard.locator('input[type="password"]').fill(KEY_A);
  await summaryCard.locator('button[title="Add to pool (or press Enter)"]').click();
  await page.waitForTimeout(500);

  // -- FILL EXTRACTION POOL (Ghost B) ------------------------------------
  console.log('[9] configuring Ghost B (Extraction) pool: deepseek · concurrency 33 · key #2…');
  const extractionCard = page.locator('div', { hasText: /Extraction Models \(GHOST B\)/i }).last();
  await extractionCard.locator('select').selectOption('deepseek');
  await page.waitForTimeout(300);
  await extractionCard.locator('input[placeholder="base_url (blank = default)"]').fill('https://api.deepseek.com/v1');
  await extractionCard.locator('input[placeholder="model (required)"]').fill('deepseek/deepseek-chat');
  const extractionConc = extractionCard.locator('input[type="number"]');
  await extractionConc.click({ clickCount: 3 });
  await extractionConc.fill(String(CONCURRENCY));
  await extractionCard.locator('input[type="password"]').fill(KEY_B);
  await extractionCard.locator('button[title="Add to pool (or press Enter)"]').click();
  await page.waitForTimeout(500);

  // -- SUBMIT CREATE -----------------------------------------------------
  console.log('[10] clicking Create…');
  await page.click('[data-testid="corpus-create-submit"]');
  await page.waitForTimeout(1500);

  // -- OPEN CORPUS DETAIL ------------------------------------------------
  console.log('[11] opening the newly created corpus…');
  await page.waitForSelector('[data-testid="corpus-browse-btn"]', { timeout: 8000 });
  await page.locator('[data-testid="corpus-browse-btn"]').first().click();
  await page.waitForTimeout(800);

  // -- UPLOAD BOTH FILES -------------------------------------------------
  console.log('[12] uploading both files via the ingest input…');
  await page.locator('[data-testid="corpus-file-input"]').setInputFiles(FILES);

  console.log('[13] ingestion started. Watch the UI — pipeline status will progress');
  console.log('     through parse → chunk → ghosts → mongo → embed → qdrant → neo4j');
  console.log('     for each document. DeepSeek is doing ~40+ LLM roundtrips per doc.');
  console.log('');
  console.log('Browser stays open. Close it manually when done reviewing.');

  // Keep node process alive so the browser window doesn't close.
  await new Promise(() => {});
})().catch(err => {
  console.error('❌ demo failed:', err);
  process.exit(1);
});
