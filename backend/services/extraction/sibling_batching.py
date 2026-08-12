"""Sibling-batched extraction — parent-scope prompts, child-scope artifacts.

Owner ruling 2026-08-12. Measured problem: extraction ran one LLM call per
CHILD chunk (median 86 tokens of content) against a ~1,600-token instruction
prompt, so the pipeline paid ~8 output tokens per token of content and made
176,219 calls for one corpus. Worse, a relation whose subject and object sat
in different children was UNEXTRACTABLE at any model size — the measured
recall gap in the qualification battery.

This module batches siblings (children sharing a parent_id) into ONE prompt
and then splits the response back into per-child artifacts by matching each
item's verbatim span (``evidence_phrase`` for relations/facts, ``surface_form``
for entities) against the child texts.

Why this shape instead of parent-level JOBS: the job model, contract hashes,
terminal-artifact matching, readiness accounting, promotion, and retrieval
provenance all key on child ``chunk_id``. Keeping one artifact per child
leaves every one of those seams untouched — the batching lives entirely in
the execution path. Blast radius is this file plus one call site.

Guarantees:
  - every child in a group receives exactly one result (possibly empty), so
    job flips and coverage accounting stay 1:1 with chunks;
  - concatenation preserves each child's text verbatim, so evidence-phrase
    matching is exact, not fuzzy;
  - items that match no child fall back to the group anchor (first child),
    so nothing extracted is ever dropped.
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable, Sequence

# A group is prompted as one unit; these bound its size so a group never
# overruns the extraction context or the per-call output budget.
DEFAULT_MAX_GROUP_TOKENS = 1600
DEFAULT_MAX_GROUP_CHILDREN = 12
_JOIN = "\n\n"


def batching_enabled() -> bool:
    raw = os.environ.get("EXTRACTION_SIBLING_BATCHING", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _limits() -> tuple[int, int]:
    def _int(name: str, default: int) -> int:
        try:
            return max(1, int(os.environ.get(name, "") or default))
        except ValueError:
            return default

    return (
        _int("EXTRACTION_SIBLING_GROUP_MAX_TOKENS", DEFAULT_MAX_GROUP_TOKENS),
        _int("EXTRACTION_SIBLING_GROUP_MAX_CHILDREN", DEFAULT_MAX_GROUP_CHILDREN),
    )


def _approx_tokens(text: str) -> int:
    # Deliberately cheap: grouping only needs a bound, and the real budget is
    # enforced downstream by _context_bounded_completion_tokens.
    return max(1, len(text) // 4)


def _parent_key(task: Any) -> str:
    meta = getattr(task, "metadata", None) or {}
    parent = str(meta.get("parent_id") or "").strip()
    if parent:
        return parent
    # No parent recorded: give the task its own group so behavior degrades to
    # exactly the per-child path rather than merging unrelated text.
    return f"__solo__:{getattr(task, 'chunk_id', '')}"


def group_sibling_tasks(tasks: Sequence[Any]) -> list[list[Any]]:
    """Group tasks by parent_id, bounded by token and child-count limits.

    Order is preserved so concatenated text reads in document order, which
    matters for the model's ability to resolve cross-sentence references.
    """
    max_tokens, max_children = _limits()
    by_parent: dict[str, list[Any]] = {}
    order: list[str] = []
    for task in tasks:
        key = _parent_key(task)
        if key not in by_parent:
            by_parent[key] = []
            order.append(key)
        by_parent[key].append(task)

    groups: list[list[Any]] = []
    for key in order:
        current: list[Any] = []
        current_tokens = 0
        for task in by_parent[key]:
            tokens = _approx_tokens(str(getattr(task, "text", "") or ""))
            too_many = len(current) >= max_children
            too_big = current and (current_tokens + tokens) > max_tokens
            if too_many or too_big:
                groups.append(current)
                current, current_tokens = [], 0
            current.append(task)
            current_tokens += tokens
        if current:
            groups.append(current)
    return groups


def merge_group(group: Sequence[Any], task_cls: Any) -> Any:
    """Build the single prompted task for a group (anchor = first child)."""
    anchor = group[0]
    text = _JOIN.join(str(getattr(t, "text", "") or "") for t in group)
    meta = dict(getattr(anchor, "metadata", None) or {})
    meta["sibling_group_chunk_ids"] = [str(getattr(t, "chunk_id", "")) for t in group]
    return task_cls(
        chunk_id=str(getattr(anchor, "chunk_id", "")),
        doc_id=str(getattr(anchor, "doc_id", "")),
        corpus_id=str(getattr(anchor, "corpus_id", "")),
        text=text,
        chunk_kind=str(getattr(anchor, "chunk_kind", "") or "body"),
        metadata=meta,
    )


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _owner_index(spans: Iterable[str], child_texts: list[str]) -> int | None:
    """First child whose text contains any of the given verbatim spans."""
    for span in spans:
        needle = _norm(span)
        if len(needle) < 3:
            continue
        for idx, haystack in enumerate(child_texts):
            if needle in haystack:
                return idx
    return None


def split_result(result: Any, group: Sequence[Any], result_cls: Any) -> list[Any]:
    """Split one group result into one result per child.

    Attribution is by verbatim span: relations and facts follow their
    ``evidence_phrase`` (which the schema requires to be an exact quote from
    the prompted text), entities follow ``surface_form`` then
    ``canonical_name``. Unmatched items go to the anchor so nothing is lost.
    """
    child_texts = [_norm(str(getattr(t, "text", "") or "")) for t in group]
    buckets: list[dict[str, list[Any]]] = [
        {"entities": [], "relations": [], "facts": []} for _ in group
    ]

    for entity in list(getattr(result, "entities", None) or []):
        idx = _owner_index(
            (
                getattr(entity, "surface_form", "") or "",
                getattr(entity, "canonical_name", "") or "",
            ),
            child_texts,
        )
        buckets[idx if idx is not None else 0]["entities"].append(entity)

    for relation in list(getattr(result, "relations", None) or []):
        idx = _owner_index(
            (
                getattr(relation, "evidence_phrase", "") or "",
                getattr(relation, "relation_cue", "") or "",
            ),
            child_texts,
        )
        buckets[idx if idx is not None else 0]["relations"].append(relation)

    for fact in list(getattr(result, "facts", None) or []):
        idx = _owner_index(
            (getattr(fact, "evidence_phrase", "") or "",), child_texts
        )
        buckets[idx if idx is not None else 0]["facts"].append(fact)

    out: list[Any] = []
    for child, bucket in zip(group, buckets):
        out.append(
            result_cls(
                schema_version=getattr(result, "schema_version", ""),
                chunk_id=str(getattr(child, "chunk_id", "")),
                doc_id=str(getattr(child, "doc_id", "")),
                corpus_id=str(getattr(child, "corpus_id", "")),
                entities=bucket["entities"],
                relations=bucket["relations"],
                facts=bucket["facts"],
                text=str(getattr(child, "text", "") or ""),
            )
        )
    return out


def expand_report(
    report: Any,
    groups: Sequence[Sequence[Any]],
    *,
    result_cls: Any,
    failure_cls: Any | None = None,
) -> tuple[list[Any], list[Any]]:
    """Map a batch report over merged tasks back to per-child results.

    A group's failure fans out to every child in that group, so a failed
    group leaves no child silently unaccounted for.
    """
    group_by_anchor: dict[str, Sequence[Any]] = {
        str(getattr(g[0], "chunk_id", "")): g for g in groups
    }
    results: list[Any] = []
    for result in list(getattr(report, "results", None) or []):
        group = group_by_anchor.get(str(getattr(result, "chunk_id", "")))
        if not group or len(group) == 1:
            results.append(result)
            continue
        results.extend(split_result(result, group, result_cls))

    failures: list[Any] = []
    for failure in list(getattr(report, "failures", None) or []):
        anchor_id = str(getattr(failure, "chunk_id", ""))
        group = group_by_anchor.get(anchor_id)
        if not group or len(group) == 1 or failure_cls is None:
            failures.append(failure)
            continue
        for child in group:
            child_id = str(getattr(child, "chunk_id", ""))
            if child_id == anchor_id:
                failures.append(failure)
                continue
            try:
                clone = failure_cls(
                    **{
                        **{
                            f: getattr(failure, f)
                            for f in getattr(failure, "__dataclass_fields__", {})
                        },
                        "chunk_id": child_id,
                    }
                )
            except Exception:  # noqa: BLE001 — never lose a failure record
                clone = failure
            failures.append(clone)
    return results, failures
