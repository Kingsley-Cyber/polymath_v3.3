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

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Sequence

from services.ingestion.section_classifier import should_summarize_parent
from services.ingestion.summary_semantics import _snake

ROLLUP_WINDOW_MIN = 12
ROLLUP_WINDOW_MAX = 20
PROFILE_MAX_SECTIONS = 8
PROFILE_MAX_CONCEPTS = 12
NODE_CONCEPTS_CAP = 16
TREE_SCHEMA_VERSION = "polymath.summary_tree.v1"

# Parent-row fields whose union defines a node's deterministic concepts
# (checklist P0.2/P2.1 — populate `summary_tree.concepts` at construction).
CONCEPT_SOURCE_FIELDS = ("key_terms", "mechanisms", "concept_tags")


def derive_node_concepts(
    parent_rows: list[dict], cap: int = NODE_CONCEPTS_CAP
) -> list[str]:
    """Deterministic node concepts — pure Python, zero LLM (owner directive).

    Union of each row's ``key_terms`` + ``mechanisms`` + ``concept_tags``,
    snake_case-normalized (shared ``summary_semantics._snake``), deduped
    per row (so frequency = number of rows carrying the concept), capped at
    ``cap``, ordered frequency-desc then alphabetically (``top_terms`` order).

    Rollup nodes derive from their member parent rows; section/document
    nodes union their children's already-derived concepts by passing them
    back through this helper as ``{"concept_tags": child.concepts}`` rows.
    """
    counts: dict[str, int] = {}
    for row in parent_rows or []:
        if not isinstance(row, dict):
            continue
        seen: set[str] = set()
        for field_name in CONCEPT_SOURCE_FIELDS:
            for value in row.get(field_name) or []:
                if value is None or isinstance(value, (bool, dict, list, tuple)):
                    continue
                concept = _snake(str(value))
                if concept and concept not in seen:
                    seen.add(concept)
                    counts[concept] = counts.get(concept, 0) + 1
    if cap <= 0:
        return []
    return top_terms(counts, int(cap))


def _child_concept_rows(children: Sequence[Any]) -> list[dict]:
    """Adapt derived child concepts into ``derive_node_concepts`` rows."""
    return [{"concept_tags": list(child.concepts or ())} for child in children]


LlmFn = Callable[[str], Awaitable[str]]
TreeEmbedFn = Callable[[list[str], dict[str, Any] | None], Awaitable[list[list[float]]]]


@dataclass(frozen=True)
class ParentSummaryIn:
    """L1 input row — from parent_chunks (summary already exists via Ghost A)."""

    parent_id: str
    summary: str
    heading_path: tuple[str, ...] = ()
    domain: str = ""
    concepts: tuple[str, ...] = ()  # optional (promoted metadata when present)


@dataclass
class TreeNode:
    node_id: str
    node_type: str  # rollup | section | document
    doc_id: str
    corpus_id: str
    parent_ids: list[str] = field(default_factory=list)  # L1 members (rollups)
    child_node_ids: list[str] = field(default_factory=list)  # tree children
    section_range: str = ""
    summary: str = ""
    concepts: list[str] = field(default_factory=list)
    domains: dict[str, int] = field(default_factory=dict)
    schema_version: str = TREE_SCHEMA_VERSION


def _config_dict(config: Any | None) -> dict[str, Any] | None:
    if config is None:
        return None
    if isinstance(config, dict):
        return dict(config)
    dump = getattr(config, "model_dump", None)
    if callable(dump):
        return dict(dump(mode="python"))
    legacy_dump = getattr(config, "dict", None)
    if callable(legacy_dump):
        return dict(legacy_dump())
    return None


def _node_record(node: TreeNode | dict[str, Any]) -> dict[str, Any]:
    if isinstance(node, dict):
        return dict(node)
    from dataclasses import asdict

    return asdict(node)


