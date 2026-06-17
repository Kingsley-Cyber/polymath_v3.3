import { chromium } from '@playwright/test';
import fs from 'fs';

const BASE = process.env.BASE_URL || 'http://localhost:3000';
const API = process.env.API_URL || 'http://localhost:8000';
const USER = process.env.ADMIN_USER || 'admin';
const PASS = process.env.ADMIN_PASSWORD || '';
const QUERY = process.env.PROBE_QUERY ||
  'How does cognitive dissonance connect to cognitive-behavioral therapy in this library?';
const DIR = 'tab-screenshots/pm-film';
fs.mkdirSync(DIR, { recursive: true });

const tokenResp = await fetch(`${API}/api/auth/login`, {
  method: 'POST', headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ username: USER, password: PASS }),
});
const { access_token } = await tokenResp.json();
const user = await (await fetch(`${API}/api/auth/me`, { headers: { Authorization: `Bearer ${access_token}` } })).json();

const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 1440, height: 900 } });
await p.addInitScript(([t, u]) => {
  localStorage.setItem('polymath-auth', JSON.stringify({ state: { token: t, user: u }, version: 0 }));
}, [access_token, user]);
await p.goto(BASE, { waitUntil: 'domcontentloaded' });
await p.waitForSelector('[data-testid="query-input"]', { timeout: 20000 });
await p.waitForTimeout(800);

await p.fill('[data-testid="query-input"]', QUERY);
await p.click('[data-testid="query-submit"]');
const t0 = Date.now();

const frames = [];
for (let i = 0; i < 45; i++) {
  const t = ((Date.now() - t0) / 1000).toFixed(1);
  const snap = await p.evaluate(() => {
    const cards = document.querySelectorAll('.process-group').length;
    const active = document.querySelectorAll('.process-group-active').length;
    const shiny = document.querySelectorAll('.pm-process-title.shiny-text, .shiny-text').length;
    const draft = !!document.querySelector('.pm-live-answer-draft');
    const draftLabel = (document.querySelector('.pm-live-answer-draft-head')?.innerText || '').slice(0, 50);
    const titles = [...document.querySelectorAll('.pm-process-title')].map((e) => e.innerText.trim()).slice(-6);
    const ans = (document.querySelector('[data-testid="response-panel"]')?.innerText || '').length;
    return { cards, active, shiny, draft, draftLabel, titles, ans };
  }).catch(() => null);
  if (snap) {
    frames.push({ t, ...snap });
    await p.screenshot({ path: `${DIR}/f${String(i).padStart(2, '0')}_${t}s.png`, fullPage: false });
  }
  // stop once answer is long and no active card for a few frames
  if (snap && snap.active === 0 && snap.ans > 400 && i > 6) {
    const recent = frames.slice(-3);
    if (recent.length === 3 && recent.every((f) => f.active === 0)) break;
  }
  await p.waitForTimeout(1200);
}

console.log(JSON.stringify(frames.map((f) => ({ t: f.t, cards: f.cards, active: f.active, shiny: f.shiny, draft: f.draft, label: f.draftLabel, ans: f.ans, last: f.titles[f.titles.length - 1] })), null, 0));
await b.close();
