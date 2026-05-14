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
CHILD_TARGET_TOKENS = 350
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
    child_min_tokens: int = 128
    child_target_tokens: int = CHILD_TARGET_TOKENS
    child_max_tokens: int = 512
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

    The backend treats `child_chunk_algorithm` as a requested policy hint, not
    a hard file-type decision. Semantic splitting is intentionally resolved to
    sentence_merge until the semantic splitter is fully implemented.
    """
    parent_budget = getattr(config, "parent_chunk_tokens", None)
    child_budget = getattr(config, "child_chunk_tokens", None)
    parent_target = max(200, _budget_value(parent_budget, "target_tokens", PARENT_TARGET_TOKENS))
    parent_min = max(100, min(_budget_value(parent_budget, "min_tokens", 500), parent_target))
    parent_max = max(parent_target, _budget_value(parent_budget, "max_tokens", 2000))
    child_target = max(100, _budget_value(child_budget, "target_tokens", CHILD_TARGET_TOKENS))
    child_min = max(50, min(_budget_value(child_budget, "min_tokens", 128), child_target))
    child_max = max(child_target, _budget_value(child_budget, "max_tokens", 512))
    requested = str(getattr(config, "child_chunk_algorithm", "sentence_merge") or "sentence_merge")
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
        resolved_child_strategy="sentence_merge",
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
    # Pandoc fenced div close: a line containing only `:::` (one or more).
    (re.compile(r"^\s*:::+\s*$", re.MULTILINE), ""),
    # Pandoc bracketed anchors / pagebreak markers:
    #   []{#anchor .class aria-label="…" role="…"}
    (re.compile(r"\[\]\{[^\n}]*\}"), ""),
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
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _hard_split_oversize(chunks: list[str], max_tokens: int) -> list[str]:
    """Last-line-of-defense split for chunks that exceed `max_tokens`.

    `_split_at_boundary` prefers paragraph / sentence boundaries, but
    pathological inputs (long unbroken code blocks, single-line walls of
    text, large tables stringified) can still produce oversized chunks.
    Without this guard those chunks silently truncate at the embedder's
    1024-token ceiling, dropping the tail content.

    The split lands at exact token boundaries via re-encoding through the
    cl100k_base tokenizer. Boundaries can land mid-word — that's still
    better than letting the embedder truncate randomly, since both halves
    of the split survive in Qdrant with their full text in Mongo.
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
        for i in range(0, len(toks), max_tokens):
            sub = _TOKENIZER.decode(toks[i:i + max_tokens]).strip()
            if sub:
                out.append(sub)
    if over_count:
        logger.info(
            "tier_chunker hard-split: %d/%d chunks force-broken at max_tokens=%d",
            over_count, len(chunks), max_tokens,
        )
    return out


def _split_at_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


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


_Block = tuple[list[str] | None, str, str, str | None]
# 4-tuple: (heading_path, text, chunk_kind, language)


