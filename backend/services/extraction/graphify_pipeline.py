"""Resumable, fail-closed orchestration for the canonical Graphify path."""

from __future__ import annotations

import asyncio
import os
import inspect
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from models.graphify_contracts import (
    AdaptedOpenIEArgumentV1,
    AssertionDecisionV1,
    CompletedMentionV1,
    DocumentEntityV1,
    EntityTerminalState,
    ExtractionWindowV1,
    NormalizedDocumentV1,
    OpenIEAssertionV1,
    OpenIEPredicateCandidateV1,
    OpenIEPropositionFamilyV1,
    OpenIERawPropositionV1,
    PipelineStage,
    RawMentionV1,
    StageReceiptV1,
    SurfaceRelationV1,
    stable_digest,
    stable_id,
)
from services.control_plane import ledger
from services.ops_drills.kill_seam import ops_kill_point
from services.extraction.canonical import normalize_entity_type
from services.extraction.gliner2_cpu_provider import (
    MODEL_ID,
    MODEL_REVISION,
    PROVIDER_RELEASE,
    GLiNER2CPUProvider,
    get_gliner2_cpu_provider,
    provider_release_hash,
)
from services.extraction.graphify_census import (
    CENSUS_RELEASE,
    CensusOutput,
    InMemoryRawMentionSink,
    run_entity_census,
)
from services.extraction.graphify_completion import (
    COMPLETION_RELEASE,
    CompletionOutput,
    complete_document_mentions,
)
from services.extraction.graphify_normalization import (
    NORMALIZATION_RELEASE,
    normalize_document,
)
from services.extraction.graphify_openie import (
    OPENIE_RELEASE,
    OpenIEUnit,
    get_triplet_extract_cpu_provider,
    run_openie_extraction,
)
from services.extraction.graphify_argument_adapter import (
    ARGUMENT_ADAPTER_RELEASE,
    adapt_openie_arguments,
)
from services.extraction.graphify_proposition_reducer import (
    PROPOSITION_REDUCER_RELEASE,
    reduce_openie_propositions,
)
from services.extraction.graphify_predicate_compiler import (
    OPENIE_PREDICATE_COMPILER_RELEASE,
    compile_openie_predicates,
)
from services.extraction.graphify_assertion_assembler import (
    OPENIE_ASSERTION_POLICY_RELEASE,
    assemble_openie_assertions,
)
from services.extraction.graphify_reducer import (
    REDUCER_RELEASE,
    ReducerOutput,
    reduce_document_entities,
)
from services.extraction.graphify_relations import (
    RELATION_RELEASE,
    RelationFastPathOutput,
    evaluate_relation_eligibility,
    openie_fact_merge_disposition,
    run_relation_fast_path,
)
from services.extraction.graphify_survey import (
    SURVEY_RELEASE,
    DocumentSurveyV1,
    survey_document,
)
from services.ghost_b import (
    EntityItem,
    ExtractionBatchReport,
    ExtractionResult,
    RelationItem,
)
from services.storage import mongo_reader, mongo_writer

PIPELINE_RELEASE = "graphify-cpu-pipeline-v15"


@dataclass(frozen=True)
class GraphifyPipelineOutput:
    report: ExtractionBatchReport
    identity_digest: str
    stage_receipts: tuple[dict[str, Any], ...]
    resumed_stages: tuple[str, ...]


@dataclass(frozen=True)
class _Child:
    chunk_id: str
    text: str


def _child_rows(children: Sequence[Any]) -> tuple[_Child, ...]:
    rows = [
        _Child(str(getattr(item, "chunk_id", "")), str(getattr(item, "text", "")))
        for item in children
        if getattr(item, "chunk_id", None)
    ]
    return tuple(sorted(rows, key=lambda item: item.chunk_id))


def _record_payload(rows: Sequence[Any]) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in rows]


def _logical_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "report"}


