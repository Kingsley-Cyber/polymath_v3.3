#!/usr/bin/env python3
"""Score the frozen Meridian development fixture at every Graphify checkpoint."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO / "backend"))

from dotenv import load_dotenv

load_dotenv(REPO / ".env", override=False)

from config import get_settings
from motor.motor_asyncio import AsyncIOMotorClient
from neo4j import AsyncGraphDatabase


SCORER_VERSION = "meridian-stage-scorer-v1"
MATCH_CLASSES = (
    "EXACT",
    "DECLARED_ALIAS",
    "NORMALIZED_VARIANT",
    "DECLARED_SUBSUMPTION",
    "NO_MATCH",
)
FAILURE_CLASSES = (
    "MISSING_ENTITY",
    "GENERIC_ENTITY_FALSE_POSITIVE",
    "MISSING_PAIR",
    "WRONG_ARGUMENT_ALIGNMENT",
    "WRONG_DIRECTION",
    "WRONG_PREDICATE",
    "ENTITY_TYPE_ERROR",
    "ENDPOINT_SIGNATURE_ERROR",
    "POLARITY_ERROR",
    "MODALITY_ERROR",
    "ATTRIBUTION_ERROR",
    "CONDITIONAL_SCOPE_ERROR",
    "KNOWN_FALSE_PROMOTION",
    "DUPLICATE_PROPOSITION",
    "GATE_ERROR",
    "SCORER_ERROR",
)
TYPE_MAP = {
    "Person": "person",
    "Org": "organization",
    "Software": "software",
    "Library": "software",
    "Service": "software",
    "Dataset": "artifact",
    "Document": "document",
    "Concept": "concept",
    "Process": "method",
    "Metric": "concept",
    "Location": "location",
    "Event": "event",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", str(value).casefold()))


def normalized(value: str) -> str:
    return " ".join(tokens(value))


def predicate_key(value: str) -> str:
    return "_".join(tokens(value))


def local_url(value: str, service: str, port: int, scheme: str) -> str:
    result = str(value)
    result = result.replace(f"{scheme}://{service}:{port}", f"{scheme}://127.0.0.1:{port}")
    return result.replace(f"@{service}:{port}", f"@127.0.0.1:{port}")


class Policy:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.aliases: dict[str, str] = {}
        for group in payload["declared_alias_groups"]:
            canonical = normalized(group["canonical"])
            self.aliases[canonical] = canonical
            for alias in group["aliases"]:
                self.aliases[normalized(alias)] = canonical
        self.subsumptions = {
            (normalized(row["gold"]), normalized(row["predicted"]))
            for row in payload["declared_subsumptions"]
        }
        self.surface = {
            predicate: tuple(normalized(cue) for cue in cues)
            for predicate, cues in payload["surface_predicate_lexicon"].items()
        }

    def canonical(self, value: str) -> str:
        value_norm = normalized(value)
        return self.aliases.get(value_norm, value_norm)

    def name_class(self, gold: str, predicted: str) -> str:
        if gold == predicted:
            return "EXACT"
        gold_norm = normalized(gold)
        predicted_norm = normalized(predicted)
        gold_alias = self.aliases.get(gold_norm)
        predicted_alias = self.aliases.get(predicted_norm)
        if gold_norm != predicted_norm and gold_alias and gold_alias == predicted_alias:
            return "DECLARED_ALIAS"
        if gold_norm == predicted_norm:
            return "NORMALIZED_VARIANT"
        if (gold_norm, predicted_norm) in self.subsumptions:
            return "DECLARED_SUBSUMPTION"
        return "NO_MATCH"

    def pair(self, gold: tuple[str, str, str], subject: str, obj: str) -> bool:
        return self.name_class(gold[0], subject) != "NO_MATCH" and self.name_class(gold[2], obj) != "NO_MATCH"

    def reverse_pair(self, gold: tuple[str, str, str], subject: str, obj: str) -> bool:
        return self.name_class(gold[0], obj) != "NO_MATCH" and self.name_class(gold[2], subject) != "NO_MATCH"

    def triple_class(self, gold: tuple[str, str, str], predicted: dict[str, Any]) -> str:
        if predicate_key(gold[1]) != predicate_key(predicted["predicate"]):
            return "NO_MATCH"
        endpoint = (
            self.name_class(gold[0], predicted["subject"]),
            self.name_class(gold[2], predicted["object"]),
        )
        if "NO_MATCH" in endpoint:
            return "NO_MATCH"
        for name in ("DECLARED_SUBSUMPTION", "NORMALIZED_VARIANT", "DECLARED_ALIAS", "EXACT"):
            if name in endpoint:
                return name
        return "NO_MATCH"

    def surface_matches(self, predicate: str, surface: str) -> bool:
        value = normalized(surface)
        return any(value == cue or re.search(rf"(?:^| )({re.escape(cue)})(?: |$)", value) for cue in self.surface[predicate])


def parse_answer_key(path: Path) -> tuple[list[dict[str, str]], list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    entities: list[dict[str, str]] = []
    positives: list[tuple[str, str, str]] = []
    traps: list[tuple[str, str, str]] = []
    section = ""
    triple_pattern = re.compile(r"^\d+\. `(.+?) —\[([^]]+)\]→ (.+?)`$")
    entity_pattern = re.compile(r"^\|\s*\d+\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|$")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("# Expected Entities"):
            section = "entities"
        elif line.startswith("# Expected Positive Triples"):
            section = "positives"
        elif line.startswith("# Negative / Trap Relations"):
            section = "traps"
        elif line.startswith("# Alias Resolution") or line.startswith("# Accuracy Check"):
            section = ""
        entity_match = entity_pattern.match(line)
        if section == "entities" and entity_match:
            entities.append({"name": entity_match.group(1), "type": entity_match.group(2)})
        triple_match = triple_pattern.match(line)
        if triple_match and section in {"positives", "traps"}:
            triple = (triple_match.group(1), triple_match.group(2), triple_match.group(3))
            (positives if section == "positives" else traps).append(triple)
    return entities, positives, traps


def argument_name(argument: dict[str, Any], entity_by_id: dict[str, dict[str, Any]]) -> str:
    entity = entity_by_id.get(str(argument.get("entity_id") or "")) or {}
    return str(entity.get("canonical_name") or argument.get("normalized_value") or argument.get("surface") or "")


def raw_contains(surface: str, name: str, policy: Policy) -> bool:
    surface_tokens = policy.canonical(surface).split()
    name_tokens = policy.canonical(name).split()
    width = len(name_tokens)
    return any(surface_tokens[index:index + width] == name_tokens for index in range(len(surface_tokens) - width + 1))


async def promoted_rows(args: argparse.Namespace, policy: Policy) -> list[dict[str, Any]]:
    if args.promoted_json:
        payload = json.loads(Path(args.promoted_json).read_text(encoding="utf-8"))
        return [
            {"subject": str(row["subject"]), "predicate": predicate_key(row["predicate"]), "object": str(row["object"])}
            for row in payload
        ]
    settings = get_settings()
    uri = os.environ.get("GRAPHIFY_E2E_NEO4J_URI") or local_url(settings.NEO4J_URI, "neo4j", 7687, "bolt")
    driver = AsyncGraphDatabase.driver(uri, auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD))
    try:
        async with driver.session() as session:
            result = await session.run(
                """
                MATCH (s:Entity)-[r:RELATES_TO]->(o:Entity)
                WHERE $corpus_id IN coalesce(r.corpus_ids, [])
                RETURN coalesce(s.display_name, s.canonical_name, s.entity_id) AS subject,
                       coalesce(r.predicate, 'related_to') AS predicate,
                       coalesce(o.display_name, o.canonical_name, o.entity_id) AS object
                ORDER BY subject, predicate, object
                """,
                corpus_id=args.corpus_id,
            )
            return [
                {"subject": str(row["subject"]), "predicate": predicate_key(row["predicate"]), "object": str(row["object"])}
                async for row in result
            ]
    finally:
        await driver.close()


async def audit(args: argparse.Namespace) -> int:
    manifest = json.loads((HERE / "FREEZE_MANIFEST.json").read_text(encoding="utf-8"))
    freeze = manifest["meridian_adversarial_44"]
    fixture = Path(freeze["fixture"]["path"])
    answer_key = Path(freeze["answer_key"]["path"])
    policy_path = REPO / freeze["matching_policy"]["path"]
    for name, path, expected in (
        ("fixture", fixture, freeze["fixture"]["sha256"]),
        ("answer_key", answer_key, freeze["answer_key"]["sha256"]),
        ("matching_policy", policy_path, freeze["matching_policy"]["sha256"]),
    ):
        if sha256(path) != expected:
            raise RuntimeError(f"frozen {name} digest mismatch")
    policy = Policy(json.loads(policy_path.read_text(encoding="utf-8")))
    gold_entities, gold, traps = parse_answer_key(answer_key)
    if (len(gold_entities), len(gold), len(traps)) != (45, 44, 3):
        raise RuntimeError("frozen answer-key counts changed")

    settings = get_settings()
    mongo = AsyncIOMotorClient(local_url(settings.MONGODB_URI, "mongodb", 27017, "mongodb"))
    try:
        database = mongo[args.mongo_database]
        artifacts = await database["graphify_stage_artifacts"].find(
            {"corpus_id": args.corpus_id, "doc_id": args.document_id}, {"_id": 0},
        ).to_list(length=None)
    finally:
        mongo.close()
    payloads = {str(row["stage"]): dict(row["payload"]) for row in artifacts}
    required_stages = {
        "NORMALIZED", "ENTITY_REDUCTION_COMPLETE", "MENTION_COMPLETION_COMPLETE",
        "OPENIE_EXTRACTION_COMPLETE", "OPENIE_ARGUMENT_ADAPTATION_COMPLETE",
        "OPENIE_PREDICATE_COMPILATION_COMPLETE", "OPENIE_ASSERTION_ASSEMBLY_COMPLETE",
        "RELATION_COMPILATION_COMPLETE", "ASSERTION_VALIDATION_COMPLETE",
    }
    if missing := sorted(required_stages - set(payloads)):
        raise RuntimeError(f"missing stage artifacts: {missing}")

    relation_payload = payloads["RELATION_COMPILATION_COMPLETE"]
    entity_rows = [*payloads["ENTITY_REDUCTION_COMPLETE"]["entities"], *relation_payload.get("endpoint_entities", [])]
    eligible_entities = [row for row in entity_rows if row.get("state") in {"promoted", "document_local"}]
    entity_by_id = {str(row["entity_id"]): row for row in entity_rows}
    entity_names = sorted({str(row["canonical_name"]) for row in eligible_entities})
    entity_types: dict[str, set[str]] = {}
    for row in eligible_entities:
        entity_types.setdefault(policy.canonical(str(row["canonical_name"])), set()).add(str(row["entity_type"]))

    argument_rows = payloads["OPENIE_ARGUMENT_ADAPTATION_COMPLETE"]["arguments"]
    argument_by_id = {str(row["argument_id"]): row for row in argument_rows}
    roles_by_proposition: dict[str, dict[str, dict[str, Any]]] = {}
    for row in argument_rows:
        roles_by_proposition.setdefault(str(row["proposition_id"]), {})[str(row["role"])] = row
    raw_rows = payloads["OPENIE_EXTRACTION_COMPLETE"]["propositions"]
    adapted_rows = []
    for proposition_id, roles in roles_by_proposition.items():
        if set(roles) >= {"subject", "object"}:
            adapted_rows.append({
                "source": "openie_adapted",
                "id": proposition_id,
                "subject": argument_name(roles["subject"], entity_by_id),
                "object": argument_name(roles["object"], entity_by_id),
                "subject_kind": roles["subject"].get("kind"),
                "object_kind": roles["object"].get("kind"),
            })

    candidate_rows = []
    candidate_by_id: dict[str, dict[str, Any]] = {}
    for row in payloads["OPENIE_PREDICATE_COMPILATION_COMPLETE"]["candidates"]:
        item = {
            **row,
            "source": "openie",
            "subject": argument_name(argument_by_id[str(row["subject_argument_id"])], entity_by_id),
            "object": argument_name(argument_by_id[str(row["object_argument_id"])], entity_by_id),
        }
        candidate_rows.append(item)
        candidate_by_id[str(row["candidate_id"])] = item
    assertion_rows = []
    for row in payloads["OPENIE_ASSERTION_ASSEMBLY_COMPLETE"]["assertions"]:
        candidate = candidate_by_id[str(row["candidate_id"])]
        assertion_rows.append({**candidate, **row, "source": "openie"})

    mention_rows = [*payloads["MENTION_COMPLETION_COMPLETE"]["mentions"], *relation_payload.get("endpoint_mentions", [])]
    mention_by_id = {str(row["mention_id"]): row for row in mention_rows}
    syntax_rows = []
    for row in payloads["ASSERTION_VALIDATION_COMPLETE"]["mapped_relations"]:
        subject_mention = mention_by_id.get(str(row["subject_mention_id"])) or {}
        object_mention = mention_by_id.get(str(row["object_mention_id"])) or {}
        subject_entity = entity_by_id.get(str(subject_mention.get("entity_id") or "")) or {}
        object_entity = entity_by_id.get(str(object_mention.get("entity_id") or "")) or {}
        syntax_rows.append({
            **row,
            "source": "syntax",
            "subject": str(subject_entity.get("canonical_name") or subject_mention.get("surface") or ""),
            "object": str(object_entity.get("canonical_name") or object_mention.get("surface") or ""),
            "surface_relation": str(row.get("surface_predicate") or ""),
            "canonical_predicate": row.get("canonical_candidate"),
            "lane": str(row.get("terminal_state") or ""),
        })
    internal_pairs = [*adapted_rows, *syntax_rows]
    canonical_rows = [*candidate_rows, *syntax_rows]
    terminal_rows = [
        row for row in [*assertion_rows, *syntax_rows]
        if row.get("lane") in {"FACT", "accepted"}
    ]
    promoted = await promoted_rows(args, policy)

    match_rank = {name: index for index, name in enumerate(MATCH_CLASSES)}
    used_predictions: set[int] = set()
    comparisons = []
    failure_counts: Counter[str] = Counter()
    for index, gold_row in enumerate(gold, start=1):
        candidates = [
            (policy.triple_class(gold_row, row), prediction_index, row)
            for prediction_index, row in enumerate(promoted)
            if prediction_index not in used_predictions and policy.triple_class(gold_row, row) != "NO_MATCH"
        ]
        candidates.sort(key=lambda item: (match_rank[item[0]], item[1]))
        match_class, prediction_index, prediction = candidates[0] if candidates else ("NO_MATCH", None, None)
        if prediction_index is not None:
            used_predictions.add(prediction_index)

        subject_key = policy.canonical(gold_row[0])
        object_key = policy.canonical(gold_row[2])
        subject_found = subject_key in entity_types
        object_found = object_key in entity_types
        expected_subject_type = TYPE_MAP[next(row["type"] for row in gold_entities if policy.name_class(row["name"], gold_row[0]) != "NO_MATCH")]
        expected_object_type = TYPE_MAP[next(row["type"] for row in gold_entities if policy.name_class(row["name"], gold_row[2]) != "NO_MATCH")]
        type_ok = (
            expected_subject_type in entity_types.get(subject_key, set())
            and expected_object_type in entity_types.get(object_key, set())
        )
        raw_pair = any(raw_contains(row["subject"], gold_row[0], policy) and raw_contains(row["object"], gold_row[2], policy) for row in raw_rows)
        pair_generated = any(policy.pair(gold_row, row["subject"], row["object"]) for row in internal_pairs)
        reverse_generated = any(policy.reverse_pair(gold_row, row["subject"], row["object"]) for row in internal_pairs)
        surface_generated = any(
            policy.pair(gold_row, row["subject"], row["object"])
            and policy.surface_matches(gold_row[1], str(row.get("surface_relation") or ""))
            for row in canonical_rows
        )
        canonical_generated = any(
            policy.pair(gold_row, row["subject"], row["object"])
            and row.get("canonical_predicate") == gold_row[1]
            for row in canonical_rows
        )
        terminal_generated = any(
            policy.pair(gold_row, row["subject"], row["object"])
            and row.get("canonical_predicate") == gold_row[1]
            for row in terminal_rows
        )
        checkpoint = {
            "gold_endpoint_entities_generated": subject_found and object_found,
            "gold_endpoint_pair_generated": pair_generated,
            "correct_surface_proposition_generated": surface_generated,
            "correct_canonical_candidate_generated": canonical_generated,
            "correct_terminal_positive_assertion_generated": terminal_generated,
            "correct_canonical_assertion_promoted": match_class != "NO_MATCH",
        }
        failure = None
        reason = None
        if match_class == "NO_MATCH":
            matching_canonical = [
                row for row in canonical_rows
                if policy.pair(gold_row, row["subject"], row["object"])
                and row.get("canonical_predicate") == gold_row[1]
            ]
            matching_terminal = [
                row for row in [*assertion_rows, *syntax_rows]
                if policy.pair(gold_row, row["subject"], row["object"])
                and row.get("canonical_predicate") == gold_row[1]
            ]
            if not subject_found or not object_found:
                failure, reason = "MISSING_ENTITY", "one or both canonical endpoint entities were unavailable"
            elif not type_ok:
                failure, reason = "ENTITY_TYPE_ERROR", "one or both endpoint entity types differ from the frozen key"
            elif not pair_generated and reverse_generated:
                failure, reason = "WRONG_DIRECTION", "the endpoint pair was generated only in reverse"
            elif raw_pair and not pair_generated:
                failure, reason = "WRONG_ARGUMENT_ALIGNMENT", "raw OpenIE arguments contained both endpoints but alignment lost the pair"
            elif not pair_generated:
                failure, reason = "MISSING_PAIR", "no aligned OpenIE or deterministic syntax pair was generated"
            elif not surface_generated or not canonical_generated:
                failure, reason = "WRONG_PREDICATE", "the directed pair existed without the required surface and canonical predicate"
            elif any("endpoint_signature" in str(row.get("mapping_rule") or "") for row in matching_canonical):
                failure, reason = "ENDPOINT_SIGNATURE_ERROR", "the correct canonical candidate was blocked by endpoint signature policy"
            elif any(str(row.get("polarity") or "positive") != "positive" for row in matching_terminal):
                failure, reason = "POLARITY_ERROR", "the matching assertion was marked non-positive"
            elif any(str(row.get("modality") or "asserted") != "asserted" for row in matching_terminal):
                failure, reason = "MODALITY_ERROR", "the matching assertion was marked modal"
            elif any(str(row.get("attribution") or "direct") not in {"", "direct"} for row in matching_terminal):
                failure, reason = "ATTRIBUTION_ERROR", "the matching assertion was attributed"
            elif not terminal_generated:
                failure, reason = "GATE_ERROR", "the correct candidate did not enter the positive terminal lane"
            else:
                failure, reason = "GATE_ERROR", "the correct terminal assertion did not reach Neo4j"
            failure_counts[failure] += 1
        comparisons.append({
            "id": index,
            "gold": {"subject": gold_row[0], "predicate": gold_row[1], "object": gold_row[2]},
            "match_class": match_class,
            "prediction": prediction,
            "checkpoints": checkpoint,
            "failure_class": failure,
            "failure_reason": reason,
        })

    gold_entity_hits = []
    used_entity_names: set[str] = set()
    entity_type_errors = []
    for row in gold_entities:
        matches = [name for name in entity_names if policy.name_class(row["name"], name) != "NO_MATCH"]
        if matches:
            selected = sorted(matches)[0]
            used_entity_names.add(selected)
            gold_entity_hits.append(row["name"])
            if TYPE_MAP[row["type"]] not in entity_types.get(policy.canonical(selected), set()):
                entity_type_errors.append(row["name"])
    entity_precision = len(gold_entity_hits) / len(entity_names) if entity_names else 0.0
    entity_recall = len(gold_entity_hits) / len(gold_entities)
    extra_entity_names = sorted(set(entity_names) - used_entity_names)
    failure_counts["GENERIC_ENTITY_FALSE_POSITIVE"] += len(extra_entity_names)

    match_counts = Counter(row["match_class"] for row in comparisons)
    true_positive = len(comparisons) - match_counts["NO_MATCH"]
    triple_precision = true_positive / len(promoted) if promoted else 0.0
    triple_recall = true_positive / len(gold)
    triple_f1 = 2 * triple_precision * triple_recall / (triple_precision + triple_recall) if triple_precision + triple_recall else 0.0
    directed_pair_hits = sum(row["checkpoints"]["gold_endpoint_pair_generated"] for row in comparisons)
    undirected_pair_hits = sum(
        any(
            policy.pair(gold[index], row["subject"], row["object"])
            or policy.reverse_pair(gold[index], row["subject"], row["object"])
            for row in internal_pairs
        )
        for index in range(len(gold))
    )
    canonical_given_pair = sum(row["checkpoints"]["correct_canonical_candidate_generated"] for row in comparisons)
    trap_rows = []
    for index, trap in enumerate(traps, start=1):
        leaked = [row for row in promoted if policy.triple_class(trap, row) != "NO_MATCH"]
        trap_rows.append({"id": index, "gold": trap, "positive_graph_leak": bool(leaked), "predictions": leaked})
    trap_leaks = sum(row["positive_graph_leak"] for row in trap_rows)
    failure_counts["KNOWN_FALSE_PROMOTION"] += trap_leaks

    text = str(payloads["NORMALIZED"]["document"]["normalized_text"])
    evidence_rows = payloads["ASSERTION_VALIDATION_COMPLETE"]["mapped_relations"]
    evidence_aligned = sum(
        text[int(row["evidence_start"]):int(row["evidence_end"])] == row["evidence_text"]
        for row in evidence_rows
    )
    openie_report = payloads["OPENIE_ASSERTION_ASSEMBLY_COMPLETE"].get("report", {})
    syntax_report = relation_payload.get("report", {})
    forced_related = int(payloads["OPENIE_PREDICATE_COMPILATION_COMPLETE"].get("report", {}).get("forced_related_to_fallbacks", 0))
    forced_related += sum(
        row.get("canonical_candidate") == "related_to" and "related" not in str(row.get("surface_predicate") or "").casefold()
        for row in syntax_rows
    )
    promoted_counter = Counter((policy.canonical(row["subject"]), row["predicate"], policy.canonical(row["object"])) for row in promoted)
    duplicate_promoted = sum(count - 1 for count in promoted_counter.values() if count > 1)

    checkpoints = {
        key: sum(row["checkpoints"][key] for row in comparisons)
        for key in comparisons[0]["checkpoints"]
    }
    checkpoints["gold_total"] = len(gold)
    checkpoints["recall"] = {key: value / len(gold) for key, value in checkpoints.items() if key != "gold_total"}
    thresholds = manifest["release_thresholds"]
    gates = {
        "entity_recall": entity_recall >= thresholds["entity_recall_min"],
        "entity_precision": entity_precision >= thresholds["entity_precision_min"],
        "undirected_pair_recall": undirected_pair_hits / len(gold) >= thresholds["undirected_gold_pair_recall_min"],
        "directed_pair_recall": directed_pair_hits / len(gold) >= thresholds["directed_gold_pair_recall_min"],
        "predicate_accuracy_given_directed_pair": (canonical_given_pair / directed_pair_hits if directed_pair_hits else 0.0) >= thresholds["predicate_accuracy_given_directed_pair_min"],
        "canonical_triple_precision": triple_precision >= thresholds["canonical_triple_precision_min"],
        "strict_canonical_triple_f1": triple_f1 >= thresholds["meridian_strict_f1_min"],
        "trap_leakage_zero": trap_leaks == thresholds["trap_leakage_max"],
        "wildcard_terminal_endpoints_zero": int(openie_report.get("wildcard_non_reject_endpoints", 0)) == 0 and int(syntax_report.get("wildcard_non_rejected_endpoints", 0)) == 0,
        "forced_related_to_fallback_zero": forced_related == thresholds["forced_related_to_fallback_max"],
        "exact_evidence_alignment": evidence_aligned == len(evidence_rows),
        "duplicate_promoted_proposition_zero": duplicate_promoted == 0,
    }
    score = {
        "status": "passed" if all(gates.values()) else "failed",
        "evaluation_class": "exposed_development_regression",
        "scorer_version": SCORER_VERSION,
        "matching_policy_version": policy.payload["version"],
        "freeze_id": manifest["freeze_id"],
        "namespace": {"mongo_database": args.mongo_database, "corpus_id": args.corpus_id, "document_id": args.document_id},
        "counts": {"gold_entities": len(gold_entities), "predicted_entities": len(entity_names), "gold_positive": len(gold), "promoted_positive": len(promoted), "true_positive": true_positive, "false_positive": len(promoted) - len(used_predictions), "false_negative": len(gold) - true_positive, "trap_leaks": trap_leaks},
        "entity_metrics": {"precision": entity_precision, "recall": entity_recall, "type_errors": entity_type_errors, "missing": sorted(set(row["name"] for row in gold_entities) - set(gold_entity_hits)), "extra": extra_entity_names},
        "pair_metrics": {"directed_recall": directed_pair_hits / len(gold), "undirected_recall": undirected_pair_hits / len(gold)},
        "predicate_metrics": {"correct_given_directed_pair": canonical_given_pair, "directed_pairs": directed_pair_hits, "accuracy_given_directed_pair": canonical_given_pair / directed_pair_hits if directed_pair_hits else 0.0},
        "triple_metrics": {"precision": triple_precision, "recall": triple_recall, "f1": triple_f1},
        "match_class_counts": {name: match_counts[name] for name in MATCH_CLASSES},
        "failure_taxonomy_counts": {name: failure_counts[name] for name in FAILURE_CLASSES},
        "recall_checkpoints": checkpoints,
        "assertion_containment": {"traps": trap_rows, "leakage": trap_leaks},
        "hard_invariants": {"evidence_rows": len(evidence_rows), "evidence_aligned": evidence_aligned, "forced_related_to_fallbacks": forced_related, "duplicate_promoted_propositions": duplicate_promoted, "openie_wildcard_non_reject_endpoints": int(openie_report.get("wildcard_non_reject_endpoints", 0)), "syntax_wildcard_non_rejected_endpoints": int(syntax_report.get("wildcard_non_rejected_endpoints", 0))},
        "gates": gates,
        "prohibited_matchers_used": [],
    }
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "score.json").write_text(json.dumps(score, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / "comparison.jsonl").write_text("\n".join(json.dumps(row, sort_keys=True) for row in comparisons) + "\n", encoding="utf-8")
    print(json.dumps(score, indent=2, sort_keys=True))
    return 0 if score["status"] == "passed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mongo-database", required=True)
    parser.add_argument("--corpus-id", required=True)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--promoted-json")
    return asyncio.run(audit(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
