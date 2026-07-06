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

import hashlib
import json
import re
from datetime import datetime, timezone

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
    "summary", "text", "topic", "passage", "paragraph", "chapter", "book",
    "article", "paper", "source", "material", "this", "that", "these",
    "those", "from", "with", "into", "about", "covers", "discusses",
    "provides", "describes", "explains", "includes", "overview",
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


def looks_like_raw_json_text(value: str | None) -> bool:
    """True when a generated field is really a leaked JSON object/fragment."""
    text = (value or "").strip()
    if not text:
        return False
    head = text[:800]
    if text[0] in "{[" and (":" in head or '"summary"' in head):
        return True
    if re.search(r'"\w[\w_]*"\s*:', head) and (
        '"summary"' in head or '"central_claim"' in head or '"key_points"' in head
    ):
        return True
    return False


def _clean_generated_text(value, *, max_words: int | None = None) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    if looks_like_raw_json_text(text):
        return ""
    if text.startswith("```") or text.lower().startswith(("json ", "here is", "here's")):
        return ""
    return _clip_words(text, max_words) if max_words else text


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
        point = _clean_generated_text(sentence, max_words=24)
        if point:
            points.append({"point": point, "supporting_child_ids": ids})
    while len(points) < 3 and summary:
        points.append({
            "point": _clean_generated_text(summary, max_words=24),
            "supporting_child_ids": ids,
        })
    return points[:3]


def _normalize_key_points(value, *, summary: str, source_child_ids: list[str]) -> list[dict]:
    allowed = set(source_child_ids)
    points: list[dict] = []
    for item in _as_list(value):
        if not isinstance(item, dict):
            continue
        point = _clean_generated_text(item.get("point"), max_words=24)
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
            if looks_like_raw_json_text(text):
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


def _json_string_field(text: str, field: str) -> str:
    match = re.search(
        rf'"{re.escape(field)}"\s*:\s*"((?:\\.|[^"\\])*)"',
        text,
        re.DOTALL,
    )
    if not match:
        return ""
    raw = match.group(1)
    try:
        return str(json.loads(f'"{raw}"'))
    except Exception:
        return raw.replace('\\"', '"').replace("\\n", "\n").strip()


def _json_string_array_field(text: str, field: str) -> list[str]:
    match = re.search(
        rf'"{re.escape(field)}"\s*:\s*\[(.*?)\]',
        text,
        re.DOTALL,
    )
    if not match:
        return []
    values: list[str] = []
    for item in re.findall(r'"((?:\\.|[^"\\])*)"', match.group(1)):
        try:
            value = str(json.loads(f'"{item}"')).strip()
        except Exception:
            value = item.replace('\\"', '"').replace("\\n", "\n").strip()
        if value:
            values.append(value)
    return values


