"""Measurement-only engine parity harness for P2.6/P2.7.

The harness compares like-with-like candidate artifacts for the same chunks.
It emits symmetric deltas and coverage/failure/fallback observations; it has
no pass/fail, winner, preference, or automatic adjudication surface.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from models.extraction_artifact import CandidateExtractionArtifact, ExtractionEngine
from models.hash_taxonomy import namespace_hash


ENGINE_PARITY_REPORT_VERSION = "engine_parity_report.v1"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, protected_namespaces=())


class EngineMeasures(StrictModel):
    engine: ExtractionEngine
    engine_runtime_versions: list[str]
    model_ids: list[str]
    model_revisions: list[str]
    source_wire_contract_versions: list[str]
    expected_chunks: int
    observed_artifacts: int
    candidate_chunks: int
    failed_chunks: int
    skipped_chunks: int
    missing_chunks: int
    failure_events: int
    fallback_events: int
    fallback_artifacts: int
    failure_rate: float
    fallback_rate: float
    entity_count: int
    relation_count: int
    exact_evidence_count: int
    ontology_typed_entity_count: int
    graph_promotion_eligible_count: int


class PairwiseMeasures(StrictModel):
    engine_a: ExtractionEngine
    engine_b: ExtractionEngine
    paired_candidate_chunks: int
    unpaired_or_noncandidate_chunks: int
    entity_count_b_minus_a: int
    relation_count_b_minus_a: int
    exact_evidence_count_b_minus_a: int
    ontology_typed_count_b_minus_a: int
    graph_promotion_eligible_count_b_minus_a: int
    entity_jaccard_mean: float | None
    relation_jaccard_mean: float | None
    evidence_jaccard_mean: float | None
    ontology_jaccard_mean: float | None
    graph_promotion_jaccard_mean: float | None


class EngineParityReport(StrictModel):
    schema_version: Literal["engine_parity_report.v1"]
    authority: Literal["measurement_only_no_adjudication"]
    production_readiness: Literal["not_evaluated"]
    shared_contract_hash: str
    expected_chunk_set_hash: str
    expected_chunks: int
    engines: list[EngineMeasures]
    pairwise: list[PairwiseMeasures]

    @property
    def report_id(self) -> str:
        digest = namespace_hash("work", self.model_dump()).split(":", 1)[1]
        return f"engine-parity:{digest}"


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 6)


def _jaccard(left: set[Any], right: set[Any]) -> float:
    if not left and not right:
        return 1.0
    return len(left & right) / len(left | right)


def _entity_set(artifact: CandidateExtractionArtifact) -> set[tuple[str, str]]:
    return {
        (item.canonical_name.casefold(), item.entity_type.casefold())
        for item in artifact.entities
    }


def _relation_set(
    artifact: CandidateExtractionArtifact,
) -> set[tuple[str, str, str]]:
    return {
        (item.subject.casefold(), item.predicate.casefold(), item.object.casefold())
        for item in artifact.relations
    }


def _evidence_set(
    artifact: CandidateExtractionArtifact,
) -> set[tuple[str, int | None, int | None]]:
    return {
        (item.text, item.span.char_start, item.span.char_end)
        for item in artifact.evidence
        if item.method == "exact_source_substring"
    }


def _ontology_set(
    artifact: CandidateExtractionArtifact,
) -> set[tuple[str, str]]:
    return {
        (item.canonical_name.casefold(), item.entity_type.casefold())
        for item in artifact.entities
        if item.entity_type and item.entity_type.casefold() != "other"
    }


def _graph_set(
    artifact: CandidateExtractionArtifact,
) -> set[tuple[str, str, str]]:
    return {
        (item.subject.casefold(), item.predicate.casefold(), item.object.casefold())
        for item in artifact.relations
        if item.graph_promotion_eligible
    }


def _engine_measures(
    *,
    engine: ExtractionEngine,
    artifacts: list[CandidateExtractionArtifact],
    expected_count: int,
) -> EngineMeasures:
    candidate = [item for item in artifacts if item.artifact_status == "candidate"]
    failed = [item for item in artifacts if item.artifact_status == "failed"]
    skipped = [item for item in artifacts if item.artifact_status == "skipped"]
    fallback_artifacts = sum(bool(item.provenance.fallback_count) for item in artifacts)
    return EngineMeasures(
        engine=engine,
        engine_runtime_versions=sorted(
            {item.provenance.engine_runtime_version for item in artifacts}
        ),
        model_ids=sorted({item.provenance.model_id for item in artifacts}),
        model_revisions=sorted(
            {
                item.provenance.model_revision
                for item in artifacts
                if item.provenance.model_revision
            }
        ),
        source_wire_contract_versions=sorted(
            {item.provenance.source_wire_contract_version for item in artifacts}
        ),
        expected_chunks=expected_count,
        observed_artifacts=len(artifacts),
        candidate_chunks=len(candidate),
        failed_chunks=len(failed),
        skipped_chunks=len(skipped),
        missing_chunks=max(0, expected_count - len(artifacts)),
        failure_events=sum(item.provenance.failure_count for item in artifacts),
        fallback_events=sum(item.provenance.fallback_count for item in artifacts),
        fallback_artifacts=fallback_artifacts,
        failure_rate=round(len(failed) / expected_count, 6) if expected_count else 0.0,
        fallback_rate=(
            round(fallback_artifacts / len(artifacts), 6) if artifacts else 0.0
        ),
        entity_count=sum(len(item.entities) for item in candidate),
        relation_count=sum(len(item.relations) for item in candidate),
        exact_evidence_count=sum(len(_evidence_set(item)) for item in candidate),
        ontology_typed_entity_count=sum(len(_ontology_set(item)) for item in candidate),
        graph_promotion_eligible_count=sum(len(_graph_set(item)) for item in candidate),
    )


def compare_engine_artifacts(
    artifacts: list[CandidateExtractionArtifact],
    *,
    expected_chunk_ids: list[str],
    engines: list[ExtractionEngine],
) -> EngineParityReport:
    """Compare engine artifacts without deciding which output is correct."""

    if len(set(engines)) < 2 or len(engines) != len(set(engines)):
        raise ValueError("parity requires at least two unique engine names")
    if not artifacts:
        raise ValueError("parity requires at least one candidate or failure artifact")
    expected = sorted(set(expected_chunk_ids))
    if len(expected) != len(expected_chunk_ids):
        raise ValueError("expected chunk IDs must be unique")
    expected_set = set(expected)

    shared_hashes = {item.provenance.shared_contract_hash for item in artifacts}
    shared_versions = {item.provenance.shared_contract_version for item in artifacts}
    if len(shared_hashes) != 1 or len(shared_versions) != 1:
        raise ValueError("all parity artifacts must use one shared wire contract")
    if any(item.chunk_id not in expected_set for item in artifacts):
        raise ValueError("parity artifact contains a chunk outside the expected set")
    if any(item.provenance.engine not in engines for item in artifacts):
        raise ValueError("parity artifact contains an undeclared engine")

    by_key: dict[tuple[str, ExtractionEngine], CandidateExtractionArtifact] = {}
    by_chunk: dict[
        str, dict[ExtractionEngine, CandidateExtractionArtifact]
    ] = defaultdict(dict)
    by_engine: dict[ExtractionEngine, list[CandidateExtractionArtifact]] = {
        engine: [] for engine in engines
    }
    for artifact in artifacts:
        key = (artifact.chunk_id, artifact.provenance.engine)
        if key in by_key:
            raise ValueError("duplicate engine artifact for the same chunk")
        by_key[key] = artifact
        by_chunk[artifact.chunk_id][artifact.provenance.engine] = artifact
        by_engine[artifact.provenance.engine].append(artifact)

    for chunk_artifacts in by_chunk.values():
        source_hashes = {item.source_text_sha256 for item in chunk_artifacts.values()}
        if len(source_hashes) > 1:
            raise ValueError("same-chunk parity requires identical source text hashes")

    engine_rows = [
        _engine_measures(
            engine=engine,
            artifacts=by_engine[engine],
            expected_count=len(expected),
        )
        for engine in sorted(engines)
    ]

    pair_rows: list[PairwiseMeasures] = []
    for engine_a, engine_b in itertools.combinations(sorted(engines), 2):
        paired: list[
            tuple[CandidateExtractionArtifact, CandidateExtractionArtifact]
        ] = []
        for chunk_id in expected:
            left = by_chunk.get(chunk_id, {}).get(engine_a)
            right = by_chunk.get(chunk_id, {}).get(engine_b)
            if (
                left is not None
                and right is not None
                and left.artifact_status == "candidate"
                and right.artifact_status == "candidate"
            ):
                paired.append((left, right))

        set_functions = (
            _entity_set,
            _relation_set,
            _evidence_set,
            _ontology_set,
            _graph_set,
        )
        jaccards = [
            [_jaccard(function(left), function(right)) for left, right in paired]
            for function in set_functions
        ]
        left_engine = by_engine[engine_a]
        right_engine = by_engine[engine_b]
        left_candidates = [
            item for item in left_engine if item.artifact_status == "candidate"
        ]
        right_candidates = [
            item for item in right_engine if item.artifact_status == "candidate"
        ]
        pair_rows.append(
            PairwiseMeasures(
                engine_a=engine_a,
                engine_b=engine_b,
                paired_candidate_chunks=len(paired),
                unpaired_or_noncandidate_chunks=len(expected) - len(paired),
                entity_count_b_minus_a=sum(
                    len(item.entities) for item in right_candidates
                )
                - sum(len(item.entities) for item in left_candidates),
                relation_count_b_minus_a=sum(
                    len(item.relations) for item in right_candidates
                )
                - sum(len(item.relations) for item in left_candidates),
                exact_evidence_count_b_minus_a=sum(
                    len(_evidence_set(item)) for item in right_candidates
                )
                - sum(len(_evidence_set(item)) for item in left_candidates),
                ontology_typed_count_b_minus_a=sum(
                    len(_ontology_set(item)) for item in right_candidates
                )
                - sum(len(_ontology_set(item)) for item in left_candidates),
                graph_promotion_eligible_count_b_minus_a=sum(
                    len(_graph_set(item)) for item in right_candidates
                )
                - sum(len(_graph_set(item)) for item in left_candidates),
                entity_jaccard_mean=_mean(jaccards[0]),
                relation_jaccard_mean=_mean(jaccards[1]),
                evidence_jaccard_mean=_mean(jaccards[2]),
                ontology_jaccard_mean=_mean(jaccards[3]),
                graph_promotion_jaccard_mean=_mean(jaccards[4]),
            )
        )

    return EngineParityReport(
        schema_version=ENGINE_PARITY_REPORT_VERSION,
        authority="measurement_only_no_adjudication",
        production_readiness="not_evaluated",
        shared_contract_hash=next(iter(shared_hashes)),
        expected_chunk_set_hash=namespace_hash("input-set", expected),
        expected_chunks=len(expected),
        engines=engine_rows,
        pairwise=pair_rows,
    )
