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

// Repeated group: converted files stack extensions ("Book.pdf.md").
const FILE_EXT_RE = /(?:\.(?:md|pdf|docx|epub|txt|html|htm))+$/i;
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

// ============================================================================
// parseBookMeta — full filename → {title, author, year, series} metadata.
//
// Unlike cleanBookLabel (canvas labels: aggressively truncated), this returns
// UNTRUNCATED fields for list rows/tooltips and understands both filename
// conventions in the corpus:
//
//   Anna's Archive : "Title -- Author [Author, A.] -- 1977 -- Publisher -- <hash>"
//   libgen         : "[Series] Author(s) - Title_ subtitle (Year, Publisher)"
//
// libgen encodings handled: "_ " = ":" (colon is illegal in filenames),
// "_" joins co-authors ("Kapadia, Amar_Rajana, Kris"), "{Author}" braces when
// there is no " - " separator, "(auth.)"/"(editor)" annotations, degree noise
// ("(Hons), Gene Da Rocha Msc Bsc"), "coll_" = collective author, and
// "Lastname, Firstname" flipped to display order.
// ============================================================================

export interface BookMeta {
  /** Full human title (":" restored, snake_case unsnaked). Never truncated. */
  title: string;
  /** Primary author in "First Last" display order; "" when unknown. */
  author: string;
  /** 4-digit publication year; "" when unknown. */
  year: string;
  /** Series / journal bracket prefix; "" when absent. */
  series: string;
  /** The original input, untouched — for tooltips. */
  raw: string;
}

const YEAR_RE = /\b(1[89]\d{2}|20\d{2})\b/;
const DEGREE_NOISE_RE =
  /\b(?:msc|bsc|ba|ma|phd|ph\.d\.?|m\.d\.?|dr\.?|prof\.?|hons)\b\.?/gi;

function collapse(s: string): string {
  return s.replace(/\s{2,}/g, " ").trim();
}

/** Strip trailing annotation parens/brackets; capture a year if one appears. */
function stripTrailingAnnotations(s: string): { text: string; year: string } {
  let year = "";
  let out = s.trim();
  for (let i = 0; i < 4; i++) {
    const m = out.match(/\s*[([]([^)\]]*)[)\]]\s*$/);
    if (!m) break;
    const y = m[1].match(YEAR_RE);
    if (y && !year) year = y[1];
    out = out.slice(0, m.index).trim();
  }
  return { text: out, year };
}

/** Author segment → single display-order author. */
function cleanAuthorSegment(seg: string): string {
  let s = seg;
  s = s.replace(/\{[^}]*\}?/g, " "); // stray {Author} braces
  s = s.replace(/\([^)]*\)/g, " "); // "(auth.)", "(editor)", "(Hons)"
  s = s.replace(/\[[^\]]*\]/g, " "); // "[Becker, Ernest]" dupes
  s = s.replace(/_+/g, "; "); // libgen co-author joiner
  s = collapse(s).replace(/^[,;\s]+/, "");
  // First listed author only.
  s = s.split(";")[0];
  s = s.replace(/\s+(?:and|with|&)\s+.+$/i, "");
  const commas = (s.match(/,/g) || []).length;
  if (commas >= 2) {
    // "First Last, First Last, ..." co-author list — keep the first.
    s = s.slice(0, s.indexOf(","));
  } else if (commas === 1) {
    const [last, first] = s.split(",").map((p) => p.trim());
    if (first && last && !last.includes(" ") && first.split(/\s+/).length <= 3) {
      // "Lastname, Firstname" → display order.
      s = `${first} ${last}`;
    } else {
      // "First Last, First Last" two-author list — keep the first.
      s = last;
    }
  }
  s = s.replace(DEGREE_NOISE_RE, " ");
  return collapse(s).replace(/^[,.\s-]+|[,.\s-]+$/g, "");
}

/** Title segment → readable title (libgen "_ " = ":", "..." tails dropped). */
function cleanTitleSegment(seg: string): string {
  let s = seg;
  s = s.replace(/\{[^}]*\}?\s*$/g, " "); // embedded {Author} at tail
  s = s.replace(/\s*\.{3,}\s*/g, " "); // truncation ellipses
  s = s.replace(/\s*_+\s/g, ": "); // libgen colon encoding ("_ " or " __ ")
  s = s.replace(/_/g, " "); // snake_case remainder
  return collapse(s).replace(/^[,:\s-]+|[,\s-]+$/g, "");
}

// Small words stay lowercase in Title Case (unless they lead the title).
const TITLE_SMALL_WORDS = new Set([
  "a", "an", "and", "as", "at", "but", "by", "for", "in", "into", "nor",
  "of", "on", "or", "the", "to", "via", "vs", "with",
]);

