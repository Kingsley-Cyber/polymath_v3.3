#!/usr/bin/env python3
"""Audit a frozen Graphify stress namespace without rerunning extraction."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from run_graphify_fixture_e2e import _Stores, _stage_payloads
from models.graphify_contracts import PipelineStage

SCORER_VERSION = "stress-answer-key-scorer-v4-qualified-taxonomy"
MATCH_CLASSES = (
    "EXACT",
    "DECLARED_ALIAS",
    "NORMALIZED_VARIANT",
    "DECLARED_SUBSUMPTION",
    "NO_MATCH",
)
FAILURE_CLASSES = (
    "MISSING_PAIR",
    "ARGUMENT_ALIGNMENT",
    "ENTITY_TYPE",
    "PREDICATE_MAPPING",
    "DIRECTION",
    "POLARITY",
    "MODALITY",
    "ATTRIBUTION",
    "CONDITIONAL_SCOPE",
    "GATE_POLICY",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tokens(value: str) -> list[str]:
    return re.findall(r"[\w]+", value.casefold().replace("’", "'"))


def _basic_norm(value: str) -> str:
    words = _tokens(value)
    while words and words[0] in {"a", "an", "the"}:
        words.pop(0)
    return " ".join(words)


def _singular(token: str) -> str:
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("sses") and len(token) > 5:
        return token[:-2]
    if token.endswith("ses") and len(token) > 4:
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss") and len(token) > 3:
        return token[:-1]
    return token


def _variant_norm(value: str) -> str:
    words = _tokens(value)
    while words and words[0] in {"a", "an", "the"}:
        words.pop(0)
    words = [word for index, word in enumerate(words) if not (word == "s" and index > 0)]
    if words:
        words[-1] = _singular(words[-1])
    return " ".join(words)


class MatchingPolicy:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.alias_by_name: dict[str, str] = {}
        for group in payload["declared_alias_groups"]:
            canonical = _basic_norm(group["canonical"])
            self.alias_by_name[canonical] = canonical
            for alias in group["aliases"]:
                self.alias_by_name[_basic_norm(alias)] = canonical
        self.subsumptions = {
            (_variant_norm(row["gold"]), _variant_norm(row["predicted"]))
            for row in payload["declared_subsumptions"]
        }
        self.surface_predicates = {
            predicate: tuple(_basic_norm(value) for value in values)
            for predicate, values in payload["surface_predicate_lexicon"].items()
        }

    def name_class(self, gold: str, predicted: str) -> str:
        if gold == predicted:
            return "EXACT"
        gold_basic, predicted_basic = _basic_norm(gold), _basic_norm(predicted)
        if (
            gold_basic != predicted_basic
            and self.alias_by_name.get(gold_basic)
            and self.alias_by_name.get(gold_basic) == self.alias_by_name.get(predicted_basic)
        ):
            return "DECLARED_ALIAS"
        if _variant_norm(gold) == _variant_norm(predicted):
            return "NORMALIZED_VARIANT"
        if (_variant_norm(gold), _variant_norm(predicted)) in self.subsumptions:
            return "DECLARED_SUBSUMPTION"
        return "NO_MATCH"

    def triple_class(self, gold: dict[str, Any], predicted: dict[str, Any]) -> str:
        if str(gold["predicate"]) != str(predicted["predicate"]):
            return "NO_MATCH"
        endpoint_classes = (
            self.name_class(str(gold["subject"]), str(predicted["subject"])),
            self.name_class(str(gold["object"]), str(predicted["object"])),
        )
        if "NO_MATCH" in endpoint_classes:
            return "NO_MATCH"
        for match_class in (
            "DECLARED_SUBSUMPTION",
            "NORMALIZED_VARIANT",
            "DECLARED_ALIAS",
            "EXACT",
        ):
            if match_class in endpoint_classes:
                return match_class
        return "NO_MATCH"

    def pair_matches(self, gold: dict[str, Any], subject: str, obj: str) -> bool:
        return (
            self.name_class(str(gold["subject"]), subject) != "NO_MATCH"
            and self.name_class(str(gold["object"]), obj) != "NO_MATCH"
        )

    def surface_matches(self, predicate: str, surface: str) -> bool:
        normalized = _basic_norm(surface)
        return any(
            normalized == cue or cue in normalized.split(" by ") or cue in normalized
            for cue in self.surface_predicates.get(predicate, ())
        )


async def _promoted_triples(stores: _Stores) -> list[dict[str, Any]]:
    query = """
        MATCH (s:Entity)-[r:RELATES_TO]->(o:Entity)
        WHERE $corpus_id IN coalesce(r.corpus_ids, [])
        RETURN coalesce(s.canonical_name, s.entity_id) AS subject,
               coalesce(r.predicate, 'related_to') AS predicate,
               coalesce(o.canonical_name, o.entity_id) AS object,
               s.entity_id AS subject_entity_id,
               o.entity_id AS object_entity_id,
               size([x IN coalesce(r.evidence_chunk_keys, [])
                     WHERE x STARTS WITH $prefix]) AS evidence_count
        ORDER BY subject, predicate, object
    """
    async with stores.neo4j.session() as session:
        result = await session.run(
            query,
            corpus_id=stores.corpus_id,
            prefix=f"{stores.corpus_id}::",
        )
        return [dict(row) async for row in result]


def _argument_name(
    argument: dict[str, Any], entity_by_id: dict[str, dict[str, Any]],
) -> str:
    entity = entity_by_id.get(argument.get("entity_id")) or {}
    return str(
        entity.get("canonical_name")
        or argument.get("normalized_value")
        or argument.get("surface")
        or ""
    )


def _classify_failure(
    *,
    gold: dict[str, Any],
    policy: MatchingPolicy,
    entity_names: list[str],
    raw_pairs: list[dict[str, Any]],
    adapted_pairs: list[dict[str, Any]],
    candidate_pairs: list[dict[str, Any]],
    assertion_rows: list[dict[str, Any]],
) -> tuple[str, str]:
    matching_assertions = [
        row for row in assertion_rows
        if policy.pair_matches(gold, row["subject"], row["object"])
        and row.get("canonical_predicate") == gold["predicate"]
    ]
    if any(row.get("polarity") not in {None, "positive"} for row in matching_assertions):
        return "POLARITY", "matching assertion was explicitly non-positive"
    if any(row.get("modality") not in {None, "asserted"} for row in matching_assertions):
        return "MODALITY", "matching assertion remained modal"
    if any(row.get("attribution") not in {None, "direct"} for row in matching_assertions):
        return "ATTRIBUTION", "matching assertion remained attributed"
    if any(re.search(r"\b(?:if|unless|provided that|when)\b", row.get("evidence_text", ""), re.I) for row in matching_assertions):
        return "CONDITIONAL_SCOPE", "matching assertion remained inside conditional scope"

    reverse_pair = any(
        policy.name_class(str(gold["subject"]), row["object"]) != "NO_MATCH"
        and policy.name_class(str(gold["object"]), row["subject"]) != "NO_MATCH"
        for row in [*raw_pairs, *adapted_pairs, *candidate_pairs]
    )
    if reverse_pair:
        return "DIRECTION", "gold endpoints were generated in reverse direction"

    subject_entity = any(policy.name_class(str(gold["subject"]), name) != "NO_MATCH" for name in entity_names)
    object_entity = any(policy.name_class(str(gold["object"]), name) != "NO_MATCH" for name in entity_names)
    if not subject_entity or not object_entity:
        missing = []
        if not subject_entity:
            missing.append("subject")
        if not object_entity:
            missing.append("object")
        return "ENTITY_TYPE", f"canonical {' and '.join(missing)} endpoint was not available"

    raw_match = any(policy.pair_matches(gold, row["subject"], row["object"]) for row in raw_pairs)
    adapted_match = any(
        policy.pair_matches(gold, row["subject"], row["object"])
        for row in [*adapted_pairs, *candidate_pairs]
    )
    if raw_match and not adapted_match:
        return "ARGUMENT_ALIGNMENT", "raw endpoint surfaces existed but did not survive argument adaptation"
    if not adapted_match:
        return "MISSING_PAIR", "both endpoint entities existed but no directed endpoint pair was generated"

    surface_match = any(
        policy.pair_matches(gold, row["subject"], row["object"])
        and policy.surface_matches(str(gold["predicate"]), row["surface_relation"])
        for row in candidate_pairs
    )
    canonical_match = any(
        policy.pair_matches(gold, row["subject"], row["object"])
        and row.get("canonical_predicate") == gold["predicate"]
        for row in candidate_pairs
    )
    if not surface_match or not canonical_match:
        return "PREDICATE_MAPPING", "endpoint pair existed but the required surface or canonical predicate did not"
    return "GATE_POLICY", "correct canonical candidate existed but was not promoted to Neo4j"


async def _audit(args: argparse.Namespace) -> int:
    freeze_dir = Path(args.freeze_dir).resolve()
    manifest = json.loads((freeze_dir / "FREEZE_MANIFEST.json").read_text(encoding="utf-8"))
    key_path = freeze_dir / "gold_triples.json"
    fixture_path = freeze_dir / "fixture.md"
    policy_path = freeze_dir / "SCORING_POLICY_V2.json"
    if _sha256(key_path) != manifest["answer_key"]["sha256"]:
        raise RuntimeError("frozen answer key digest mismatch")
    if _sha256(fixture_path) != manifest["fixture"]["sha256"]:
        raise RuntimeError("frozen fixture digest mismatch")
    if _sha256(policy_path) != args.policy_sha256:
        raise RuntimeError("frozen scoring policy digest mismatch")

    gold_payload = json.loads(key_path.read_text(encoding="utf-8"))
    policy_payload = json.loads(policy_path.read_text(encoding="utf-8"))
    policy = MatchingPolicy(policy_payload)
    namespace = manifest["namespace"]["name"]
    doc_id = manifest["namespace"]["document_id"]
    stores = _Stores(namespace)
    try:
        if stores.db_name != manifest["namespace"]["mongo_database"] or stores.corpus_id != manifest["namespace"]["corpus_id"]:
            raise RuntimeError("derived namespace configuration differs from frozen manifest")
        payloads = await _stage_payloads(stores, doc_id)
        promoted_all = await _promoted_triples(stores)
    finally:
        await stores.close()

    entities = payloads[PipelineStage.ENTITY_REDUCTION_COMPLETE.value]["entities"]
    allowed_predicates = set(gold_payload["ontology"]["predicates"])
    promoted = [row for row in promoted_all if row["predicate"] in allowed_predicates]
    promoted_out_of_ontology = [
        row for row in promoted_all if row["predicate"] not in allowed_predicates
    ]
    relation_payload = payloads[PipelineStage.RELATION_COMPILATION_COMPLETE.value]
    endpoint_entities = relation_payload.get("endpoint_entities", [])
    entity_by_id = {row["entity_id"]: row for row in [*entities, *endpoint_entities]}
    entity_names = [str(row["canonical_name"]) for row in entity_by_id.values()]
    arguments = payloads[PipelineStage.OPENIE_ARGUMENT_ADAPTATION_COMPLETE.value]["arguments"]
    argument_by_id = {row["argument_id"]: row for row in arguments}
    argument_by_proposition: dict[str, dict[str, dict[str, Any]]] = {}
    for row in arguments:
        argument_by_proposition.setdefault(row["proposition_id"], {})[row["role"]] = row
    propositions = payloads[PipelineStage.OPENIE_EXTRACTION_COMPLETE.value]["propositions"]
    raw_pairs = [
        {
            "proposition_id": row["proposition_id"],
            "subject": row["subject"],
            "object": row["object"],
            "surface_relation": row["relation"],
        }
        for row in propositions
    ]
    adapted_pairs = []
    for proposition_id, roles in argument_by_proposition.items():
        if set(roles) != {"subject", "object"}:
            continue
        adapted_pairs.append({
            "proposition_id": proposition_id,
            "subject": _argument_name(roles["subject"], entity_by_id),
            "object": _argument_name(roles["object"], entity_by_id),
            "subject_kind": roles["subject"]["kind"],
            "object_kind": roles["object"]["kind"],
        })
    candidates = payloads[PipelineStage.OPENIE_PREDICATE_COMPILATION_COMPLETE.value]["candidates"]
    candidate_pairs = []
    candidate_by_id: dict[str, dict[str, Any]] = {}
    proposition_by_id = {row["proposition_id"]: row for row in propositions}
    for row in candidates:
        subject_arg = argument_by_id[row["subject_argument_id"]]
        object_arg = argument_by_id[row["object_argument_id"]]
        item = {
            **row,
            "subject": _argument_name(subject_arg, entity_by_id),
            "object": _argument_name(object_arg, entity_by_id),
            "evidence_text": proposition_by_id[row["representative_proposition_id"]]["evidence_text"],
        }
        candidate_pairs.append(item)
        candidate_by_id[row["candidate_id"]] = item
    mention_rows = [
        *payloads[PipelineStage.MENTION_COMPLETION_COMPLETE.value]["mentions"],
        *relation_payload.get("endpoint_mentions", []),
    ]
    mention_by_id = {row["mention_id"]: row for row in mention_rows}
    for row in relation_payload.get("mapped_relations", []):
        subject_mention = mention_by_id.get(row["subject_mention_id"]) or {}
        object_mention = mention_by_id.get(row["object_mention_id"]) or {}
        subject_entity = entity_by_id.get(subject_mention.get("entity_id")) or {}
        object_entity = entity_by_id.get(object_mention.get("entity_id")) or {}
        candidate_pairs.append({
            "candidate_id": f"syntax:{row['relation_id']}",
            "subject": str(subject_entity.get("canonical_name") or subject_mention.get("surface") or ""),
            "object": str(object_entity.get("canonical_name") or object_mention.get("surface") or ""),
            "surface_relation": row["surface_predicate"],
            "canonical_predicate": row.get("canonical_candidate"),
            "mapping_status": row.get("terminal_state"),
            "mapping_rule": row.get("mapping_rule"),
            "evidence_text": row.get("evidence_text", ""),
            "polarity": row.get("polarity"),
            "modality": row.get("modality"),
            "attribution": row.get("attribution"),
            "source": "syntax_fast_path",
        })
    assertion_rows = []
    for row in payloads[PipelineStage.OPENIE_ASSERTION_ASSEMBLY_COMPLETE.value]["assertions"]:
        candidate = candidate_by_id[row["candidate_id"]]
        assertion_rows.append({**candidate, **row})

    used_predictions: set[int] = set()
    comparison_rows: list[dict[str, Any]] = []
    checkpoint_rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    class_rank = {name: index for index, name in enumerate(MATCH_CLASSES)}
    for gold in gold_payload["positive_canonical_triples"]:
        possible = [
            (policy.triple_class(gold, predicted), index, predicted)
            for index, predicted in enumerate(promoted)
            if index not in used_predictions and policy.triple_class(gold, predicted) != "NO_MATCH"
        ]
        possible.sort(key=lambda item: (class_rank[item[0]], item[1]))
        match_class, match_index, prediction = possible[0] if possible else ("NO_MATCH", None, None)
        if match_index is not None:
            used_predictions.add(match_index)

        pair_generated = any(policy.pair_matches(gold, row["subject"], row["object"]) for row in [*adapted_pairs, *candidate_pairs])
        surface_generated = any(
            policy.pair_matches(gold, row["subject"], row["object"])
            and policy.surface_matches(str(gold["predicate"]), row["surface_relation"])
            for row in candidate_pairs
        )
        canonical_promoted = match_class != "NO_MATCH"
        checkpoint = {
            "id": gold["id"],
            "endpoint_pair_generated": pair_generated,
            "correct_surface_proposition_generated": surface_generated,
            "correct_canonical_assertion_promoted": canonical_promoted,
        }
        checkpoint_rows.append(checkpoint)
        comparison = {
            "id": gold["id"],
            "match_class": match_class,
            "gold": gold,
            "prediction": prediction,
            "checkpoints": checkpoint,
        }
        if not canonical_promoted:
            failure_class, reason = _classify_failure(
                gold=gold,
                policy=policy,
                entity_names=entity_names,
                raw_pairs=raw_pairs,
                adapted_pairs=adapted_pairs,
                candidate_pairs=candidate_pairs,
                assertion_rows=assertion_rows,
            )
            comparison["failure_class"] = failure_class
            comparison["failure_reason"] = reason
            failures.append({
                "id": gold["id"],
                "failure_class": failure_class,
                "reason": reason,
                "gold": gold,
                "checkpoints": checkpoint,
                "matching_candidates": [
                    {
                        key: row.get(key)
                        for key in (
                            "candidate_id", "source", "subject", "surface_relation",
                            "canonical_predicate", "object", "mapping_status",
                            "mapping_rule", "polarity", "modality", "attribution",
                            "evidence_text",
                        )
                    }
                    for row in candidate_pairs
                    if policy.pair_matches(gold, row["subject"], row["object"])
                ],
                "matching_assertions": [
                    {
                        key: row.get(key)
                        for key in (
                            "candidate_id", "lane", "subject", "canonical_predicate",
                            "object", "polarity", "modality", "attribution",
                            "mapping_status", "mapping_rule", "evidence_text",
                        )
                    }
                    for row in assertion_rows
                    if policy.pair_matches(gold, row["subject"], row["object"])
                ],
            })
        comparison_rows.append(comparison)

    match_counts = Counter(row["match_class"] for row in comparison_rows)
    failure_counts = Counter(row["failure_class"] for row in failures)
    qualified_gold = list(gold_payload.get("qualified_statements", []))
    qualified_leaks = [
        row for row in qualified_gold
        if any(policy.triple_class(row, prediction) != "NO_MATCH" for prediction in promoted)
    ]
    qualified_retained = [
        row for row in qualified_gold
        if any(
            policy.pair_matches(row, assertion["subject"], assertion["object"])
            and assertion.get("canonical_predicate") == row["predicate"]
            and assertion.get("lane") == "QUALIFIED_CLAIM"
            for assertion in assertion_rows
        )
    ]
    matched = len(comparison_rows) - match_counts["NO_MATCH"]
    precision = matched / len(promoted) if promoted else 0.0
    recall = matched / len(comparison_rows) if comparison_rows else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    checkpoints = {
        "gold_total": len(checkpoint_rows),
        "endpoint_pair_generated": sum(row["endpoint_pair_generated"] for row in checkpoint_rows),
        "correct_surface_proposition_generated": sum(row["correct_surface_proposition_generated"] for row in checkpoint_rows),
        "correct_canonical_assertion_promoted": sum(row["correct_canonical_assertion_promoted"] for row in checkpoint_rows),
    }
    checkpoints["recall"] = {
        key: value / checkpoints["gold_total"]
        for key, value in checkpoints.items()
        if key != "gold_total" and isinstance(value, int)
    }
    score = {
        "scorer_version": SCORER_VERSION,
        "matching_policy_version": policy_payload["version"],
        "matching_policy_sha256": args.policy_sha256,
        "freeze_id": manifest["freeze_id"],
        "namespace": namespace,
        "status": "passed" if (
            recall >= 0.80
            and 58 <= len(promoted) <= 72
            and not qualified_leaks
        ) else "failed",
        "counts": {
            "gold_positive": len(comparison_rows),
            "neo4j_promoted_all_predicates": len(promoted_all),
            "neo4j_promoted_in_gold_ontology": len(promoted),
            "neo4j_promoted_out_of_gold_ontology": len(promoted_out_of_ontology),
            "matched_positive": matched,
            "unmatched_gold": match_counts["NO_MATCH"],
            "unmatched_promoted": len(promoted) - len(used_predictions),
            "qualified_gold": len(qualified_gold),
            "qualified_retained": len(qualified_retained),
            "qualified_leaked_to_positive": len(qualified_leaks),
        },
        "match_class_counts": {name: match_counts[name] for name in MATCH_CLASSES},
        "failure_taxonomy_counts": {name: failure_counts[name] for name in FAILURE_CLASSES},
        "metrics": {"precision": precision, "recall": recall, "f1": f1},
        "recall_checkpoints": checkpoints,
        "prohibited_matchers_used": [],
        "qualified": {
            "retained_ids": [row["id"] for row in qualified_retained],
            "leaked_ids": [row["id"] for row in qualified_leaks],
        },
        "out_of_ontology_promoted_predicates": dict(sorted(Counter(
            row["predicate"] for row in promoted_out_of_ontology
        ).items())),
    }
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "score_v4.json").write_text(json.dumps(score, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / "answer_key_comparison_v4.jsonl").write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in comparison_rows) + "\n",
        encoding="utf-8",
    )
    (output_dir / "failure_taxonomy_v4.json").write_text(
        json.dumps({"counts": score["failure_taxonomy_counts"], "failures": failures}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "recall_checkpoints_v4.json").write_text(
        json.dumps({"summary": checkpoints, "assertions": checkpoint_rows}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(score, indent=2, sort_keys=True))
    return 0 if score["status"] == "passed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze-dir", required=True)
    parser.add_argument("--policy-sha256", required=True)
    parser.add_argument("--output-dir", required=True)
    return asyncio.run(_audit(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