async def _stage(
    *,
    db: Any,
    run_id: str,
    corpus_id: str,
    doc_id: str,
    stage: PipelineStage,
    release: str,
    input_hash: str,
    input_count: int,
    compute: Callable[[], Any],
    output_count: Callable[[dict[str, Any]], int],
    release_pins: dict[str, str],
    conservation: Callable[[dict[str, Any]], bool] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    receipt_id = ledger.graphify_receipt_id(
        run_id=run_id, stage=stage.value, input_hash=input_hash, release=release,
    )
    artifact_id = stable_id("graphify-stage-artifact", receipt_id)
    existing_receipt = await ledger.get_graphify_stage_receipt(db, receipt_id=receipt_id)
    existing_artifact = await mongo_reader.read_graphify_stage_artifact(
        db, artifact_id=artifact_id,
    )
    if (
        existing_receipt
        and existing_receipt.get("status") == "passed"
        and existing_artifact
        and existing_artifact.get("input_hash") == input_hash
        and existing_artifact.get("output_hash")
    ):
        payload = dict(existing_artifact["payload"])
        if stable_digest(_logical_payload(payload)) != existing_artifact["output_hash"]:
            raise RuntimeError(f"Graphify resume artifact hash mismatch at {stage.value}")
        return payload, dict(existing_receipt["receipt"]), True

    # Crash-recovery reconciliation: the artifact write precedes the receipt
    # write, so a crash in that gap leaves a durable, hash-verified artifact
    # with no passing receipt. Stages are deterministic — adopt the artifact
    # and complete the receipt instead of inferring again.
    if (
        existing_artifact
        and existing_artifact.get("input_hash") == input_hash
        and existing_artifact.get("output_hash")
        and (existing_receipt or {}).get("status") != "passed"
    ):
        payload = dict(existing_artifact["payload"])
        if stable_digest(_logical_payload(payload)) == existing_artifact["output_hash"]:
            receipt_model = StageReceiptV1(
                run_id=run_id,
                document_id=doc_id,
                stage=stage,
                status="passed",
                input_hash=input_hash,
                output_hash=existing_artifact["output_hash"],
                release_pins={**release_pins, "stage_release": release},
                elapsed_seconds=0.0,
                input_count=input_count,
                output_count=int(output_count(payload)),
                errors=(),
                warnings=("reconciled_existing_artifact",),
                retry_count=int((existing_receipt or {}).get("attempt_no") or 0),
            ).model_dump(mode="json")
            stored = await ledger.record_graphify_stage_receipt(
                db,
                receipt_id=receipt_id,
                run_id=run_id,
                corpus_id=corpus_id,
                doc_id=doc_id,
                stage=stage.value,
                status="passed",
                receipt=receipt_model,
            )
            await ledger.record_stage_attempt(
                db,
                corpus_id=corpus_id,
                run_id=run_id,
                doc_id=doc_id,
                stage=stage.value,
                action="graphify_stage_reconcile",
                status="passed",
                executor=PIPELINE_RELEASE,
                receipt=receipt_model,
                duration_ms=0,
            )
            return payload, dict(stored["receipt"]), True

    ops_kill_point(f"{stage.value}:before_compute")
    started = time.perf_counter()
    stage_pins = {**release_pins, "stage_release": release}
    retry_count = int((existing_receipt or {}).get("attempt_no") or 0)
    try:
        payload = compute()
        if inspect.isawaitable(payload):
            payload = await payload
        if not isinstance(payload, dict):
            raise TypeError(f"Graphify stage {stage.value} did not return a payload")
        count = int(output_count(payload))
        conserved = conservation(payload) if conservation is not None else True
        if not conserved:
            raise RuntimeError(f"Graphify stage conservation failed at {stage.value}")
        output_hash = stable_digest(_logical_payload(payload))
        ops_kill_point(f"{stage.value}:after_compute")
        await mongo_writer.persist_graphify_stage_artifact(
            db,
            artifact_id=artifact_id,
            corpus_id=corpus_id,
            doc_id=doc_id,
            stage=stage.value,
            input_hash=input_hash,
            output_hash=output_hash,
            release=release,
            payload=payload,
        )
        ops_kill_point(f"{stage.value}:after_artifact")
        elapsed = time.perf_counter() - started
        receipt_model = StageReceiptV1(
            run_id=run_id,
            document_id=doc_id,
            stage=stage,
            status="passed",
            input_hash=input_hash,
            output_hash=output_hash,
            release_pins=stage_pins,
            elapsed_seconds=elapsed,
            input_count=input_count,
            output_count=count,
            errors=(),
            warnings=(),
            retry_count=retry_count,
        ).model_dump(mode="json")
        stored = await ledger.record_graphify_stage_receipt(
            db,
            receipt_id=receipt_id,
            run_id=run_id,
            corpus_id=corpus_id,
            doc_id=doc_id,
            stage=stage.value,
            status="passed",
            receipt=receipt_model,
        )
        await ledger.record_stage_attempt(
            db,
            corpus_id=corpus_id,
            run_id=run_id,
            doc_id=doc_id,
            stage=stage.value,
            action="graphify_stage",
            status="passed",
            executor=PIPELINE_RELEASE,
            receipt=receipt_model,
            duration_ms=round(elapsed * 1000),
        )
        return payload, dict(stored["receipt"]), False
    except Exception as exc:
        elapsed = time.perf_counter() - started
        failure = {
            "schema_version": "polymath.graphify_stage_receipt.v1",
            "run_id": run_id,
            "document_id": doc_id,
            "stage": stage.value,
            "status": "failed",
            "input_hash": input_hash,
            "output_hash": stable_digest([]),
            "release_pins": stage_pins,
            "elapsed_seconds": elapsed,
            "input_count": input_count,
            "output_count": 0,
            "errors": [f"{type(exc).__name__}: {exc}"],
            "warnings": [],
            "retry_count": retry_count,
        }
        await ledger.record_graphify_stage_receipt(
            db,
            receipt_id=receipt_id,
            run_id=run_id,
            corpus_id=corpus_id,
            doc_id=doc_id,
            stage=stage.value,
            status="failed",
            receipt=failure,
        )
        await ledger.record_stage_attempt(
            db,
            corpus_id=corpus_id,
            run_id=run_id,
            doc_id=doc_id,
            stage=stage.value,
            action="graphify_stage",
            status="failed",
            executor=PIPELINE_RELEASE,
            failure_class=type(exc).__name__,
            receipt=failure,
            duration_ms=round(elapsed * 1000),
        )
        raise


def _chunk_for_text(text: str, children: tuple[_Child, ...]) -> str:
    matches = [item.chunk_id for item in children if text and text in item.text]
    return min(matches) if matches else children[0].chunk_id


def _legacy_results(
    *,
    document: NormalizedDocumentV1,
    corpus_id: str,
    children: tuple[_Child, ...],
    entities: Sequence[DocumentEntityV1],
    mentions: Sequence[CompletedMentionV1],
    relations: Sequence[SurfaceRelationV1],
) -> list[ExtractionResult]:
    if not children:
        return []
    mention_by_id = {item.mention_id: item for item in mentions}
    accepted_endpoint_ids = {
        mention.entity_id
        for relation in relations
        if relation.terminal_state.value == "accepted" and relation.canonical_candidate
        for mention in (
            mention_by_id.get(relation.subject_mention_id),
            mention_by_id.get(relation.object_mention_id),
        )
        if mention is not None
    }
    eligible_entities = {
        item.entity_id: item for item in entities
        if item.state in {EntityTerminalState.PROMOTED, EntityTerminalState.DOCUMENT_LOCAL}
        and (
            item.reducer_release != RELATION_RELEASE
            or item.entity_id in accepted_endpoint_ids
        )
    }
    entities_by_chunk: dict[str, dict[tuple[str, str], EntityItem]] = {
        item.chunk_id: {} for item in children
    }
    relations_by_chunk: dict[str, list[RelationItem]] = {item.chunk_id: [] for item in children}
    for entity in eligible_entities.values():
        chunk_id = _chunk_for_text(entity.canonical_name, children)
        key = (entity.canonical_name.casefold(), entity.entity_type)
        entities_by_chunk[chunk_id][key] = EntityItem(
            canonical_name=entity.canonical_name,
            surface_form=entity.canonical_name,
            entity_type=normalize_entity_type(entity.entity_type),
            confidence=entity.confidence,
            query_aliases=list(entity.aliases),
        )
    for relation in relations:
        if relation.terminal_state.value != "accepted" or not relation.canonical_candidate:
            continue
        subject_mention = mention_by_id.get(relation.subject_mention_id)
        object_mention = mention_by_id.get(relation.object_mention_id)
        if subject_mention is None or object_mention is None:
            raise RuntimeError(f"relation endpoint missing for {relation.relation_id}")
        subject = eligible_entities[subject_mention.entity_id]
        object_entity = eligible_entities[object_mention.entity_id]
        chunk_id = _chunk_for_text(relation.evidence_text, children)
        for entity in (subject, object_entity):
            key = (entity.canonical_name.casefold(), entity.entity_type)
            entities_by_chunk[chunk_id][key] = EntityItem(
                canonical_name=entity.canonical_name,
                surface_form=entity.canonical_name,
                entity_type=normalize_entity_type(entity.entity_type),
                confidence=entity.confidence,
                query_aliases=list(entity.aliases),
            )
        predicate = relation.canonical_candidate
        relations_by_chunk[chunk_id].append(RelationItem(
            subject=subject.canonical_name,
            predicate=predicate,
            object=object_entity.canonical_name,
            object_kind="entity",
            confidence=1.0,
            evidence_phrase=relation.evidence_text,
            relation_cue=relation.surface_predicate,
            source_predicate=relation.canonical_candidate,
            validation_status="accepted",
        ))
    results = []
    for child in children:
        child_relations = sorted(
            relations_by_chunk[child.chunk_id],
            key=lambda item: (item.subject.casefold(), item.predicate, item.object.casefold()),
        )
        results.append(ExtractionResult(
            schema_version="polymath.extract.v1",
            chunk_id=child.chunk_id,
            doc_id=document.document_id,
            corpus_id=corpus_id,
            entities=sorted(
                entities_by_chunk[child.chunk_id].values(),
                key=lambda item: (item.canonical_name.casefold(), item.entity_type),
            ),
            relations=child_relations,
            facts=[],
            text=child.text,
            model=MODEL_ID,
            provider="graphify_gliner2_cpu",
            lane=0,
            attempts=1,
            schema_mode="frozen_graphify",
            output_mode="deterministic",
        ))
    return results


async def run_graphify_pipeline(
    *,
    db: Any,
    corpus_id: str,
    doc_id: str,
    text: str,
    children: Sequence[Any],
    source_uri: str = "",
    provider: GLiNER2CPUProvider | None = None,
    fail_after_stage: PipelineStage | str | None = None,
) -> GraphifyPipelineOutput:
    """Run or resume the canonical document pipeline without partial graph output."""
    run_id = ledger.run_id_for(corpus_id=corpus_id, doc_id=doc_id)
    # Provider-neutral encoder seam (owner 2026-08-08): default resolves the
    # frozen GLiNER2 baseline; GRAPHIFY_ENTITY_PROVIDER selects the pinned
    # candidate. Downstream stages never learn which encoder ran.
    from services.extraction.entity_encoder import get_entity_encoder_provider

    selected_provider = provider or get_entity_encoder_provider()
    pins = {
        "pipeline": PIPELINE_RELEASE,
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "provider": PROVIDER_RELEASE,
        "provider_hash": provider_release_hash(),
    }
    receipts: list[dict[str, Any]] = []
    resumed: list[str] = []

    async def run_one(**kwargs):
        payload, receipt, was_resumed = await _stage(
            db=db, run_id=run_id, corpus_id=corpus_id, doc_id=doc_id,
            release_pins=pins, **kwargs,
        )
        receipts.append(receipt)
        if was_resumed:
            resumed.append(kwargs["stage"].value)
        target = fail_after_stage.value if isinstance(fail_after_stage, PipelineStage) else fail_after_stage
        if target == kwargs["stage"].value:
            raise RuntimeError(f"injected Graphify failure after {target}")
        return payload

    discovered_payload = await run_one(
        stage=PipelineStage.DISCOVERED,
        release=PIPELINE_RELEASE,
        input_hash=stable_digest({"doc_id": doc_id, "text": text, "source_uri": source_uri}),
        input_count=1,
        compute=lambda: {
            "document_id": doc_id,
            "source_uri": source_uri,
            "source_hash": stable_digest(text),
        },
        output_count=lambda payload: 1,
    )

    normalized_payload = await run_one(
        stage=PipelineStage.NORMALIZED,
        release=NORMALIZATION_RELEASE,
        input_hash=stable_digest(discovered_payload),
        input_count=1,
        compute=lambda: {"document": normalize_document(doc_id, text, source_uri).model_dump(mode="json")},
        output_count=lambda payload: 1,
    )
    document = NormalizedDocumentV1.model_validate(normalized_payload["document"])

    survey_payload = await run_one(
        stage=PipelineStage.SURVEY_COMPLETE,
        release=SURVEY_RELEASE,
        input_hash=stable_digest(document.model_dump(mode="json")),
        input_count=1,
        compute=lambda: {"survey": survey_document(document).model_dump(mode="json")},
        output_count=lambda payload: 1,
    )
    survey = DocumentSurveyV1.model_validate(survey_payload["survey"])

    if os.environ.get("GRAPHIFY_ADAPTER_COMPILER", "").strip() == "1":
        # Released production path: gold-blind per-document adapter
        # compilation (profiler reads ONLY the document text; packs are
        # frozen declarative knowledge; validator fails closed).
        from services.ontology_adapter.profiler import profile_document
        from services.ontology_adapter.schema_compiler import compile_adapter
        from services.ontology_adapter.providers.relex import activate_for_document

        compiled_ir = compile_adapter(
            profile_document(document.normalized_text),
            adapter_id=document.document_id[:16],
        )
        activate_for_document(document.document_id, compiled_ir)

    census_input_hash = stable_digest({
        "document": document.normalized_sha256,
        "survey": survey.survey_hash,
        "provider": provider_release_hash(),
    })

    async def census_compute() -> dict[str, Any]:
        output: CensusOutput = await asyncio.to_thread(
            run_entity_census,
            [document], [survey], selected_provider, InMemoryRawMentionSink(),
        )
        return {
            "windows": _record_payload(output.windows),
            "mentions": _record_payload(output.mentions),
            "report": output.report,
            "relex_relations": list(output.relex_relations),
        }

    census_payload = await run_one(
        stage=PipelineStage.ENTITY_CENSUS_COMPLETE,
        release=CENSUS_RELEASE,
        input_hash=census_input_hash,
        input_count=1,
        compute=census_compute,
        output_count=lambda payload: len(payload["mentions"]),
        conservation=lambda payload: bool(payload["report"].get("conservation")),
    )
    raw_mentions = tuple(RawMentionV1.model_validate(item) for item in census_payload["mentions"])

    def reducer_compute() -> dict[str, Any]:
        output: ReducerOutput = reduce_document_entities(document, raw_mentions, survey)
        return {
            "entities": _record_payload(output.entities),
            "assignments": [item.as_dict() for item in output.assignments],
            "report": output.report,
        }

    reducer_payload = await run_one(
        stage=PipelineStage.ENTITY_REDUCTION_COMPLETE,
        release=REDUCER_RELEASE,
        input_hash=stable_digest(_record_payload(raw_mentions)),
        input_count=len(raw_mentions),
        compute=reducer_compute,
        output_count=lambda payload: len(payload["assignments"]),
        conservation=lambda payload: bool(payload["report"].get("conservation")),
    )
    entities = tuple(DocumentEntityV1.model_validate(item) for item in reducer_payload["entities"])

    def completion_compute() -> dict[str, Any]:
        output: CompletionOutput = complete_document_mentions(document, entities, raw_mentions)
        return {"mentions": _record_payload(output.mentions), "report": output.report}

    completion_payload = await run_one(
        stage=PipelineStage.MENTION_COMPLETION_COMPLETE,
        release=COMPLETION_RELEASE,
        input_hash=stable_digest({
            "entities": _record_payload(entities), "raw_mentions": _record_payload(raw_mentions),
        }),
        input_count=len(entities),
        compute=completion_compute,
        output_count=lambda payload: len(payload["mentions"]),
    )
    completed_mentions = tuple(
        CompletedMentionV1.model_validate(item) for item in completion_payload["mentions"]
    )

    eligibility_payload = await run_one(
        stage=PipelineStage.RELATION_ELIGIBILITY_COMPLETE,
        release=RELATION_RELEASE,
        input_hash=stable_digest({
            "mentions": _record_payload(completed_mentions),
            "survey": survey.survey_hash,
        }),
        input_count=len(completed_mentions),
        compute=lambda: {
            "eligibility": [
                item.as_dict() for item in evaluate_relation_eligibility(
                    [document], [survey], completed_mentions,
                )
            ],
        },
        output_count=lambda payload: len(payload["eligibility"]),
    )

    openie_units = tuple(OpenIEUnit(
        unit_id=str(item["unit_id"]), document_id=str(item["document_id"]),
        start=int(item["start"]), end=int(item["end"]),
        text=document.normalized_text[int(item["start"]):int(item["end"])],
        eligible=bool(item["eligible"]),
    ) for item in eligibility_payload["eligibility"])

    async def openie_compute() -> dict[str, Any]:
        # Factory: farm dispatch blocks the calling thread, so it runs off
        # the event loop — concurrent documents keep the worker pool fed.
        output = await asyncio.to_thread(
            run_openie_extraction,
            [document], openie_units, get_triplet_extract_cpu_provider(),
        )
        return {"propositions": _record_payload(output.propositions), "report": output.report}

    openie_payload = await run_one(
        stage=PipelineStage.OPENIE_EXTRACTION_COMPLETE,
        release=OPENIE_RELEASE,
        input_hash=stable_digest(eligibility_payload["eligibility"]),
        input_count=len(openie_units),
        compute=openie_compute,
        output_count=lambda payload: len(payload["propositions"]),
        conservation=lambda payload: bool(payload["report"].get("conservation")),
    )
    openie_propositions = tuple(
        OpenIERawPropositionV1.model_validate(item) for item in openie_payload["propositions"]
    )

    def adapter_compute() -> dict[str, Any]:
        output = adapt_openie_arguments(openie_propositions, completed_mentions, entities)
        return {"arguments": _record_payload(output.arguments), "report": output.report}

    adapter_payload = await run_one(
        stage=PipelineStage.OPENIE_ARGUMENT_ADAPTATION_COMPLETE,
        release=ARGUMENT_ADAPTER_RELEASE,
        input_hash=stable_digest({
            "propositions": openie_payload["propositions"],
            "mentions": _record_payload(completed_mentions),
            "entities": _record_payload(entities),
        }),
        input_count=len(openie_propositions) * 2,
        compute=adapter_compute,
        output_count=lambda payload: len(payload["arguments"]),
        conservation=lambda payload: bool(payload["report"].get("classification_conservation")),
    )
    openie_arguments = tuple(
        AdaptedOpenIEArgumentV1.model_validate(item) for item in adapter_payload["arguments"]
    )

    def proposition_compute() -> dict[str, Any]:
        output = reduce_openie_propositions(openie_propositions, openie_arguments)
        return {"families": _record_payload(output.families), "report": output.report}

    proposition_payload = await run_one(
        stage=PipelineStage.OPENIE_PROPOSITION_REDUCTION_COMPLETE,
        release=PROPOSITION_REDUCER_RELEASE,
        input_hash=stable_digest({
            "propositions": openie_payload["propositions"],
            "arguments": adapter_payload["arguments"],
        }),
        input_count=len(openie_propositions),
        compute=proposition_compute,
        output_count=lambda payload: len(payload["families"]),
        conservation=lambda payload: bool(payload["report"].get("conservation")),
    )
    openie_families = tuple(
        OpenIEPropositionFamilyV1.model_validate(item) for item in proposition_payload["families"]
    )

    def predicate_compute() -> dict[str, Any]:
        output = compile_openie_predicates(openie_families, openie_propositions, openie_arguments)
        return {"candidates": _record_payload(output.candidates), "report": output.report}

    predicate_payload = await run_one(
        stage=PipelineStage.OPENIE_PREDICATE_COMPILATION_COMPLETE,
        release=OPENIE_PREDICATE_COMPILER_RELEASE,
        input_hash=stable_digest({
            "families": proposition_payload["families"],
            "compiler": OPENIE_PREDICATE_COMPILER_RELEASE,
        }),
        input_count=len(openie_families),
        compute=predicate_compute,
        output_count=lambda payload: len(payload["candidates"]),
        conservation=lambda payload: bool(payload["report"].get("decision_conservation")),
    )
    openie_candidates = tuple(
        OpenIEPredicateCandidateV1.model_validate(item) for item in predicate_payload["candidates"]
    )

    def openie_assertion_compute() -> dict[str, Any]:
        output = assemble_openie_assertions(openie_candidates, openie_arguments)
        return {"assertions": _record_payload(output.assertions), "report": output.report}

    openie_assertion_payload = await run_one(
        stage=PipelineStage.OPENIE_ASSERTION_ASSEMBLY_COMPLETE,
        release=OPENIE_ASSERTION_POLICY_RELEASE,
        input_hash=stable_digest({
            "candidates": predicate_payload["candidates"],
            "policy": OPENIE_ASSERTION_POLICY_RELEASE,
        }),
        input_count=len(openie_candidates),
        compute=openie_assertion_compute,
        output_count=lambda payload: len(payload["assertions"]),
        conservation=lambda payload: bool(payload["report"].get("decision_conservation")),
    )
    openie_assertions = tuple(
        OpenIEAssertionV1.model_validate(item) for item in openie_assertion_payload["assertions"]
    )

    async def relation_compute() -> dict[str, Any]:
        # Factory: the spaCy parse + deterministic compile run off the event
        # loop so one document's fast path overlaps another's model stages.
        return await asyncio.to_thread(_relation_compute_sync)

    def _relation_compute_sync() -> dict[str, Any]:
        output: RelationFastPathOutput = run_relation_fast_path(
            [document], [survey], completed_mentions, entities,
            relex_candidates=census_payload.get("relex_relations"),
        )
        candidate_by_id = {item.candidate_id: item for item in openie_candidates}
        mapped = list(output.mapped_relations)
        decisions = list(output.assertions)
        existing_keys = {
            (item.subject_mention_id, item.object_mention_id, item.canonical_candidate)
            for item in mapped if item.terminal_state.value == "accepted"
        }
        qualified_spans_by_key: dict[tuple, list[tuple[int, int]]] = {}
        for item in mapped:
            # Qualifier VETO authority belongs to the syntax lanes alone:
            # a demoted semantic-lane candidate is a withheld proposal, not
            # evidence that the sentence qualifies the fact. Burned sealed
            # counterexample: relex duplicates of accepted golds landed
            # QUALIFIED and silently blocked the same-evidence OpenIE FACTs.
            if (
                item.terminal_state.value == "qualified"
                and item.dependency_frame != "relex:semantic"
            ):
                qualified_spans_by_key.setdefault(
                    (item.subject_mention_id, item.object_mention_id, item.canonical_candidate),
                    [],
                ).append((item.evidence_start, item.evidence_end))
        blocked_by_qualified_syntax = 0
        blocked_closed_class_endpoint = 0
        closed_class_endpoint_ids = set(
            output.report.get("closed_class_mention_ids") or ()
        )
        for assertion in openie_assertions:
            if (
                assertion.lane != "FACT" or not assertion.subject_mention_id
                or not assertion.object_mention_id or not assertion.canonical_predicate
            ):
                continue
            if (
                assertion.subject_mention_id in closed_class_endpoint_ids
                or assertion.object_mention_id in closed_class_endpoint_ids
            ):
                # #2 structural endpoint eligibility: closed-class-headed
                # endpoint spans never promote; the assertion stays recorded.
                blocked_closed_class_endpoint += 1
                continue
            key = (
                assertion.subject_mention_id, assertion.object_mention_id,
                assertion.canonical_predicate,
            )
            disposition = openie_fact_merge_disposition(
                key, (assertion.evidence_start, assertion.evidence_end),
                existing_keys, qualified_spans_by_key,
            )
            if disposition == "duplicate":
                continue
            if disposition == "blocked_same_evidence_qualified":
                blocked_by_qualified_syntax += 1
                continue
            existing_keys.add(key)
            candidate = candidate_by_id[assertion.candidate_id]
            relation_id = stable_id("openie-surface-relation", assertion.assertion_id)
            relation = SurfaceRelationV1(
                relation_id=relation_id, document_id=assertion.document_id,
                evidence_text=document.normalized_text[assertion.evidence_start:assertion.evidence_end],
                evidence_start=assertion.evidence_start, evidence_end=assertion.evidence_end,
                subject_mention_id=assertion.subject_mention_id,
                object_mention_id=assertion.object_mention_id,
                surface_predicate=assertion.surface_relation,
                lemma=candidate.relation_lemma,
                dependency_frame="openie:triplet-extract",
                dependency_path="openie",
                voice=candidate.direction_rule,
                polarity=assertion.polarity, modality=assertion.modality,
                attribution=assertion.attribution,
                canonical_candidate=assertion.canonical_predicate,
                mapping_rule=candidate.mapping_rule,
                mapping_release=candidate.compiler_release,
                terminal_state="accepted",
                reasons=assertion.reasons,
            )
            mapped.append(relation)
            decisions.append(AssertionDecisionV1(
                decision_id=stable_id("assertion-decision", relation_id, "accepted", assertion.canonical_predicate),
                relation_id=relation_id, document_id=assertion.document_id,
                status="accepted", canonical_predicate=assertion.canonical_predicate,
                score=assertion.score, reasons=assertion.reasons,
                policy_release=assertion.policy_release,
            ))
        terminal_counts: dict[str, int] = {}
        for item in decisions:
            terminal_counts[item.status.value] = terminal_counts.get(item.status.value, 0) + 1
        report = dict(output.report)
        report.update({
            "mapped_records": len(mapped),
            "assertion_decisions": len(decisions),
            "decision_conservation": len(mapped) == len(decisions),
            "terminal_state_counts": terminal_counts,
            "openie_entity_facts_added": len(mapped) - len(output.mapped_relations),
            "openie_fact_blocked_by_qualified_syntax": blocked_by_qualified_syntax,
            "openie_fact_blocked_closed_class_endpoint": blocked_closed_class_endpoint,
            "openie_lane_counts": openie_assertion_payload["report"].get("lane_counts", {}),
        })
        return {
            "eligibility": [item.as_dict() for item in output.eligibility],
            "endpoint_entities": _record_payload(output.endpoint_entities),
            "endpoint_mentions": _record_payload(output.endpoint_mentions),
            "surface_relations": _record_payload(output.surface_relations),
            "mapped_relations": _record_payload(mapped),
            "assertions": _record_payload(decisions),
            "report": report,
        }

    relation_payload = await run_one(
        stage=PipelineStage.RELATION_COMPILATION_COMPLETE,
        release=RELATION_RELEASE,
        input_hash=stable_digest({
            "mentions": _record_payload(completed_mentions),
            "entities": _record_payload(entities),
            "survey": survey.survey_hash,
            "eligibility": eligibility_payload["eligibility"],
        }),
        input_count=len(completed_mentions),
        compute=relation_compute,
        output_count=lambda payload: len(payload["assertions"]),
        conservation=lambda payload: bool(payload["report"].get("decision_conservation")),
    )

    assertion_payload = await run_one(
        stage=PipelineStage.ASSERTION_VALIDATION_COMPLETE,
        release=RELATION_RELEASE,
        input_hash=stable_digest({
            "mapped_relations": relation_payload["mapped_relations"],
            "assertions": relation_payload["assertions"],
        }),
        input_count=len(relation_payload["mapped_relations"]),
        compute=lambda: {
            "mapped_relations": relation_payload["mapped_relations"],
            "assertions": relation_payload["assertions"],
            "decision_conservation": (
                len(relation_payload["mapped_relations"])
                == len(relation_payload["assertions"])
            ),
        },
        output_count=lambda payload: len(payload["assertions"]),
        conservation=lambda payload: bool(payload["decision_conservation"]),
    )
    endpoint_entities = tuple(
        DocumentEntityV1.model_validate(item) for item in relation_payload["endpoint_entities"]
    )
    endpoint_mentions = tuple(
        CompletedMentionV1.model_validate(item) for item in relation_payload["endpoint_mentions"]
    )
    mapped_relations = tuple(
        SurfaceRelationV1.model_validate(item) for item in assertion_payload["mapped_relations"]
    )
    assertions = tuple(
        AssertionDecisionV1.model_validate(item) for item in assertion_payload["assertions"]
    )
    logical_identity = stable_digest({
        "document": document.normalized_sha256,
        "raw_mentions": _record_payload(raw_mentions),
        "entities": _record_payload(entities),
        "completed_mentions": _record_payload(completed_mentions),
        "endpoint_entities": _record_payload(endpoint_entities),
        "endpoint_mentions": _record_payload(endpoint_mentions),
        "relations": _record_payload(mapped_relations),
        "assertions": _record_payload(assertions),
        "openie_propositions": openie_payload["propositions"],
        "openie_arguments": adapter_payload["arguments"],
        "openie_families": proposition_payload["families"],
        "openie_candidates": predicate_payload["candidates"],
        "openie_assertions": openie_assertion_payload["assertions"],
    })
    child_rows = _child_rows(children)
    results = _legacy_results(
        document=document,
        corpus_id=corpus_id,
        children=child_rows,
        entities=[*entities, *endpoint_entities],
        mentions=[*completed_mentions, *endpoint_mentions],
        relations=mapped_relations,
    )
    metrics = {
        "engine": "graphify_cpu",
        "provider": "graphify_gliner2_cpu",
        "model_id": MODEL_ID,
        "requested_chunks": len(child_rows),
        "extracted_chunks": len(results),
        "failed_chunks": 0,
        "identity_digest": logical_identity,
        "raw_mention_conservation": bool(census_payload["report"].get("conservation")),
        "entity_reducer_conservation": bool(reducer_payload["report"].get("conservation")),
        "relation_decision_conservation": bool(relation_payload["report"].get("decision_conservation")),
        "parse_once": bool(relation_payload["report"].get("parse_once")),
        "stage_receipts": receipts,
        "resumed_stages": resumed,
        "release_pins": pins,
        "entity_counts": {
            "raw_mentions": len(raw_mentions),
            "document_entities": len(entities),
            "completed_mentions": len(completed_mentions),
        },
        "relation_counts": dict(relation_payload["report"].get("terminal_state_counts") or {}),
        "openie_counts": {
            "raw_propositions": len(openie_propositions),
            "families": len(openie_families),
            "predicate_candidates": len(openie_candidates),
            "assertion_lanes": dict(openie_assertion_payload["report"].get("lane_counts") or {}),
            "entity_facts_added": relation_payload["report"].get("openie_entity_facts_added", 0),
        },
    }
    return GraphifyPipelineOutput(
        report=ExtractionBatchReport(results=results, failures=[], metrics=metrics),
        identity_digest=logical_identity,
        stage_receipts=tuple(receipts),
        resumed_stages=tuple(resumed),
    )
