"""
Tier chunker — hierarchical parent/child splitting.

Phase 7.6 — parent-splitting now consumes a `DoclingParseResult` from the
docling sidecar. Sections come pre-walked; we only do the token-budget
splitting + child generation.

Tier A / B / B+:  parent = one DoclingParseResult.section per heading,
                  re-split on token budget when a section >1.5x target.
                  (B+ already had inject_synthetic_headers run BEFORE
                  docling parsed, so its sections look identical to A.)
Tier C:           parent = token budget over the markdown fallback,
                  child = sentence groups inside each parent.
OCR AST (PDF):    parent = consecutive page groups sized by token budget,
                  child = sentence groups inside each parent; page ranges are
                  preserved as metadata.
"""
import logging
import re
from dataclasses import dataclass, field

import tiktoken

logger = logging.getLogger(__name__)

from models.schemas import SourceTier
from services.ingestion.b_plus_normalizer import InjectedHeader  # re-exported for worker
from services.ingestion.section_classifier import ChunkKind, classify_chunk
from services.ingestion import code_splitter
from services.text_quality import is_separator_only_text


def _embedder_safe_max_tokens() -> int:
    """Pull the embedder-safety cap from Settings at call time (don't cache
    so tests can override via `monkeypatch.setattr(...)`). Fallback 960
    matches the Settings default so misconfig still keeps code under a 1024
    tokenizer ceiling."""
    try:
        from config import settings as _settings
        return int(getattr(_settings, "EMBEDDER_SAFE_MAX_TOKENS", 960))
    except Exception:
        return 960

_TOKENIZER = tiktoken.get_encoding("cl100k_base")
PARENT_TARGET_TOKENS = 1200
# 128-token children (was 350): higher vector-retrieval precision on
# cross-domain corpora — recall is preserved by small-to-big (the retriever
# dedupes by parent_id and hydrates parent text) — and it's the band the
# local GLiNER/GLiREL extraction stack was validated on. Mirrors the
# ChildTokenBudget defaults in models/_schemas_legacy.py; corpus config wins
# when present.
CHILD_TARGET_TOKENS = 128
# semantic_split coalesce floor. The normal child_min (64) would re-merge
# idea-separated short paragraphs straight back into one chunk, undoing the
# split. For semantic_split we only absorb true FRAGMENTS (a stray heading or
# one-line sentence) below this floor, so single-idea paragraphs of ~30-60 tok
# survive as their own retrieval units.
_SEMANTIC_FRAGMENT_FLOOR = 24
# Phase K — adaptive parent sizing. Heading-aware tiers (A/B/B+) sometimes
# produce many small parents when docs have dense subheadings. The coalesce
# pass merges consecutive below-MIN sections up to MAX to keep parents near
# TARGET without losing structure-awareness.
MIN_PARENT_TOKENS = 400
MAX_PARENT_TOKENS = int(PARENT_TARGET_TOKENS * 1.5)  # = 1800


@dataclass(frozen=True)
class ChunkingPolicy:
    parent_min_tokens: int = 500
    parent_target_tokens: int = PARENT_TARGET_TOKENS
    parent_max_tokens: int = 2000
    child_min_tokens: int = 64
    child_target_tokens: int = CHILD_TARGET_TOKENS
    child_max_tokens: int = 256
    parent_overlap_tokens: int = 200
    requested_child_strategy: str = "sentence_merge"
    resolved_child_strategy: str = "sentence_merge"


@dataclass
class ChildChunk:
    chunk_id: str
    parent_id: str
    doc_id: str
    corpus_id: str
    text: str
    heading_path: list[str] | None
    source_tier: str
    token_count: int
    page_start: int | None = None
    page_end: int | None = None
    # Semantic role within the document (body / toc / bibliography / index /
    # appendix / front_matter / back_matter / code). Inherited from the parent's
    # heading classification. Defaults to BODY so legacy code paths and
    # rehydrated data without this field behave identically to a normal chunk.
    chunk_kind: str = ChunkKind.BODY
    # Code lane: language tag (e.g. "python", "luau") and AST-derived metadata
    # (symbols_defined / symbols_called / imports / ast_signature / file_path).
    # Both default to None/{} so prose chunks and legacy data are unaffected.
    language: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class ParentChunk:
    parent_id: str
    doc_id: str
    corpus_id: str
    text: str
    heading_path: list[str] | None
    source_tier: str
    children: list[ChildChunk] = field(default_factory=list)
    page_start: int | None = None
    page_end: int | None = None
    chunk_kind: str = ChunkKind.BODY
    language: str | None = None
    metadata: dict = field(default_factory=dict)


def _count_tokens(text: str) -> int:
    return len(_TOKENIZER.encode(text, disallowed_special=()))


def _budget_value(budget, field_name: str, default: int) -> int:
    if budget is None:
        return default
    if isinstance(budget, dict):
        raw = budget.get(field_name, default)
    else:
        raw = getattr(budget, field_name, default)
    try:
        return int(raw)
    except Exception:
        return default