async def index_summary_tree_nodes(
    *,
    qdrant_client: Any,
    db: Any | None = None,
    corpus_id: str,
    nodes: Sequence[TreeNode | dict[str, Any]],
    embedding_config: Any | None,
    embed_fn: TreeEmbedFn | None = None,
    batch_size: int = 128,
) -> dict[str, Any]:
    """Persist L2/L3 hierarchy vectors from existing summary artifacts.

    This is an embedding/indexing operation, not an extraction or summary-model
    call. Stable node IDs make it safe to rerun after a summary tree changes.
    """

    from services.embedder import embed_documents
    from services.storage.qdrant_writer import (
        delete_summary_tree_entries,
        upsert_summary_tree_entries,
    )

    entries = [
        record
        for node in nodes
        for record in [_node_record(node)]
        if str(record.get("node_type") or "") in {"section", "rollup"}
        and str(record.get("node_id") or "")
        and str(record.get("summary") or "").strip()
    ]
    if not entries:
        return {"indexed": 0, "eligible": 0}
    rollups = {
        str(row.get("node_id") or ""): row
        for row in entries
        if str(row.get("node_type") or "") == "rollup"
    }
    for row in entries:
        if str(row.get("node_type") or "") != "section":
            continue
        child_ids = [
            str(value) for value in (row.get("child_node_ids") or []) if str(value)
        ]
        if len(child_ids) != 1 or child_ids[0] not in rollups:
            continue
        child = rollups[child_ids[0]]
        row["passthrough_rollup_id"] = child_ids[0]
        row["passthrough_parent_ids"] = list(child.get("parent_ids") or [])
    if db is not None:
        all_parent_ids = sorted(
            {
                str(parent_id)
                for row in rollups.values()
                for parent_id in (row.get("parent_ids") or [])
                if str(parent_id)
            }
        )
        all_parent_id_set = set(all_parent_ids)
        lexicon_rows = (
            await db["corpus_lexicon"]
            .find(
                {
                    "corpus_id": corpus_id,
                    "retrieval_eligible": {"$ne": False},
                    "source_parent_ids": {"$in": all_parent_ids},
                },
                {
                    "_id": 0,
                    "lexicon_id": 1,
                    "source_parent_ids": 1,
                    "support_count": 1,
                },
            )
            .limit(20_000)
            .to_list(length=20_000)
            if all_parent_ids
            else []
        )
        lexicon_by_parent: dict[str, list[tuple[int, str]]] = {}
        for lexicon_row in lexicon_rows:
            lexicon_id = str(lexicon_row.get("lexicon_id") or "")
            support = int(lexicon_row.get("support_count") or 0)
            if not lexicon_id:
                continue
            for parent_id in lexicon_row.get("source_parent_ids") or []:
                normalized_parent_id = str(parent_id or "")
                if normalized_parent_id in all_parent_id_set:
                    lexicon_by_parent.setdefault(normalized_parent_id, []).append(
                        (support, lexicon_id)
                    )
        lexicon_by_rollup: dict[str, list[str]] = {}
        for node_id, row in rollups.items():
            ranked = [
                value
                for parent_id in (row.get("parent_ids") or [])
                for value in lexicon_by_parent.get(str(parent_id), [])
            ]
            lexicon_by_rollup[node_id] = list(
                dict.fromkeys(
                    lexicon_id
                    for _support, lexicon_id in sorted(
                        ranked,
                        key=lambda item: (-item[0], item[1]),
                    )
                )
            )[:96]
            row["lexicon_ids"] = lexicon_by_rollup[node_id]
        for row in entries:
            if str(row.get("node_type") or "") != "section":
                continue
            row["lexicon_ids"] = list(
                dict.fromkeys(
                    lexicon_id
                    for child_id in (row.get("child_node_ids") or [])
                    for lexicon_id in lexicon_by_rollup.get(str(child_id), [])
                )
            )[:96]
            if row.get("passthrough_rollup_id"):
                row["passthrough_lexicon_ids"] = list(row.get("lexicon_ids") or [])
    config = _config_dict(embedding_config)
    embedding_call = embed_fn or embed_documents
    batch_size = max(1, min(int(batch_size or 128), 256))
    embedded_vectors: list[list[float]] = []
    for start in range(0, len(entries), batch_size):
        batch = entries[start : start + batch_size]
        texts = [
            " ".join(
                part
                for part in (
                    str(row.get("section_range") or "").strip(),
                    str(row.get("summary") or "").strip(),
                )
                if part
            )[:3000]
            for row in batch
        ]
        vectors = await embedding_call(texts, config)
        if len(vectors) != len(batch):
            raise RuntimeError(
                "summary-tree embedding returned "
                f"{len(vectors)} vectors for {len(batch)} nodes"
            )
        embedded_vectors.extend(vectors)
    for doc_id in sorted(
        {str(row.get("doc_id") or "") for row in entries if row.get("doc_id")}
    ):
        await delete_summary_tree_entries(
            qdrant_client,
            corpus_id,
            doc_id=doc_id,
        )
    indexed = 0
    for start in range(0, len(entries), batch_size):
        indexed += await upsert_summary_tree_entries(
            qdrant_client,
            corpus_id,
            entries[start : start + batch_size],
            embedded_vectors[start : start + batch_size],
        )
    return {"indexed": indexed, "eligible": len(entries)}


