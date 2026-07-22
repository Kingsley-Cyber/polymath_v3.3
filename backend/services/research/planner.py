"""Deterministic research question planner."""

from __future__ import annotations

import re
from typing import Any

from models.research import ResearchBudgets


def _compact_text(text: str) -> str:
    return " ".join(str(text or "").split()).strip()


def _keywords(question: str, limit: int = 5) -> list[str]:
    stop = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "this",
        "to",
        "what",
        "when",
        "where",
        "why",
        "with",
    }
    seen: set[str] = set()
    out: list[str] = []
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9_/-]{2,}", question.lower()):
        if raw in stop or raw in seen:
            continue
        seen.add(raw)
        out.append(raw)
        if len(out) >= limit:
            break
    return out


def plan_subquestions(
    question: str,
    budgets: ResearchBudgets,
) -> list[dict[str, Any]]:
    """Create bounded research workers from a user question.

    This intentionally starts deterministic. A later planner lane can replace
    the heuristics with an LLM rewrite, but the control plane should not depend
    on a model just to produce a safe first artifact.
    """
    root = _compact_text(question)
    terms = _keywords(root)
    term_text = ", ".join(terms) if terms else root
    candidates = [
        (root, "answer"),
        (f"What evidence directly supports or challenges: {root}", "evidence"),
        (f"What entities, mechanisms, and relationships matter for: {term_text}", "graph"),
        (f"What gaps, caveats, or conflicts appear around: {root}", "gaps"),
        (f"What practical implications follow from: {root}", "implications"),
    ]
    planned: list[dict[str, Any]] = []
    for index, (subquestion, purpose) in enumerate(candidates[: budgets.max_subquestions], 1):
        planned.append(
            {
                "id": f"sq{index}",
                "question": subquestion,
                "purpose": purpose,
            }
        )
    return planned
