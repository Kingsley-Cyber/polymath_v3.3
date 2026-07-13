"""Bibliographic + date identity for documents (T-HOOK-3 / P2.1).

Deterministic capture and resolution of document-level bibliographic fields:

    author, title, language,
    document_date, source_published_at, date_confidence,
    bibliographic_provenance {method, source, captured_at, precision?, reason?}

The de-conflation rule (TEMPORAL_RAG_E2E_IMPLEMENTATION_REPORT_2026-07-12 §6.1)
is the core contract of this module:

  * ``document_date`` / ``source_published_at`` are PUBLICATION identity.
  * File-creation and revision timestamps (DOCX core-props ``created``, PDF
    ``/CreationDate`` / ``/ModDate``, frontmatter ``created``/``modified``)
    NEVER populate them — ``document_date`` must never silently mean mtime.
  * Unknown stays ``null`` with a reason code in provenance, never guessed.

Everything here is pure stdlib + regex — no LLM, no network, no provider
calls. The same functions serve the ingest-time capture hook
(``docling_adapter.finalize_source_meta``) and the deterministic backfill
(``scripts/bibliographic_backfill.py``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Optional

# ─── Candidate kinds ────────────────────────────────────────────────────────
# publication        — an explicit publication-time signal
# ambiguous_date_key — a `date:` style key: conventionally publication, but
#                      the key does not say so (medium/low confidence)
# file_creation      — file-system / container creation time (NEVER publication)
# revision           — modification/revision time (NEVER publication)
KIND_PUBLICATION = "publication"
KIND_AMBIGUOUS = "ambiguous_date_key"
KIND_FILE_CREATION = "file_creation"
KIND_REVISION = "revision"

_PUBLICATION_GRADE_KINDS = (KIND_PUBLICATION, KIND_AMBIGUOUS)

# ─── Confidence by capture method ───────────────────────────────────────────
CONFIDENCE_BY_METHOD: dict[str, str] = {
    # explicit publication metadata
    "frontmatter_published": "high",
    "html_meta_published": "high",
    # conventional-but-unlabelled publication signals
    "frontmatter_date": "medium",
    "epub_dc_date": "medium",
    "citation_pattern": "medium",       # `Author - Title (2020, Wiley)` etc.
    # weak deterministic signals
    "text_head_date_line": "low",
    "text_head_copyright": "low",
    "filename_year": "low",
    "legacy_document_date": "low",      # pre-T-HOOK-3 conflated field
    # file-time methods carry no publication confidence at all
    "frontmatter_created": None,
    "frontmatter_modified": None,
    "docx_core_created": None,
    "docx_core_modified": None,
    "pdf_creation_date": None,
    "pdf_mod_date": None,
}

_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}

REASON_NO_DATE_SOURCE = "no_date_source"
REASON_FILE_DATE_ONLY = "file_date_only"
REASON_UNPARSEABLE_DATE = "unparseable_date"


@dataclass(frozen=True)
class DateCandidate:
    """One raw date observation with its capture provenance."""

    raw: str
    kind: str      # KIND_* above
    method: str    # key of CONFIDENCE_BY_METHOD
    source: str    # the exact key/pattern/filename fragment that produced it

    def as_dict(self) -> dict:
        return {
            "raw": self.raw,
            "kind": self.kind,
            "method": self.method,
            "source": self.source,
        }


def candidate_from_dict(d: dict) -> DateCandidate:
    return DateCandidate(
        raw=str(d.get("raw") or ""),
        kind=str(d.get("kind") or KIND_AMBIGUOUS),
        method=str(d.get("method") or "frontmatter_date"),
        source=str(d.get("source") or "")[:200],
    )


# ─── Frontmatter key → (kind, method) mapping (capture hook) ────────────────
FRONTMATTER_DATE_KEYS: dict[str, tuple[str, str]] = {
    "published": (KIND_PUBLICATION, "frontmatter_published"),
    "publish_date": (KIND_PUBLICATION, "frontmatter_published"),
    "pubdate": (KIND_PUBLICATION, "frontmatter_published"),
    "publication_date": (KIND_PUBLICATION, "frontmatter_published"),
    "date": (KIND_AMBIGUOUS, "frontmatter_date"),
    "created": (KIND_FILE_CREATION, "frontmatter_created"),
    "modified": (KIND_REVISION, "frontmatter_modified"),
    "updated": (KIND_REVISION, "frontmatter_modified"),
}

FRONTMATTER_LANGUAGE_KEYS = ("language", "lang")

# ─── Date normalization ─────────────────────────────────────────────────────
_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_ISO_FULL_RE = re.compile(r"\b(1[89]\d\d|20\d\d)-(\d\d)-(\d\d)(?!\d)")
_ISO_MONTH_RE = re.compile(r"\b(1[89]\d\d|20\d\d)-(\d\d)(?!\d)")
_YEAR_RE = re.compile(r"\b(1[89]\d\d|20\d\d)\b")
_MONTH_NAME_DAY_YEAR_RE = re.compile(
    r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(1[89]\d\d|20\d\d)\b"
)
_DAY_MONTH_NAME_YEAR_RE = re.compile(
    r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(1[89]\d\d|20\d\d)\b"
)
_YEAR_MONTH_NAME_RE = re.compile(
    r"\b(1[89]\d\d|20\d\d)\s+([A-Za-z]{3,9})\b"
)


def _valid_ymd(y: int, m: int, d: int) -> bool:
    try:
        datetime(y, m, d)
        return True
    except ValueError:
        return False


def normalize_date_string(raw: str) -> tuple[Optional[str], Optional[str]]:
    """Parse a raw date string into ``(iso_date, precision)``.

    precision ∈ {"day", "month", "year"}; year/month precision are stored
    normalized to the first day of the period (precision is recorded in
    provenance so this is never mistaken for day-level truth).
    Returns ``(None, None)`` when nothing parseable is found — never guesses.
    """
    s = (raw or "").strip()
    if not s:
        return None, None

    m = _ISO_FULL_RE.search(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and _valid_ymd(y, mo, d):
            return f"{y:04d}-{mo:02d}-{d:02d}", "day"

    m = _MONTH_NAME_DAY_YEAR_RE.search(s)
    if m and m.group(1).lower() in _MONTHS:
        mo, d, y = _MONTHS[m.group(1).lower()], int(m.group(2)), int(m.group(3))
        if _valid_ymd(y, mo, d):
            return f"{y:04d}-{mo:02d}-{d:02d}", "day"

    m = _DAY_MONTH_NAME_YEAR_RE.search(s)
    if m and m.group(2).lower() in _MONTHS:
        d, mo, y = int(m.group(1)), _MONTHS[m.group(2).lower()], int(m.group(3))
        if _valid_ymd(y, mo, d):
            return f"{y:04d}-{mo:02d}-{d:02d}", "day"

    m = _ISO_MONTH_RE.search(s)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-01", "month"

    m = _YEAR_MONTH_NAME_RE.search(s)
    if m and m.group(2).lower() in _MONTHS:
        return f"{int(m.group(1)):04d}-{_MONTHS[m.group(2).lower()]:02d}-01", "month"

    m = _YEAR_RE.search(s)
    if m:
        return f"{int(m.group(1)):04d}-01-01", "year"

    return None, None


# ─── The de-conflation resolver ─────────────────────────────────────────────

def resolve_document_dates(
    candidates: Iterable[DateCandidate | dict],
) -> dict:
    """Resolve raw date candidates into the document date fields.

    Returns::

        {document_date, source_published_at, date_confidence,
         method, source, precision, reason}

    Rules (unit-tested, the T-HOOK-3 contract):
      1. Only publication-grade kinds (publication / ambiguous_date_key) may
         populate ``document_date`` / ``source_published_at``.
      2. file_creation / revision candidates are recorded implicitly by the
         reason code but NEVER become a date field value.
      3. Best candidate = highest confidence, ties broken by input order
         (callers pass candidates in source-precedence order).
      4. Nothing parseable → nulls + reason code. Never ingest-time defaults.
    """
    norm: list[DateCandidate] = [
        c if isinstance(c, DateCandidate) else candidate_from_dict(c)
        for c in (candidates or [])
    ]
    saw_file_time = False
    saw_unparseable = False
    best: Optional[tuple[int, str, str, DateCandidate]] = None  # (rank, iso, precision, cand)
    for cand in norm:
        if cand.kind not in _PUBLICATION_GRADE_KINDS:
            saw_file_time = True
            continue
        confidence = CONFIDENCE_BY_METHOD.get(cand.method)
        if confidence is None:
            saw_file_time = True
            continue
        iso, precision = normalize_date_string(cand.raw)
        if iso is None:
            saw_unparseable = True
            continue
        rank = _CONFIDENCE_RANK[confidence]
        if best is None or rank > best[0]:
            best = (rank, iso, precision or "day", cand)

    if best is not None:
        rank, iso, precision, cand = best
        confidence = CONFIDENCE_BY_METHOD[cand.method]
        return {
            "document_date": iso,
            "source_published_at": iso,
            "date_confidence": confidence,
            "method": cand.method,
            "source": cand.source[:200],
            "precision": precision,
            "reason": None,
        }

    if saw_unparseable:
        reason = REASON_UNPARSEABLE_DATE
    elif saw_file_time:
        reason = REASON_FILE_DATE_ONLY
    else:
        reason = REASON_NO_DATE_SOURCE
    return {
        "document_date": None,
        "source_published_at": None,
        "date_confidence": None,
        "method": None,
        "source": None,
        "precision": None,
        "reason": reason,
    }


# ─── Filename / title citation patterns ─────────────────────────────────────

_EXT_RE = re.compile(
    r"\.(pdf|epub|mobi|docx?|md|txt|html?)\s*$", re.IGNORECASE
)
_LIBGEN_SUFFIX_RE = re.compile(r"[-\s]*\b(libgen(\.\w+)*|z-?lib(\.\w+)*)\b.*$",
                               re.IGNORECASE)
# `Author - Title (2020, Wiley)` — libgen/citation file naming
_AUTHOR_TITLE_YEAR_RE = re.compile(
    r"^(?P<author>[^\-\(\)\[\]{}]{3,80}?)\s+-\s+"
    r"(?P<title>[^\(\){}]{3,200}?)\s*"
    r"\((?P<year>1[89]\d\d|20\d\d)(?:\s*,\s*(?P<publisher>[^)]{1,60}))?\)",
)
# `Title{Author1_ Author2_ ...}(2010)` — journal-scrape naming
_TITLE_AUTHORS_BRACE_YEAR_RE = re.compile(
    r"^(?P<title>[^{}]{3,240}?)\s*\{(?P<authors>[^{}]{3,200})\}\s*"
    r"\((?P<year>1[89]\d\d|20\d\d)\)",
)
# `TITLE(1975 January)` — journal-scrape naming with month
_TITLE_YEAR_MONTH_RE = re.compile(
    r"^(?P<title>[^\(\){}]{3,240}?)\s*"
    r"\((?P<year>1[89]\d\d|20\d\d)(?:\s+(?P<month>[A-Za-z]{3,9}))?\)",
)
_LEADING_BRACKET_RE = re.compile(r"^\s*\[[^\]]{0,120}\]\s*")
# trailing year token in a slug filename: `...-2016.md`
_SLUG_TRAILING_YEAR_RE = re.compile(r"[-_ .](1[89]\d\d|20\d\d)$")

_AUTHOR_SANITY_RE = re.compile(
    r"^[A-Za-z][A-Za-z .,'\-]{2,79}$"
)


def _clean_citation_title(title: str) -> str:
    # libgen convention: `_ ` stands for `: ` inside filenames
    t = re.sub(r"_\s", ": ", title).strip(" -_")
    return re.sub(r"\s+", " ", t)[:300]


def _clean_citation_authors(authors: str) -> str:
    # journal-scrape convention: `_ ` separates authors
    a = re.sub(r"_\s", "; ", authors).strip(" -_")
    return re.sub(r"\s+", " ", a)[:200]


def _basename_no_ext(name: str) -> str:
    base = PureWindowsPath(name).name if "\\" in name else PurePosixPath(name).name
    base = _EXT_RE.sub("", base.strip())
    return base.strip()


def parse_citation_name(name: str) -> dict:
    """Deterministically parse a citation-style file/heading name.

    Handles the three naming families observed in the live corpora (libgen
    `Author - Title (Year, Publisher)`, journal `Title{Authors}(Year)`,
    journal `TITLE(Year Month)`). Returns any of {author, title, year_raw,
    pattern}; empty dict when no pattern matches. Slug filenames
    (`some-book-title-author.md`) intentionally do NOT match — an author is
    never guessed out of a slug.
    """
    if not name:
        return {}
    base = _basename_no_ext(str(name))
    base = _LIBGEN_SUFFIX_RE.sub("", base).strip(" -_")
    stripped = _LEADING_BRACKET_RE.sub("", base).strip()
    if not stripped:
        return {}

    m = _AUTHOR_TITLE_YEAR_RE.match(stripped)
    if m:
        author = re.sub(r"\s+", " ", m.group("author")).strip(" -_")
        title = _clean_citation_title(m.group("title"))
        out: dict = {
            "title": title,
            "year_raw": m.group("year"),
            "pattern": "author_title_year",
        }
        if _AUTHOR_SANITY_RE.match(author) and 1 <= len(author.split()) <= 6:
            out["author"] = author[:200]
        return out

    m = _TITLE_AUTHORS_BRACE_YEAR_RE.match(stripped)
    if m:
        return {
            "author": _clean_citation_authors(m.group("authors")),
            "title": _clean_citation_title(m.group("title")),
            "year_raw": m.group("year"),
            "pattern": "title_authors_brace_year",
        }

    m = _TITLE_YEAR_MONTH_RE.match(stripped)
    if m:
        year_raw = m.group("year")
        month = (m.group("month") or "").strip()
        if month and month.lower() in _MONTHS:
            year_raw = f"{m.group('year')} {month}"
        return {
            "title": _clean_citation_title(m.group("title")),
            "year_raw": year_raw,
            "pattern": "title_year_month",
        }
    return {}


def filename_year_candidate(filename: str) -> Optional[DateCandidate]:
    """Trailing year in a slug filename (`...-theory-and-practice-2016.md`)."""
    if not filename:
        return None
    base = _basename_no_ext(str(filename))
    m = _SLUG_TRAILING_YEAR_RE.search(base)
    if not m:
        return None
    return DateCandidate(
        raw=m.group(1),
        kind=KIND_AMBIGUOUS,
        method="filename_year",
        source=base[-80:],
    )


# ─── Text-head extraction (conservative) ────────────────────────────────────

_COPYRIGHT_RE = re.compile(
    r"Copyright\s*(?:©|\(c\))?\s*(1[89]\d\d|20\d\d)", re.IGNORECASE
)
_STANDALONE_DATE_LINE_RE = re.compile(
    r"^(?:"
    r"[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+(?:1[89]\d\d|20\d\d)"
    r"|\d{1,2}\s+[A-Za-z]{3,9}\.?\s+(?:1[89]\d\d|20\d\d)"
    r"|(?:1[89]\d\d|20\d\d)-\d\d-\d\d"
    r")$"
)
_MD_HEAD_LINE_RE = re.compile(r"^#{1,6}\s+(.+)$")
_SOURCE_LINE_RE = re.compile(r"^\*\*Source:\*\*\s*`?([^`\n]+)`?\s*$")

DEFAULT_HEAD_CHARS = 600


def extract_text_head_biblio(head_text: str) -> dict:
    """Deterministic bibliographic signals from a document's first chars.

    Recognizes ONLY:
      * a first markdown heading that is itself a citation-style filename
        (scraped-book exports put the original filename there);
      * a ``**Source:** path`` metadata line (same exports);
      * ``Copyright © YYYY`` (first occurrence);
      * a standalone date line (`Dec 24, 2025` / `2025-12-24`) near the top
        (Substack-style article exports).

    Returns {author?, title?, candidates: [DateCandidate...]}.
    """
    out: dict = {"candidates": []}
    if not head_text:
        return out
    head = head_text[: DEFAULT_HEAD_CHARS * 2]

    for line in head.splitlines()[:20]:
        line = line.strip()
        if not line:
            continue
        hm = _MD_HEAD_LINE_RE.match(line)
        sm = _SOURCE_LINE_RE.match(line)
        name = hm.group(1) if hm else (sm.group(1) if sm else None)
        if name:
            cite = parse_citation_name(name)
            if cite.get("year_raw"):
                out["candidates"].append(
                    DateCandidate(
                        raw=cite["year_raw"],
                        kind=KIND_AMBIGUOUS,
                        method="citation_pattern",
                        source=("text_head_heading:" if hm else "text_head_source_line:")
                        + name.strip()[:150],
                    )
                )
            if cite.get("author") and not out.get("author"):
                out["author"] = cite["author"]
            if cite.get("title") and not out.get("title"):
                out["title"] = cite["title"]
            continue
        if _STANDALONE_DATE_LINE_RE.match(line):
            out["candidates"].append(
                DateCandidate(
                    raw=line,
                    kind=KIND_AMBIGUOUS,
                    method="text_head_date_line",
                    source=line[:80],
                )
            )

    cm = _COPYRIGHT_RE.search(head)
    if cm:
        out["candidates"].append(
            DateCandidate(
                raw=cm.group(1),
                kind=KIND_AMBIGUOUS,
                method="text_head_copyright",
                source=cm.group(0)[:80],
            )
        )
    return out


# ─── Language normalization (explicit metadata only — no detection) ─────────

def normalize_language(raw: Any) -> Optional[str]:
    if not raw:
        return None
    s = str(raw).strip().lower()
    if not s or len(s) > 32:
        return None
    # keep BCP47-ish tags and plain names as-is, lowercased
    if not re.match(r"^[a-z][a-z0-9 _\-]{0,31}$", s):
        return None
    return s[:16]


# ─── Provenance + document-record promotion ─────────────────────────────────

def build_provenance(
    *,
    method: Optional[str],
    source: Optional[str],
    precision: Optional[str] = None,
    reason: Optional[str] = None,
    origin: str = "ingest",
    fields: Optional[list[str]] = None,
    captured_at: Optional[str] = None,
) -> dict:
    prov = {
        "method": method or "none",
        "source": (source or "")[:200] or None,
        # Callers that may replay/finalize the same artifact pass the original
        # timestamp back in.  This keeps the persistence payload byte-stable
        # across idempotent retries while retaining a real first-capture time.
        "captured_at": captured_at or datetime.now(timezone.utc).isoformat(),
        "origin": origin,
    }
    if precision:
        prov["precision"] = precision
    if reason:
        prov["reason"] = reason
    if fields:
        prov["fields"] = fields
    return prov


BIBLIO_DOC_FIELDS = (
    "author",
    "title",
    "language",
    "document_date",
    "source_published_at",
    "date_confidence",
    "bibliographic_provenance",
)

DATE_IDENTITY_FIELDS = (
    "document_date",
    "source_published_at",
    "date_confidence",
)


def _present(value: Any) -> bool:
    return value is not None and value != ""


def _date_family_score(doc: dict) -> tuple[int, int, int, int]:
    """Comparable quality score for one *whole* date-identity family.

    A populated, confidence-bearing publication date always beats an empty
    family or a legacy date with no provenance.  At equal confidence, explicit
    ingest metadata wins over a deterministic backfill inference.  Completeness
    is only a tie-breaker; it never lets a lower-confidence date win.
    """

    if not _present(doc.get("document_date")):
        return (0, 0, 0, 0)
    confidence = str(doc.get("date_confidence") or "").lower()
    confidence_rank = _CONFIDENCE_RANK.get(confidence, 0)
    provenance = doc.get("bibliographic_provenance") or {}
    method = str(provenance.get("method") or "")
    if not confidence_rank:
        confidence_rank = _CONFIDENCE_RANK.get(
            str(CONFIDENCE_BY_METHOD.get(method) or ""), 0
        )
    origin_rank = 1 if provenance.get("origin") == "ingest" else 0
    completeness = sum(_present(doc.get(field)) for field in DATE_IDENTITY_FIELDS)
    return (1, confidence_rank, origin_rank, completeness)


def _copy_date_family(target: dict, source: dict) -> None:
    """Replace target's date family atomically from source (including absence)."""

    for field_name in DATE_IDENTITY_FIELDS:
        if field_name in source and _present(source.get(field_name)):
            target[field_name] = source[field_name]
        else:
            target.pop(field_name, None)
    provenance = source.get("bibliographic_provenance")
    if isinstance(provenance, dict) and provenance:
        target["bibliographic_provenance"] = dict(provenance)
    else:
        target.pop("bibliographic_provenance", None)