# ── pure structure ──────────────────────────────────────────────────────────
_PAGE_HEADING_RE = re.compile(
    r"^(?:page|p\.?)[\s_-]*(?:\d+|[ivxlcdm]+)(?:\s*(?:of|/)\s*\d+)?$",
    re.IGNORECASE,
)


def _top_heading(p: ParentSummaryIn) -> str:
    return p.heading_path[0] if p.heading_path else "(untitled)"


def _is_page_heading(value: str) -> bool:
    return bool(_PAGE_HEADING_RE.fullmatch(" ".join(str(value or "").split())))


def group_by_section(
    parents: Sequence[ParentSummaryIn],
) -> list[tuple[str, list[ParentSummaryIn]]]:
    """Group CONSECUTIVE parents by top-level heading (document order kept —
    a heading that reappears later starts a new group, preserving structure)."""
    groups: list[tuple[str, list[ParentSummaryIn]]] = []
    active_heading = "(untitled)"
    for p in parents:
        h = _top_heading(p)
        # Parser-emitted page labels are pagination, not semantic structure.
        # Inherit the last real heading so one PDF does not become thousands of
        # one-rollup sections.
        if _is_page_heading(h):
            h = active_heading
        else:
            active_heading = h
        if groups and groups[-1][0] == h:
            groups[-1][1].append(p)
        else:
            groups.append((h, [p]))
    return groups


def windows(
    items: Sequence[ParentSummaryIn],
    lo: int = ROLLUP_WINDOW_MIN,
    hi: int = ROLLUP_WINDOW_MAX,
) -> list[list[ParentSummaryIn]]:
    """Split a section's parents into rollup windows of lo..hi, deterministic:
    equal-ish sizes, never below lo unless the whole section is smaller."""
    n = len(items)
    if n == 0:
        return []
    if n <= hi:
        return [list(items)]
    count = (n + hi - 1) // hi  # fewest windows within hi
    base, extra = divmod(n, count)
    out, i = [], 0
    for w in range(count):
        size = base + (1 if w < extra else 0)
        out.append(list(items[i : i + size]))
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
        lines.append(
            "Detected domains: "
            + ", ".join(
                f"{d}({c})"
                for d, c in sorted(domains.items(), key=lambda kv: -kv[1])[:6]
            )
        )
    if concepts:
        lines.append(
            "Top concepts: " + ", ".join(top_terms(concepts, PROFILE_MAX_CONCEPTS))
        )
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
        except Exception as exc:
            from services.ingestion.summary_cost_control import SummaryCostError

            if isinstance(exc, SummaryCostError):
                raise
            pass  # best-effort — deterministic fallback below
    return _extractive_fallback(fallback_texts)


