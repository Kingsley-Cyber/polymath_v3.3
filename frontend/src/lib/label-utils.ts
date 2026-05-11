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

const MAX_FALLBACK_LEN = 40;

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
    return `${parts[0]} -- ${parts[1]}`;
  }
  if (parts.length === 1 && parts[0].length <= MAX_FALLBACK_LEN) {
    return parts[0];
  }

  // 5. Fallback: truncate at MAX_FALLBACK_LEN with ellipsis.
  if (s.length <= MAX_FALLBACK_LEN) return s;
  return s.slice(0, MAX_FALLBACK_LEN).trimEnd() + "…";
}