function titleCaseSlug(s: string): string {
  return s
    .split(/\s+/)
    .map((w, i) =>
      i > 0 && TITLE_SMALL_WORDS.has(w)
        ? w
        : w.charAt(0).toUpperCase() + w.slice(1),
    )
    .join(" ");
}

/** Spaced single-dash separators (" - ", " – ", uneven spacing tolerated). */
function dashSeparators(s: string): Array<{ index: number; length: number }> {
  const re = /\s+[-–]\s+/g;
  const out: Array<{ index: number; length: number }> = [];
  for (let m = re.exec(s); m; m = re.exec(s)) {
    out.push({ index: m.index, length: m[0].length });
  }
  return out;
}

/** True when the left side of "L - R" reads like an author, not a title. */
function looksAuthorFirst(left: string, right: string, sawYearParen: boolean): boolean {
  if (sawYearParen) return true; // "(Year, Publisher)" tail ⇒ libgen ⇒ author-first
  if (left.includes(",")) return true; // "Camerer, Colin F - ..."
  const lw = left.split(/\s+/).length;
  const rw = right.split(/\s+/).length;
  return lw <= 4 && rw >= 5; // short name, long title
}

export function parseBookMeta(raw: string | null | undefined): BookMeta {
  const original = String(raw ?? "");
  let s = original.trim();
  const empty: BookMeta = { title: "", author: "", year: "", series: "", raw: original };
  if (!s) return empty;

  s = s.replace(FILE_EXT_RE, "");
  s = s.replace(TRAILING_HASH_RE, ""); // "-- <hex> -- Anna's Archive" tails

  // Kebab slug ("atomic-habits-james-clear", all lowercase, no spaces):
  // unhyphenate + Title Case; a 4-digit year splits off the publisher tail.
  // Author can't be reliably separated in a flat slug — left in the title.
  if (s && !/[\s_]/.test(s) && !/[A-Z]/.test(s)) {
    let t = collapse(s.replace(/-+/g, " "));
    t = t.replace(/\b[a-f0-9]{8,40}\b\s*$/i, "").trim(); // slugified hash tail
    let year = "";
    const ym = t.match(YEAR_RE);
    if (ym && typeof ym.index === "number") {
      year = ym[1];
      t = t.slice(0, ym.index).trim(); // year + publisher tail cut
    }
    return { title: titleCaseSlug(t), author: "", year, series: "", raw: original };
  }

  // Leading "[Series]" / "[Journal ...]" prefix.
  let series = "";
  const sm = s.match(/^\s*\[([^\]]*)\]\s*/);
  if (sm) {
    series = collapse(sm[1].replace(/_+/g, " "));
    s = s.slice(sm[0].length);
  }

  // "coll_" = collective/institutional author (libgen marker).
  if (/^coll[_\s]/i.test(s)) s = s.replace(/^coll[_\s]+/i, "");

  const { text, year: parenYear } = stripTrailingAnnotations(s);
  s = text;
  let year = parenYear;

  let title: string;
  let author = "";

  const dd = s.split(SEGMENT_SEP_RE).map((p) => p.trim()).filter(Boolean);
  if (dd.length >= 2) {
    // Anna's Archive: Title -- Author [-- Year -- Publisher ...]
    title = cleanTitleSegment(dd[0]);
    author = cleanAuthorSegment(dd[1]);
    if (!year) {
      const ySeg = dd.slice(2).find((p) => /^(1[89]\d{2}|20\d{2})$/.test(p));
      if (ySeg) year = ySeg;
    }
  } else if (dashSeparators(s).length > 0) {
    const seps = dashSeparators(s);
    const first = seps[0];
    if (
      looksAuthorFirst(
        s.slice(0, first.index),
        s.slice(first.index + first.length),
        Boolean(parenYear),
      )
    ) {
      // libgen: Author - Title (split at FIRST sep; titles may contain dashes)
      author = cleanAuthorSegment(s.slice(0, first.index));
      title = cleanTitleSegment(s.slice(first.index + first.length));
    } else {
      // "Title - Author" (split at LAST sep)
      const last = seps[seps.length - 1];
      title = cleanTitleSegment(s.slice(0, last.index));
      author = cleanAuthorSegment(s.slice(last.index + last.length));
    }
  } else {
    // No separator. Check for libgen "{Author}" embedded at the tail.
    const bm = s.match(/\{([^}]*)\}?\s*$/);
    if (bm && bm[1].trim()) author = cleanAuthorSegment(bm[1]);
    title = cleanTitleSegment(s);
  }

  if (!title) title = cleanTitleSegment(s) || collapse(original);
  return { title, author, year, series, raw: original };
}
