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

import json
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


# ── persistence + ingest hook (PARENT summaries in → DOCUMENT profile out) ──
async def _default_llm(prompt: str) -> str:
    from services.llm import llm_service

    return await llm_service.complete_chat([{"role": "user", "content": prompt}])


async def build_and_store_tree(
    *,
    db,
    doc_id: str,
    corpus_id: str,
    llm_fn: LlmFn | None = None,
    use_llm: bool = True,
    heal_missing: bool = True,
    heal_limit: int = 2000,
) -> dict[str, Any]:
    """Read PARENT-level summaries (parent_chunks.summary — Ghost A output;
    never child chunks), build the L2→L4 tree, upsert nodes into the
    `summary_tree` collection (stable node_id ⇒ idempotent/resumable), and
    stamp the L4 profile onto the documents record as `doc_profile` — the
    document-level summary the system never had. Best-effort by design."""
    from dataclasses import asdict
    from datetime import datetime

    doc = await db["documents"].find_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {"title": 1, "filename": 1, "source_type": 1},
    )
    if not doc:
        return {"skipped": "no_document"}
    rows = await db["parent_chunks"].find(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {"parent_id": 1, "summary": 1, "heading_path": 1, "domain": 1,
         "chunk_kind": 1, "text": 1, "child_ids": 1, "source_child_ids": 1},
    ).sort("parent_id", 1).to_list(length=None)  # {doc}_parent_NNNN → lexical = doc order
    body_rows = [r for r in rows if (r.get("chunk_kind") or "body") == "body"]
    fn = llm_fn if llm_fn is not None else (_default_llm if use_llm else None)

    # GUARD RAIL — the summarized-parent invariant. Ghost A is conditional
    # (global enabled flag + model pool); this backstop ensures every BODY
    # parent carries a summary before the tree builds. Deterministic
    # identification (chunk_kind == body via section_classifier), bounded,
    # best-effort per parent, persisted so re-runs skip healed rows.
    healed = 0
    if fn is not None and heal_missing:
        for r in body_rows:
            if healed >= heal_limit:
                break
            if (r.get("summary") or "").strip():
                continue
            text = (r.get("text") or "")[:6000]
            if not text.strip():
                continue
            from services.ingestion.summary_semantics import (
                SEMANTIC_SUMMARY_INSTRUCTION,
                canonical_parent_summary_fields,
                parse_semantic_summary,
                topic_key_for,
            )
            source_child_ids = [
                str(v)
                for v in (r.get("source_child_ids") or r.get("child_ids") or [])
                if str(v)
            ]
            try:
                raw = (await fn(
                    "Summarize and classify this passage. "
                    + SEMANTIC_SUMMARY_INSTRUCTION
                    + "\n\nsource_child_ids: "
                    + json.dumps(source_child_ids)
                    + "\n\nPASSAGE:\n"
                    + text
                ) or "").strip()
            except Exception:  # noqa: BLE001 — guard rail never fails the tree
                raw = ""
            sem = parse_semantic_summary(
                raw,
                source_child_ids=source_child_ids,
                source_text=text,
            )
            artifact = canonical_parent_summary_fields(
                sem,
                parent_id=str(r["parent_id"]),
                doc_id=doc_id,
                corpus_id=corpus_id,
                source_text=text,
                source_child_ids=source_child_ids,
                summary_model="summary_tree_heal",
                repair_status=sem.get("repair_status"),
            )
            if artifact["summary"] and artifact["validation_status"] == "valid":
                domain = sem["domain"] or r.get("domain")
                update_fields = {
                    "summary": artifact["summary"],
                    "domain": domain,
                    "semantic_chunk_type": sem["semantic_chunk_type"],
                    "key_terms": sem["key_terms"] or None,
                    "mechanisms": sem["mechanisms"] or None,
                    "schema_version": artifact["schema_version"],
                    "summary_type": artifact["summary_type"],
                    "central_claim": artifact["central_claim"],
                    "key_points": artifact["key_points"] or None,
                    "main_mechanism": artifact["main_mechanism"],
                    "concept_tags": artifact["concept_tags"] or None,
                    "entity_hints": artifact["entity_hints"] or None,
                    "retrieval_uses": artifact["retrieval_uses"] or None,
                    "abstraction_level": artifact["abstraction_level"],
                    "source_child_ids": artifact["source_child_ids"] or source_child_ids,
                    "summary_id": artifact["summary_id"],
                    "source_hash": artifact["source_hash"],
                    "summary_model": artifact["summary_model"],
                    "summary_created_at": artifact["summary_created_at"],
                    "validation_status": artifact["validation_status"],
                    "repair_status": artifact["repair_status"],
                    "quality_score": artifact["quality_score"],
                    "quality_flags": artifact["quality_flags"],
                    "retrieval_text": artifact["retrieval_text"],
                    "topic_key": topic_key_for(domain, r.get("heading_path")),
                }
                r["summary"] = artifact["summary"]
                r["domain"] = domain
                await db["parent_chunks"].update_one(
                    {"corpus_id": corpus_id, "parent_id": r["parent_id"]},
                    {"$set": update_fields},
                )
                healed += 1

    parents = [
        ParentSummaryIn(
            parent_id=str(r["parent_id"]),
            summary=str(r.get("summary") or ""),
            heading_path=tuple(r.get("heading_path") or ()),
            domain=str(r.get("domain") or ""),
        )
        for r in body_rows
    ]
    if not any(p.summary for p in parents):
        return {"skipped": "no_parent_summaries", "parents": len(parents), "healed": healed}
    nodes = await build_tree(
        doc_id=doc_id,
        corpus_id=corpus_id,
        title=str(doc.get("title") or doc.get("filename") or doc_id[:12]),
        source_type=str(doc.get("source_type") or ""),
        parents=parents,
        llm_fn=fn,
    )
    now = datetime.utcnow()
    for n in nodes:
        rec = asdict(n)
        rec["updated_at"] = now
        await db["summary_tree"].replace_one({"node_id": n.node_id}, rec, upsert=True)
    profile = nodes[-1]
    await db["documents"].update_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {"$set": {"doc_profile": {
            "summary_id": profile.node_id,
            "summary": profile.summary,
            "concepts": profile.concepts,
            "domains": profile.domains,
            "section_ids": profile.child_node_ids,
            "schema_version": profile.schema_version,
            "updated_at": now,
        }}},
    )
    counts: dict[str, Any] = {}
    for n in nodes:
        counts[n.node_type] = counts.get(n.node_type, 0) + 1
    counts["parents_in"] = len(parents)
    counts["summaries_healed"] = healed
    return counts
