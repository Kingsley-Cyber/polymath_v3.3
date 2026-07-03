"""§10.1 — the semantic parent-summary contract (POLYMATH_ARCHITECTURE §10.1).

ONE implementation of the prompt + defensive parse, shared by Ghost A (live
ingest) and the summary-tree HEAL guard, so a parent summary has the same
structured shape no matter which path produced it:

    summary              prose gist (embeddable; the waterfall summary rung)
    semantic_chunk_type  closed enum (clamped; junk → "narrative")
    key_terms            <=8 proper nouns / defined terms FROM the passage
    mechanisms           <=5 transferable snake_case mechanisms
    topic_key            derived IN CODE (never by the LLM): {domain}.{heading slug}

Determinism guards: enum clamp, snake_case normalization, hard caps, and the
extractive fallback fills `summary` ONLY — structure is never fabricated.
"""

from __future__ import annotations

import json
import re

SEMANTIC_CHUNK_TYPES = (
    "definition", "claim", "procedure", "principle", "framework",
    "example", "comparison", "warning", "narrative",
)
MAX_KEY_TERMS = 8
MAX_MECHANISMS = 5

SEMANTIC_SUMMARY_INSTRUCTION = (
    "Respond with ONLY a JSON object: "
    '{"summary": "<2-3 dense factual sentences preserving key terms and proper '
    'nouns; no information not in the passage>", '
    '"domain": "<one taxonomy value>", '
    '"semantic_chunk_type": "<one of: ' + "|".join(SEMANTIC_CHUNK_TYPES) + '>", '
    '"key_terms": ["<up to 8 proper nouns or defined terms that appear in the passage>"], '
    '"mechanisms": ["<up to 5 transferable mechanisms as snake_case, e.g. '
    'compounding, feedback_loop>"]}'
)


def _snake(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def parse_semantic_summary(raw: str) -> dict:
    """Lenient parse → clamped semantic dict. A model that ignores the JSON
    instruction never breaks the pipeline: the whole string becomes `summary`
    and every structured field stays empty (untagged, never fabricated)."""
    out = {
        "summary": "",
        "domain": None,
        "semantic_chunk_type": None,
        "key_terms": [],
        "mechanisms": [],
    }
    text = (raw or "").strip()
    if not text:
        return out
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            summary = str(obj.get("summary") or "").strip()
            if summary:
                out["summary"] = summary
                dom = _snake(str(obj.get("domain") or ""))
                out["domain"] = dom or None
                sct = _snake(str(obj.get("semantic_chunk_type") or ""))
                if sct:
                    out["semantic_chunk_type"] = (
                        sct if sct in SEMANTIC_CHUNK_TYPES else "narrative"
                    )
                seen: set[str] = set()
                for t in obj.get("key_terms") or []:
                    s = " ".join(str(t).split()).strip()
                    if s and s.lower() not in seen and len(s) <= 80:
                        seen.add(s.lower())
                        out["key_terms"].append(s)
                        if len(out["key_terms"]) >= MAX_KEY_TERMS:
                            break
                mseen: set[str] = set()
                for m in obj.get("mechanisms") or []:
                    s = _snake(m)
                    if s and s not in mseen and len(s) <= 60:
                        mseen.add(s)
                        out["mechanisms"].append(s)
                        if len(out["mechanisms"]) >= MAX_MECHANISMS:
                            break
                return out
        except Exception:
            pass
    out["summary"] = text  # fallback: prose only, no fabricated structure
    return out


def topic_key_for(domain: str | None, heading_path) -> str | None:
    """Deterministic topic_key = {domain}.{slug(top heading)} — computed in
    code per §10.1, never emitted by the LLM."""
    dom = _snake(domain or "")
    head = ""
    if heading_path:
        head = _snake(heading_path[0] if isinstance(heading_path, (list, tuple)) else heading_path)
    if dom and head:
        return f"{dom}.{head}"
    return dom or head or None