def _salvage_json_fragment(text: str) -> dict:
    """Best-effort field extraction from truncated LLM JSON.

    This deliberately extracts only bounded LLM-owned fields. Deterministic
    IDs, timestamps, hashes, and storage metadata are attached later in code.
    """
    return {
        "summary": _json_string_field(text, "summary"),
        "domain": _json_string_field(text, "domain"),
        "semantic_chunk_type": _json_string_field(text, "semantic_chunk_type"),
        "key_terms": _json_string_array_field(text, "key_terms"),
        "mechanisms": _json_string_array_field(text, "mechanisms"),
        "central_claim": _json_string_field(text, "central_claim"),
        "main_mechanism": _json_string_field(text, "main_mechanism"),
        "concept_tags": _json_string_array_field(text, "concept_tags"),
        "entity_hints": _json_string_array_field(text, "entity_hints"),
        "retrieval_uses": _json_string_array_field(text, "retrieval_uses"),
        "abstraction_level": _json_string_field(text, "abstraction_level"),
    }


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
    summary = _clean_generated_text(summary, max_words=180)
    central_claim = _clip_words(
        _clean_generated_text(obj.get("central_claim"), max_words=30)
        or _first_sentence(summary, max_words=30),
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
            _clean_generated_text(obj.get("main_mechanism"), max_words=30) or None
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


def source_hash_for_text(source_text: str | None) -> str:
    return hashlib.sha256((source_text or "").encode("utf-8")).hexdigest()


def summary_id_for_parent(parent_id: str | None) -> str:
    digest = hashlib.sha256(str(parent_id or "").encode("utf-8")).hexdigest()[:24]
    return f"sum_parent_{digest}"


def _coerce_timestamp(value=None) -> str:
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    text = str(value or "").strip()
    if text:
        return text
    return datetime.now(timezone.utc).isoformat()


def summary_retrieval_text(fields: dict) -> str:
    parts: list[str] = []
    for key in ("central_claim", "summary", "main_mechanism"):
        value = _clean_generated_text(fields.get(key))
        if value:
            parts.append(value)
    points = []
    for item in _as_list(fields.get("key_points")):
        if isinstance(item, dict):
            point = _clean_generated_text(item.get("point"))
            if point:
                points.append(point)
    if points:
        parts.append("Key points: " + " ".join(points))
    tags = [
        str(t).strip()
        for t in _as_list(fields.get("concept_tags"))
        if str(t).strip()
    ]
    if tags:
        parts.append("Concepts: " + ", ".join(tags[:MAX_CONCEPT_TAGS]))
    uses = [
        str(u).strip()
        for u in _as_list(fields.get("retrieval_uses"))
        if str(u).strip()
    ]
    if uses:
        parts.append("Retrieval uses: " + ", ".join(uses[:4]))
    return "\n".join(dict.fromkeys(parts)).strip()


def _validate_summary_fields(fields: dict) -> tuple[str, list[str], float]:
    flags: list[str] = []
    summary = fields.get("summary") or ""
    central_claim = fields.get("central_claim") or ""
    source_child_ids = [str(v) for v in _as_list(fields.get("source_child_ids")) if str(v)]
    allowed = set(source_child_ids)

    if not summary:
        flags.append("missing_summary")
    elif looks_like_raw_json_text(summary):
        flags.append("raw_json_summary")
    word_count = len(_words(summary))
    if summary and word_count < 40:
        flags.append("summary_short")
    if summary and word_count > 180:
        flags.append("summary_long")
    if not central_claim:
        flags.append("missing_central_claim")
    elif looks_like_raw_json_text(central_claim):
        flags.append("raw_json_central_claim")
    if len(_words(central_claim)) > 30:
        flags.append("central_claim_long")

    valid_points = 0
    for item in _as_list(fields.get("key_points")):
        if not isinstance(item, dict):
            continue
        point = _clean_generated_text(item.get("point"))
        supporting = [str(v) for v in _as_list(item.get("supporting_child_ids")) if str(v)]
        if not point:
            continue
        if allowed and not all(v in allowed for v in supporting):
            flags.append("key_point_bad_child_anchor")
            continue
        if not supporting and allowed:
            flags.append("key_point_missing_child_anchor")
            continue
        valid_points += 1
    if valid_points == 0:
        flags.append("missing_key_points")

    tags = [str(v).strip() for v in _as_list(fields.get("concept_tags")) if str(v).strip()]
    if not tags:
        flags.append("missing_concept_tags")

    hard_flags = {
        "missing_summary",
        "raw_json_summary",
        "raw_json_central_claim",
        "missing_key_points",
    }
    status = "quarantined" if any(flag in hard_flags for flag in flags) else "valid"
    score = 1.0
    penalties = {
        "summary_short": 0.12,
        "summary_long": 0.12,
        "missing_central_claim": 0.18,
        "central_claim_long": 0.08,
        "missing_concept_tags": 0.10,
        "key_point_bad_child_anchor": 0.20,
        "key_point_missing_child_anchor": 0.12,
    }
    for flag in flags:
        score -= penalties.get(flag, 0.35 if flag in hard_flags else 0.05)
    return status, list(dict.fromkeys(flags)), max(0.0, round(score, 3))


def canonical_parent_summary_fields(
    parsed: dict,
    *,
    parent_id: str,
    doc_id: str,
    corpus_id: str,
    source_text: str | None,
    source_child_ids: list[str] | None,
    summary_model: str | None,
    summary_created_at=None,
    repair_status: str | None = None,
) -> dict:
    """Attach deterministic compiler-artifact metadata to parsed LLM fields."""
    source_child_ids = [
        str(v)
        for v in (source_child_ids or parsed.get("source_child_ids") or [])
        if str(v)
    ]
    summary = _clean_generated_text(parsed.get("summary"), max_words=180)
    normalized = parent_summary_artifact_fields(
        parsed,
        summary=summary,
        domain=parsed.get("domain"),
        semantic_chunk_type=parsed.get("semantic_chunk_type"),
        key_terms=parsed.get("key_terms") or [],
        mechanisms=parsed.get("mechanisms") or [],
        source_child_ids=source_child_ids,
        source_text=source_text,
    )
    fields = {
        **normalized,
        "summary_id": summary_id_for_parent(parent_id),
        "corpus_id": corpus_id,
        "doc_id": doc_id,
        "parent_id": parent_id,
        "source_hash": source_hash_for_text(source_text or ""),
        "summary": summary,
        "summary_model": summary_model or "unknown",
        "summary_created_at": _coerce_timestamp(summary_created_at),
        "repair_status": repair_status or parsed.get("repair_status") or "none",
    }
    status, flags, score = _validate_summary_fields(fields)
    if status == "quarantined":
        fields["repair_status"] = "quarantined"
    fields["validation_status"] = status
    fields["quality_flags"] = flags
    fields["quality_score"] = score
    fields["retrieval_text"] = summary_retrieval_text(fields) if status == "valid" else ""
    return fields


def repair_parent_summary_row(
    row: dict,
    *,
    default_summary_model: str = "legacy_unknown",
    now=None,
) -> dict:
    """Return canonical fields for an existing parent_chunks row.

    The caller decides whether to persist these fields. This function never
    trusts raw JSON-looking text as prose; it repairs what it can and marks the
    rest quarantined for regeneration.
    """
    source_child_ids = [
        str(v)
        for v in (row.get("source_child_ids") or row.get("child_ids") or [])
        if str(v)
    ]
    source_text = row.get("text") or row.get("parent_text") or ""
    summary = str(row.get("summary") or "").strip()
    needs_repair = any(
        [
            looks_like_raw_json_text(summary),
            looks_like_raw_json_text(row.get("central_claim")),
            not row.get("summary_model"),
            not row.get("source_hash"),
            not row.get("summary_created_at"),
            not row.get("retrieval_text"),
        ]
    )
    if looks_like_raw_json_text(summary):
        parsed = parse_semantic_summary(
            summary,
            source_child_ids=source_child_ids,
            source_text=source_text,
        )
    else:
        obj = {
            "summary": summary,
            "domain": row.get("domain"),
            "semantic_chunk_type": row.get("semantic_chunk_type"),
            "key_terms": row.get("key_terms") or [],
            "mechanisms": row.get("mechanisms") or [],
            "central_claim": row.get("central_claim"),
            "key_points": row.get("key_points") or [],
            "main_mechanism": row.get("main_mechanism"),
            "concept_tags": row.get("concept_tags") or [],
            "entity_hints": row.get("entity_hints") or [],
            "retrieval_uses": row.get("retrieval_uses") or [],
            "abstraction_level": row.get("abstraction_level") or "medium",
        }
        parsed = parse_semantic_summary(
            json.dumps(obj),
            source_child_ids=source_child_ids,
            source_text=source_text,
        )
        parsed["repair_status"] = "repaired" if needs_repair else (
            row.get("repair_status") or "none"
        )
    return canonical_parent_summary_fields(
        parsed,
        parent_id=str(row.get("parent_id") or ""),
        doc_id=str(row.get("doc_id") or ""),
        corpus_id=str(row.get("corpus_id") or ""),
        source_text=source_text,
        source_child_ids=source_child_ids,
        summary_model=row.get("summary_model") or default_summary_model,
        summary_created_at=row.get("summary_created_at") or now,
        repair_status=parsed.get("repair_status") or ("repaired" if needs_repair else "none"),
    )


def parse_semantic_summary(
    raw: str,
    *,
    source_child_ids: list[str] | None = None,
    source_text: str | None = None,
) -> dict:
    """Parse → clamped semantic dict.

    Plain prose can still become a fallback summary, but JSON-looking output
    must parse, be salvageable, or be quarantined. Raw JSON fragments are never
    trusted as summary prose.
    """
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
        "validation_status": "quarantined",
        "repair_status": "quarantined",
        "quality_flags": ["missing_summary"],
        "quality_score": 0.0,
        "retrieval_text": "",
    }
    text = (raw or "").strip()
    if not text:
        return out

    obj: dict | None = None
    parsed_json = False
    repaired_fragment = False
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            parsed_json = True
        except Exception:
            obj = _salvage_json_fragment(text) if looks_like_raw_json_text(text) else None
            repaired_fragment = bool(obj and obj.get("summary"))
    elif looks_like_raw_json_text(text):
        obj = _salvage_json_fragment(text)
        repaired_fragment = bool(obj and obj.get("summary"))

    if isinstance(obj, dict):
        summary = _clean_generated_text(obj.get("summary"), max_words=180)
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
            out["repair_status"] = "repaired" if repaired_fragment else "none"
            status, flags, score = _validate_summary_fields(out)
            out["validation_status"] = status
            out["quality_flags"] = flags
            out["quality_score"] = score
            out["retrieval_text"] = summary_retrieval_text(out) if status == "valid" else ""
            if status == "quarantined":
                out["repair_status"] = "quarantined"
            return out
        if parsed_json or repaired_fragment or looks_like_raw_json_text(text):
            out["repair_status"] = "quarantined"
            return out

    if looks_like_raw_json_text(text):
        out["repair_status"] = "quarantined"
        return out

    out["summary"] = _clean_generated_text(text, max_words=180)
    out.update(parent_summary_artifact_fields(
        {},
        summary=out["summary"],
        source_child_ids=source_child_ids,
        source_text=source_text,
    ))
    out["repair_status"] = "repaired"
    status, flags, score = _validate_summary_fields(out)
    out["validation_status"] = status
    out["quality_flags"] = flags
    out["quality_score"] = score
    out["retrieval_text"] = summary_retrieval_text(out) if status == "valid" else ""
    if status == "quarantined":
        out["repair_status"] = "quarantined"
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
