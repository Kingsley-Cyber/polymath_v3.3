"""Entity label quality gates for graph storage and analytics.

This module does not decide whether Ghost B is allowed to extract an entity.
It classifies whether a persisted label is safe to use as a topic label,
synthesis anchor, or graph insight candidate. Low-quality labels stay in Neo4j
for provenance; analytics can filter them out of headline/topology surfaces.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

ENTITY_QUALITY_VERSION = "2026-04-30-v1"

LABEL_CLEAN = "clean"
LABEL_TITLE = "title"
LABEL_GENERIC_ROLE = "generic_role"
LABEL_CLAIM_LIKE = "claim_like"
LABEL_JOINED_LIST = "joined_list"
LABEL_CODE_LIKE = "code_like"
LABEL_NUMERIC_OR_DATE = "numeric_or_date"
LABEL_NOISY = "noisy"

INELIGIBLE_TOPIC_LABEL_QUALITIES = {
    LABEL_CLAIM_LIKE,
    LABEL_JOINED_LIST,
    LABEL_CODE_LIKE,
    LABEL_NUMERIC_OR_DATE,
    LABEL_NOISY,
    LABEL_GENERIC_ROLE,
}
INELIGIBLE_SYNTHESIS_QUALITIES = {
    LABEL_CLAIM_LIKE,
    LABEL_JOINED_LIST,
    LABEL_NUMERIC_OR_DATE,
    LABEL_NOISY,
    LABEL_GENERIC_ROLE,
}

_DOCUMENT_TYPES = {"document"}
_TITLE_TYPES = {"document", "rule", "law"}
_GENERIC_ROLE_LABELS = {
    "user",
    "users",
    "participant",
    "participants",
    "subject",
    "subjects",
    "respondent",
    "respondents",
    "speaker",
    "speakers",
    "listener",
    "listeners",
    "reader",
    "readers",
    "patient",
    "patients",
    "client",
    "clients",
    "student",
    "students",
    "young woman",
    "young man",
    "woman",
    "man",
    "person",
    "people",
    "child",
    "children",
    "adult",
    "adults",
}
_GENERIC_ROLE_SUFFIXES = (
    " users",
    " participants",
    " subjects",
    " respondents",
    " patients",
    " clients",
    " students",
    " speakers",
    " listeners",
)
_ORG_AMPERSAND_HINTS = (
    " & co",
    " & company",
    " & sons",
    " & schuster",
    " & francis",
    "a&m",
    "at&t",
)
_CLAIM_CUE_RE = re.compile(
    r"\b(that|because|when|while|where|which|who|whose|whom|unless|although|"
    r"though|if|therefore|whereas|whereby|whether|should|would|could|must|"
    r"cannot|can't|won't|is not|are not|was not|were not)\b",
    re.I,
)
_PRONOUN_LED_RE = re.compile(
    r"^(it|this|that|these|those|he|she|they|we|you|i|there)\b", re.I
)
_GERUND_CLAIM_RE = re.compile(r"^(using|sitting|knowing|doing|being|having|making)\b", re.I)
_NUMERIC_OR_DATE_RE = re.compile(
    r"^(\d+|\d{1,4}[-/]\d{1,2}(?:[-/]\d{1,2})?|(?:19|20)\d{2}[a-z]?)$",
    re.I,
)
_CODE_LIKE_RE = re.compile(
    r"(\bstd::|::|->|=>|\b(const|constexpr|void|class|struct|def|function|"
    r"return|public|private|protected)\b|\w+\s*\([^)]*\)|<[^>]+>|[{};])",
    re.I,
)


@dataclass(frozen=True)
class EntityQuality:
    label_quality: str
    eligible_for_topic_label: bool
    eligible_for_synthesis: bool
    quality_reasons: list[str]
    entity_quality_version: str = ENTITY_QUALITY_VERSION


def _words(label: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)?", label)


def _normalized(label: str) -> str:
    return re.sub(r"\s+", " ", label.strip()).lower()


def _looks_like_joined_list(label: str, entity_type: str) -> bool:
    normalized = _normalized(label)
    if not normalized:
        return False
    if entity_type == "organization" and any(hint in normalized for hint in _ORG_AMPERSAND_HINTS):
        return False
    slash_count = normalized.count("/") + normalized.count("|")
    comma_count = normalized.count(",")
    ampersand_count = normalized.count("&")
    if slash_count >= 1 and len(_words(label)) >= 4:
        return True
    if comma_count >= 2 and entity_type not in _TITLE_TYPES:
        return True
    if ampersand_count >= 1 and len(_words(label)) >= 5 and entity_type not in _TITLE_TYPES:
        return True
    return False


def _looks_like_claim(label: str, entity_type: str, word_count: int) -> bool:
    if entity_type in _TITLE_TYPES:
        return False
    if word_count < 6:
        return False
    if _CLAIM_CUE_RE.search(label):
        return True
    if _PRONOUN_LED_RE.search(label) and word_count >= 4:
        return True
    if _GERUND_CLAIM_RE.search(label) and word_count >= 6:
        return True
    if "," in label and word_count >= 12:
        return True
    return False


def classify_entity_label(
    label: str | None,
    entity_type: str | None,
    *,
    observed_entity_types: list[str] | None = None,
) -> EntityQuality:
    raw = str(label or "").strip()
    kind = str(entity_type or "").strip().lower()
    observed = {str(t).strip().lower() for t in (observed_entity_types or []) if str(t).strip()}
    reasons: list[str] = []
    if not raw:
        return EntityQuality(
            label_quality=LABEL_NOISY,
            eligible_for_topic_label=False,
            eligible_for_synthesis=False,
            quality_reasons=["empty_label"],
        )

    normalized = _normalized(raw)
    words = _words(raw)
    word_count = len(words)

    if _NUMERIC_OR_DATE_RE.fullmatch(normalized):
        return EntityQuality(
            label_quality=LABEL_NUMERIC_OR_DATE,
            eligible_for_topic_label=False,
            eligible_for_synthesis=False,
            quality_reasons=["numeric_or_date_label"],
        )

    if _CODE_LIKE_RE.search(raw) and kind not in _DOCUMENT_TYPES:
        return EntityQuality(
            label_quality=LABEL_CODE_LIKE,
            eligible_for_topic_label=False,
            eligible_for_synthesis=True,
            quality_reasons=["code_signature_or_syntax"],
        )

    if _looks_like_joined_list(raw, kind):
        return EntityQuality(
            label_quality=LABEL_JOINED_LIST,
            eligible_for_topic_label=False,
            eligible_for_synthesis=False,
            quality_reasons=["joined_list_punctuation"],
        )

    if kind in _DOCUMENT_TYPES:
        return EntityQuality(
            label_quality=LABEL_TITLE if word_count >= 4 else LABEL_CLEAN,
            eligible_for_topic_label=False,
            eligible_for_synthesis=True,
            quality_reasons=["document_title_allowed"] if word_count >= 4 else ["compact_document_label"],
        )

    if kind in _TITLE_TYPES and word_count >= 10:
        return EntityQuality(
            label_quality=LABEL_TITLE,
            eligible_for_topic_label=False,
            eligible_for_synthesis=True,
            quality_reasons=[f"{kind}_title_allowed"],
        )

    if normalized in _GENERIC_ROLE_LABELS or (
        word_count <= 3 and any(normalized.endswith(suffix) for suffix in _GENERIC_ROLE_SUFFIXES)
    ):
        return EntityQuality(
            label_quality=LABEL_GENERIC_ROLE,
            eligible_for_topic_label=False,
            eligible_for_synthesis=False,
            quality_reasons=["generic_role_label"],
        )

    if _looks_like_claim(raw, kind, word_count):
        return EntityQuality(
            label_quality=LABEL_CLAIM_LIKE,
            eligible_for_topic_label=False,
            eligible_for_synthesis=False,
            quality_reasons=["sentence_or_claim_cue"],
        )

    if word_count > 10 and "document" not in observed:
        return EntityQuality(
            label_quality=LABEL_NOISY,
            eligible_for_topic_label=False,
            eligible_for_synthesis=False,
            quality_reasons=["too_many_words_for_non_document_entity"],
        )

    reasons.append("compact_named_entity_or_noun_phrase")
    return EntityQuality(
        label_quality=LABEL_CLEAN,
        eligible_for_topic_label=True,
        eligible_for_synthesis=True,
        quality_reasons=reasons,
    )


def quality_payload(
    label: str | None,
    entity_type: str | None,
    *,
    observed_entity_types: list[str] | None = None,
) -> dict[str, Any]:
    return asdict(
        classify_entity_label(
            label,
            entity_type,
            observed_entity_types=observed_entity_types,
        )
    )


def is_quality_eligible_for_topic(attrs: dict[str, Any]) -> bool:
    quality = str(attrs.get("label_quality") or LABEL_CLEAN)
    return bool(attrs.get("eligible_for_topic_label", quality not in INELIGIBLE_TOPIC_LABEL_QUALITIES))


def is_quality_eligible_for_synthesis(attrs: dict[str, Any]) -> bool:
    quality = str(attrs.get("label_quality") or LABEL_CLEAN)
    return bool(attrs.get("eligible_for_synthesis", quality not in INELIGIBLE_SYNTHESIS_QUALITIES))


async def mark_graph_metrics_stale(db, corpus_id: str, *, reason: str) -> None:
    if db is None or not corpus_id:
        return
    now = datetime.utcnow()
    try:
        await db["graph_metrics_cache"].update_many(
            {"corpus_id": corpus_id},
            {
                "$set": {
                    "graph_cache_stale": True,
                    "stale_reason": reason,
                    "stale_at": now,
                    "schema_version": -1,
                    "entity_quality_version": ENTITY_QUALITY_VERSION,
                }
            },
        )
    except Exception as exc:
        logger.warning("Graph metrics stale mark failed corpus=%s reason=%s: %s", corpus_id, reason, exc)


async def entity_quality_stats(neo4j_driver, corpus_id: str | None = None) -> dict[str, Any]:
    if neo4j_driver is None:
        return {}
    if corpus_id:
        cypher = """
        MATCH (e:Entity)<-[:MENTIONS]-(:Chunk)<-[:HAS_CHUNK]-(d:Document {corpus_id:$corpus_id})
        WITH DISTINCT e
        RETURN count(e) AS total,
               sum(CASE WHEN coalesce(e.eligible_for_topic_label, true) THEN 1 ELSE 0 END) AS topic_eligible,
               sum(CASE WHEN coalesce(e.eligible_for_synthesis, true) THEN 1 ELSE 0 END) AS synthesis_eligible,
               collect(coalesce(e.label_quality, 'unknown')) AS qualities,
               sum(CASE WHEN e.entity_quality_version = $version THEN 1 ELSE 0 END) AS versioned
        """
        params = {"corpus_id": corpus_id, "version": ENTITY_QUALITY_VERSION}
    else:
        cypher = """
        MATCH (e:Entity)
        RETURN count(e) AS total,
               sum(CASE WHEN coalesce(e.eligible_for_topic_label, true) THEN 1 ELSE 0 END) AS topic_eligible,
               sum(CASE WHEN coalesce(e.eligible_for_synthesis, true) THEN 1 ELSE 0 END) AS synthesis_eligible,
               collect(coalesce(e.label_quality, 'unknown')) AS qualities,
               sum(CASE WHEN e.entity_quality_version = $version THEN 1 ELSE 0 END) AS versioned
        """
        params = {"version": ENTITY_QUALITY_VERSION}
    async with neo4j_driver.session() as session:
        rec = await (await session.run(cypher, **params)).single()
    if not rec:
        return {}
    total = int(rec.get("total") or 0)
    quality_counts = Counter(str(q) for q in (rec.get("qualities") or []))
    return {
        "entity_quality_version": ENTITY_QUALITY_VERSION,
        "total_entities": total,
        "quality_counts": dict(sorted(quality_counts.items())),
        "noisy_entity_count": int(
            sum(quality_counts.get(q, 0) for q in INELIGIBLE_TOPIC_LABEL_QUALITIES)
        ),
        "claim_like_count": int(quality_counts.get(LABEL_CLAIM_LIKE, 0)),
        "generic_role_count": int(quality_counts.get(LABEL_GENERIC_ROLE, 0)),
        "joined_list_count": int(quality_counts.get(LABEL_JOINED_LIST, 0)),
        "topic_eligible_count": int(rec.get("topic_eligible") or 0),
        "synthesis_eligible_count": int(rec.get("synthesis_eligible") or 0),
        "topic_eligible_pct": round((int(rec.get("topic_eligible") or 0) / total), 4) if total else 0.0,
        "synthesis_eligible_pct": round((int(rec.get("synthesis_eligible") or 0) / total), 4) if total else 0.0,
        "versioned_count": int(rec.get("versioned") or 0),
    }


async def backfill_entity_quality(
    neo4j_driver,
    db=None,
    *,
    corpus_id: str | None = None,
    batch_size: int = 500,
    force: bool = False,
) -> dict[str, Any]:
    """Classify existing Entity nodes in batches without deleting anything."""
    if neo4j_driver is None:
        raise RuntimeError("Neo4j driver is not available")
    total_updated = 0
    quality_counts: Counter[str] = Counter()
    batch_size = max(1, min(int(batch_size or 500), 5000))
    async with neo4j_driver.session() as session:
        while True:
            if corpus_id:
                read_query = """
                MATCH (e:Entity)<-[:MENTIONS]-(:Chunk)<-[:HAS_CHUNK]-(d:Document {corpus_id:$corpus_id})
                WHERE $force OR e.entity_quality_version IS NULL OR e.entity_quality_version <> $version
                WITH DISTINCT e
                RETURN e.entity_id AS entity_id,
                       coalesce(e.display_name, e.canonical_name, e.normalized_name, e.entity_id) AS label,
                       coalesce(e.primary_entity_type, e.entity_type) AS entity_type,
                       e.observed_entity_types AS observed_entity_types
                LIMIT $limit
                """
                params = {
                    "corpus_id": corpus_id,
                    "version": ENTITY_QUALITY_VERSION,
                    "limit": batch_size,
                    "force": force,
                }
            else:
                read_query = """
                MATCH (e:Entity)
                WHERE $force OR e.entity_quality_version IS NULL OR e.entity_quality_version <> $version
                RETURN e.entity_id AS entity_id,
                       coalesce(e.display_name, e.canonical_name, e.normalized_name, e.entity_id) AS label,
                       coalesce(e.primary_entity_type, e.entity_type) AS entity_type,
                       e.observed_entity_types AS observed_entity_types
                LIMIT $limit
                """
                params = {"version": ENTITY_QUALITY_VERSION, "limit": batch_size, "force": force}
            result = await session.run(read_query, **params)
            rows = []
            async for rec in result:
                payload = quality_payload(
                    rec.get("label"),
                    rec.get("entity_type"),
                    observed_entity_types=rec.get("observed_entity_types") or [],
                )
                quality_counts[payload["label_quality"]] += 1
                rows.append({"entity_id": rec.get("entity_id"), **payload})
            if not rows:
                break
            await session.run(
                """
                UNWIND $rows AS row
                MATCH (e:Entity {entity_id: row.entity_id})
                SET e.label_quality = row.label_quality,
                    e.eligible_for_topic_label = row.eligible_for_topic_label,
                    e.eligible_for_synthesis = row.eligible_for_synthesis,
                    e.quality_reasons = row.quality_reasons,
                    e.entity_quality_version = row.entity_quality_version
                """,
                rows=rows,
            )
            total_updated += len(rows)
            if len(rows) < batch_size:
                break
    if corpus_id:
        await mark_graph_metrics_stale(db, corpus_id, reason="entity_quality_backfill")
    elif db is not None:
        corpus_ids = await db["graph_metrics_cache"].distinct("corpus_id")
        for cid in corpus_ids:
            await mark_graph_metrics_stale(db, str(cid), reason="entity_quality_backfill")
    return {
        "status": "done",
        "corpus_id": corpus_id,
        "updated_entities": total_updated,
        "quality_counts": dict(sorted(quality_counts.items())),
        "entity_quality_version": ENTITY_QUALITY_VERSION,
        "deleted_entities": 0,
    }
