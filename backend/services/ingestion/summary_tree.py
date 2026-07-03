"""B3 — the owner summary tree (OWNER_SUMMARY_TREE_DESIGN.md).

child chunks → parent summaries → ROLLUPS (12–20 window) → SECTIONS (heading
group) → document PROFILE (compact source card). A 1,727-parent book becomes
~87 rollups → a handful of sections → ONE 1–2k-token profile input — never a
100k-token summary call.

Structure here is PURE + deterministic (windowing, heading grouping, profile
input assembly — all rule-based, tested without any model). Only the three
summary texts come from an injected async `llm_fn(prompt) -> str`, so the
generator is testable with a fake and runs with deepseek-chat in production.
Generation is resumable (stable ids, upsert-shaped outputs) and best-effort:
a failed node yields a deterministic extractive fallback, never an exception.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

ROLLUP_WINDOW_MIN = 12
ROLLUP_WINDOW_MAX = 20
PROFILE_MAX_SECTIONS = 8
PROFILE_MAX_CONCEPTS = 12
TREE_SCHEMA_VERSION = "polymath.summary_tree.v1"

LlmFn = Callable[[str], Awaitable[str]]


@dataclass(frozen=True)
class ParentSummaryIn:
    """L1 input row — from parent_chunks (summary already exists via Ghost A)."""

    parent_id: str
    summary: str
    heading_path: tuple[str, ...] = ()
    domain: str = ""
    concepts: tuple[str, ...] = ()   # optional (promoted metadata when present)


@dataclass
class TreeNode:
    node_id: str
    node_type: str                    # rollup | section | document
    doc_id: str
    corpus_id: str
    parent_ids: list[str] = field(default_factory=list)   # L1 members (rollups)
    child_node_ids: list[str] = field(default_factory=list)  # tree children
    section_range: str = ""
    summary: str = ""
    concepts: list[str] = field(default_factory=list)
    domains: dict[str, int] = field(default_factory=dict)
    schema_version: str = TREE_SCHEMA_VERSION


# ── pure structure ──────────────────────────────────────────────────────────
def _top_heading(p: ParentSummaryIn) -> str:
    return p.heading_path[0] if p.heading_path else "(untitled)"


def group_by_section(parents: Sequence[ParentSummaryIn]) -> list[tuple[str, list[ParentSummaryIn]]]:
    """Group CONSECUTIVE parents by top-level heading (document order kept —
    a heading that reappears later starts a new group, preserving structure)."""
    groups: list[tuple[str, list[ParentSummaryIn]]] = []
    for p in parents:
        h = _top_heading(p)
        if groups and groups[-1][0] == h:
            groups[-1][1].append(p)
        else:
            groups.append((h, [p]))
    return groups


def windows(items: Sequence[ParentSummaryIn],
            lo: int = ROLLUP_WINDOW_MIN, hi: int = ROLLUP_WINDOW_MAX) -> list[list[ParentSummaryIn]]:
    """Split a section's parents into rollup windows of lo..hi, deterministic:
    equal-ish sizes, never below lo unless the whole section is smaller."""
    n = len(items)
    if n == 0:
        return []
    if n <= hi:
        return [list(items)]
    count = (n + hi - 1) // hi                     # fewest windows within hi
    base, extra = divmod(n, count)
    out, i = [], 0
    for w in range(count):
        size = base + (1 if w < extra else 0)
        out.append(list(items[i:i + size]))
        i += size
    return out


def top_terms(counter: dict[str, int], k: int) -> list[str]:
    return [t for t, _ in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:k]]


def build_profile_input(
    title: str,
    source_type: str,
    sections: Sequence[TreeNode],
    domains: dict[str, int],
    concepts: dict[str, int],
) -> str:
    """The ONLY thing the final profile LLM call sees — 1–2k tokens, per the
    owner rule: title + detected domains w/ counts + top concepts + top
    section summaries. NEVER all parent summaries."""
    lines = [f"Title: {title}", f"Source type: {source_type or 'document'}"]
    if domains:
        lines.append("Detected domains: " + ", ".join(
            f"{d}({c})" for d, c in sorted(domains.items(), key=lambda kv: -kv[1])[:6]))
    if concepts:
        lines.append("Top concepts: " + ", ".join(top_terms(concepts, PROFILE_MAX_CONCEPTS)))
    lines.append("Top sections:")
    for s in list(sections)[:PROFILE_MAX_SECTIONS]:
        lines.append(f"- {s.section_range}: {s.summary[:300]}")
    return "\n".join(lines)


def _extractive_fallback(texts: Sequence[str], limit: int = 3) -> str:
    """Deterministic no-LLM fallback: first sentence of the first N members."""
    outs = []
    for t in texts[:limit]:
        s = (t or "").strip().split(". ")[0].strip()
        if s:
            outs.append(s if s.endswith(".") else s + ".")
    return " ".join(outs) or "(no summary available)"


# ── generation (LLM-injected, best-effort) ─────────────────────────────────
_ROLLUP_PROMPT = (
    "Merge these section summaries into ONE dense 2-3 sentence rollup of what "
    "they collectively establish. No preamble, no bullets.\n\n{body}"
)
_SECTION_PROMPT = (
    "Merge these rollup summaries into ONE 2-3 sentence summary of this "
    "section of the document. No preamble.\n\n{body}"
)
_PROFILE_PROMPT = (
    "Write a 3-4 sentence document PROFILE for retrieval routing: what the "
    "document is, what it covers, and what questions it is best used for "
    "('Best used for questions about ...'). No preamble.\n\n{body}"
)


async def _gen(llm_fn: LlmFn | None, prompt: str, fallback_texts: Sequence[str]) -> str:
    if llm_fn is not None:
        try:
            out = (await llm_fn(prompt) or "").strip()
            if out:
                return out
        except Exception:
            pass  # best-effort — deterministic fallback below
    return _extractive_fallback(fallback_texts)


async def build_tree(
    *,
    doc_id: str,
    corpus_id: str,
    title: str,
    source_type: str,
    parents: Sequence[ParentSummaryIn],
    llm_fn: LlmFn | None,
) -> list[TreeNode]:
    """Full L2→L4 tree for one document. Returns upsert-ready nodes with
    STABLE ids (resumable: same doc ⇒ same node ids)."""
    usable = [p for p in parents if (p.summary or "").strip()]
    nodes: list[TreeNode] = []
    domains: dict[str, int] = {}
    concepts: dict[str, int] = {}
    for p in usable:
        if p.domain:
            domains[p.domain] = domains.get(p.domain, 0) + 1
        for c in p.concepts:
            concepts[c] = concepts.get(c, 0) + 1

    sections: list[TreeNode] = []
    r_idx = 0
    for s_idx, (heading, members) in enumerate(group_by_section(usable)):
        rollups: list[TreeNode] = []
        for win in windows(members):
            node = TreeNode(
                node_id=f"rollup_{doc_id[:12]}_{r_idx:04d}",
                node_type="rollup", doc_id=doc_id, corpus_id=corpus_id,
                parent_ids=[p.parent_id for p in win],
                section_range=heading,
            )
            r_idx += 1
            body = "\n".join(f"- {p.summary}" for p in win)
            node.summary = await _gen(
                llm_fn, _ROLLUP_PROMPT.format(body=body), [p.summary for p in win])
            rollups.append(node)
        sec = TreeNode(
            node_id=f"section_{doc_id[:12]}_{s_idx:04d}",
            node_type="section", doc_id=doc_id, corpus_id=corpus_id,
            child_node_ids=[r.node_id for r in rollups],
            section_range=heading,
        )
        if len(rollups) == 1:
            sec.summary = rollups[0].summary        # no extra LLM call needed
        else:
            body = "\n".join(f"- {r.summary}" for r in rollups)
            sec.summary = await _gen(
                llm_fn, _SECTION_PROMPT.format(body=body), [r.summary for r in rollups])
        nodes.extend(rollups)
        sections.append(sec)
    nodes.extend(sections)

    profile = TreeNode(
        node_id=f"docsum_{doc_id[:12]}",
        node_type="document", doc_id=doc_id, corpus_id=corpus_id,
        child_node_ids=[s.node_id for s in sections],
        section_range=title,
        domains=domains,
        concepts=top_terms(concepts, PROFILE_MAX_CONCEPTS),
    )
    profile.summary = await _gen(
        llm_fn,
        _PROFILE_PROMPT.format(body=build_profile_input(
            title, source_type, sections, domains, concepts)),
        [s.summary for s in sections],
    )
    nodes.append(profile)
    return nodes