def _build_policy(config=None) -> ChunkingPolicy:
    """Resolve the corpus settings into the per-file auto chunking policy.

    `child_chunk_algorithm` now drives the splitter:
    - "semantic_split" (default for NEW corpora): one child per paragraph/idea
      via `_split_by_paragraph_idea` — finer, single-idea retrieval units.
    - "sentence_merge": legacy paragraph-packing via `_split_at_boundary`.
    Existing corpora keep their FROZEN config, so old data is grandfathered.
    """
    parent_budget = getattr(config, "parent_chunk_tokens", None)
    child_budget = getattr(config, "child_chunk_tokens", None)
    parent_target = max(200, _budget_value(parent_budget, "target_tokens", PARENT_TARGET_TOKENS))
    parent_min = max(100, min(_budget_value(parent_budget, "min_tokens", 500), parent_target))
    parent_max = max(parent_target, _budget_value(parent_budget, "max_tokens", 2000))
    child_target = max(100, _budget_value(child_budget, "target_tokens", CHILD_TARGET_TOKENS))
    child_min = max(50, min(_budget_value(child_budget, "min_tokens", 128), child_target))
    child_max = max(child_target, _budget_value(child_budget, "max_tokens", 512))
    requested = str(getattr(config, "child_chunk_algorithm", "semantic_split") or "semantic_split")
    resolved = "semantic_split" if requested == "semantic_split" else "sentence_merge"
    raw_overlap = getattr(config, "chunk_overlap", 200)
    try:
        overlap = int(raw_overlap)
    except Exception:
        overlap = 200
    overlap = max(0, min(overlap, parent_target // 2))
    return ChunkingPolicy(
        parent_min_tokens=parent_min,
        parent_target_tokens=parent_target,
        parent_max_tokens=parent_max,
        child_min_tokens=child_min,
        child_target_tokens=child_target,
        child_max_tokens=child_max,
        parent_overlap_tokens=overlap,
        requested_child_strategy=requested,
        resolved_child_strategy=resolved,
    )


# Markup-noise patterns. Run BEFORE chunking so the cleaned text drives both
# token budgeting and embedding noise reduction. Each pattern strips markup
# but preserves readable text — pandoc div fences, EPUB pagebreak anchors,
# image markdown, and ornamental HTML tags become embedding noise otherwise
# (cover images, page numbers, figure scaffolding). Ordered: most specific
# patterns first so they don't get consumed by broader cleanups.
_MARKUP_NOISE_PATTERNS: tuple[tuple["re.Pattern[str]", str], ...] = (
    # Pandoc fenced div open: `::: {.section …}` (eats the trailing whitespace
    # so the fence doesn't leave a blank line behind).
    (re.compile(r":::\s*\{[^\n}]*\}\s*"), ""),
    # Pandoc fenced div bare open: `::: Para` / `::: section`.
    (re.compile(r"^\s*:::\s+[A-Za-z][\w .:\-]*\s*$", re.MULTILINE), ""),
    # Pandoc fenced div close: a line containing only `:::` (one or more).
    (re.compile(r"^\s*:::+\s*$", re.MULTILINE), ""),
    # Pandoc bracketed anchors / pagebreak markers:
    #   []{#anchor .class aria-label="…" role="…"}
    (re.compile(r"\[\]\{[^\n}]*\}"), ""),
    # Pandoc inline spans: `[visible text]{.class}` — keep the visible text.
    (re.compile(r"\[([^\]\n]+)\]\{[^\n}]*\}"), r"\1"),
    # Heading / section anchors: `# Title {#anchor}` or `# {#anchor}`.
    (re.compile(r"\s*\{#[^\n}]*\}"), ""),
    # Image markdown — drop entirely. Includes the alt-text, which is rarely
    # useful for retrieval (usually a filename or short caption).
    (re.compile(r"!\[[^\]]*\]\([^)]+\)"), ""),
    # HTML self-closing / void scaffolding tags. <img>, <br>, <hr>, <svg>.
    (re.compile(r"<(?:img|br|hr|svg|path|g|polygon|line|circle|rect)\b[^>]*/?>", re.IGNORECASE), ""),
    # Open/close pairs we strip without preserving inner text — those tags
    # carry layout, not content (figure/figcaption are usually image captions).
    (re.compile(r"</?(?:figure|figcaption|aside|nav|svg|style|script)(?:\s[^>]*)?>", re.IGNORECASE), ""),
    # Span / anchor tags that wrap text — strip the tag, keep the inner content.
    (re.compile(r"<(?:span|a)\b[^>]*>"), ""),
    (re.compile(r"</(?:span|a)>"), ""),
)

_PATHOLOGICAL_LINE_CHARS = 5_000
_PATHOLOGICAL_LINE_SLICE_CHARS = 2_000


def _split_pathological_line(line: str) -> list[str]:
    if len(line) <= _PATHOLOGICAL_LINE_CHARS:
        return [line]

    pieces: list[str] = []
    remaining = line.rstrip()
    while len(remaining) > _PATHOLOGICAL_LINE_SLICE_CHARS:
        window = remaining[:_PATHOLOGICAL_LINE_SLICE_CHARS]
        cut = max(
            window.rfind(" "),
            window.rfind("\t"),
            window.rfind("|"),
            window.rfind(","),
            window.rfind(";"),
        )
        if cut < (_PATHOLOGICAL_LINE_SLICE_CHARS // 2):
            cut = _PATHOLOGICAL_LINE_SLICE_CHARS
        else:
            cut += 1
        piece = remaining[:cut].strip()
        if piece:
            pieces.append(piece)
        remaining = remaining[cut:].lstrip()

    tail = remaining.strip()
    if tail:
        pieces.append(tail)
    return pieces or [line]


def _split_pathological_lines(text: str) -> str:
    """Break ebook-conversion mega-lines before token boundary splitting.

    Calibre/Pandoc Markdown can contain 10k+ character single lines for layout
    tables. Treat those as paragraph-sized slices so the boundary splitter and
    tokenizer do not spend minutes on one unbroken string.
    """
    if not text or len(text) <= _PATHOLOGICAL_LINE_CHARS:
        return text

    out: list[str] = []
    changed = False
    for line in text.splitlines():
        if len(line) <= _PATHOLOGICAL_LINE_CHARS:
            out.append(line)
            continue
        changed = True
        parts = _split_pathological_line(line)
        for idx, part in enumerate(parts):
            if idx:
                out.append("")
            out.append(part)
    return "\n".join(out) if changed else text


def _scrub_markup_noise(text: str) -> str:
    """Strip HTML/EPUB scaffolding before chunking.

    Preserves readable text; removes pandoc divs, bracketed anchors, image
    markdown, EPUB pagebreak markers, and ornamental tags that would
    otherwise become embedding noise (e.g. the cover-image XHTML soup that
    used to land in chunk_0000 of every EPUB-derived markdown). Idempotent.
    Collapses runs of 3+ blank lines so paragraph-boundary chunking still
    works on the post-scrub text.
    """
    if not text:
        return text
    cleaned = text
    for pat, repl in _MARKUP_NOISE_PATTERNS:
        cleaned = pat.sub(repl, cleaned)
    cleaned = _split_pathological_lines(cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _clean_heading_segment(text: str) -> str:
    return _scrub_markup_noise(text or "")


def _clean_heading_path(path: list[str] | None) -> list[str] | None:
    if not path:
        return path
    cleaned = [_clean_heading_segment(str(part)).strip() for part in path]
    return [part for part in cleaned if part]


def _hard_split_oversize(chunks: list[str], max_tokens: int) -> list[str]:
    """Last-line-of-defense split for chunks that exceed `max_tokens`.

    `_split_at_boundary` prefers paragraph / sentence boundaries, but
    pathological inputs (long unbroken code blocks, single-line walls of
    text, large tables stringified) can still produce oversized chunks.
    Without this guard those chunks silently truncate at the embedder's
    1024-token ceiling, dropping the tail content.

    The split works at token boundaries via the cl100k_base tokenizer, but
    prefers a WHITESPACE-LEADING token near the cap (router 2: never cut
    mid-word when a word boundary exists in the back half of the window —
    a mid-word cut corrupts both halves' embeddings). Texts with no
    whitespace tokens (CJK, minified blobs) still cut at the exact cap.
    """
    if max_tokens <= 0:
        return chunks
    out: list[str] = []
    over_count = 0
    for c in chunks:
        toks = _TOKENIZER.encode(c, disallowed_special=())
        if len(toks) <= max_tokens:
            out.append(c)
            continue
        over_count += 1
        i = 0
        n = len(toks)
        while i < n:
            end = min(i + max_tokens, n)
            if end < n:
                # Backtrack to the nearest token that STARTS a new word
                # (leading space/newline/tab) so the cut lands on a word
                # boundary. Only scan the back half — never shrink a chunk
                # below half the cap chasing a boundary.
                j = end
                floor = i + max(1, (end - i) // 2)
                while j > floor:
                    piece = _TOKENIZER.decode([toks[j]])
                    if piece[:1] in (" ", "\n", "\t"):
                        break
                    j -= 1
                if j > floor:
                    end = j
            sub = _TOKENIZER.decode(toks[i:end]).strip()
            if sub:
                out.append(sub)
            i = end
    if over_count:
        logger.info(
            "tier_chunker hard-split: %d/%d chunks force-broken at max_tokens=%d",
            over_count, len(chunks), max_tokens,
        )
    return out


def _coalesce_small_child_texts(
    texts: list[str],
    *,
    child_min_tokens: int,
    child_max_tokens: int,
) -> list[str]:
    """Merge tiny child texts without violating the child max-token contract."""
    if child_min_tokens <= 0 or len(texts) <= 1:
        return texts
    out: list[str] = []
    i = 0
    while i < len(texts):
        current = texts[i].strip()
        if not current:
            i += 1
            continue
        if _count_tokens(current) >= child_min_tokens:
            out.append(current)
            i += 1
            continue

        if out:
            prev_combined = f"{out[-1]}\n\n{current}"
            if _count_tokens(prev_combined) <= child_max_tokens:
                out[-1] = prev_combined
                i += 1
                continue

        if i + 1 < len(texts):
            next_text = texts[i + 1].strip()
            next_combined = f"{current}\n\n{next_text}" if next_text else current
            if next_text and _count_tokens(next_combined) <= child_max_tokens:
                out.append(next_combined)
                i += 2
                continue

        out.append(current)
        i += 1
    return out


# ── Structured-text routers (POLYMATH_ARCHITECTURE §3.S2, routers 1+2) ──────
# Deterministic layer-2 routing for text shapes the paragraph splitter shreds:
# list blocks split at ITEM boundaries (items never broken), low-punctuation
# multi-line blocks group by LINES, and sentence splitting can use the SaT
# model (wtpsplit, punctuation-agnostic) instead of the [.!?] regex. All rules;
# the only model (SaT) is deterministic for a fixed model+text and gated by
# CHUNKER_SENTENCE_ENGINE with a logged regex fallback.

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_LIST_MARKER_RE = re.compile(
    r"^\s{0,8}(?:[-*+•▪◦‣]|\d{1,3}[.)]|\(\d{1,3}\)|[a-zA-Z][.)])\s+"
)
_SENT_FINAL_RE = re.compile(r"[.!?][\"')\]]?(?:\s|$)")


def _routers_enabled() -> bool:
    try:
        from config import get_settings

        return bool(getattr(get_settings(), "CHUNKER_STRUCTURED_ROUTERS", True))
    except Exception:  # config unavailable in some tooling contexts
        return True


def _nonempty_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip()]


def _is_list_block(text: str) -> bool:
    """A block is list-shaped when >=3 lines carry list markers and markers
    cover at least half the non-empty lines (bullets, 1./1)/(1), a./a))."""
    lines = _nonempty_lines(text)
    if len(lines) < 3:
        return False
    markers = sum(1 for ln in lines if _LIST_MARKER_RE.match(ln))
    return markers >= 3 and markers * 2 >= len(lines)


def _split_list_items(text: str) -> list[str]:
    """One unit per list item: a marker line plus its continuation lines.
    Preamble lines before the first marker become their own unit."""
    items: list[str] = []
    current: list[str] = []
    for ln in text.splitlines():
        if not ln.strip():
            continue
        if _LIST_MARKER_RE.match(ln):
            if current:
                items.append("\n".join(current))
            current = [ln]
        else:
            current.append(ln)
    if current:
        items.append("\n".join(current))
    return items


def _is_low_punct_multiline(text: str) -> bool:
    """Line-structured text (transcripts, poetry, chat logs, logs): many lines,
    few sentence-final punctuation marks. Sentence splitting has nothing to
    grip there — lines are the real units."""
    lines = _nonempty_lines(text)
    if len(lines) < 5:
        return False
    punct = len(_SENT_FINAL_RE.findall(text))
    return punct * 3 < len(lines)


def _pack_units(
    units: list[str], target_tokens: int, max_tokens: int, *, joiner: str = "\n"
) -> list[str]:
    """Greedily pack whole units (list items / lines / sentences) to
    ~target_tokens without ever splitting inside a unit. A single unit above
    max_tokens falls to sentence packing; a unit with no sentence boundaries
    stays whole for the downstream hard splitter."""
    out: list[str] = []
    buf: list[str] = []
    buf_tok = 0
    for unit in units:
        ut = _count_tokens(unit)
        if ut > max_tokens:
            if buf:
                out.append(joiner.join(buf))
                buf, buf_tok = [], 0
            sentences = _split_at_sentences(unit)
            if len(sentences) <= 1:
                out.append(unit)  # pathological — _hard_split_oversize catches it
            else:
                out.extend(
                    _pack_units(sentences, target_tokens, max_tokens, joiner=" ")
                )
            continue
        if buf and buf_tok + ut > target_tokens:
            out.append(joiner.join(buf))
            buf, buf_tok = [unit], ut
        else:
            buf.append(unit)
            buf_tok += ut
    if buf:
        out.append(joiner.join(buf))
    return [c for c in out if c.strip()]


def _route_structured_block(
    para: str, target_tokens: int, max_tokens: int
) -> list[str] | None:
    """Router 1+2a: oversize blocks that are list-shaped split at item
    boundaries; line-structured low-punctuation blocks group by lines.
    Returns None when the block is ordinary prose (caller sentence-splits)."""
    if not _routers_enabled():
        return None
    if _is_list_block(para):
        return _pack_units(_split_list_items(para), target_tokens, max_tokens)
    if _is_low_punct_multiline(para):
        return _pack_units(_nonempty_lines(para), target_tokens, max_tokens)
    return None


# ── Sentence engine (router 2b): SaT (wtpsplit) with regex fallback ─────────
_SAT_MODEL = None
_SAT_FAILED = False


def _sat_split(text: str) -> list[str] | None:
    """Punctuation-agnostic sentence segmentation via SaT (sat-3l-sm).
    Returns None when disabled/unavailable — caller uses the regex. The model
    is a lazy module singleton; a load failure is logged ONCE and latches."""
    global _SAT_MODEL, _SAT_FAILED
    if _SAT_FAILED:
        return None
    try:
        from config import get_settings

        engine = str(
            getattr(get_settings(), "CHUNKER_SENTENCE_ENGINE", "sat") or "sat"
        ).lower()
    except Exception:
        engine = "sat"
    if engine != "sat":
        return None
    if _SAT_MODEL is None:
        try:
            try:
                from wtpsplit_lite import SaT  # minimal ONNX build
            except ImportError:
                from wtpsplit import SaT  # full package fallback
            _SAT_MODEL = SaT("sat-3l-sm")
            logger.info("tier_chunker sentence engine: SaT sat-3l-sm loaded")
        except Exception as exc:
            _SAT_FAILED = True
            logger.warning(
                "SaT sentence engine unavailable (%s) — regex fallback in effect",
                exc,
            )
            return None
    try:
        return [s.strip() for s in _SAT_MODEL.split(text) if s.strip()]
    except Exception as exc:  # never let the model kill ingestion
        logger.warning("SaT split failed (%s) — regex fallback for this block", exc)
        return None


def _split_at_sentences(text: str) -> list[str]:
    sats = _sat_split(text)
    if sats and len(sats) > 1:
        return sats
    return [s.strip() for s in _SENTENCE_RE.split(text) if s.strip()]


# ── Router 5: semantic-deviation escalation (topic-fused paragraphs) ────────
# The TechViz method: embed consecutive sentences, place chunk boundaries at
# similarity-deviation dips so an oversize multi-topic paragraph splits at
# TOPIC shifts instead of arbitrary token counts. Applied ONLY to flagged
# pathological blocks (oversize paragraph, >= _ESCALATION_MIN_SENTENCES
# sentences), batched into ONE embedder call, deterministic for a fixed
# model+text, and every failure falls back to greedy sentence packing.
_ESCALATION_MIN_SENTENCES = 8
_ESCALATION_FAILED = False


def _escalation_enabled() -> bool:
    try:
        from config import get_settings

        return bool(getattr(get_settings(), "CHUNKER_SEMANTIC_ESCALATION", True))
    except Exception:
        return False


def _embed_for_escalation(sentences: list[str]) -> list[list[float]] | None:
    """One batched call to the OpenAI-compatible local embedder sidecar.
    Latches off after the first failure (bulk ingests must not retry a dead
    sidecar per paragraph)."""
    global _ESCALATION_FAILED
    if _ESCALATION_FAILED:
        return None
    try:
        import httpx
        from config import get_settings

        s = get_settings()
        url = str(getattr(s, "EMBEDDER_URL", "") or "").rstrip("/")
        if not url:
            _ESCALATION_FAILED = True
            return None
        if not url.endswith("/embeddings"):
            url = url + "/embeddings"
        resp = httpx.post(
            url,
            json={
                "input": sentences,
                "model": str(getattr(s, "EMBEDDER_MODEL_NAME", "") or "local"),
            },
            timeout=8.0,
        )
        resp.raise_for_status()
        data = resp.json().get("data") or []
        vecs = [row.get("embedding") for row in data]
        if len(vecs) != len(sentences) or any(not v for v in vecs):
            raise ValueError("embedding count/shape mismatch")
        return vecs
    except Exception as exc:  # noqa: BLE001
        _ESCALATION_FAILED = True
        logger.warning(
            "semantic escalation embedder unavailable (%s) — greedy packing in effect",
            exc,
        )
        return None


def _semantic_deviation_split(
    sentences: list[str], target_tokens: int, max_tokens: int
) -> list[str] | None:
    """Boundaries where consecutive-sentence cosine dips below mean − std,
    with a 3-sentence minimum segment. Returns None on any failure."""
    vecs = _embed_for_escalation(sentences)
    if vecs is None:
        return None
    import math

    def _cos(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)) or 1e-9
        nb = math.sqrt(sum(x * x for x in b)) or 1e-9
        return dot / (na * nb)

    sims = [_cos(vecs[i], vecs[i + 1]) for i in range(len(vecs) - 1)]
    if not sims:
        return None
    mean = sum(sims) / len(sims)
    var = sum((x - mean) ** 2 for x in sims) / len(sims)
    threshold = mean - math.sqrt(var)

    segments: list[list[str]] = [[sentences[0]]]
    since_boundary = 1
    for i, sim in enumerate(sims):
        if sim < threshold and since_boundary >= 3:
            segments.append([sentences[i + 1]])
            since_boundary = 1
        else:
            segments[-1].append(sentences[i + 1])
            since_boundary += 1
    if len(segments) <= 1:
        return None  # no topical structure found — greedy packing is fine
    # One chunk PER topic segment (idea-per-chunk, like semantic_split's
    # paragraphs) — never re-pack segments together; only split a segment that
    # alone exceeds the cap. Tiny segments coalesce downstream at the floor.
    out: list[str] = []
    for seg in segments:
        seg_text = " ".join(seg)
        if _count_tokens(seg_text) <= max_tokens:
            out.append(seg_text)
        else:
            out.extend(_pack_units(seg, target_tokens, max_tokens, joiner=" "))
    return out


def _semantic_parent_blocks(
    text: str, *, min_tokens: int, target_tokens: int, max_tokens: int
) -> list[str] | None:
    """Semantic PARENT formation for structureless text (tier_c).

    Token-window parents on a structureless doc straddle topics — children
    retrieve precisely but hydrate to diluted parents. This draws parent
    boundaries at semantic-deviation dips between paragraph units instead of
    blind token cuts, budget-clamped to [min_tokens, max_tokens].

    DETERMINISTIC by construction: fixed embedder model + same text → same
    vectors → same cosine sequence → same mean−std threshold → same
    boundaries; pure arithmetic, no RNG, no dict-order dependence. The only
    environment dependence is embedder availability — a failure latches OFF
    (logged) and the caller falls back to the legacy token-window split.
    Returns None whenever there is nothing to gain (small doc, <4 units,
    embedder down, no topical structure) — caller keeps legacy behaviour.
    """
    try:
        from config import get_settings

        if not bool(getattr(get_settings(), "CHUNKER_SEMANTIC_PARENTS", True)):
            return None
    except Exception:
        return None
    if _count_tokens(text) <= max_tokens:
        return None  # fits one parent — nothing to segment
    units = [p.strip() for p in re.split(r"\n\n+", text) if p.strip()]
    if len(units) < 4:
        units = _split_at_sentences(text)  # giant single paragraph → SaT units
    if len(units) < 4:
        return None
    vecs = _embed_for_escalation(units)
    if vecs is None:
        return None
    import math

    def _cos(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)) or 1e-9
        nb = math.sqrt(sum(x * x for x in b)) or 1e-9
        return dot / (na * nb)

    sims = [_cos(vecs[i], vecs[i + 1]) for i in range(len(vecs) - 1)]
    mean = sum(sims) / len(sims)
    std = math.sqrt(sum((x - mean) ** 2 for x in sims) / len(sims))
    threshold = mean - std
    # boundary BEFORE unit i+1 when the dip crosses the threshold
    boundaries = {i + 1 for i, sim in enumerate(sims) if sim < threshold}

    parents: list[str] = []
    buf: list[str] = []
    buf_tok = 0
    for i, unit in enumerate(units):
        ut = _count_tokens(unit)
        if buf and (
            buf_tok + ut > max_tokens
            or (i in boundaries and buf_tok >= min_tokens)
        ):
            parents.append("\n\n".join(buf))
            buf, buf_tok = [], 0
        buf.append(unit)
        buf_tok += ut
    if buf:
        tail = "\n\n".join(buf)
        if (
            parents
            and buf_tok < min_tokens
            and _count_tokens(parents[-1]) + buf_tok <= max_tokens
        ):
            parents[-1] = parents[-1] + "\n\n" + tail  # merge small tail back
        else:
            parents.append(tail)
    if len(parents) <= 1:
        return None
    logger.info(
        "semantic parent formation: %d units → %d topic-aligned parents", len(units), len(parents)
    )
    return parents


def _tail_token_text(text: str, overlap_tokens: int) -> str:
    if overlap_tokens <= 0:
        return ""
    tokens = _TOKENIZER.encode(text, disallowed_special=())
    if not tokens:
        return ""
    return _TOKENIZER.decode(tokens[-overlap_tokens:]).strip()


def _apply_overlap(chunks: list[str], overlap_tokens: int) -> list[str]:
    if overlap_tokens <= 0 or len(chunks) <= 1:
        return chunks
    overlapped = [chunks[0]]
    for previous, current in zip(chunks, chunks[1:]):
        tail = _tail_token_text(previous, overlap_tokens)
        overlapped.append(f"{tail}\n\n{current}" if tail else current)
    return overlapped


def _split_at_boundary(
    text: str, target_tokens: int, *, overlap_tokens: int = 0
) -> list[str]:
    """
    Split text into ~target_tokens chunks at paragraph boundaries.
    Falls back to sentence splitting when a paragraph alone exceeds 1.5x budget.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\n+", text) if p.strip()]
    if not paragraphs:
        return [text.strip()] if text.strip() else []

    chunks: list[str] = []
    buf: list[str] = []
    buf_tok = 0

    for para in paragraphs:
        para_tok = _count_tokens(para)

        if para_tok > target_tokens * 1.5:
            if buf:
                chunks.append("\n\n".join(buf))
                buf, buf_tok = [], 0
            # Router 1+2a — same structured routing as the semantic splitter,
            # so sentence_merge mode and parent formation keep list items and
            # line-structured text intact too.
            routed = _route_structured_block(
                para, target_tokens, max(int(target_tokens * 1.5), target_tokens + 1)
            )
            if routed is not None:
                chunks.extend(routed)
                continue
            sentences = _split_at_sentences(para)
            # Pathological-paragraph bailout. Code listings, inline math,
            # stringified tables, and other no-sentence-boundary content
            # produce _split_at_sentences() == [para] — the whole paragraph
            # is one "sentence". Without this guard we'd then re-tokenize
            # that mega-sentence inside the loop below for zero useful
            # work, AND hard_split_oversize would re-encode it AGAIN
            # downstream. Both passes are O(N) over the paragraph's tokens,
            # so on docs with thousands of these (e.g. PBR4) the chunker
            # CPU-stalls for many minutes before yielding to the worker.
            # Skip straight to handing the whole paragraph to the hard
            # splitter, which slices once at exact token boundaries.
            if len(sentences) <= 1:
                chunks.append(para)
                continue
            s_buf: list[str] = []
            s_tok = 0
            for s in sentences:
                st = _count_tokens(s)
                if s_tok + st > target_tokens and s_buf:
                    chunks.append(" ".join(s_buf))
                    s_buf, s_tok = [s], st
                else:
                    s_buf.append(s)
                    s_tok += st
            if s_buf:
                chunks.append(" ".join(s_buf))
            continue

        if buf_tok + para_tok > target_tokens and buf:
            chunks.append("\n\n".join(buf))
            buf, buf_tok = [para], para_tok
        else:
            buf.append(para)
            buf_tok += para_tok

    if buf:
        chunks.append("\n\n".join(buf))

    chunks = [c for c in chunks if c.strip()]
    return _apply_overlap(chunks, overlap_tokens)


def _split_by_paragraph_idea(
    text: str, target_tokens: int, max_tokens: int
) -> list[str]:
    """Semantic/proposition split: ONE chunk per paragraph (idea), instead of
    PACKING several paragraphs to fill the token budget the way
    ``_split_at_boundary`` does. A paragraph is usually a single idea, so this
    keeps each child focused on one thing → cleaner embedding, more precise
    retrieval (a 128-tok child no longer blends 2-3 unrelated paragraphs).

    Oversize paragraphs (> ``max_tokens``) split at sentence boundaries, greedy
    to ``target_tokens``. Tiny fragments are merged downstream by
    ``_coalesce_small_child_texts`` — which only touches sub-min pieces, so the
    paragraph (idea) separation survives. Children are variable-size by design.
    """

    paragraphs = [p.strip() for p in re.split(r"\n\n+", text) if p.strip()]
    if not paragraphs:
        return [text.strip()] if text.strip() else []
    out: list[str] = []
    for para in paragraphs:
        if _count_tokens(para) <= max_tokens:
            out.append(para)
            continue
        # Router 1+2a — oversize list blocks split at item boundaries and
        # line-structured blocks group by lines BEFORE sentence splitting
        # shreds them at arbitrary points (probes C/D).
        routed = _route_structured_block(para, target_tokens, max_tokens)
        if routed is not None:
            out.extend(routed)
            continue
        sentences = _split_at_sentences(para)
        if len(sentences) <= 1:
            out.append(para)  # pathological (code/table) — _hard_split handles it
            continue
        # Router 5 — topic-fused oversize paragraphs split at semantic
        # deviation boundaries instead of arbitrary token counts (probe B).
        if len(sentences) >= _ESCALATION_MIN_SENTENCES and _escalation_enabled():
            escalated = _semantic_deviation_split(sentences, target_tokens, max_tokens)
            if escalated is not None:
                out.extend(escalated)
                continue
        s_buf: list[str] = []
        s_tok = 0
        for s in sentences:
            st = _count_tokens(s)
            if s_tok + st > target_tokens and s_buf:
                out.append(" ".join(s_buf))
                s_buf, s_tok = [s], st
            else:
                s_buf.append(s)
                s_tok += st
        if s_buf:
            out.append(" ".join(s_buf))
    return [c for c in out if c.strip()]


_Block = tuple[list[str] | None, str, str, str | None, dict]
# 5-tuple: (heading_path, text, chunk_kind, language, metadata)


def _coalesce_small_blocks(
    blocks: list[_Block],
    *,
    min_parent_tokens: int,
    max_parent_tokens: int,
) -> list[_Block]:
    """Merge consecutive below-MIN BODY sections up to MAX_PARENT_TOKENS.

    Policy: if current block is BODY AND under MIN_PARENT_TOKENS AND combining
    with the next block (also BODY under the SAME heading_path) keeps us at or
    below MAX_PARENT_TOKENS, fold them together. Distinct heading paths are a
    structural boundary and must never be discarded merely to hit a parent
    size target. Otherwise, push current and advance.

    Code lane: NEVER merges across a kind boundary. A small BODY block
    immediately followed by a CODE block (or vice versa) stays separate so
    the code-aware splitter can route the CODE block to code_splitter.pack().

    Preserves structure-awareness for well-sized sections while smoothing out
    doc-specific pathologies (e.g. a 4K-token docx with 37 tiny heading
    sections).
    """
    if not blocks:
        return blocks
    out: list[_Block] = []
    cur_path, cur_text, cur_kind, cur_lang, cur_meta = blocks[0]
    merges = 0
    for next_path, next_text, next_kind, next_lang, next_meta in blocks[1:]:
        if (
            cur_kind == ChunkKind.BODY
            and next_kind == ChunkKind.BODY
            and cur_path == next_path
            and not cur_meta
            and not next_meta
        ):
            cur_tok = _count_tokens(cur_text)
            if cur_tok < min_parent_tokens:
                combined = cur_text + "\n\n" + next_text
                combined_tok = _count_tokens(combined)
                if combined_tok <= max_parent_tokens:
                    cur_text = combined
                    merges += 1
                    continue
        out.append((cur_path, cur_text, cur_kind, cur_lang, cur_meta))
        cur_path, cur_text, cur_kind, cur_lang, cur_meta = (
            next_path, next_text, next_kind, next_lang, next_meta,
        )
    out.append((cur_path, cur_text, cur_kind, cur_lang, cur_meta))
    if merges > 0:
        logger.info(
            "tier_chunker coalesce: %d/%d blocks merged (input=%d, output=%d)",
            merges, len(blocks), len(blocks), len(out),
        )
    return out


def _sections_to_parent_blocks(parse_sections) -> list[_Block]:
    """Fold the docling section walk into 5-tuple blocks.

    Returns list[(heading_path, text, chunk_kind, language, metadata)]:
      - section_heading starts a new BODY block (heading text becomes first line).
      - paragraph / list sections accumulate into the current BODY block.
      - transcript_block sections flush BODY and emit timestamped BODY blocks
        carrying provenance/time metadata.
      - table sections flush the BODY buffer and emit their own TABLE block.
      - code_block sections flush the BODY buffer and emit their own CODE block
        carrying the language tag. The chunker routes CODE blocks through
        code_splitter.pack() instead of the prose sentence/token splitters.
    """
    blocks: list[_Block] = []
    current_path: list[str] | None = None
    current_buf: list[str] = []

    def flush_body(*, drop_heading_only: bool = False):
        nonlocal current_buf
        if current_buf:
            if drop_heading_only and len(current_buf) == 1 and re.match(r"^#{1,6}\s+\S", current_buf[0].strip()):
                current_buf = []
                return
            text = "\n\n".join(current_buf).strip()
            if text:
                blocks.append((_clean_heading_path(current_path), text, ChunkKind.BODY, None, {}))
            current_buf = []

    for sec in parse_sections:
        if sec.element_type == "section_heading":
            flush_body()
            current_path = _clean_heading_path(list(sec.heading_path or [])) or []
            # Render the heading itself as the first line of the next BODY
            # block so downstream summarizers/embedders see the title.
            level = sec.level or 1
            current_buf = [f"{'#' * min(level, 6)} {sec.text}".strip()]
        elif sec.element_type == "code_block":
            # Code lane: emit the fenced block as its own CODE-kind block
            # under the active heading path. Don't fold into BODY.
            flush_body()
            code_path = _clean_heading_path(list(current_path) if current_path else list(sec.heading_path or []))
            blocks.append((code_path, sec.text, ChunkKind.CODE, sec.language, getattr(sec, "metadata", None) or {}))
        elif sec.element_type == "table":
            flush_body(drop_heading_only=True)
            table_path = _clean_heading_path(list(current_path) if current_path else list(sec.heading_path or []))
            blocks.append((table_path, sec.text, ChunkKind.TABLE, None, getattr(sec, "metadata", None) or {}))
        elif sec.element_type == "transcript_block":
            flush_body(drop_heading_only=True)
            transcript_path = _clean_heading_path(
                list(current_path) if current_path else list(sec.heading_path or [])
            )
            blocks.append((
                transcript_path,
                sec.text,
                ChunkKind.BODY,
                None,
                getattr(sec, "metadata", None) or {},
            ))
        else:
            # Paragraph / list chunk → accumulate into BODY buffer.
            if current_path is None and sec.heading_path:
                current_path = _clean_heading_path(list(sec.heading_path or [])) or []
            current_buf.append(sec.text)

    flush_body()
    return blocks


def _split_table_rows_for_children(
    table_text: str,
    metadata: dict | None,
    *,
    child_target_tokens: int,
    child_max_tokens: int,
) -> list[tuple[str, dict]]:
    """Split a linearized table by row groups, repeating table context.

    The local markdown parser emits:
      Table: ...
      Section: ...
      Caption: ...
      Columns: ...

      Row 1: ...

    For large tables, boundary splitting can cut through rows. This helper
    keeps rows intact where possible and repeats the header context so each
    child is independently meaningful to the embedder, reranker, and Ghost B.
    """
    meta = dict(metadata or {})
    normalized_table = _split_pathological_lines(table_text or "")
    lines = [line.rstrip() for line in normalized_table.splitlines()]
    first_row = next(
        (idx for idx, line in enumerate(lines) if re.match(r"^Row\s+\d+:", line)),
        None,
    )
    if first_row is None:
        return [(normalized_table.strip(), meta)] if normalized_table.strip() else []

    header_lines = [line for line in lines[:first_row] if line.strip()]
    row_lines = [line for line in lines[first_row:] if line.strip()]
    if not row_lines:
        return [(normalized_table.strip(), meta)] if normalized_table.strip() else []

    max_tokens = max(1, child_max_tokens)
    target_tokens = max(1, min(child_target_tokens, child_max_tokens))
    groups: list[tuple[str, dict]] = []
    buf: list[str] = []

    def row_number(row_line: str) -> int | None:
        match = re.match(r"^Row\s+(\d+):", row_line)
        return int(match.group(1)) if match else None

    def render(candidate_rows: list[str]) -> str:
        return "\n".join(header_lines + [""] + candidate_rows).strip()

    def flush() -> None:
        nonlocal buf
        if not buf:
            return
        group_meta = dict(meta)
        numbers = [n for n in (row_number(row) for row in buf) if n is not None]
        if numbers:
            group_meta["row_start"] = min(numbers)
            group_meta["row_end"] = max(numbers)
        groups.append((render(buf), group_meta))
        buf = []

    for row in row_lines:
        candidate = buf + [row]
        candidate_tokens = _count_tokens(render(candidate))
        if buf and candidate_tokens > target_tokens:
            flush()
        if _count_tokens(render([row])) > max_tokens:
            # A single oversized row is rare. Keep it whole for provenance;
            # _make_children will still enforce the hard cap as a safety net.
            buf = [row]
            flush()
            continue
        buf.append(row)
    flush()
    return groups


def _make_children(
    parent_id: str,
    doc_id: str,
    corpus_id: str,
    parent_text: str,
    heading_path: list[str] | None,
    source_tier: str,
    child_index: int,
    *,
    child_target_tokens: int,
    child_min_tokens: int = 128,
    child_max_tokens: int = 700,
    page_start: int | None = None,
    page_end: int | None = None,
    chunk_kind: str = ChunkKind.BODY,
    language: str | None = None,
    metadata: dict | None = None,
    child_strategy: str = "sentence_merge",
) -> tuple[list[ChildChunk], int]:
    # semantic_split is a PROSE strategy: one child per paragraph/idea. Scope it
    # to plain body prose — structured content (tables = TABLE kind, code = CODE
    # kind, timed transcripts = a source_format) keeps its specialized splitter,
    # otherwise paragraph-splitting would shear off transcript headers / table
    # row groups.
    use_semantic = (
        str(child_strategy or "").lower() == "semantic_split"
        and chunk_kind == ChunkKind.BODY
        and not (metadata or {}).get("source_format")
    )
    if use_semantic:
        texts = _split_by_paragraph_idea(
            parent_text, child_target_tokens, child_max_tokens
        ) or [parent_text.strip()]
    else:
        texts = _split_at_boundary(parent_text, child_target_tokens) or [parent_text.strip()]
    # Hard cap: any child still over `child_max_tokens` after the boundary
    # splitter (rare but happens on long unbroken code blocks / tables) gets
    # force-broken at exact token boundaries so the embedder doesn't silently
    # truncate at its 1024 ceiling.
    texts = _hard_split_oversize(texts, child_max_tokens)
    # For semantic_split, coalesce only true fragments (below the fragment
    # floor), not whole short paragraphs — otherwise the idea-separated children
    # would be re-packed straight back into one chunk. sentence_merge keeps the
    # normal child_min behaviour.
    coalesce_min = (
        min(child_min_tokens, _SEMANTIC_FRAGMENT_FLOOR) if use_semantic else child_min_tokens
    )
    texts = _coalesce_small_child_texts(
        texts,
        child_min_tokens=coalesce_min,
        child_max_tokens=child_max_tokens,
    )
    children: list[ChildChunk] = []
    for ct in texts:
        if not ct.strip() or is_separator_only_text(ct):
            continue
        children.append(
            ChildChunk(
                chunk_id=f"{doc_id}_{child_index:04d}",
                parent_id=parent_id,
                doc_id=doc_id,
                corpus_id=corpus_id,
                text=ct,
                heading_path=heading_path,
                source_tier=source_tier,
                token_count=_count_tokens(ct),
                page_start=page_start,
                page_end=page_end,
                chunk_kind=chunk_kind,
                language=language,
                metadata=dict(metadata or {}),
            )
        )
        child_index += 1
    return children, child_index


def _page_blocks(
    pages: list[str],
    policy: ChunkingPolicy,
) -> list[tuple[list[str], str, int, int]]:
    """Group consecutive PDF pages into parent-sized text blocks.

    This preserves page ranges while avoiding the old one-page-per-parent shape,
    which overproduced parent summaries and made large books feel much larger
    than their actual semantic units.
    """
    blocks: list[tuple[list[str], str, int, int]] = []
    buf: list[str] = []
    buf_tokens = 0
    start_page: int | None = None
    end_page: int | None = None

    def flush() -> None:
        nonlocal buf, buf_tokens, start_page, end_page
        if not buf or start_page is None or end_page is None:
            return
        text = "\n\n".join(buf).strip()
        if text:
            label = (
                [f"page_{start_page}"]
                if start_page == end_page
                else [f"pages_{start_page}-{end_page}"]
            )
            blocks.append((label, text, start_page, end_page))
        buf, buf_tokens, start_page, end_page = [], 0, None, None

    for zero_idx, page_text in enumerate(pages):
        page_no = zero_idx + 1
        text = (page_text or "").strip()
        if not text:
            continue
        page_tokens = _count_tokens(text)
        if page_tokens > policy.parent_max_tokens:
            if buf:
                flush()
            for sub_text in _split_at_boundary(
                text,
                policy.parent_target_tokens,
                overlap_tokens=policy.parent_overlap_tokens,
            ):
                if sub_text.strip():
                    blocks.append(([f"page_{page_no}"], sub_text, page_no, page_no))
            continue

        would_exceed_target = buf and (buf_tokens + page_tokens > policy.parent_target_tokens)
        already_coherent = buf_tokens >= policy.parent_min_tokens
        would_exceed_max = buf and (buf_tokens + page_tokens > policy.parent_max_tokens)
        if would_exceed_max or (would_exceed_target and already_coherent):
            flush()

        if start_page is None:
            start_page = page_no
        buf.append(text)
        buf_tokens += page_tokens
        end_page = page_no

    if buf:
        flush()
    return blocks


def describe_chunking(parse_result, config=None) -> dict:
    """Return the resolved per-file chunking policy for audit/UI display."""
    policy = _build_policy(config)
    source_tier: SourceTier = parse_result.source_tier
    if source_tier == SourceTier.ocr_ast:
        parent_strategy = "pdf_page_grouped"
    elif source_tier == SourceTier.tier_code:
        parent_strategy = "ast_bound_code"
    elif source_tier in (SourceTier.tier_a, SourceTier.tier_b):
        parent_strategy = "heading_bound"
    elif source_tier == SourceTier.tier_b_plus:
        parent_strategy = "heading_bound_injected"
    else:
        parent_strategy = "token_window"

    details = {
        "mode": "auto",
        "parent_strategy": parent_strategy,
        "child_strategy": policy.resolved_child_strategy,
        "requested_child_strategy": policy.requested_child_strategy,
        "semantic_split_enabled": policy.resolved_child_strategy == "semantic_split",
        "semantic_split_reason": (
            "one child per paragraph/idea (finer, single-idea retrieval units)"
            if policy.resolved_child_strategy == "semantic_split"
            else None
        ),
        "chunk_overlap": policy.parent_overlap_tokens,
        "token_budgets": {
            "parent_min": policy.parent_min_tokens,
            "parent_target": policy.parent_target_tokens,
            "parent_max": policy.parent_max_tokens,
            "child_min": policy.child_min_tokens,
            "child_target": policy.child_target_tokens,
            "child_max": policy.child_max_tokens,
        },
        "page_ranges_preserved": source_tier == SourceTier.ocr_ast,
    }
    return {k: v for k, v in details.items() if v is not None}


def _emit_code_parents(
    blocks: list[_Block],
    *,
    doc_id: str,
    corpus_id: str,
    tier_value: str,
    parent_idx: int,
    child_idx: int,
    file_path: str | None,
) -> tuple[list[ParentChunk], list[ChildChunk], int, int]:
    """Build ParentChunk + ChildChunk for every CODE-kind block via
    code_splitter.pack(). Returns (parents, children, parent_idx, child_idx).

    Embedder-safety contract: every emitted ChildChunk.text fits
    EMBEDDER_SAFE_MAX_TOKENS cl100k tokens. When pack() can't meet the cap
    (unsupported language, missing tree-sitter pack, pathological source),
    we hard-split via _hard_split_oversize so the embedder never silently
    truncates. A WARNING is logged in that path so ops can see what slipped.
    """
    safe_cap = _embedder_safe_max_tokens()
    parents: list[ParentChunk] = []
    children: list[ChildChunk] = []

    for heading_path, text, kind, language, _metadata in blocks:
        if kind != ChunkKind.CODE:
            continue
        if not text or not text.strip():
            continue

        slices = code_splitter.pack(text, language or "", safe_cap)

        # Fallback: pack returned single (source, {}) for an over-cap input
        # because tree-sitter couldn't meet the budget. Hard-split with a
        # warning so embedder safety holds.
        if len(slices) == 1 and _count_tokens(slices[0][0]) > safe_cap:
            logger.warning(
                "code_splitter: pack failed to meet embedder cap; hard-splitting "
                "doc_id=%s lang=%s tokens=%d cap=%d",
                doc_id[:12] if doc_id else "?",
                language,
                _count_tokens(slices[0][0]),
                safe_cap,
            )
            forced = _hard_split_oversize([slices[0][0]], safe_cap)
            slices = [(t, {}) for t in forced]

        for slice_text, meta in slices:
            if not slice_text.strip():
                continue
            parent_id = f"{doc_id}_parent_{parent_idx:04d}"
            chunk_id = f"{doc_id}_{child_idx:04d}"
            chunk_meta = dict(meta) if meta else {}
            if file_path and "file_path" not in chunk_meta:
                chunk_meta["file_path"] = file_path
            child = ChildChunk(
                chunk_id=chunk_id,
                parent_id=parent_id,
                doc_id=doc_id,
                corpus_id=corpus_id,
                text=slice_text,
                heading_path=heading_path,
                source_tier=tier_value,
                token_count=_count_tokens(slice_text),
                chunk_kind=ChunkKind.CODE,
                language=language,
                metadata=chunk_meta,
            )
            parent = ParentChunk(
                parent_id=parent_id,
                doc_id=doc_id,
                corpus_id=corpus_id,
                text=slice_text,
                heading_path=heading_path,
                source_tier=tier_value,
                children=[child],
                chunk_kind=ChunkKind.CODE,
                language=language,
                metadata=chunk_meta,
            )
            parents.append(parent)
            children.append(child)
            parent_idx += 1
            child_idx += 1

    return parents, children, parent_idx, child_idx


def chunk(
    parse_result,
    doc_id: str,
    corpus_id: str,
    config=None,
) -> tuple[list[ParentChunk], list[ChildChunk], list]:
    """Phase 7.6 — split a parsed document into parent + child chunks.

    Args:
        parse_result: services.ingestion.docling_adapter.DoclingParseResult.
        doc_id: Stable SHA-256 content hash.
        corpus_id: Target corpus UUID.

    Returns:
        (parents, all_children, injected_headers) — `injected_headers` is the
        audit list returned by the adapter when it pre-augmented a plain-text
        upload before sending it to docling. Same shape as the legacy field
        so callers don't change.
    """
    parents: list[ParentChunk] = []
    all_children: list[ChildChunk] = []
    child_idx = 0
    parent_idx = 0
    policy = _build_policy(config)

    source_tier: SourceTier = parse_result.source_tier
    tier_value = source_tier.value
    parse_filename = getattr(parse_result, "filename", None)

    if source_tier == SourceTier.tier_code:
        # Code-file ingest (early-intercept lane). DoclingParseResult contains
        # exactly one Section(element_type="code_block"). Run it through the
        # AST packer with the embedder-safety cap; never hand to prose splitters.
        code_blocks: list[_Block] = []
        for sec in parse_result.sections or []:
            if not sec.text or not sec.text.strip():
                continue
            code_blocks.append((
                list(sec.heading_path or []),
                sec.text,
                ChunkKind.CODE,
                sec.language or getattr(parse_result, "language", None),
                getattr(sec, "metadata", None) or {},
            ))
        code_parents, code_children, parent_idx, child_idx = _emit_code_parents(
            code_blocks,
            doc_id=doc_id,
            corpus_id=corpus_id,
            tier_value=tier_value,
            parent_idx=parent_idx,
            child_idx=child_idx,
            file_path=parse_filename,
        )
        parents.extend(code_parents)
        all_children.extend(code_children)

    elif source_tier == SourceTier.ocr_ast:
        # Scrub markup noise (pandoc divs, bracketed anchors, image markdown,
        # ornamental HTML) per page before page-block grouping. Catches the
        # EPUB cover image and pagebreak scaffolding that used to leak into
        # body chunk_0000 and become embedding noise.
        raw_pages_in = parse_result.pages or (
            [parse_result.markdown or parse_result.text]
            if (parse_result.markdown or parse_result.text)
            else []
        )
        raw_pages = [_scrub_markup_noise(p or "") for p in raw_pages_in]
        for heading_path, page_text, page_start, page_end in _page_blocks(raw_pages, policy):
            parent_id = f"{doc_id}_parent_{parent_idx:04d}"
            parent_idx += 1
            # OCR pages have heading_path=["page_N"] which is inconclusive —
            # classify_chunk falls through to content-based detection so
            # bibliography / index / TOC pages get tagged.
            kind = classify_chunk(heading_path, page_text)
            p_children, child_idx = _make_children(
                parent_id,
                doc_id,
                corpus_id,
                page_text.strip(),
                heading_path,
                tier_value,
                child_idx,
                child_target_tokens=policy.child_target_tokens,
                child_min_tokens=policy.child_min_tokens,
                child_max_tokens=policy.child_max_tokens,
                page_start=page_start,
                page_end=page_end,
                chunk_kind=kind,
                child_strategy=policy.resolved_child_strategy,
            )
            parents.append(
                ParentChunk(
                    parent_id=parent_id,
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                    text=page_text.strip(),
                    heading_path=heading_path,
                    source_tier=tier_value,
                    children=p_children,
                    page_start=page_start,
                    page_end=page_end,
                    chunk_kind=kind,
                )
            )
            all_children.extend(p_children)

    elif source_tier in (SourceTier.tier_a, SourceTier.tier_b, SourceTier.tier_b_plus):
        blocks = _sections_to_parent_blocks(parse_result.sections)
        # Fallback: if docling produced no walkable sections (rare for these
        # tiers but possible on malformed input), fall back to markdown.
        if not blocks and (parse_result.markdown or parse_result.text):
            md = parse_result.markdown or parse_result.text
            blocks = [(None, md.strip(), ChunkKind.BODY, None, {})]
        # Scrub markup noise per BODY section. CODE blocks keep their fences
        # and original whitespace verbatim — scrubbing would mangle backticks
        # and the AST round-trip the language tag is part of.
        blocks = [
            (hp, _scrub_markup_noise(t) if k == ChunkKind.BODY else t, k, lang, meta)
            for hp, t, k, lang, meta in blocks if t
        ]
        blocks = [(hp, t, k, lang, meta) for hp, t, k, lang, meta in blocks if t]

        # Phase K — adaptive coalesce: merge consecutive small BODY sections
        # so heading-heavy docs don't explode into hundreds of tiny parents.
        # Coalesce refuses to merge across kind boundaries (code stays separate).
        blocks = _coalesce_small_blocks(
            blocks,
            min_parent_tokens=policy.parent_min_tokens,
            max_parent_tokens=policy.parent_max_tokens,
        )

        # Code lane: split CODE blocks out first and run them through the
        # AST packer. Whatever remains is prose handled by the existing path.
        code_parents, code_children, parent_idx, child_idx = _emit_code_parents(
            [b for b in blocks if b[2] == ChunkKind.CODE],
            doc_id=doc_id,
            corpus_id=corpus_id,
            tier_value=tier_value,
            parent_idx=parent_idx,
            child_idx=child_idx,
            file_path=parse_filename,
        )
        parents.extend(code_parents)
        all_children.extend(code_children)

        for heading_path, section_text, block_kind, _block_lang, block_meta in blocks:
            if block_kind == ChunkKind.CODE:
                continue  # already handled by _emit_code_parents above
            if not section_text.strip():
                continue
            if block_kind == ChunkKind.TABLE:
                sub_texts_with_meta = _split_table_rows_for_children(
                    section_text,
                    block_meta,
                    child_target_tokens=policy.child_target_tokens,
                    child_max_tokens=policy.child_max_tokens,
                )
            elif _count_tokens(section_text) > policy.parent_max_tokens:
                sub_texts = _split_at_boundary(
                    section_text,
                    policy.parent_target_tokens,
                    overlap_tokens=policy.parent_overlap_tokens,
                )
                sub_texts_with_meta = [(text, {}) for text in sub_texts]
            else:
                sub_texts_with_meta = [(section_text, {})]

            # Heading-bound tiers normally classify by heading text alone —
            # passing sub_text lets content-fallback fire when the heading
            # itself is missing (rare but possible on malformed inputs).
            sub_texts_for_classify = [text for text, _meta in sub_texts_with_meta]
            kind = (
                ChunkKind.TABLE
                if block_kind == ChunkKind.TABLE
                else classify_chunk(
                    heading_path,
                    " ".join(sub_texts_for_classify) if sub_texts_for_classify else None,
                )
            )
            for sub_text, sub_meta in sub_texts_with_meta:
                if not sub_text.strip():
                    continue
                parent_id = f"{doc_id}_parent_{parent_idx:04d}"
                parent_idx += 1
                metadata = dict(block_meta or {})
                metadata.update(sub_meta or {})
                p_children, child_idx = _make_children(
                    parent_id,
                    doc_id,
                    corpus_id,
                    sub_text,
                    heading_path,
                    tier_value,
                    child_idx,
                    child_target_tokens=policy.child_target_tokens,
                    child_min_tokens=policy.child_min_tokens,
                    child_max_tokens=policy.child_max_tokens,
                    chunk_kind=kind,
                    metadata=metadata,
                    child_strategy=policy.resolved_child_strategy,
                )
                parents.append(
                    ParentChunk(
                        parent_id=parent_id,
                        doc_id=doc_id,
                        corpus_id=corpus_id,
                        text=sub_text,
                        heading_path=heading_path,
                        source_tier=tier_value,
                        children=p_children,
                        chunk_kind=kind,
                        metadata=metadata,
                    )
                )
                all_children.extend(p_children)

    else:  # tier_c — pure token budget over the docling text/markdown fallback
        # Scrub markup noise on the markdown blob before token-budget splitting.
        # Plain-text uploads (which classify as tier_c) sometimes carry pandoc
        # / EPUB residue when they were generated from converted ebooks.
        text = _scrub_markup_noise(parse_result.text or parse_result.markdown or "")
        # Semantic parents first (topic-aligned, deterministic); legacy
        # token-window split is the always-available fallback.
        parent_texts = _semantic_parent_blocks(
            text,
            min_tokens=policy.parent_min_tokens,
            target_tokens=policy.parent_target_tokens,
            max_tokens=policy.parent_max_tokens,
        ) or _split_at_boundary(
            text,
            policy.parent_target_tokens,
            overlap_tokens=policy.parent_overlap_tokens,
        )
        for pt in parent_texts:
            if not pt.strip():
                continue
            parent_id = f"{doc_id}_parent_{parent_idx:04d}"
            parent_idx += 1
            # tier_c has no headings — classify_chunk routes straight into
            # content-based detection (citation density, dot-leaders, etc.).
            kind = classify_chunk(None, pt)
            p_children, child_idx = _make_children(
                parent_id,
                doc_id,
                corpus_id,
                pt,
                None,
                tier_value,
                child_idx,
                child_target_tokens=policy.child_target_tokens,
                child_min_tokens=policy.child_min_tokens,
                child_max_tokens=policy.child_max_tokens,
                chunk_kind=kind,
                child_strategy=policy.resolved_child_strategy,
            )
            parents.append(
                ParentChunk(
                    parent_id=parent_id,
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                    text=pt,
                    heading_path=None,
                    source_tier=tier_value,
                    children=p_children,
                    chunk_kind=kind,
                )
            )
            all_children.extend(p_children)

    # Re-pack the adapter's audit dicts as InjectedHeader objects so the
    # worker's existing audit-trail code keeps working unchanged.
    injected = [
        InjectedHeader(
            line_no=int(h.get("line_no", 0)),
            level=int(h.get("level", 1)),
            pattern=str(h.get("pattern", "")),
            original_line=str(h.get("original_line", "")),
        )
        for h in (parse_result.injected_headers_audit or [])
    ]
    return parents, all_children, injected
