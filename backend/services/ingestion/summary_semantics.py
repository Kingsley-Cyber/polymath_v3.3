"""§10.1 — the semantic parent-summary contract (POLYMATH_ARCHITECTURE §10.1).

ONE implementation of the prompt + defensive parse, shared by Ghost A (live
ingest) and the summary-tree HEAL guard, so a parent summary has the same
structured shape no matter which path produced it:

    summary              prose gist (embeddable; the waterfall summary rung)
    summary_type         fixed: parent_retrieval_replacement
    key_points           retrieval-useful points with child evidence anchors
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
PARENT_SUMMARY_SCHEMA_VERSION = "parent_summary.v1"
PARENT_SUMMARY_TYPE = "parent_retrieval_replacement"
MAX_CONCEPT_TAGS = 8
MAX_KEY_POINTS = 5
ALLOWED_RETRIEVAL_USES = {
    "definition", "mechanism", "comparison", "example", "claim", "method",
    "cause_effect", "critique", "framework", "evidence", "synthesis",
}
_GENERIC_TAGS = {
    "content", "document", "example", "information", "knowledge", "section",
    "summary", "text", "topic",
}

SEMANTIC_SUMMARY_INSTRUCTION = (
    "Respond with ONLY a JSON object: "
    '{"summary": "<2-3 dense factual sentences preserving key terms and proper '
    'nouns; no information not in the passage>", '
    '"domain": "<one taxonomy value>", '
    '"semantic_chunk_type": "<one of: ' + "|".join(SEMANTIC_CHUNK_TYPES) + '>", '
    '"key_terms": ["<up to 8 proper nouns or defined terms that appear in the passage>"], '
    '"mechanisms": ["<up to 5 transferable mechanisms as snake_case, e.g. '
    'compounding, feedback_loop>"], '
    '"central_claim": "<one sentence under 30 words>", '
    '"key_points": [{"point": "<short retrieval-useful point>", '
    '"supporting_child_ids": ["<child id from source_child_ids>"]}], '
    '"main_mechanism": "<one sentence mechanism or null>", '
    '"concept_tags": ["<3-8 normalized concepts>"], '
    '"entity_hints": ["<explicit source entities only>"], '
    '"retrieval_uses": ["<definition|mechanism|comparison|example|claim|method|'
    'cause_effect|critique|framework|evidence|synthesis>"], '
    '"abstraction_level": "<low|medium|high>"}'
)


def _snake(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def _words(value: str) -> list[str]:
    return re.findall(r"\S+", value or "")


def _clip_words(value: str, limit: int) -> str:
    words = _words(value)
    if len(words) <= limit:
        return (value or "").strip()
    return " ".join(words[:limit]).rstrip(" ,;:") + "."


def _sentences(value: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", value or "") if s.strip()]


def _first_sentence(value: str, *, max_words: int) -> str:
    sentences = _sentences(value)
    first = sentences[0] if sentences else value
    return _clip_words(first, max_words)


def _as_list(value) -> list:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _normalize_child_ids(value, allowed: set[str]) -> list[str]:
    ids = [str(v).strip() for v in _as_list(value) if str(v).strip()]
    if allowed:
        ids = [v for v in ids if v in allowed]
    return list(dict.fromkeys(ids))


def _fallback_key_points(summary: str, source_child_ids: list[str]) -> list[dict]:
    ids = source_child_ids[:1]
    sentences = _sentences(summary)
    points = []
    for sentence in sentences[:3]:
        point = _clip_words(sentence, 24)
        if point:
            points.append({"point": point, "supporting_child_ids": ids})
    while len(points) < 3 and summary:
        points.append({
            "point": _clip_words(summary, 24),
            "supporting_child_ids": ids,
        })
    return points[:3]


def _normalize_key_points(value, *, summary: str, source_child_ids: list[str]) -> list[dict]:
    allowed = set(source_child_ids)
    points: list[dict] = []
    for item in _as_list(value):
        if not isinstance(item, dict):
            continue
        point = _clip_words(str(item.get("point") or "").strip(), 24)
        supporting = _normalize_child_ids(item.get("supporting_child_ids"), allowed)
        if point and supporting:
            points.append({"point": point, "supporting_child_ids": supporting})
        if len(points) >= MAX_KEY_POINTS:
            break
    if len(points) < 3:
        points.extend(_fallback_key_points(summary, source_child_ids))
    deduped = []
    seen = set()
    for point in points:
        key = point["point"].lower()
        if key not in seen:
            seen.add(key)
            deduped.append(point)
        if len(deduped) >= MAX_KEY_POINTS:
            break
    return deduped[:MAX_KEY_POINTS]


def _normalize_tags(*values) -> list[str]:
    tags: list[str] = []
    seen = set()
    for value in values:
        for item in _as_list(value):
            if isinstance(item, dict):
                continue
            text = " ".join(str(item).replace("_", " ").split()).strip().lower()
            if not text or text in _GENERIC_TAGS or len(text) < 3:
                continue
            if text not in seen:
                seen.add(text)
                tags.append(text)
            if len(tags) >= MAX_CONCEPT_TAGS:
                return tags
    return tags


def _normalize_entity_hints(value, source_text: str | None) -> list[str]:
    source_lower = (source_text or "").lower()
    hints: list[str] = []
    seen = set()
    for item in _as_list(value):
        text = " ".join(str(item).split()).strip()
        if not text or text.lower() in seen:
            continue
        if source_lower and text.lower() not in source_lower:
            continue
        seen.add(text.lower())
        hints.append(text)
        if len(hints) >= 10:
            break
    return hints


def _normalize_retrieval_uses(value, semantic_chunk_type: str | None) -> list[str]:
    uses: list[str] = []
    for item in _as_list(value):
        use = _snake(item)
        if use in ALLOWED_RETRIEVAL_USES and use not in uses:
            uses.append(use)
    semantic = _snake(semantic_chunk_type or "")
    if semantic in ALLOWED_RETRIEVAL_USES and semantic not in uses:
        uses.append(semantic)
    if not uses:
        uses.append("evidence")
    return uses[:4]


def parent_summary_artifact_fields(
    obj: dict,
    *,
    summary: str,
    domain: str | None = None,
    semantic_chunk_type: str | None = None,
    key_terms: list[str] | None = None,
    mechanisms: list[str] | None = None,
    source_child_ids: list[str] | None = None,
    source_text: str | None = None,
) -> dict:
    """Normalize LLM + deterministic parent-summary contract fields.

    This keeps the hot ingestion path best-effort while making the artifact
    shape explicit and queryable. IDs/timestamps stay owned by writer code.
    """
    source_child_ids = [str(v) for v in (source_child_ids or []) if str(v)]
    summary = _clip_words(summary, 180)
    central_claim = _clip_words(
        str(obj.get("central_claim") or "").strip() or _first_sentence(summary, max_words=30),
        30,
    )
    concepts = _normalize_tags(
        obj.get("concept_tags"),
        key_terms or [],
        mechanisms or [],
        domain,
        semantic_chunk_type,
    )
    if len(concepts) < 3:
        concepts = _normalize_tags(
            concepts,
            re.findall(r"[A-Za-z][A-Za-z0-9_+-]{3,}", summary)[:8],
        )[:MAX_CONCEPT_TAGS]
    key_points = _normalize_key_points(
        obj.get("key_points"),
        summary=summary,
        source_child_ids=source_child_ids,
    )
    return {
        "schema_version": PARENT_SUMMARY_SCHEMA_VERSION,
        "summary_type": PARENT_SUMMARY_TYPE,
        "central_claim": central_claim,
        "key_points": key_points,
        "main_mechanism": (
            _clip_words(str(obj.get("main_mechanism") or "").strip(), 30) or None
        ),
        "concept_tags": concepts[:MAX_CONCEPT_TAGS],
        "entity_hints": _normalize_entity_hints(obj.get("entity_hints") or key_terms or [], source_text),
        "retrieval_uses": _normalize_retrieval_uses(obj.get("retrieval_uses"), semantic_chunk_type),
        "abstraction_level": (
            obj.get("abstraction_level")
            if obj.get("abstraction_level") in {"low", "medium", "high"}
            else "medium"
        ),
        "source_child_ids": source_child_ids,
    }


def parse_semantic_summary(
    raw: str,
    *,
    source_child_ids: list[str] | None = None,
    source_text: str | None = None,
) -> dict:
    """Lenient parse → clamped semantic dict. A model that ignores the JSON
    instruction never breaks the pipeline: the whole string becomes `summary`
    and every structured field stays empty (untagged, never fabricated)."""
    out = {
        "summary": "",
        "domain": None,
        "semantic_chunk_type": None,
        "key_terms": [],
        "mechanisms": [],
        "schema_version": PARENT_SUMMARY_SCHEMA_VERSION,
        "summary_type": PARENT_SUMMARY_TYPE,
        "central_claim": "",
        "key_points": [],
        "main_mechanism": None,
        "concept_tags": [],
        "entity_hints": [],
        "retrieval_uses": [],
        "abstraction_level": "medium",
        "source_child_ids": source_child_ids or [],
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
                out.update(parent_summary_artifact_fields(
                    obj,
                    summary=out["summary"],
                    domain=out["domain"],
                    semantic_chunk_type=out["semantic_chunk_type"],
                    key_terms=out["key_terms"],
                    mechanisms=out["mechanisms"],
                    source_child_ids=source_child_ids,
                    source_text=source_text,
                ))
                return out
        except Exception:
            pass
    out["summary"] = text  # fallback: prose only, no fabricated structure
    out.update(parent_summary_artifact_fields(
        {},
        summary=out["summary"],
        source_child_ids=source_child_ids,
        source_text=source_text,
    ))
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