async def _gather_generation(coroutines: Sequence[Awaitable[str]]) -> list[str]:
    """Cancel sibling provider work immediately when the cost guard stops."""

    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    if not tasks:
        return []
    try:
        return await asyncio.gather(*tasks)
    except Exception as exc:
        from services.ingestion.summary_cost_control import SummaryCostError

        if isinstance(exc, SummaryCostError):
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def build_tree(
    *,
    doc_id: str,
    corpus_id: str,
    title: str,
    source_type: str,
    parents: Sequence[ParentSummaryIn],
    llm_fn: LlmFn | None,
    max_concurrent: int = 16,
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

    generation_semaphore = asyncio.Semaphore(max(1, min(int(max_concurrent or 1), 64)))

    async def _bounded_gen(prompt: str, fallback_texts: Sequence[str]) -> str:
        async with generation_semaphore:
            return await _gen(llm_fn, prompt, fallback_texts)

    sections: list[TreeNode] = []
    section_rollups: list[list[TreeNode]] = []
    rollup_generators: list[Awaitable[str]] = []
    r_idx = 0
    for s_idx, (heading, members) in enumerate(group_by_section(usable)):
        rollups: list[TreeNode] = []
        for win in windows(members):
            node = TreeNode(
                node_id=f"rollup_{doc_id[:12]}_{r_idx:04d}",
                node_type="rollup",
                doc_id=doc_id,
                corpus_id=corpus_id,
                parent_ids=[p.parent_id for p in win],
                section_range=heading,
                concepts=derive_node_concepts(_child_concept_rows(win)),
            )
            r_idx += 1
            body = "\n".join(f"- {p.summary}" for p in win)
            rollup_generators.append(
                _bounded_gen(
                    _ROLLUP_PROMPT.format(body=body),
                    [p.summary for p in win],
                )
            )
            rollups.append(node)
        sec = TreeNode(
            node_id=f"section_{doc_id[:12]}_{s_idx:04d}",
            node_type="section",
            doc_id=doc_id,
            corpus_id=corpus_id,
            child_node_ids=[r.node_id for r in rollups],
            section_range=heading,
            concepts=derive_node_concepts(_child_concept_rows(rollups)),
        )
        nodes.extend(rollups)
        sections.append(sec)
        section_rollups.append(rollups)

    rollup_summaries = await _gather_generation(rollup_generators)
    for node, summary in zip(
        (node for rollups in section_rollups for node in rollups),
        rollup_summaries,
        strict=True,
    ):
        node.summary = summary

    section_generators: list[Awaitable[str]] = []
    generated_section_indexes: list[int] = []
    for section_index, (section, rollups) in enumerate(
        zip(sections, section_rollups, strict=True)
    ):
        if len(rollups) == 1:
            section.summary = rollups[0].summary
            continue
        body = "\n".join(f"- {rollup.summary}" for rollup in rollups)
        generated_section_indexes.append(section_index)
        section_generators.append(
            _bounded_gen(
                _SECTION_PROMPT.format(body=body),
                [rollup.summary for rollup in rollups],
            )
        )
    section_summaries = await _gather_generation(section_generators)
    for section_index, summary in zip(
        generated_section_indexes,
        section_summaries,
        strict=True,
    ):
        sections[section_index].summary = summary
    nodes.extend(sections)

    profile = TreeNode(
        node_id=f"docsum_{doc_id[:12]}",
        node_type="document",
        doc_id=doc_id,
        corpus_id=corpus_id,
        child_node_ids=[s.node_id for s in sections],
        section_range=title,
        domains=domains,
        concepts=derive_node_concepts(_child_concept_rows(sections)),
    )
    profile.summary = await _gen(
        llm_fn,
        _PROFILE_PROMPT.format(
            body=build_profile_input(title, source_type, sections, domains, concepts)
        ),
        [s.summary for s in sections],
    )
    nodes.append(profile)
    return nodes


# ── persistence + ingest hook (PARENT summaries in → DOCUMENT profile out) ──
async def _attach_doc_artifact(
    db: Any,
    *,
    corpus_id: str,
    doc_id: str,
    doc: dict[str, Any],
    rows: Sequence[dict[str, Any]],
    doc_profile: dict[str, Any],
) -> dict[str, Any]:
    """Add deterministic routing metadata to an existing document profile."""

    try:
        from services.ingestion.doc_artifact import build_doc_artifact

        corpus_doc = await db["corpora"].find_one(
            {"corpus_id": corpus_id},
            {"_id": 0, "description": 1},
        )
        ghost_rows = (
            await db["ghost_b_extractions"]
            .find(
                {"doc_id": doc_id, "corpus_id": corpus_id, "status": "ok"},
                {"_id": 0, "entities": 1},
            )
            .limit(300)
            .to_list(length=300)
        )
        ghost_entities = [
            entity for row in ghost_rows for entity in (row.get("entities") or [])
        ]
        chunk_kind_stats: dict[str, int] = {}
        for row in rows:
            kind = str(row.get("chunk_kind") or "body")
            chunk_kind_stats[kind] = chunk_kind_stats.get(kind, 0) + 1
        existing_artifact = (doc.get("doc_profile") or {}).get("doc_artifact") or {}
        artifact = build_doc_artifact(
            doc_profile=doc_profile,
            facet_profile=doc.get("facet_profile") or {},
            source_meta={
                "title": doc.get("title"),
                "filename": doc.get("filename"),
                "source_type": doc.get("source_type"),
                "source_path": doc.get("source_path"),
            },
            ghost_b_entities=ghost_entities,
            chunk_kind_stats=chunk_kind_stats,
            owner_fields=existing_artifact,
            corpus_description=(corpus_doc or {}).get("description"),
        )
        if artifact:
            doc_profile["doc_artifact"] = artifact
    except Exception:
        pass
    return doc_profile


async def sync_document_profile_from_existing_tree(
    *,
    db: Any,
    doc_id: str,
    corpus_id: str,
    force: bool = False,
) -> dict[str, Any]:
    """Restore a missing profile from its durable L4 node without inference."""

    from datetime import datetime

    doc = await db["documents"].find_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {
            "title": 1,
            "filename": 1,
            "source_type": 1,
            "source_path": 1,
            "facet_profile": 1,
            "doc_profile": 1,
        },
    )
    if not doc:
        return {"status": "no_document", "doc_id": doc_id}
    root = await db["summary_tree"].find_one(
        {
            "doc_id": doc_id,
            "corpus_id": corpus_id,
            "node_type": "document",
            "summary": {"$exists": True, "$nin": [None, ""]},
        },
        {"_id": 0},
        sort=[("updated_at", -1)],
    )
    if not root:
        return {"status": "no_tree", "doc_id": doc_id}
    if str((doc.get("doc_profile") or {}).get("summary") or "").strip() and not force:
        return {
            "status": "already_synced",
            "doc_id": doc_id,
            "node_id": root.get("node_id"),
        }

    rows = (
        await db["parent_chunks"]
        .find(
            {"doc_id": doc_id, "corpus_id": corpus_id},
            {"_id": 0, "chunk_kind": 1},
        )
        .to_list(length=None)
    )
    now = datetime.utcnow()
    doc_profile = {
        "summary_id": root.get("node_id"),
        "summary": str(root.get("summary") or ""),
        "concepts": root.get("concepts") or [],
        "domains": root.get("domains") or {},
        "section_ids": root.get("child_node_ids") or [],
        "schema_version": root.get("schema_version") or TREE_SCHEMA_VERSION,
        "updated_at": now,
    }
    doc_profile = await _attach_doc_artifact(
        db,
        corpus_id=corpus_id,
        doc_id=doc_id,
        doc=doc,
        rows=rows,
        doc_profile=doc_profile,
    )
    await db["documents"].update_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {"$set": {"doc_profile": doc_profile}},
    )
    return {
        "status": "synced",
        "doc_id": doc_id,
        "node_id": root.get("node_id"),
    }


