"""
Tier chunker — hierarchical parent/child splitting.

Phase 7.6 — parent-splitting now consumes a `DoclingParseResult` from the
docling sidecar. Sections come pre-walked; we only do the token-budget
splitting + child generation.

Tier A / B / B+:  parent = one DoclingParseResult.section per heading,
                  re-split on token budget when a section >1.5x target.
                  (B+ already had inject_synthetic_headers run BEFORE
                  docling parsed, so its sections look identical to A.)
Tier C:           parent = token budget (~1200 tok) over the markdown
                  fallback, child = sentence groups (~350 tok).
OCR AST (PDF):    parent = one per docling page (result.pages[]),
                  child = paragraph groups within page (~350 tok).
"""
import re
from dataclasses import dataclass, field

import tiktoken

from models.schemas import SourceTier
from services.ingestion.b_plus_normalizer import InjectedHeader  # re-exported for worker

_TOKENIZER = tiktoken.get_encoding("cl100k_base")
PARENT_TARGET_TOKENS = 1200
CHILD_TARGET_TOKENS = 350


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


@dataclass
class ParentChunk:
    parent_id: str
    doc_id: str
    corpus_id: str
    text: str
    heading_path: list[str] | None
    source_tier: str
    children: list[ChildChunk] = field(default_factory=list)


def _count_tokens(text: str) -> int:
    return len(_TOKENIZER.encode(text, disallowed_special=()))


def _split_at_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _split_at_boundary(text: str, target_tokens: int) -> list[str]:
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

    return [c for c in chunks if c.strip()]


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
) -> tuple[list[ChildChunk], int]:
    texts = _split_at_boundary(parent_text, CHILD_TARGET_TOKENS) or [parent_text.strip()]
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
            )
        )
        child_index += 1
    return children, child_index


def chunk(
    parse_result,
    doc_id: str,
    corpus_id: str,
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

    source_tier: SourceTier = parse_result.source_tier
    tier_value = source_tier.value

    if source_tier == SourceTier.ocr_ast:
        raw_pages = parse_result.pages or ([parse_result.markdown or parse_result.text] if (parse_result.markdown or parse_result.text) else [])
        for pi, page_text in enumerate(raw_pages):
            if not page_text or not page_text.strip():
                continue
            parent_id = f"{doc_id}_parent_{parent_idx:04d}"
            parent_idx += 1
            hp = [f"page_{pi + 1}"]
            p_children, child_idx = _make_children(
                parent_id, doc_id, corpus_id, page_text.strip(), hp, tier_value, child_idx
            )
            parents.append(
                ParentChunk(
                    parent_id=parent_id,
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                    text=page_text.strip(),
                    heading_path=hp,
                    source_tier=tier_value,
                    children=p_children,
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

        for heading_path, section_text in blocks:
            if not section_text.strip():
                continue
            if _count_tokens(section_text) > PARENT_TARGET_TOKENS * 1.5:
                sub_texts = _split_at_boundary(section_text, PARENT_TARGET_TOKENS)
            else:
                sub_texts = [section_text]

            for sub_text in sub_texts:
                if not sub_text.strip():
                    continue
                parent_id = f"{doc_id}_parent_{parent_idx:04d}"
                parent_idx += 1
                p_children, child_idx = _make_children(
                    parent_id, doc_id, corpus_id, sub_text, heading_path, tier_value, child_idx
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
                    )
                )
                all_children.extend(p_children)

    else:  # tier_c — pure token budget over the docling text/markdown fallback
        text = parse_result.text or parse_result.markdown or ""
        parent_texts = _split_at_boundary(text, PARENT_TARGET_TOKENS)
        for pt in parent_texts:
            if not pt.strip():
                continue
            parent_id = f"{doc_id}_parent_{parent_idx:04d}"
            parent_idx += 1
            p_children, child_idx = _make_children(
                parent_id, doc_id, corpus_id, pt, None, tier_value, child_idx
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
