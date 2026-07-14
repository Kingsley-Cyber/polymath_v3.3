#!/usr/bin/env python3
"""Run the preregistered C2 ClaimRecordV1 WITH/WITHOUT GLiREL benchmark.

This is a read-only evaluation harness.  It never calls a provider, persists
semantic rows, promotes a candidate, or writes to Mongo, Qdrant, or Neo4j.
The decisive arm uses GLiNER spans under the owner entity registry.  An
oracle-span arm is diagnostic only.  Legacy relation labels are never mapped
onto owner PredicateType values.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
LOCAL = ROOT / "local_ghost_b"
for import_path in (BACKEND, LOCAL, LOCAL / "tools"):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from evals.semantic_extraction_scoring import (  # noqa: E402
    normalize_name,
    score_claim_candidates,
    score_extraction_lane,
)
from models.claim_record import ClaimCompilationV1, ClaimRecordV1  # noqa: E402
from models.hash_taxonomy import namespace_hash  # noqa: E402
from models.local_extraction import (  # noqa: E402
    EntityMention,
    LocalExtractionV1,
    RelationCandidate,
)
from models.registry_loader import (  # noqa: E402
    load_all,
    normalize_predicate_lemma,
)
from models.semantic_artifacts import ObservationBundle  # noqa: E402
from services.ingestion.claim_compiler import (  # noqa: E402
    compile_claim_records_v1,
)
from services.ingestion.semantic_observations import (  # noqa: E402
    build_spacy_observation_bundle,
    compile_local_extraction_v1,
    validate_evidence_round_trip,
)
from glirel_infer import GliRELClassifier, pick_device  # noqa: E402

FROZEN_SPEC_SHA256 = "6e0502d6352786286a583d0943fe083a8abaf1feb506ee4bd31b14d6ddef6de9"
REPORT_SCHEMA = "polymath.glirel_claim_compiler_c2_benchmark.v1"
DEFAULT_SPEC = BACKEND / "evals" / "glirel_claim_compiler_c2_gate_v1.json"
DEFAULT_FIXTURE = BACKEND / "evals" / "semantic_extraction_gold_v1.json"
DEFAULT_CHECKPOINT = ROOT / "models" / "glirel_ghost_b_v1" / "best"
DEFAULT_HISTORICAL = (
    ROOT / "docs" / "baselines" / "SEMANTIC_EXTRACTION_LOCAL_2026-07-13.json"
)


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_line_sha256(value: Any) -> str:
    """Hash one jq-compatible compact JSON line, including its LF terminator."""

    return hashlib.sha256((canonical_json(value) + "\n").encode("utf-8")).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _package_version(name: str) -> str:
    return importlib.metadata.version(name)


def _assert_hash(path: Path, expected: str) -> None:
    observed = sha256_path(path)
    if observed != expected:
        raise ValueError(
            f"frozen input hash mismatch for {path}: {observed} != {expected}"
        )


def verify_frozen_inputs(
    *,
    spec_path: Path,
    fixture_path: Path,
    checkpoint: Path,
    gliner_snapshot: Path,
) -> dict[str, Any]:
    _assert_hash(spec_path, FROZEN_SPEC_SHA256)
    spec = load_json(spec_path)
    if spec.get("schema_version") != "polymath.glirel_claim_compiler_c2_gate.v1":
        raise ValueError("unsupported C2 gate spec")
    _assert_hash(fixture_path, spec["inputs"]["gold_fixture"]["sha256"])
    _assert_hash(
        BACKEND / "registries" / "extraction_vocabularies.v1.json",
        spec["inputs"]["extraction_vocabulary"]["sha256"],
    )
    _assert_hash(
        BACKEND / "registries" / "predicate_normalization.v1.json",
        spec["inputs"]["predicate_normalization"]["sha256"],
    )
    for filename, key in (
        ("pytorch_model.bin", "weights_sha256"),
        ("glirel_config.json", "config_sha256"),
        ("labels.json", "trained_labels_file_sha256"),
    ):
        _assert_hash(checkpoint / filename, spec["model_contract"]["glirel"][key])
    _assert_hash(
        gliner_snapshot / "model.safetensors",
        spec["model_contract"]["gliner"]["weights_sha256"],
    )
    _assert_hash(
        gliner_snapshot / "gliner_config.json",
        spec["model_contract"]["gliner"]["config_sha256"],
    )
    registries = load_all()
    entity_labels = list(registries["vocab"]["entity_types"])
    predicate_labels = list(registries["vocab"]["predicate_types"])
    if (
        canonical_line_sha256(entity_labels)
        != spec["inputs"]["extraction_vocabulary"]["entity_labels_sha256"]
    ):
        raise ValueError("controlled entity-label hash mismatch")
    if (
        canonical_line_sha256(predicate_labels)
        != spec["inputs"]["extraction_vocabulary"]["predicate_labels_sha256"]
    ):
        raise ValueError("controlled predicate-label hash mismatch")
    return spec


def _overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def select_gliner_mentions(
    *,
    sample_id: str,
    text: str,
    raw_entities: Iterable[dict[str, Any]],
    controlled_types: list[str],
) -> tuple[list[EntityMention], Counter[str]]:
    """Select one controlled type per exact span, then remove span overlap.

    Selection is confidence-first with deterministic tie breaks and does not
    inspect fixture gold.  Bad offsets, non-substrings, and out-of-registry
    labels fail closed into counters rather than being coerced.
    """

    type_order = {label: index for index, label in enumerate(controlled_types)}
    by_span: dict[tuple[int, int], dict[str, Any]] = {}
    counts: Counter[str] = Counter()
    for row in raw_entities:
        counts["raw"] += 1
        try:
            start = int(row["start"])
            end = int(row["end"])
            surface = str(row["text"])
            label = str(row["label"])
            score = float(row["score"])
        except (KeyError, TypeError, ValueError):
            counts["malformed"] += 1
            continue
        if label not in type_order:
            counts["label_violations"] += 1
            continue
        if start < 0 or end <= start or text[start:end] != surface:
            counts["offset_violations"] += 1
            continue
        candidate = {
            "start": start,
            "end": end,
            "text": surface,
            "label": label,
            "score": score,
        }
        previous = by_span.get((start, end))
        if previous is None or (
            -score,
            type_order[label],
        ) < (
            -float(previous["score"]),
            type_order[str(previous["label"])],
        ):
            if previous is not None:
                counts["same_span_dropped"] += 1
            by_span[(start, end)] = candidate
        else:
            counts["same_span_dropped"] += 1

    accepted: list[dict[str, Any]] = []
    for candidate in sorted(
        by_span.values(),
        key=lambda item: (
            -float(item["score"]),
            -(int(item["end"]) - int(item["start"])),
            int(item["start"]),
            int(item["end"]),
            type_order[str(item["label"])],
        ),
    ):
        coordinate = (int(candidate["start"]), int(candidate["end"]))
        if any(
            _overlap(
                coordinate,
                (int(existing["start"]), int(existing["end"])),
            )
            for existing in accepted
        ):
            counts["overlap_dropped"] += 1
            continue
        accepted.append(candidate)

    mentions = [
        EntityMention(
            mention_id=namespace_hash(
                "logical-artifact",
                {
                    "kind": "c2-entity-mention",
                    "sample_id": sample_id,
                    "start": item["start"],
                    "end": item["end"],
                    "entity_type": item["label"],
                    "surface": item["text"],
                },
            ),
            text=str(item["text"]),
            entity_type=str(item["label"]),
            start_char=int(item["start"]),
            end_char=int(item["end"]),
            canonical_label=normalize_name(item["text"]),
            confidence=float(item["score"]),
        )
        for item in sorted(
            accepted,
            key=lambda item: (
                int(item["start"]),
                int(item["end"]),
                str(item["label"]),
            ),
        )
    ]
    counts["selected"] = len(mentions)
    return mentions, counts


def oracle_mentions(
    *, sample: dict[str, Any], crosswalk: dict[str, str]
) -> list[EntityMention]:
    text = str(sample["text"])
    mentions: list[EntityMention] = []
    occupied: Counter[tuple[str, str]] = Counter()
    for row in sample["gold"]["entities"]:
        surface = str(row["surface_form"])
        legacy_type = str(row["entity_type"])
        entity_type = crosswalk.get(legacy_type)
        if entity_type is None:
            raise ValueError(f"oracle crosswalk missing {legacy_type}")
        key = (surface, legacy_type)
        occurrence = occupied[key]
        occupied[key] += 1
        start = -1
        for _ in range(occurrence + 1):
            start = text.index(surface, start + 1)
        end = start + len(surface)
        mentions.append(
            EntityMention(
                mention_id=namespace_hash(
                    "logical-artifact",
                    {
                        "kind": "c2-entity-mention",
                        "sample_id": sample["id"],
                        "start": start,
                        "end": end,
                        "entity_type": entity_type,
                        "surface": surface,
                    },
                ),
                text=surface,
                entity_type=entity_type,
                start_char=start,
                end_char=end,
                canonical_label=normalize_name(row["canonical_name"]),
                confidence=1.0,
            )
        )
    return mentions


def _glirel_entities(mentions: list[EntityMention]) -> list[dict[str, Any]]:
    return [
        {
            "canonical_name": item.canonical_label,
            "surface_form": item.text,
            "entity_type": item.entity_type,
            "query_aliases": [],
        }
        for item in mentions
    ]


def _edge_structure(edges: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    return [
        sorted(
            [
                {
                    "sub": normalize_name(edge.get("sub")),
                    "pred": str(edge.get("pred") or ""),
                    "obj": normalize_name(edge.get("obj")),
                    "ev": normalize_name(edge.get("ev")),
                }
                for edge in group
            ],
            key=lambda row: (row["sub"], row["pred"], row["obj"], row["ev"]),
        )
        for group in edges
    ]


def _unique_mention(
    value: Any, mentions: list[EntityMention]
) -> tuple[EntityMention | None, str | None]:
    key = normalize_name(value)
    options = [
        item
        for item in mentions
        if key in {normalize_name(item.text), normalize_name(item.canonical_label)}
    ]
    if len(options) == 1:
        return options[0], None
    return None, "endpoint_missing" if not options else "endpoint_ambiguous"


def _evidence_id(
    edge: dict[str, Any], bundle: ObservationBundle
) -> tuple[str | None, str | None]:
    edge_evidence = normalize_name(edge.get("ev"))
    options = [
        item.evidence_ref_id
        for item in bundle.evidence_refs
        if edge_evidence == normalize_name(item.quote)
    ]
    if len(options) == 1:
        return options[0], None
    return None, "evidence_missing" if not options else "evidence_ambiguous"


def bind_relation_candidates(
    *,
    sample_id: str,
    edges: list[dict[str, Any]],
    mentions: list[EntityMention],
    extraction: LocalExtractionV1,
    bundle: ObservationBundle,
    controlled_predicates: set[str],
) -> tuple[list[RelationCandidate], Counter[str]]:
    """Bind labels to a unique same-sentence typed predicate, not to gold.

    Dependency direction is intentionally left to the certified claim compiler.
    """

    spans = {item.observation_id: item for item in bundle.spans}
    observations_by_coordinate = {}
    for observation in bundle.predicates:
        span = spans[observation.predicate_span_id]
        observations_by_coordinate[(span.start, span.end, span.text)] = observation
    typed_rows = []
    for mention in extraction.predicates:
        observation = observations_by_coordinate[
            (mention.start_char, mention.end_char, mention.surface_text)
        ]
        typed_rows.append((mention, observation.evidence_ref_id))

    candidates: list[RelationCandidate] = []
    counts: Counter[str] = Counter()
    for ordinal, edge in enumerate(
        sorted(
            edges,
            key=lambda row: (
                normalize_name(row.get("sub")),
                str(row.get("pred") or ""),
                normalize_name(row.get("obj")),
                -float(row.get("score") or 0.0),
            ),
        )
    ):
        counts["raw_proposals"] += 1
        label = str(edge.get("pred") or "")
        if label not in controlled_predicates:
            counts["controlled_label_violations"] += 1
            continue
        source, source_error = _unique_mention(edge.get("sub"), mentions)
        target, target_error = _unique_mention(edge.get("obj"), mentions)
        if source_error or target_error or source is None or target is None:
            counts[source_error or target_error or "endpoint_missing"] += 1
            continue
        evidence_id, evidence_error = _evidence_id(edge, bundle)
        if evidence_error or evidence_id is None:
            counts[evidence_error or "evidence_missing"] += 1
            continue
        predicate_options = [
            mention
            for mention, predicate_evidence_id in typed_rows
            if mention.normalized_predicate == label
            and predicate_evidence_id == evidence_id
        ]
        if len(predicate_options) != 1:
            counts[
                "predicate_missing" if not predicate_options else "predicate_ambiguous"
            ] += 1
            continue
        predicate = predicate_options[0]
        candidates.append(
            RelationCandidate(
                relation_id=namespace_hash(
                    "logical-artifact",
                    {
                        "kind": "c2-relation-candidate",
                        "sample_id": sample_id,
                        "ordinal": ordinal,
                        "source": source.mention_id,
                        "predicate": predicate.predicate_id,
                        "target": target.mention_id,
                        "relation_type": label,
                        "evidence": evidence_id,
                    },
                ),
                source_mention_id=source.mention_id,
                predicate_id=predicate.predicate_id,
                target_mention_id=target.mention_id,
                relation_type=label,
                condition_mention_ids=[],
                temporal_mention_ids=[],
                evidence_sentence_ids=[evidence_id],
                confidence=float(edge.get("score") or 0.0),
            )
        )
        counts["bound_candidates"] += 1
    return candidates, counts


def _claim_score_row(claim: ClaimRecordV1) -> dict[str, Any]:
    return {
        "predicate_lemma": claim.predicate_lemma,
        "polarity": "negated" if claim.polarity == "negative" else "affirmed",
        "modal_force": claim.modality,
        "assertion_mode": claim.assertion_mode,
        "claim_type": claim.claim_type,
        "conditions": claim.conditions,
        "exceptions": claim.exceptions,
    }


def _core_score(
    samples: list[dict[str, Any]],
    compilations: dict[str, ClaimCompilationV1],
    bundles: dict[str, ObservationBundle],
) -> dict[str, Any]:
    return score_claim_candidates(
        samples,
        {
            sample_id: [_claim_score_row(claim) for claim in compilation.claims]
            for sample_id, compilation in compilations.items()
        },
        {
            sample_id: [item.model_dump() for item in bundle.qualifiers]
            for sample_id, bundle in bundles.items()
        },
    )


def _core_material(compilation: ClaimCompilationV1) -> list[dict[str, Any]]:
    rows = []
    for claim in compilation.claims:
        row = claim.model_dump()
        row.pop("source_relation_ids", None)
        rows.append(row)
    return sorted(rows, key=lambda row: row["claim_id"])


def _claim_order(
    compilation: ClaimCompilationV1, bundle: ObservationBundle
) -> list[ClaimRecordV1]:
    order = {item.observation_id: index for index, item in enumerate(bundle.predicates)}
    return sorted(
        compilation.claims,
        key=lambda claim: (order[claim.predicate_observation_id], claim.claim_id),
    )


def _support_key(
    *,
    sample_id: str,
    claim: ClaimRecordV1,
    occurrence: int,
) -> tuple[Any, ...] | None:
    subjects = tuple(
        sorted(
            normalize_name(item.surface)
            for item in claim.arguments
            if item.role == "subject"
        )
    )
    objects = tuple(
        sorted(
            normalize_name(item.surface)
            for item in claim.arguments
            if item.role == "object"
        )
    )
    if not subjects or not objects or claim.normalized_predicate is None:
        return None
    return (
        sample_id,
        claim.normalized_predicate,
        normalize_name(claim.predicate_lemma),
        occurrence,
        subjects,
        objects,
    )


def _prf(predicted: Counter[Any], expected: Counter[Any]) -> dict[str, Any]:
    true_positive = sum((predicted & expected).values())
    false_positive = sum(predicted.values()) - true_positive
    false_negative = sum(expected.values()) - true_positive
    precision = (
        true_positive / (true_positive + false_positive)
        if true_positive + false_positive
        else 0.0
    )
    recall = (
        true_positive / (true_positive + false_negative)
        if true_positive + false_negative
        else 0.0
    )
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "tp": true_positive,
        "fp": false_positive,
        "fn": false_negative,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def score_accepted_support(
    *,
    samples: list[dict[str, Any]],
    compilations: dict[str, ClaimCompilationV1],
    bundles: dict[str, ObservationBundle],
) -> tuple[dict[str, Any], Counter[Any]]:
    expected: Counter[Any] = Counter()
    predicted: Counter[Any] = Counter()
    expected_samples: set[str] = set()
    expected_predicates: set[str] = set()

    for sample in samples:
        sample_id = str(sample["id"])
        ordered = _claim_order(compilations[sample_id], bundles[sample_id])
        by_lemma: dict[str, list[tuple[int, ClaimRecordV1]]] = defaultdict(list)
        occurrence_by_lemma: Counter[str] = Counter()
        for claim in ordered:
            lemma = normalize_name(claim.predicate_lemma)
            occurrence = occurrence_by_lemma[lemma]
            occurrence_by_lemma[lemma] += 1
            by_lemma[lemma].append((occurrence, claim))

        expected_options = {lemma: list(rows) for lemma, rows in by_lemma.items()}
        for gold_claim in sample["gold"]["claims"]:
            lemma = normalize_name(gold_claim.get("predicate_lemma"))
            normalized = normalize_predicate_lemma(lemma)
            if normalized is None:
                continue
            options = expected_options.get(lemma) or []
            if not options:
                continue
            occurrence, claim = options.pop(0)
            if claim.typing_status != "typed":
                continue
            key = _support_key(
                sample_id=sample_id,
                claim=claim,
                occurrence=occurrence,
            )
            if key is None:
                continue
            expected[key] += 1
            expected_samples.add(sample_id)
            expected_predicates.add(str(claim.normalized_predicate))

        for lemma_rows in by_lemma.values():
            for occurrence, claim in lemma_rows:
                if not claim.source_relation_ids:
                    continue
                key = _support_key(
                    sample_id=sample_id,
                    claim=claim,
                    occurrence=occurrence,
                )
                if key is None:
                    continue
                predicted[key] += len(claim.source_relation_ids)

    score = _prf(predicted, expected)
    score.update(
        {
            "decision_base_typed_compiled_gold_claims": sum(expected.values()),
            "decision_base_distinct_samples": len(expected_samples),
            "decision_base_distinct_predicate_types": len(expected_predicates),
            "accepted_relation_predictions": sum(predicted.values()),
        }
    )
    return score, expected


def count_untyped_endpoint_agreement(
    *,
    edges: list[dict[str, Any]],
    mentions: list[EntityMention],
    compilation: ClaimCompilationV1,
) -> int:
    count = 0
    for edge in edges:
        source, source_error = _unique_mention(edge.get("sub"), mentions)
        target, target_error = _unique_mention(edge.get("obj"), mentions)
        if source_error or target_error or source is None or target is None:
            continue
        for claim in compilation.claims:
            if claim.typing_status != "untyped":
                continue
            subjects = {
                item.filler_ref
                for item in claim.arguments
                if item.role == "subject" and item.filler_kind == "entity_mention"
            }
            objects = {
                item.filler_ref
                for item in claim.arguments
                if item.role == "object" and item.filler_kind == "entity_mention"
            }
            if source.mention_id in subjects and target.mention_id in objects:
                count += 1
                break
    return count


def _aggregate_receipts(compilations: dict[str, ClaimCompilationV1]) -> dict[str, Any]:
    summed: Counter[str] = Counter()
    recipe_hashes: set[str] = set()
    for compilation in compilations.values():
        receipt = compilation.receipt()
        recipe_hashes.add(str(receipt["compiler_recipe_hash"]))
        for key, value in receipt.items():
            if isinstance(value, int):
                summed[key] += value
    return {
        **dict(sorted(summed.items())),
        "compiler_recipe_hashes": sorted(recipe_hashes),
    }


def _compilation_errors(
    *,
    bundles: dict[str, ObservationBundle],
    compilations: dict[str, ClaimCompilationV1],
) -> int:
    errors = 0
    for sample_id, compilation in compilations.items():
        receipt = compilation.receipt()
        if len(bundles[sample_id].predicates) != (
            int(receipt["claim_count"]) + int(receipt["skipped_predicate_count"])
        ):
            errors += 1
        if int(receipt["claim_count"]) != (
            int(receipt["typed_claim_count"]) + int(receipt["untyped_claim_count"])
        ):
            errors += 1
    return errors


def _relation_reference_errors(
    *,
    extractions: dict[str, LocalExtractionV1],
    compilations: dict[str, ClaimCompilationV1],
) -> int:
    errors = 0
    for sample_id, extraction in extractions.items():
        compilation = compilations[sample_id]
        accepted = {
            relation_id
            for claim in compilation.claims
            for relation_id in claim.source_relation_ids
        }
        rejected = set(compilation.rejected_relation_ids)
        candidate_ids = {item.relation_id for item in extraction.relations}
        if accepted & rejected:
            errors += 1
        if accepted | rejected != candidate_ids:
            errors += 1
    return errors


def _label_predicate_conflicts(
    *,
    extractions: dict[str, LocalExtractionV1],
    compilations: dict[str, ClaimCompilationV1],
) -> int:
    conflicts = 0
    for sample_id, extraction in extractions.items():
        relation_by_id = {item.relation_id: item for item in extraction.relations}
        for claim in compilations[sample_id].claims:
            for relation_id in claim.source_relation_ids:
                if (
                    relation_by_id[relation_id].relation_type
                    != claim.normalized_predicate
                ):
                    conflicts += 1
    return conflicts


def _controlled_legacy_score(
    *,
    samples: list[dict[str, Any]],
    mentions_by_id: dict[str, list[EntityMention]],
    edges_by_id: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    results = []
    for sample in samples:
        sample_id = str(sample["id"])
        results.append(
            {
                "id": sample_id,
                "text": sample["text"],
                "entities": [
                    {
                        "canonical_name": item.canonical_label,
                        "surface_form": item.text,
                        "entity_type": item.entity_type,
                    }
                    for item in mentions_by_id[sample_id]
                ],
                "relations": [
                    {
                        "subject": edge["sub"],
                        "predicate": edge["pred"],
                        "object": edge["obj"],
                        "evidence_phrase": edge["ev"],
                    }
                    for edge in edges_by_id[sample_id]
                ],
            }
        )
    score = score_extraction_lane(samples, results)
    return {
        "label_crosswalk": None,
        "decision_weight": False,
        "entities": score["entities"],
        "relations": score["relations"],
        "relation_evidence_exact_rate": score["relation_evidence_exact_rate"],
        "relation_endpoint_valid_rate": score["relation_endpoint_valid_rate"],
    }


def run_arm(
    *,
    name: str,
    decision_weight: bool,
    samples: list[dict[str, Any]],
    bundles: dict[str, ObservationBundle],
    base_extractions: dict[str, LocalExtractionV1],
    mentions_by_id: dict[str, list[EntityMention]],
    classifier: GliRELClassifier,
    controlled_predicates: list[str],
) -> dict[str, Any]:
    without_extractions: dict[str, LocalExtractionV1] = {}
    without_compilations: dict[str, ClaimCompilationV1] = {}
    chunks = []
    for sample in samples:
        sample_id = str(sample["id"])
        extraction = LocalExtractionV1.model_validate(
            {
                **base_extractions[sample_id].model_dump(),
                "entities": [item.model_dump() for item in mentions_by_id[sample_id]],
                "relations": [],
            }
        )
        without_extractions[sample_id] = extraction
        without_compilations[sample_id] = compile_claim_records_v1(
            bundle=bundles[sample_id], extraction=extraction
        )
        chunks.append(
            {
                "chunk_id": sample_id,
                "doc_id": "semantic-extraction-gold-v1",
                "text": sample["text"],
                "entities": _glirel_entities(mentions_by_id[sample_id]),
            }
        )

    first_edges = classifier.extract_chunks(chunks, unit_batch=64)
    replay_edges = classifier.extract_chunks(chunks, unit_batch=64)
    replay_equal = _edge_structure(first_edges) == _edge_structure(replay_edges)
    edges_by_id = {
        str(sample["id"]): list(edges)
        for sample, edges in zip(samples, first_edges, strict=True)
    }

    with_extractions: dict[str, LocalExtractionV1] = {}
    with_compilations: dict[str, ClaimCompilationV1] = {}
    bind_counts: Counter[str] = Counter()
    untyped_endpoint_agreement = 0
    for sample in samples:
        sample_id = str(sample["id"])
        relation_candidates, counts = bind_relation_candidates(
            sample_id=sample_id,
            edges=edges_by_id[sample_id],
            mentions=mentions_by_id[sample_id],
            extraction=without_extractions[sample_id],
            bundle=bundles[sample_id],
            controlled_predicates=set(controlled_predicates),
        )
        bind_counts.update(counts)
        extraction = LocalExtractionV1.model_validate(
            {
                **without_extractions[sample_id].model_dump(),
                "relations": [item.model_dump() for item in relation_candidates],
            }
        )
        with_extractions[sample_id] = extraction
        with_compilations[sample_id] = compile_claim_records_v1(
            bundle=bundles[sample_id], extraction=extraction
        )
        untyped_endpoint_agreement += count_untyped_endpoint_agreement(
            edges=edges_by_id[sample_id],
            mentions=mentions_by_id[sample_id],
            compilation=without_compilations[sample_id],
        )

    core_without = _core_score(samples, without_compilations, bundles)
    core_with = _core_score(samples, with_compilations, bundles)
    core_material_equal = all(
        _core_material(without_compilations[sample_id])
        == _core_material(with_compilations[sample_id])
        for sample_id in without_compilations
    )
    support_without, decision_base = score_accepted_support(
        samples=samples,
        compilations=without_compilations,
        bundles=bundles,
    )
    support_with, decision_base_with = score_accepted_support(
        samples=samples,
        compilations=with_compilations,
        bundles=bundles,
    )
    if decision_base != decision_base_with:
        raise ValueError("WITH changed the typed compiled gold decision base")

    evidence_errors = sum(
        len(validate_evidence_round_trip(bundles[str(sample["id"])], sample["text"]))
        for sample in samples
    )
    claim_conservation_errors = _compilation_errors(
        bundles=bundles, compilations=without_compilations
    ) + _compilation_errors(bundles=bundles, compilations=with_compilations)
    relation_reference_errors = _relation_reference_errors(
        extractions=with_extractions, compilations=with_compilations
    )
    label_predicate_conflicts = _label_predicate_conflicts(
        extractions=with_extractions, compilations=with_compilations
    )
    label_counts = Counter(
        str(edge.get("pred") or "") for edges in edges_by_id.values() for edge in edges
    )

    return {
        "name": name,
        "decision_weight": decision_weight,
        "entity_mention_count": sum(map(len, mentions_by_id.values())),
        "relation_structure_replay_equal": replay_equal,
        "raw_relation_proposals": sum(map(len, edges_by_id.values())),
        "raw_relation_labels": dict(sorted(label_counts.items())),
        "binding": dict(sorted(bind_counts.items())),
        "without": {
            "compiler_receipt": _aggregate_receipts(without_compilations),
            "core_quality": core_without,
            "accepted_support": support_without,
        },
        "with": {
            "compiler_receipt": _aggregate_receipts(with_compilations),
            "core_quality": core_with,
            "accepted_support": support_with,
        },
        "invariants": {
            "core_claim_material_equal": core_material_equal,
            "core_quality_equal": core_with == core_without,
            "accepted_label_predicate_conflicts": label_predicate_conflicts,
            "evidence_round_trip_errors": evidence_errors,
            "claim_conservation_errors": claim_conservation_errors,
            "relation_reference_errors": relation_reference_errors,
            "controlled_label_violations": int(
                bind_counts["controlled_label_violations"]
            ),
        },
        "future_hypothesis_observation_only": {
            "untyped_claim_endpoint_agreeing_proposals": untyped_endpoint_agreement,
            "decision_weight": False,
        },
        "legacy_exact_span_diagnostic": _controlled_legacy_score(
            samples=samples,
            mentions_by_id=mentions_by_id,
            edges_by_id=edges_by_id,
        ),
        "per_sample_counts": [
            {
                "sample_id": str(sample["id"]),
                "entities": len(mentions_by_id[str(sample["id"])]),
                "raw_relation_proposals": len(edges_by_id[str(sample["id"])]),
                "bound_relation_candidates": len(
                    with_extractions[str(sample["id"])].relations
                ),
                "accepted_relations": sum(
                    len(claim.source_relation_ids)
                    for claim in with_compilations[str(sample["id"])].claims
                ),
                "rejected_relations": len(
                    with_compilations[str(sample["id"])].rejected_relation_ids
                ),
            }
            for sample in samples
        ],
    }


def evaluate_gate(*, spec: dict[str, Any], decisive: dict[str, Any]) -> dict[str, Any]:
    with_support = decisive["with"]["accepted_support"]
    without_support = decisive["without"]["accepted_support"]
    minimums = spec["decision_base"]["thin_evidence_minimums"]
    checks = {
        "thin_evidence_typed_claims": with_support[
            "decision_base_typed_compiled_gold_claims"
        ]
        >= minimums["typed_compiled_gold_claims"],
        "thin_evidence_distinct_samples": with_support["decision_base_distinct_samples"]
        >= minimums["distinct_samples"],
        "thin_evidence_distinct_predicates": with_support[
            "decision_base_distinct_predicate_types"
        ]
        >= minimums["distinct_predicate_types"],
        "core_claim_projection_with_equals_without": (
            decisive["invariants"]["core_claim_material_equal"]
            and decisive["invariants"]["core_quality_equal"]
        ),
        "accepted_support_f1_strictly_improves": (
            with_support["f1"] > without_support["f1"]
        ),
        "accepted_support_precision_minimum": (
            with_support["precision"]
            >= spec["pass_gate"]["accepted_support_precision_minimum"]
        ),
        "accepted_label_predicate_conflicts_zero": (
            decisive["invariants"]["accepted_label_predicate_conflicts"]
            <= spec["pass_gate"]["accepted_label_predicate_conflicts_maximum"]
        ),
        "evidence_round_trip_errors_zero": (
            decisive["invariants"]["evidence_round_trip_errors"]
            <= spec["pass_gate"]["evidence_round_trip_errors_maximum"]
        ),
        "claim_conservation_errors_zero": (
            decisive["invariants"]["claim_conservation_errors"]
            <= spec["pass_gate"]["claim_conservation_errors_maximum"]
        ),
        "relation_reference_errors_zero": (
            decisive["invariants"]["relation_reference_errors"]
            <= spec["pass_gate"]["relation_reference_errors_maximum"]
        ),
        "controlled_label_violations_zero": (
            decisive["invariants"]["controlled_label_violations"]
            <= spec["pass_gate"]["controlled_label_violations_maximum"]
        ),
    }
    thin_keys = [key for key in checks if key.startswith("thin_evidence_")]
    thin_evidence_met = all(checks[key] for key in thin_keys)
    non_thin_checks = all(
        value for key, value in checks.items() if key not in thin_keys
    )
    if not thin_evidence_met:
        verdict = "insufficient_evidence"
    elif non_thin_checks:
        verdict = "with_wins"
    else:
        verdict = "without_wins"
    return {
        "verdict": verdict,
        "stage4_disposition": (
            "eligible_for_owner_ratification_candidate_only"
            if verdict == "with_wins"
            else "relations_remain_observation_only"
        ),
        "minimum_accepted_precision": spec["pass_gate"][
            "accepted_support_precision_minimum"
        ],
        "thin_evidence_minimums": minimums,
        "checks": checks,
        "all_non_thin_checks_pass": non_thin_checks,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument("--historical", type=Path, default=DEFAULT_HISTORICAL)
    parser.add_argument(
        "--gliner-snapshot",
        type=Path,
        default=(
            Path.home()
            / ".cache/huggingface/hub/models--urchade--gliner_medium-v2.1"
            / "snapshots/40ec419335d09393f298636f471328b722c6da9e"
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    spec = verify_frozen_inputs(
        spec_path=args.spec,
        fixture_path=args.fixture,
        checkpoint=args.checkpoint,
        gliner_snapshot=args.gliner_snapshot,
    )
    fixture = load_json(args.fixture)
    if fixture.get("schema_version") != "polymath.semantic_extraction_gold.v1":
        raise ValueError("unsupported gold fixture")
    samples = list(fixture["samples"])

    import spacy
    from gliner import GLiNER

    nlp = spacy.load(spec["model_contract"]["spacy"]["model"])
    if spacy.__version__ != spec["model_contract"]["spacy"]["required_spacy_version"]:
        raise ValueError("spaCy version differs from frozen C2 contract")
    if (
        nlp.meta.get("version")
        != spec["model_contract"]["spacy"]["required_model_version"]
    ):
        raise ValueError("spaCy model version differs from frozen C2 contract")

    bundles: dict[str, ObservationBundle] = {}
    base_extractions: dict[str, LocalExtractionV1] = {}
    for sample in samples:
        sample_id = str(sample["id"])
        child_id = f"child:semantic-extraction-gold-v1:{sample_id}"
        bundle = build_spacy_observation_bundle(
            text=str(sample["text"]),
            nlp=nlp,
            source_version_id="source-version:semantic-extraction-gold-v1",
            hierarchy_node_id=child_id,
            parser_id=spec["model_contract"]["spacy"]["model"],
            parser_version=str(spacy.__version__),
        )
        bundles[sample_id] = bundle
        base_extractions[sample_id] = compile_local_extraction_v1(
            bundle,
            document_id="doc:semantic-extraction-gold-v1",
            child_id=child_id,
        ).extraction

    registries = load_all()
    entity_labels = list(registries["vocab"]["entity_types"])
    predicate_labels = list(registries["vocab"]["predicate_types"])

    gliner = GLiNER.from_pretrained(str(args.gliner_snapshot), local_files_only=True)
    raw_batches = gliner.batch_predict_entities(
        [str(sample["text"]) for sample in samples],
        entity_labels,
        threshold=float(spec["model_contract"]["gliner"]["threshold"]),
        batch_size=len(samples),
    )
    decisive_mentions: dict[str, list[EntityMention]] = {}
    gliner_selection_counts: Counter[str] = Counter()
    for sample, rows in zip(samples, raw_batches, strict=True):
        mentions, counts = select_gliner_mentions(
            sample_id=str(sample["id"]),
            text=str(sample["text"]),
            raw_entities=rows or [],
            controlled_types=entity_labels,
        )
        decisive_mentions[str(sample["id"])] = mentions
        gliner_selection_counts.update(counts)

    classifier = GliRELClassifier(
        str(args.checkpoint),
        predicate_labels,
        pick_device(),
        threshold=float(spec["model_contract"]["glirel"]["threshold"]),
        type_gate=False,
        danger_guard=False,
    )
    decisive = run_arm(
        name=spec["arms"]["decisive"]["name"],
        decision_weight=True,
        samples=samples,
        bundles=bundles,
        base_extractions=base_extractions,
        mentions_by_id=decisive_mentions,
        classifier=classifier,
        controlled_predicates=predicate_labels,
    )
    decisive["gliner_selection"] = dict(sorted(gliner_selection_counts.items()))

    oracle_crosswalk = dict(spec["arms"]["diagnostic"]["entity_type_crosswalk"])
    oracle_by_id = {
        str(sample["id"]): oracle_mentions(sample=sample, crosswalk=oracle_crosswalk)
        for sample in samples
    }
    diagnostic = run_arm(
        name=spec["arms"]["diagnostic"]["name"],
        decision_weight=False,
        samples=samples,
        bundles=bundles,
        base_extractions=base_extractions,
        mentions_by_id=oracle_by_id,
        classifier=classifier,
        controlled_predicates=predicate_labels,
    )
    diagnostic["nonlexical_entity_crosswalks"] = spec["arms"]["diagnostic"][
        "nonlexical_crosswalks"
    ]

    historical = load_json(args.historical)
    historical_hash = sha256_path(args.historical)
    gate = evaluate_gate(spec=spec, decisive=decisive)
    report = {
        "schema_version": REPORT_SCHEMA,
        "run_mode": {
            "read_only": True,
            "provider_calls": 0,
            "persistence_writes": 0,
            "promotions": 0,
            "graph_writes": 0,
            "vector_writes": 0,
        },
        "frozen_gate": {
            "path": str(args.spec.relative_to(ROOT)),
            "sha256": FROZEN_SPEC_SHA256,
            "published_pre_inference_commit": "0165254039b175903288298b1f546d42d801b52a",
        },
        "fixture": {
            "path": str(args.fixture.relative_to(ROOT)),
            "sha256": spec["inputs"]["gold_fixture"]["sha256"],
            "samples": len(samples),
            "gold_entities": sum(len(sample["gold"]["entities"]) for sample in samples),
            "legacy_gold_relations": sum(
                len(sample["gold"]["relations"]) for sample in samples
            ),
            "gold_claims": sum(len(sample["gold"]["claims"]) for sample in samples),
            "legacy_relation_crosswalk": None,
        },
        "provenance": {
            "python": platform.python_version(),
            "spacy": _package_version("spacy"),
            "spacy_model": {
                "id": spec["model_contract"]["spacy"]["model"],
                "version": nlp.meta.get("version"),
                "pipeline": list(nlp.pipe_names),
            },
            "gliner": {
                "package_version": _package_version("gliner"),
                "model_id": spec["model_contract"]["gliner"]["model_id"],
                "revision": spec["model_contract"]["gliner"]["revision"],
                "config_sha256": spec["model_contract"]["gliner"]["config_sha256"],
                "weights_sha256": spec["model_contract"]["gliner"]["weights_sha256"],
                "threshold": spec["model_contract"]["gliner"]["threshold"],
                "entity_labels_sha256": spec["inputs"]["extraction_vocabulary"][
                    "entity_labels_sha256"
                ],
                "entity_label_count": len(entity_labels),
            },
            "glirel": {
                "package_version": _package_version("glirel"),
                "checkpoint": spec["model_contract"]["glirel"]["checkpoint"],
                "config_sha256": spec["model_contract"]["glirel"]["config_sha256"],
                "weights_sha256": spec["model_contract"]["glirel"]["weights_sha256"],
                "trained_labels_file_sha256": spec["model_contract"]["glirel"][
                    "trained_labels_file_sha256"
                ],
                "inference_predicate_labels_sha256": spec["inputs"][
                    "extraction_vocabulary"
                ]["predicate_labels_sha256"],
                "inference_predicate_label_count": len(predicate_labels),
                "threshold": spec["model_contract"]["glirel"]["threshold"],
                "fixed_relation_types": True,
                "legacy_type_gate": False,
                "device": pick_device(),
            },
            "predicate_normalization_sha256": spec["inputs"]["predicate_normalization"][
                "sha256"
            ],
            "runtime_limitations": [
                "The installed transformers stack emitted its known incorrect-regex warning while loading the frozen DeBERTa tokenizers; the benchmark did not alter tokenizer or checkpoint configuration.",
                "GLiNER 0.2.26 marks batch_predict_entities deprecated; this frozen benchmark retained the preregistered API and model behavior.",
            ],
        },
        "decisive": decisive,
        "diagnostic_oracle_spans": diagnostic,
        "historical_open_label_diagnostic": {
            "path": str(args.historical.relative_to(ROOT)),
            "sha256": historical_hash,
            "decision_weight": False,
            "glirel_oracle_relation_f1": historical["glirel_oracle_entities"]["score"][
                "relations"
            ]["f1"],
            "gliner_then_glirel_relation_f1": historical["gliner_then_glirel"]["score"][
                "relations"
            ]["f1"],
        },
        "gate": gate,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "out": str(args.out),
                "gate_spec_sha256": FROZEN_SPEC_SHA256,
                "verdict": gate["verdict"],
                "stage4_disposition": gate["stage4_disposition"],
                "decision_base": decisive["with"]["accepted_support"][
                    "decision_base_typed_compiled_gold_claims"
                ],
                "without_support_f1": decisive["without"]["accepted_support"]["f1"],
                "with_support_f1": decisive["with"]["accepted_support"]["f1"],
                "with_support_precision": decisive["with"]["accepted_support"][
                    "precision"
                ],
                "minimum_precision": gate["minimum_accepted_precision"],
                "raw_relation_proposals": decisive["raw_relation_proposals"],
                "accepted_relations": decisive["with"]["compiler_receipt"][
                    "glirel_agree_count"
                ],
                "untyped_endpoint_agreeing_proposals": decisive[
                    "future_hypothesis_observation_only"
                ]["untyped_claim_endpoint_agreeing_proposals"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