def _coalesce_small_blocks(
    blocks: list[_Block],
    *,
    min_parent_tokens: int,
    max_parent_tokens: int,
) -> list[_Block]:
    """Merge consecutive below-MIN BODY sections up to MAX_PARENT_TOKENS.

    Policy: if current block is BODY AND under MIN_PARENT_TOKENS AND combining
    with the next block (also BODY) keeps us at or below MAX_PARENT_TOKENS,
    fold them together and keep the first block's heading_path. Otherwise,
    push current and advance.

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
    cur_path, cur_text, cur_kind, cur_lang = blocks[0]
    merges = 0
    for next_path, next_text, next_kind, next_lang in blocks[1:]:
        if cur_kind == ChunkKind.BODY and next_kind == ChunkKind.BODY:
            cur_tok = _count_tokens(cur_text)
            if cur_tok < min_parent_tokens:
                combined = cur_text + "\n\n" + next_text
                combined_tok = _count_tokens(combined)
                if combined_tok <= max_parent_tokens:
                    cur_text = combined
                    merges += 1
                    continue
        out.append((cur_path, cur_text, cur_kind, cur_lang))
        cur_path, cur_text, cur_kind, cur_lang = (
            next_path, next_text, next_kind, next_lang,
        )
    out.append((cur_path, cur_text, cur_kind, cur_lang))
    if merges > 0:
        logger.info(
            "tier_chunker coalesce: %d/%d blocks merged (input=%d, output=%d)",
            merges, len(blocks), len(blocks), len(out),
        )
    return out


def _sections_to_parent_blocks(parse_sections) -> list[_Block]:
    """Fold the docling section walk into 4-tuple blocks.

    Returns list[(heading_path, text, chunk_kind, language)]:
      - section_heading starts a new BODY block (heading text becomes first line).
      - paragraph / list / table sections accumulate into the current BODY block.
      - code_block sections flush the BODY buffer and emit their own CODE block
        carrying the language tag. The chunker routes CODE blocks through
        code_splitter.pack() instead of the prose sentence/token splitters.
    """
    blocks: list[_Block] = []
    current_path: list[str] | None = None
    current_buf: list[str] = []

    def flush_body():
        nonlocal current_buf
        if current_buf:
            text = "\n\n".join(current_buf).strip()
            if text:
                blocks.append((current_path, text, ChunkKind.BODY, None))
            current_buf = []

    for sec in parse_sections:
        if sec.element_type == "section_heading":
            flush_body()
            current_path = list(sec.heading_path or [])
            # Render the heading itself as the first line of the next BODY
            # block so downstream summarizers/embedders see the title.
            level = sec.level or 1
            current_buf = [f"{'#' * min(level, 6)} {sec.text}".strip()]
        elif sec.element_type == "code_block":
            # Code lane: emit the fenced block as its own CODE-kind block
            # under the active heading path. Don't fold into BODY.
            flush_body()
            code_path = list(current_path) if current_path else list(sec.heading_path or [])
            blocks.append((code_path, sec.text, ChunkKind.CODE, sec.language))
        else:
            # Paragraph / list / table chunk → accumulate into BODY buffer.
            if current_path is None and sec.heading_path:
                current_path = list(sec.heading_path)
            current_buf.append(sec.text)

    flush_body()
    return blocks


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
    child_max_tokens: int = 700,
    page_start: int | None = None,
    page_end: int | None = None,
    chunk_kind: str = ChunkKind.BODY,
) -> tuple[list[ChildChunk], int]:
    texts = _split_at_boundary(parent_text, child_target_tokens) or [parent_text.strip()]
    # Hard cap: any child still over `child_max_tokens` after the boundary
    # splitter (rare but happens on long unbroken code blocks / tables) gets
    # force-broken at exact token boundaries so the embedder doesn't silently
    # truncate at its 1024 ceiling.
    texts = _hard_split_oversize(texts, child_max_tokens)
    children: list[ChildChunk] = []
    for ct in texts:
        if not ct.strip():
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
        "semantic_split_enabled": False,
        "semantic_split_reason": (
            "semantic_split is treated as a policy hint until the splitter is implemented"
            if policy.requested_child_strategy == "semantic_split"
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

    for heading_path, text, kind, language in blocks:
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
                child_max_tokens=policy.child_max_tokens,
                page_start=page_start,
                page_end=page_end,
                chunk_kind=kind,
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
            blocks = [(None, md.strip(), ChunkKind.BODY, None)]
        # Scrub markup noise per BODY section. CODE blocks keep their fences
        # and original whitespace verbatim — scrubbing would mangle backticks
        # and the AST round-trip the language tag is part of.
        blocks = [
            (hp, _scrub_markup_noise(t) if k == ChunkKind.BODY else t, k, lang)
            for hp, t, k, lang in blocks if t
        ]
        blocks = [(hp, t, k, lang) for hp, t, k, lang in blocks if t]

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

        for heading_path, section_text, block_kind, _block_lang in blocks:
            if block_kind == ChunkKind.CODE:
                continue  # already handled by _emit_code_parents above
            if not section_text.strip():
                continue
            if _count_tokens(section_text) > policy.parent_max_tokens:
                sub_texts = _split_at_boundary(
                    section_text,
                    policy.parent_target_tokens,
                    overlap_tokens=policy.parent_overlap_tokens,
                )
            else:
                sub_texts = [section_text]

            # Heading-bound tiers normally classify by heading text alone —
            # passing sub_text lets content-fallback fire when the heading
            # itself is missing (rare but possible on malformed inputs).
            kind = classify_chunk(heading_path, " ".join(sub_texts) if sub_texts else None)
            for sub_text in sub_texts:
                if not sub_text.strip():
                    continue
                parent_id = f"{doc_id}_parent_{parent_idx:04d}"
                parent_idx += 1
                p_children, child_idx = _make_children(
                    parent_id,
                    doc_id,
                    corpus_id,
                    sub_text,
                    heading_path,
                    tier_value,
                    child_idx,
                    child_target_tokens=policy.child_target_tokens,
                    child_max_tokens=policy.child_max_tokens,
                    chunk_kind=kind,
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
                    )
                )
                all_children.extend(p_children)

    else:  # tier_c — pure token budget over the docling text/markdown fallback
        # Scrub markup noise on the markdown blob before token-budget splitting.
        # Plain-text uploads (which classify as tier_c) sometimes carry pandoc
        # / EPUB residue when they were generated from converted ebooks.
        text = _scrub_markup_noise(parse_result.text or parse_result.markdown or "")
        parent_texts = _split_at_boundary(
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
                child_max_tokens=policy.child_max_tokens,
                chunk_kind=kind,
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
