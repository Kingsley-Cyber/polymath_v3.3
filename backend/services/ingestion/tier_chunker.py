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
    # appendix / front_matter / back_matter). Inherited from the parent's
    # heading classification. Defaults to BODY so legacy code paths and
    # rehydrated data without this field behave identically to a normal chunk.
    chunk_kind: str = ChunkKind.BODY


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


def _coalesce_small_blocks(
    blocks: list[tuple[list[str] | None, str]],
    *,
    min_parent_tokens: int,
    max_parent_tokens: int,
) -> list[tuple[list[str] | None, str]]:
    """Merge consecutive below-MIN sections up to MAX_PARENT_TOKENS.

    Policy: if current block is under MIN_PARENT_TOKENS AND combining with
    the next block keeps us at or below MAX_PARENT_TOKENS, fold them together
    and keep the first block's heading_path. Otherwise, push current and
    advance. Preserves structure-awareness for well-sized sections while
    smoothing out doc-specific pathologies (e.g. a 4K-token docx with 37
    tiny heading sections).

    The over-size case (single section > 1.5x target) is still handled
    downstream by the existing `_split_at_boundary` call — coalesce only
    fixes the UNDER-size failure mode.
    """
    if not blocks:
        return blocks
    out: list[tuple[list[str] | None, str]] = []
    cur_path, cur_text = blocks[0]
    merges = 0
    for next_path, next_text in blocks[1:]:
        cur_tok = _count_tokens(cur_text)
        if cur_tok < min_parent_tokens:
            combined = cur_text + "\n\n" + next_text
            combined_tok = _count_tokens(combined)
            if combined_tok <= max_parent_tokens:
                cur_text = combined
                merges += 1
                continue
        out.append((cur_path, cur_text))
        cur_path, cur_text = next_path, next_text
    out.append((cur_path, cur_text))
    if merges > 0:
        logger.info(
            "tier_chunker coalesce: %d/%d blocks merged (input=%d, output=%d)",
            merges, len(blocks), len(blocks), len(out),
        )
    return out


def _sections_to_parent_blocks(parse_sections) -> list[tuple[list[str] | None, str]]:
    """Fold the docling section walk into (heading_path, parent_text) pairs.

    Each section_heading starts a new parent block; subsequent paragraph
    sections accumulate into the current block. The heading_path on each
    paragraph is taken verbatim from the docling walker (which already
    tracks the current heading stack).
    """
    blocks: list[tuple[list[str] | None, list[str]]] = []
    current_path: list[str] | None = None
    current_buf: list[str] = []

    def flush():
        nonlocal current_buf
        if current_buf:
            blocks.append((current_path, current_buf))
            current_buf = []

    for sec in parse_sections:
        if sec.element_type == "section_heading":
            flush()
            current_path = list(sec.heading_path or [])
            # Render the heading itself as the first line of the block so
            # downstream summarizers / embedders see the title.
            level = sec.level or 1
            current_buf = [f"{'#' * min(level, 6)} {sec.text}".strip()]
        else:
            # Paragraph / list / table chunk → accumulate.
            if current_path is None and sec.heading_path:
                current_path = list(sec.heading_path)
            current_buf.append(sec.text)

    flush()
    return [(path, "\n\n".join(buf).strip()) for path, buf in blocks if buf]


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
    page_start: int | None = None,
    page_end: int | None = None,
    chunk_kind: str = ChunkKind.BODY,
) -> tuple[list[ChildChunk], int]:
    texts = _split_at_boundary(parent_text, child_target_tokens) or [parent_text.strip()]
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

    if source_tier == SourceTier.ocr_ast:
        raw_pages = parse_result.pages or ([parse_result.markdown or parse_result.text] if (parse_result.markdown or parse_result.text) else [])
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
            blocks = [(None, md.strip())]

        # Phase K — adaptive coalesce: merge consecutive small sections so
        # heading-heavy docs don't explode into hundreds of tiny parents.
        blocks = _coalesce_small_blocks(
            blocks,
            min_parent_tokens=policy.parent_min_tokens,
            max_parent_tokens=policy.parent_max_tokens,
        )

        for heading_path, section_text in blocks:
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
        text = parse_result.text or parse_result.markdown or ""
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
