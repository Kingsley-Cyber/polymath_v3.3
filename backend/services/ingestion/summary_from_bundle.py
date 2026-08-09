"""Build SummaryInformationRecordV1 from KnowledgeArtifactBundleV1.

Does not call providers. Does not create identity or Fact nodes.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable

from models.knowledge_artifact_bundle import KnowledgeArtifactBundleV1
from models.summary_information_record import (
    AggregatedSummaryRecordV1,
    SummaryInformationRecordV1,
)

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_DATE_RE = re.compile(r"\b(?:\d{4}|\d{1,2}/\d{1,2}/\d{2,4})\b")
_QTY_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:%|percent|ms|s|kg|km|mb|gb)?\b", re.IGNORECASE
)


def _sentences(text: str, limit: int = 3) -> list[str]:
    parts = [p.strip() for p in _SENT_SPLIT.split(text or "") if p.strip()]
    # Prefer definition-like sentences.
    scored = sorted(
        parts,
        key=lambda s: (
            0 if re.search(r"\b(is|are|means|refers to)\b", s, re.I) else 1,
            len(s),
        ),
    )
    return scored[:limit]


def summary_record_from_bundle(
    bundle: KnowledgeArtifactBundleV1,
    *,
    trusted_aliases: Iterable[str] | None = None,
) -> SummaryInformationRecordV1:
    aliases = sorted(
        {
            *(bundle.trusted_aliases or []),
            *(trusted_aliases or []),
            *(a for m in bundle.entity_mentions for a in m.query_aliases),
        }
    )
    primary = sorted(
        {m.canonical_name for m in bundle.entity_mentions if m.canonical_name}
    )
    assertions = [
        {
            "subject": r.subject,
            "predicate": r.predicate,
            "object": r.object,
            "validation_status": r.validation_status,
            "evidence_text": r.evidence_text,
        }
        for r in bundle.accepted_relation_assertions
    ]
    text = bundle.evidence_text or ""
    rec = SummaryInformationRecordV1(
        source_child_id=bundle.identity.chunk_id,
        parent_id=bundle.identity.parent_id,
        section_id=bundle.identity.section_id,
        document_id=bundle.identity.document_id,
        corpus_id=bundle.identity.corpus_id,
        heading_path=list(bundle.source_structure.heading_path or []),
        primary_entities=primary,
        trusted_aliases=aliases,
        definitions=list(bundle.descriptions or []),
        accepted_relation_assertions=assertions,
        qualified_claims=list(bundle.qualified_claims or []),
        qualified_facts=list(bundle.qualified_facts or []),
        dates=sorted(set(_DATE_RE.findall(text))),
        quantities=sorted(set(_QTY_RE.findall(text)))[:12],
        negation=list(bundle.negation or []),
        modality=list(bundle.modality or []),
        representative_source_sentences=_sentences(text),
        source_evidence_ids=[bundle.identity.chunk_id] if bundle.identity.chunk_id else [],
        source_child_ids=[bundle.identity.chunk_id] if bundle.identity.chunk_id else [],
    )
    return rec.with_hash()


def _aggregate(
    records: list[SummaryInformationRecordV1],
    *,
    level: str,
    corpus_id: str,
    document_id: str,
    parent_id: str = "",
    section_id: str = "",
) -> AggregatedSummaryRecordV1:
    entities: list[str] = []
    aliases: list[str] = []
    assertions: list[dict[str, Any]] = []
    sentences: list[str] = []
    child_ids: list[str] = []
    hashes: list[str] = []
    for r in records:
        entities.extend(r.primary_entities)
        aliases.extend(r.trusted_aliases)
        assertions.extend(r.accepted_relation_assertions)
        sentences.extend(r.representative_source_sentences)
        child_ids.extend(r.source_child_ids or [r.source_child_id])
        if r.input_hash:
            hashes.append(r.input_hash)
    # Dedupe preserving order
    def uniq(items: list[Any]) -> list[Any]:
        seen: set[str] = set()
        out: list[Any] = []
        for item in items:
            key = json_key(item)
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    agg = AggregatedSummaryRecordV1(
        level=level,  # type: ignore[arg-type]
        corpus_id=corpus_id,
        document_id=document_id,
        parent_id=parent_id,
        section_id=section_id,
        source_child_ids=uniq(child_ids),
        primary_entities=uniq(entities)[:24],
        trusted_aliases=uniq(aliases)[:24],
        accepted_relation_assertions=uniq(assertions)[:40],
        representative_source_sentences=uniq(sentences)[:8],
        child_input_hashes=sorted(set(hashes)),
    )
    return agg.with_hash()


def json_key(item: Any) -> str:
    if isinstance(item, dict):
        return json_dumps(item)
    return str(item)


def json_dumps(item: Any) -> str:
    import json

    return json.dumps(item, sort_keys=True, default=str, separators=(",", ":"))


def aggregate_summary_records(
    records: Iterable[SummaryInformationRecordV1],
) -> dict[str, list[AggregatedSummaryRecordV1]]:
    rows = list(records)
    by_parent: dict[tuple[str, str, str], list[SummaryInformationRecordV1]] = defaultdict(
        list
    )
    by_section: dict[tuple[str, str, str], list[SummaryInformationRecordV1]] = defaultdict(
        list
    )
    by_doc: dict[tuple[str, str], list[SummaryInformationRecordV1]] = defaultdict(list)
    for r in rows:
        by_parent[(r.corpus_id, r.document_id, r.parent_id or "parent:unknown")].append(r)
        by_section[
            (r.corpus_id, r.document_id, r.section_id or "section:default")
        ].append(r)
        by_doc[(r.corpus_id, r.document_id)].append(r)

    parents = [
        _aggregate(
            group,
            level="parent",
            corpus_id=cid,
            document_id=did,
            parent_id=pid,
        )
        for (cid, did, pid), group in sorted(by_parent.items())
    ]
    sections = [
        _aggregate(
            group,
            level="section",
            corpus_id=cid,
            document_id=did,
            section_id=sid,
        )
        for (cid, did, sid), group in sorted(by_section.items())
    ]
    documents = [
        _aggregate(group, level="document", corpus_id=cid, document_id=did)
        for (cid, did), group in sorted(by_doc.items())
    ]
    return {"parent": parents, "section": sections, "document": documents}