async def build_and_store_tree(
    *,
    db,
    doc_id: str,
    corpus_id: str,
    llm_fn: LlmFn | None = None,
    use_llm: bool = True,
    heal_missing: bool = True,
    heal_limit: int = 2000,
    max_concurrent: int = 16,
    qdrant_client: Any | None = None,
    embedding_config: Any | None = None,
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
        {
            "title": 1,
            "filename": 1,
            "source_type": 1,
            "source_path": 1,
            "facet_profile": 1,
            "doc_profile": 1,
        },
    )
    if not doc:
        return {"skipped": "no_document"}
    rows = (
        await db["parent_chunks"]
        .find(
            {"doc_id": doc_id, "corpus_id": corpus_id},
            {
                "parent_id": 1,
                "summary": 1,
                "heading_path": 1,
                "domain": 1,
                "chunk_kind": 1,
                "text": 1,
                "child_ids": 1,
                "source_child_ids": 1,
                "key_terms": 1,
                "mechanisms": 1,
                "concept_tags": 1,
            },
        )
        .sort("parent_id", 1)
        .to_list(length=None)
    )  # {doc}_parent_NNNN → lexical = doc order
    summary_rows = [
        r for r in rows if should_summarize_parent(str(r.get("chunk_kind") or "body"))
    ]
    if use_llm and llm_fn is None:
        from services.ingestion.summary_cost_control import (
            SummaryCostAuthorityRequired,
        )

        raise SummaryCostAuthorityRequired(
            "summary-tree generation requires an injected cost-controlled LLM"
        )
    fn = llm_fn if use_llm else None

    # GUARD RAIL — the summarized-parent invariant. Ghost A is conditional
    # (global enabled flag + model pool); this backstop ensures each
    # retrieval-summary parent carries a summary before the tree builds.
    # Deterministic identification comes from section_classifier, bounded and
    # best-effort per parent, persisted so re-runs skip healed rows.
    healed = 0
    if fn is not None and heal_missing:
        for r in summary_rows:
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
                raw = (
                    await fn(
                        "Summarize and classify this passage. "
                        + SEMANTIC_SUMMARY_INSTRUCTION
                        + "\n\nsource_child_ids: "
                        + json.dumps(source_child_ids)
                        + "\n\nPASSAGE:\n"
                        + text
                    )
                    or ""
                ).strip()
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
                    "latent_concepts": sem.get("latent_concepts") or [],
                    "temporal_class": sem.get("temporal_class") or "unknown",
                    "time_expressions": sem.get("time_expressions") or [],
                    "source_child_ids": artifact["source_child_ids"]
                    or source_child_ids,
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
                # Mirror the persisted concept-source fields in-memory so a
                # just-healed parent contributes node concepts this same run.
                r["key_terms"] = update_fields["key_terms"]
                r["mechanisms"] = update_fields["mechanisms"]
                r["concept_tags"] = update_fields["concept_tags"]
                from models.contracts import ParentSummaryRecord, ParentSummaryWrite
                from services.storage.mongo_writer import write_parent_summaries

                topic_key = update_fields.pop("topic_key")
                write = ParentSummaryWrite(
                    parent_id=str(r["parent_id"]),
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                    record=ParentSummaryRecord.model_validate(update_fields),
                    summary_updated_at=datetime.now(timezone.utc),
                    source_text=str(r.get("text") or r.get("parent_text") or ""),
                )
                await write_parent_summaries(db, [write])
                if topic_key:
                    await db["parent_chunks"].update_one(
                        {
                            "corpus_id": corpus_id,
                            "doc_id": doc_id,
                            "parent_id": r["parent_id"],
                        },
                        {"$set": {"topic_key": topic_key}},
                    )
                healed += 1

    parents = [
        ParentSummaryIn(
            parent_id=str(r["parent_id"]),
            summary=str(r.get("summary") or ""),
            heading_path=tuple(r.get("heading_path") or ()),
            domain=str(r.get("domain") or ""),
            concepts=tuple(derive_node_concepts([r])),
        )
        for r in summary_rows
    ]
    if not any(p.summary for p in parents):
        return {
            "skipped": "no_parent_summaries",
            "parents": len(parents),
            "healed": healed,
        }
    nodes = await build_tree(
        doc_id=doc_id,
        corpus_id=corpus_id,
        title=str(doc.get("title") or doc.get("filename") or doc_id[:12]),
        source_type=str(doc.get("source_type") or ""),
        parents=parents,
        llm_fn=fn,
        max_concurrent=max_concurrent,
    )
    now = datetime.utcnow()
    for n in nodes:
        rec = asdict(n)
        rec["updated_at"] = now
        await db["summary_tree"].replace_one(
            {"corpus_id": corpus_id, "node_id": n.node_id},
            rec,
            upsert=True,
        )
    tree_index: dict[str, Any] = {"indexed": 0, "eligible": 0}
    if qdrant_client is not None:
        try:
            tree_index = await index_summary_tree_nodes(
                qdrant_client=qdrant_client,
                db=db,
                corpus_id=corpus_id,
                nodes=nodes,
                embedding_config=embedding_config,
            )
        except Exception as exc:  # noqa: BLE001 - summary artifacts remain durable
            tree_index = {
                "indexed": 0,
                "eligible": sum(
                    1 for node in nodes if node.node_type in {"section", "rollup"}
                ),
                "error": f"{type(exc).__name__}: {exc}"[:500],
            }
    profile = nodes[-1]
    doc_profile = {
        "summary_id": profile.node_id,
        "summary": profile.summary,
        "concepts": profile.concepts,
        "domains": profile.domains,
        "section_ids": profile.child_node_ids,
        "schema_version": profile.schema_version,
        "updated_at": now,
    }
    doc_profile = await _attach_doc_artifact(
        db,
        corpus_id=corpus_id,
        doc_id=doc_id,
        doc=doc,
        rows=rows,
        doc_profile=doc_profile,
    )

    document_updates: dict[str, Any] = {"doc_profile": doc_profile}
    if qdrant_client is not None:
        index_ready = bool(
            int(tree_index.get("eligible") or 0) > 0
            and int(tree_index.get("indexed") or 0)
            == int(tree_index.get("eligible") or 0)
            and not tree_index.get("error")
        )
        document_updates.update(
            {
                "summary_tree_index_state": (
                    "summary_tree_index_ready"
                    if index_ready
                    else "summary_tree_index_pending"
                ),
                "summary_tree_indexed_nodes": int(tree_index.get("indexed") or 0),
                "summary_tree_index_eligible_nodes": int(
                    tree_index.get("eligible") or 0
                ),
                "summary_tree_index_updated_at": now,
            }
        )
        if tree_index.get("error"):
            document_updates["summary_tree_index_error"] = str(tree_index["error"])[
                :500
            ]
    update: dict[str, Any] = {"$set": document_updates}
    if qdrant_client is not None and not tree_index.get("error"):
        update["$unset"] = {"summary_tree_index_error": ""}
    await db["documents"].update_one(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        update,
    )
    counts: dict[str, Any] = {}
    for n in nodes:
        counts[n.node_type] = counts.get(n.node_type, 0) + 1
    counts["parents_in"] = len(parents)
    counts["summaries_healed"] = healed
    counts["tree_index"] = tree_index
    return counts
