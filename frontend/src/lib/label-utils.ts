/**
 * Label cleanup utilities for the Brain View canvas.
 *
 * The Polymath ingestion pipeline stores the file's literal filename on
 * `:Document.filename`, which is often a messy academic citation with
 * authors, year, publisher, and a hash tail — e.g.:
 *
 *   "The Denial of Death -- Ernest Becker [Becker, Ernest] -- 1977 --
 *    Simon and Schuster -- dc49150836d49c61a56bdb56b1c7602e -- Anna's
 *    Archive.md"
 *
 * Rendering that as a node label is unreadable. `cleanBookLabel` distills
 * the filename to "Title -- Author" by stripping extensions, trailing
 * hashes, parenthetical / bracketed annotations, and then keeping the
 * first two `--`-separated segments. The raw filename is kept on the
 * node `display_name` so tooltips and selection bars can show full
 * context on demand.
 */

const FILE_EXT_RE = /\.(md|pdf|docx|epub|txt|html|htm)$/i;
const TRAILING_HASH_RE = /\s*-{1,2}\s*[a-f0-9]{8,40}.*$/i;
const TRAILING_BRACKET_RE = /\s*\[[^\]]*\]\s*$/;
const TRAILING_PAREN_RE = /\s*\([^)]*\)\s*$/;
const SEGMENT_SEP_RE = /\s+--\s+|\s+—\s+/;

// Pt 4 polish: hard caps so books with verbose titles + co-authors don't
// render as 80-char labels that collide on the canvas.
const MAX_TITLE_LEN = 32;
const MAX_AUTHOR_LEN = 22;
const MAX_FALLBACK_LEN = 32;

/** Trim at the last word boundary <= maxLen, append ellipsis. */
function smartTrim(s: string, maxLen: number): string {
  if (s.length <= maxLen) return s;
  const slice = s.slice(0, maxLen);
  const lastSpace = slice.lastIndexOf(" ");
  // Only break at a word boundary if it's reasonably close to the end —
  // otherwise just take maxLen chars and append ellipsis.
  const cut = lastSpace > maxLen * 0.6 ? lastSpace : maxLen;
  return slice.slice(0, cut).trimEnd() + "…";
}

/** Snake_case → space-separated, then trim. */
function unsnake(s: string): string {
  return s.replace(/_/g, " ").replace(/\s{2,}/g, " ").trim();
}

/** Authors → first author only. Cuts at ', and ', ' and ', '; ', ' & ', ', '
 *  so "Connie Scoles West and Robert J_ Marzano; with Kathy Marx" becomes
 *  "Connie Scoles West". */
function firstAuthor(s: string): string {
  const cuts = [
    /\s+(?:and|with|&)\s+.*$/i,
    /\s*,\s+.*$/, // "Alvesson, Mats; Sköldberg, Kaj" → "Alvesson"
    /\s*;\s*.*$/,
  ];
  let out = s;
  for (const re of cuts) {
    out = out.replace(re, "");
  }
  return out.trim();
}

/**
 * Distill a raw filename / citation string into a short display label.
 *
 * Examples:
 *   "Mastery - Robert Greene.md" → "Mastery - Robert Greene"
 *   "The Denial of Death -- Ernest Becker [Becker, Ernest] -- 1977 -- Simon and Schuster -- dc49150836d49c61a56bd..."
 *       → "The Denial of Death -- Ernest Becker"
 *   "Effective Modern C++ - Scott Meyers.md" → "Effective Modern C++ - Scott Meyers"
 *   "Pattern_Recognition_and_Machine_Learning_Christopher_Bishop.md"
 *       → "Pattern_Recognition_and_Machine_L…" (no `--` separator → fallback)
 *
 * Pure / deterministic / side-effect-free.
 */
export function cleanBookLabel(raw: string | null | undefined): string {
  if (!raw) return "";
  let s = String(raw).trim();
  if (!s) return "";

  // 1. Strip file extension.
  s = s.replace(FILE_EXT_RE, "");

  // 2. Strip trailing hash tails (e.g. "-- dc49150836d49c61a56bdb56...").
  //    Pass 1: anything after a hex-looking blob preceded by "--" or "-".
  s = s.replace(TRAILING_HASH_RE, "");

  // 3. Strip trailing parenthetical/bracket annotations (repeat — there
  //    can be more than one). Stop after a few rounds so we don't loop
  //    forever on pathological input.
  for (let i = 0; i < 4; i++) {
    const next = s.replace(TRAILING_BRACKET_RE, "").replace(TRAILING_PAREN_RE, "");
    if (next === s) break;
    s = next;
  }
  s = s.trim();

  // 4. Split on `--` or em-dash and keep first two segments (title + author).
  const parts = s.split(SEGMENT_SEP_RE).map((p) => p.trim()).filter(Boolean);
  if (parts.length >= 2) {
    const title = smartTrim(unsnake(parts[0]), MAX_TITLE_LEN);
    const author = smartTrim(firstAuthor(unsnake(parts[1])), MAX_AUTHOR_LEN);
    return author ? `${title} — ${author}` : title;
  }
  if (parts.length === 1) {
    return smartTrim(unsnake(parts[0]), MAX_FALLBACK_LEN);
  }

  // 5. Fallback: unsnake + truncate at MAX_FALLBACK_LEN with ellipsis.
  return smartTrim(unsnake(s), MAX_FALLBACK_LEN);
}