def merge_persisted_bibliographic(
    incoming: dict,
    existing: Optional[dict],
) -> dict:
    """Merge persisted bibliographic identity without splitting date families.

    This is the storage-boundary helper for ``mongo_writer``.  ``incoming`` is
    the newly assembled replacement document and ``existing`` is the current
    durable Mongo row.  Missing scalar fields are retained from durable state;
    the date identity + its provenance are selected as one indivisible family
    by confidence/provenance quality.  The returned dict is a copy.

    Equal-quality conflicts prefer the incoming family, except that the same
    date prefers the more complete family.  Thus a fresh explicit frontmatter
    capture can supersede an inferred backfill, while a replay containing no
    bibliographic fields cannot erase prior enrichment.
    """

    merged = dict(incoming or {})
    durable = existing if isinstance(existing, dict) else {}
    for field_name in ("author", "title", "language"):
        if not _present(merged.get(field_name)) and _present(durable.get(field_name)):
            merged[field_name] = durable[field_name]

    incoming_score = _date_family_score(merged)
    durable_score = _date_family_score(durable)
    choose_durable = durable_score > incoming_score
    if durable_score == incoming_score and durable_score[0]:
        same_date = merged.get("document_date") == durable.get("document_date")
        if same_date:
            incoming_complete = sum(
                _present(merged.get(field)) for field in DATE_IDENTITY_FIELDS
            )
            durable_complete = sum(
                _present(durable.get(field)) for field in DATE_IDENTITY_FIELDS
            )
            choose_durable = durable_complete > incoming_complete
    if choose_durable:
        _copy_date_family(merged, durable)
    elif incoming_score[0]:
        # Normalize the selected family without borrowing any field from the
        # losing family. source_published_at is the same publication identity.
        selected = dict(merged)
        if not _present(selected.get("source_published_at")):
            selected["source_published_at"] = selected.get("document_date")
        _copy_date_family(merged, selected)
    elif isinstance(durable.get("bibliographic_provenance"), dict) \
            and not isinstance(merged.get("bibliographic_provenance"), dict):
        # Both date families are empty; retain the durable honest-null reason.
        merged["bibliographic_provenance"] = dict(
            durable["bibliographic_provenance"]
        )
    return merged


def promote_bibliographic(doc: dict) -> dict:
    """Promote the parse-time bibliographic block into top-level doc fields.

    The ingest worker transports parse metadata via a fixed key tuple plus
    ``routing_trace``; the bibliographic block rides
    ``routing_trace["bibliographic"]`` so the capture hook needs no worker
    seam change. Called at the storage boundary
    (``mongo_writer.upsert_document``). Non-clobbering: an existing non-null
    top-level value always wins. Returns the same dict (mutated).
    """
    trace = doc.get("routing_trace")
    if not isinstance(trace, dict):
        return doc
    block = trace.get("bibliographic")
    if not isinstance(block, dict):
        return doc
    # Keep routing_trace a pure routing report.  Treat the top-level values as
    # the incoming family and the parse block as an alternate durable-quality
    # candidate; the shared merge helper prevents cross-family date mixing.
    doc["routing_trace"] = {k: v for k, v in trace.items() if k != "bibliographic"}
    promoted = merge_persisted_bibliographic(doc, block)
    doc.clear()
    doc.update(promoted)
    return doc
