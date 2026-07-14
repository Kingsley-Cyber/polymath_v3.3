"""Additive adapters into the shared P2.6 candidate artifact contract.

The existing extraction engines and durable write paths remain unchanged.
These pure adapters are used by parity/receipt tooling only.  Query aliases
and definitional phrases are deliberately recomputed with the shared backend
rules so provider choice cannot change those deterministic fields.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, is_dataclass
from typing import Any

from models.extraction_artifact import (
    CANDIDATE_EXTRACTION_ARTIFACT_VERSION,
    CANDIDATE_EXTRACTION_AUTHORITY,
    CANDIDATE_EXTRACTION_SCHEMA_HASH,
    CandidateEntity,
    CandidateExtractionArtifact,
    CandidateFact,
    CandidateFailure,
    CandidateRelation,
    EngineCapabilities,
    EvidenceRef,
    ExtractionEngine,
    ExtractionProvenance,
    FieldMethod,
    OffsetSpan,
)
from services.ingestion.enrich import (
    extract_aliases,
    extract_definitional_phrases,
)


_GRAPH_ADMITTED_STATUSES = frozenset({"accepted", "validated", "promoted", "ok"})


def _dict(value: Any) -> dict[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return dict(value)
    return dict(value or {})


def _rows(value: Any) -> list[dict[str, Any]]:
    return [_dict(item) for item in (value or [])]


def _source_hash(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _local_id(kind: str, chunk_id: str, index: int, value: str) -> str:
    digest = hashlib.sha256(
        f"{kind}\x1f{chunk_id}\x1f{index}\x1f{value}".encode("utf-8")
    ).hexdigest()
    return f"{kind}:{digest[:24]}"


def _unique_exact_span(text: str, value: str) -> OffsetSpan:
    if not value:
        return OffsetSpan(status="unavailable")
    start = text.find(value)
    if start < 0 or text.find(value, start + 1) >= 0:
        return OffsetSpan(status="unavailable")
    return OffsetSpan(status="exact", char_start=start, char_end=start + len(value))


def _capabilities(engine: ExtractionEngine) -> EngineCapabilities:
    # Current engine truth, not an aspiration: only the legacy-local enrich
    # stack structures deterministic facts today.  Facts remain optional for
    # queryability on every engine.  RunPod's native v3 wire carries entity
    # offsets; the older shared ExtractionResult shape does not.
    return EngineCapabilities(
        deterministic_facts_supported=engine == "legacy_local",
        facts_required_for_queryability=False,
        exact_entity_offsets_supported=engine == "runpod_flash",
        exact_relation_evidence_supported=True,
    )


class _EvidenceBuilder:
    def __init__(self, *, chunk_id: str, text: str) -> None:
        self.chunk_id = chunk_id
        self.text = text
        self.rows: list[EvidenceRef] = []
        self._ids_by_key: dict[tuple[str, int | None, int | None], str] = {}

    def exact(self, value: str, *, label: str) -> str | None:
        span = _unique_exact_span(self.text, value)
        if span.status != "exact":
            return None
        key = (value, span.char_start, span.char_end)
        existing = self._ids_by_key.get(key)
        if existing:
            return existing
        evidence_id = _local_id(label, self.chunk_id, len(self.rows), value)
        self.rows.append(
            EvidenceRef(
                evidence_id=evidence_id,
                text=value,
                span=span,
                method="exact_source_substring",
            )
        )
        self._ids_by_key[key] = evidence_id
        return evidence_id


def _provenance(
    *,
    engine: ExtractionEngine,
    engine_runtime_version: str,
    model_id: str,
    model_revision: str | None,
    source_wire_contract_version: str,
    source_contract_hash: str,
    row: dict[str, Any],
    field_methods: list[FieldMethod],
    fallback_from: list[str] | None,
    fallback_count: int | None,
    failure_count: int,
) -> ExtractionProvenance:
    fallbacks = list(fallback_from or [])
    return ExtractionProvenance(
        engine=engine,
        engine_runtime_version=engine_runtime_version,
        model_id=model_id or str(row.get("model") or ""),
        model_revision=model_revision,
        source_wire_contract_version=source_wire_contract_version,
        source_contract_hash=source_contract_hash,
        shared_contract_version=CANDIDATE_EXTRACTION_ARTIFACT_VERSION,
        shared_contract_hash=CANDIDATE_EXTRACTION_SCHEMA_HASH,
        capabilities=_capabilities(engine),
        field_methods=field_methods,
        lane=int(row.get("lane")) if row.get("lane") is not None else None,
        attempts=max(0, int(row.get("attempts") or 0)),
        fallback_from=fallbacks,
        fallback_count=(len(fallbacks) if fallback_count is None else fallback_count),
        failure_count=max(0, int(failure_count or 0)),
    )


def adapt_extraction_result(
    result: Any,
    *,
    engine: ExtractionEngine,
    engine_runtime_version: str,
    source_wire_contract_version: str,
    source_contract_hash: str,
    model_id: str = "",
    model_revision: str | None = None,
    fallback_from: list[str] | None = None,
    fallback_count: int | None = None,
    failure_count: int = 0,
    grounded_object_kind_evidence: dict[str, str] | None = None,
) -> CandidateExtractionArtifact:
    """Adapt one successful engine result without changing its write path.

    ``object_kind`` is intentionally blank unless callers supply an exact
    source quote that contains the proposed kind.  ``relation_cue`` follows
    the same source-evidence rule.  Ambiguous/missing offsets are recorded as
    unavailable rather than guessed.
    """

    row = _dict(result)
    text = str(row.get("text") or "")
    chunk_id = str(row.get("chunk_id") or "")
    entity_rows = _rows(row.get("entities"))
    relation_rows = _rows(row.get("relations"))
    fact_rows = _rows(row.get("facts"))
    evidence = _EvidenceBuilder(chunk_id=chunk_id, text=text)

    # Shared deterministic post-extraction rules are the sole alias and
    # definition source in this candidate contract.
    aliases = extract_aliases(text, entity_rows)
    definitions = extract_definitional_phrases(text, entity_rows)
    kind_evidence = grounded_object_kind_evidence or {}

    entities: list[CandidateEntity] = []
    object_kind_evidence_ids: list[str] = []
    for index, entity in enumerate(entity_rows):
        canonical = str(
            entity.get("canonical_name") or entity.get("surface_form") or ""
        ).strip()
        surface = str(entity.get("surface_form") or canonical).strip()
        proposed_kind = str(entity.get("object_kind") or "").strip()
        grounded_kind = ""
        grounded_ids: list[str] = []
        quote = str(kind_evidence.get(canonical) or "").strip()
        if proposed_kind and quote and proposed_kind.lower() in quote.lower():
            evidence_id = evidence.exact(quote, label="object-kind-evidence")
            if evidence_id:
                grounded_kind = proposed_kind
                grounded_ids = [evidence_id]
                object_kind_evidence_ids.append(evidence_id)
        span = _unique_exact_span(text, surface)
        entities.append(
            CandidateEntity(
                entity_id=_local_id("candidate-entity", chunk_id, index, canonical),
                canonical_name=canonical,
                surface_form=surface,
                entity_type=str(entity.get("entity_type") or ""),
                object_kind=grounded_kind,
                confidence=float(entity.get("confidence") or 0.0),
                span=span,
                query_aliases=list(aliases.get(canonical) or []),
                definitional_phrase=str(definitions.get(canonical) or ""),
                method=(
                    "engine_model"
                    if span.status == "exact"
                    else "unavailable_legacy_shape"
                ),
                object_kind_evidence_ids=grounded_ids,
            )
        )

    relations: list[CandidateRelation] = []
    relation_evidence_ids: list[str] = []
    cue_evidence_ids: list[str] = []
    for index, relation in enumerate(relation_rows):
        phrase = str(relation.get("evidence_phrase") or "").strip()
        evidence_id = evidence.exact(phrase, label="relation-evidence")
        evidence_ids = [evidence_id] if evidence_id else []
        relation_evidence_ids.extend(evidence_ids)

        raw_cue = str(relation.get("relation_cue") or "").strip()
        cue_id = evidence.exact(raw_cue, label="relation-cue")
        cue = raw_cue if cue_id else ""
        relation_cue_ids = [cue_id] if cue_id else []
        cue_evidence_ids.extend(relation_cue_ids)

        validation_status = (
            str(relation.get("validation_status"))
            if relation.get("validation_status") is not None
            else None
        )
        relations.append(
            CandidateRelation(
                relation_id=_local_id(
                    "candidate-relation",
                    chunk_id,
                    index,
                    "|".join(
                        str(relation.get(key) or "")
                        for key in ("subject", "predicate", "object")
                    ),
                ),
                subject=str(relation.get("subject") or ""),
                predicate=str(relation.get("predicate") or ""),
                object=str(relation.get("object") or ""),
                object_kind=str(relation.get("object_kind") or ""),
                confidence=float(relation.get("confidence") or 0.0),
                evidence_ids=evidence_ids,
                relation_cue=cue,
                relation_cue_evidence_ids=relation_cue_ids,
                source_predicate=(
                    str(relation.get("source_predicate"))
                    if relation.get("source_predicate") is not None
                    else None
                ),
                validation_status=validation_status,
                graph_promotion_eligible=bool(
                    evidence_ids
                    and str(validation_status or "").lower() in _GRAPH_ADMITTED_STATUSES
                ),
                method="engine_model",
            )
        )

    capabilities = _capabilities(engine)
    facts: list[CandidateFact] = []
    fact_evidence_ids: list[str] = []
    for index, fact in enumerate(fact_rows):
        phrase = str(fact.get("evidence_phrase") or "").strip()
        evidence_id = evidence.exact(phrase, label="fact-evidence")
        evidence_ids = [evidence_id] if evidence_id else []
        fact_evidence_ids.extend(evidence_ids)
        deterministic = capabilities.deterministic_facts_supported
        facts.append(
            CandidateFact(
                fact_id=_local_id(
                    "candidate-fact",
                    chunk_id,
                    index,
                    "|".join(
                        str(fact.get(key) or "")
                        for key in ("subject", "fact_type", "property_name", "value")
                    ),
                ),
                subject=str(fact.get("subject") or ""),
                fact_type=str(fact.get("fact_type") or ""),
                property_name=str(fact.get("property_name") or ""),
                value=str(fact.get("value") or ""),
                unit=str(fact.get("unit")) if fact.get("unit") is not None else None,
                condition=(
                    str(fact.get("condition"))
                    if fact.get("condition") is not None
                    else None
                ),
                confidence=float(fact.get("confidence") or 0.0),
                evidence_ids=evidence_ids,
                deterministic=deterministic,
                method=("deterministic_python" if deterministic else "engine_model"),
            )
        )

    field_methods = [
        FieldMethod(
            field_path="entities",
            method="engine_model",
            producer=model_id or str(row.get("model") or engine),
            evidence_ids=[],
        ),
        FieldMethod(
            field_path="entities[*].query_aliases",
            method="shared_backend_validation",
            producer="services.ingestion.enrich.extract_aliases",
            evidence_ids=[],
        ),
        FieldMethod(
            field_path="entities[*].definitional_phrase",
            method="shared_backend_validation",
            producer="services.ingestion.enrich.extract_definitional_phrases",
            evidence_ids=[],
        ),
        FieldMethod(
            field_path="entities[*].object_kind",
            method=(
                "source_evidenced" if object_kind_evidence_ids else "omitted_ungrounded"
            ),
            producer="candidate_extraction_artifact.v1",
            evidence_ids=object_kind_evidence_ids,
        ),
        FieldMethod(
            field_path="relations[*].evidence_ids",
            method="source_evidenced",
            producer="candidate_extraction_artifact.v1",
            evidence_ids=relation_evidence_ids,
        ),
        FieldMethod(
            field_path="relations[*].relation_cue",
            method=("source_evidenced" if cue_evidence_ids else "omitted_ungrounded"),
            producer="candidate_extraction_artifact.v1",
            evidence_ids=cue_evidence_ids,
        ),
        FieldMethod(
            field_path="facts",
            method=(
                "deterministic_python"
                if capabilities.deterministic_facts_supported
                else "engine_model"
            ),
            producer=(
                "services.ingestion.enrich"
                if capabilities.deterministic_facts_supported
                else model_id or str(row.get("model") or engine)
            ),
            evidence_ids=fact_evidence_ids,
        ),
    ]

    return CandidateExtractionArtifact(
        schema_version=CANDIDATE_EXTRACTION_ARTIFACT_VERSION,
        authority=CANDIDATE_EXTRACTION_AUTHORITY,
        artifact_status="candidate",
        corpus_id=str(row.get("corpus_id") or ""),
        doc_id=str(row.get("doc_id") or ""),
        chunk_id=chunk_id,
        source_text_sha256=_source_hash(text),
        entities=entities,
        relations=relations,
        facts=facts,
        evidence=evidence.rows,
        provenance=_provenance(
            engine=engine,
            engine_runtime_version=engine_runtime_version,
            model_id=model_id,
            model_revision=model_revision,
            source_wire_contract_version=source_wire_contract_version,
            source_contract_hash=source_contract_hash,
            row=row,
            field_methods=field_methods,
            fallback_from=fallback_from,
            fallback_count=fallback_count,
            failure_count=failure_count,
        ),
    )


def adapt_extraction_failure(
    failure: Any,
    *,
    engine: ExtractionEngine,
    engine_runtime_version: str,
    source_wire_contract_version: str,
    source_contract_hash: str,
    source_text: str,
    model_id: str = "",
    model_revision: str | None = None,
    fallback_from: list[str] | None = None,
    fallback_count: int | None = None,
) -> CandidateExtractionArtifact:
    """Adapt one terminal engine failure for failure-rate parity accounting."""

    row = _dict(failure)
    return CandidateExtractionArtifact(
        schema_version=CANDIDATE_EXTRACTION_ARTIFACT_VERSION,
        authority=CANDIDATE_EXTRACTION_AUTHORITY,
        artifact_status="failed",
        corpus_id=str(row.get("corpus_id") or ""),
        doc_id=str(row.get("doc_id") or ""),
        chunk_id=str(row.get("chunk_id") or ""),
        source_text_sha256=_source_hash(source_text),
        entities=[],
        relations=[],
        facts=[],
        evidence=[],
        provenance=_provenance(
            engine=engine,
            engine_runtime_version=engine_runtime_version,
            model_id=model_id,
            model_revision=model_revision,
            source_wire_contract_version=source_wire_contract_version,
            source_contract_hash=source_contract_hash,
            row=row,
            field_methods=[],
            fallback_from=fallback_from,
            fallback_count=fallback_count,
            failure_count=1,
        ),
        failure=CandidateFailure(
            error_type=str(row.get("error_type") or "unknown"),
            error_message=str(row.get("error_message") or "")[:1000],
        ),
    )
