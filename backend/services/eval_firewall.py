"""Held-out evaluation contamination firewall (checklist P1.1).

The held-out suite in ``backend/evals/heldout_questions.jsonl`` measures
retrieval quality. Its questions must NEVER leak into production
representations: attested-query harvesting, generated user-language
representations, alias/lexicon curation, or prompt seeds. Any pipeline that
persists query-derived artifacts MUST call :func:`is_heldout_query` and skip
matches.

The frozen hashes live in ``backend/evals/heldout_hashes.json`` (written by
``backend/scripts/freeze_heldout_eval.py``). Hashing is over a normalized
form (lowercase, collapsed whitespace, stripped terminal punctuation), so
trivial reformatting cannot bypass the firewall.
"""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

_HASHES_PATH = Path(__file__).resolve().parents[1] / "evals" / "heldout_hashes.json"
_WS = re.compile(r"\s+")


def normalize_eval_query(text: str) -> str:
    normalized = _WS.sub(" ", str(text or "").strip().lower()).strip()
    return normalized.rstrip("?.! ")


def heldout_query_hash(text: str) -> str:
    return hashlib.sha256(normalize_eval_query(text).encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def _frozen_hashes() -> frozenset[str]:
    try:
        payload = json.loads(_HASHES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset()
    return frozenset(str(h) for h in payload.get("hashes") or [])


def is_heldout_query(text: str) -> bool:
    """True when the text matches a frozen held-out evaluation question."""

    hashes = _frozen_hashes()
    if not hashes:
        return False
    return heldout_query_hash(text) in hashes


def firewall_active() -> bool:
    """True when frozen hashes are present (harvesting may proceed only with
    this returning True and per-query is_heldout_query checks in place)."""

    return bool(_frozen_hashes())
