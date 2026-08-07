"""Downstream replay harness — freeze upstream observations, replay the compiler.

Freeze line: raw GLiNER2 mentions + raw OpenIE propositions (plus the
normalized document and survey, both deterministic functions of the text).
Everything downstream of the freeze line re-executes in-process through the
production functions on every invocation, so compiler behavior can be
debugged repeatedly without touching GLiNER2 or triplet-extract.

    FREEZE:   document · survey · raw_mentions · raw_openie_propositions
    REPLAY:   entity reduction → mention completion → argument alignment
              → proposition reduction → predicate compilation
              → assertion assembly → syntax fast path → FACT merge

Emits a stage waterfall, enforces the conservation equation

    raw propositions = FACT + QUALIFIED_CLAIM + OPEN_RELATION + REVIEW + REJECT

(with alignment-failure sub-attribution — a proposition must never silently
disappear), and, given an answer key, a per-lost-gold first-loss trace.

Usage:
    replay_downstream.py --source-db graphify_e2e_<ns>_<hash> [--answer-key K]
    replay_downstream.py --source-db ... --export-frozen DIR
    replay_downstream.py --frozen-dir DIR [--answer-key K]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from models.graphify_contracts import (  # noqa: E402
    NormalizedDocumentV1,
    OpenIEArgumentKind,
    OpenIERawPropositionV1,
    RawMentionV1,
)
from services.extraction.graphify_survey import DocumentSurveyV1  # noqa: E402
from services.extraction.graphify_argument_adapter import adapt_openie_arguments  # noqa: E402
from services.extraction.graphify_assertion_assembler import assemble_openie_assertions  # noqa: E402
from services.extraction.graphify_completion import complete_document_mentions  # noqa: E402
from services.extraction.graphify_predicate_compiler import compile_openie_predicates  # noqa: E402
from services.extraction.graphify_proposition_reducer import reduce_openie_propositions  # noqa: E402
from services.extraction.graphify_reducer import reduce_document_entities  # noqa: E402
from services.extraction.graphify_relations import run_relation_fast_path  # noqa: E402

FROZEN_FILES = {
    "document": "document.json",
    "survey": "survey.json",
    "mentions": "raw_mentions.jsonl",
    "propositions": "raw_openie_propositions.jsonl",
}


def _load_frozen_from_db(dbname: str) -> dict:
    from dotenv import dotenv_values
    from pymongo import MongoClient

    env = dotenv_values(os.path.join(_BACKEND, "..", ".env"))
    uri = re.sub(r"@mongodb:", "@localhost:", env.get("MONGODB_URI") or env.get("MONGO_URI") or "")
    db = MongoClient(uri, serverSelectionTimeoutMS=8000)[dbname]

    def payload(stage: str) -> dict:
        row = db["graphify_stage_artifacts"].find_one({"stage": stage})
        if row is None:
            raise SystemExit(f"frozen stage artifact missing in {dbname}: {stage}")
        if "payload" in row:
            return row["payload"]
        from services.storage.graphify_artifact_codec import decode_stage_payload

        parts = list(db["graphify_stage_artifact_parts"].find(
            {"artifact_id": row["artifact_id"]}, {"_id": 0, "part": 1, "blob": 1},
        ).sort("part", 1))
        return decode_stage_payload(
            row, lambda _artifact_id, _expected: [item["blob"] for item in parts],
        )

    return {
        "document": payload("NORMALIZED")["document"],
        "survey": payload("SURVEY_COMPLETE")["survey"],
        "mentions": payload("ENTITY_CENSUS_COMPLETE")["mentions"],
        "propositions": payload("OPENIE_EXTRACTION_COMPLETE")["propositions"],
    }


def _load_frozen_from_dir(path: Path) -> dict:
    def rows(name: str) -> list[dict]:
        return [json.loads(line) for line in (path / name).read_text().splitlines() if line.strip()]

    return {
        "document": json.loads((path / FROZEN_FILES["document"]).read_text()),
        "survey": json.loads((path / FROZEN_FILES["survey"]).read_text()),
        "mentions": rows(FROZEN_FILES["mentions"]),
        "propositions": rows(FROZEN_FILES["propositions"]),
    }


def _export_frozen(frozen: dict, path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / FROZEN_FILES["document"]).write_text(json.dumps(frozen["document"], indent=1))
    (path / FROZEN_FILES["survey"]).write_text(json.dumps(frozen["survey"], indent=1))
    for key in ("mentions", "propositions"):
        (path / FROZEN_FILES[key]).write_text(
            "\n".join(json.dumps(row, sort_keys=True) for row in frozen[key]) + "\n"
        )


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()


def _name_match(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    return bool(
        a == b
        or re.search(rf"(?:^| ){re.escape(b)}(?: |$)", a)
        or re.search(rf"(?:^| ){re.escape(a)}(?: |$)", b)
    )


def replay(frozen: dict) -> dict:
    """Run the downstream compiler on frozen observations. Pure; repeatable."""
    document = NormalizedDocumentV1.model_validate(frozen["document"])
    survey = DocumentSurveyV1.model_validate(frozen["survey"])
    raw_mentions = tuple(RawMentionV1.model_validate(item) for item in frozen["mentions"])
    propositions = tuple(
        OpenIERawPropositionV1.model_validate(item) for item in frozen["propositions"]
    )

    entities = tuple(reduce_document_entities(document, raw_mentions, survey).entities)
    completed = tuple(complete_document_mentions(document, entities, raw_mentions).mentions)
    arguments = tuple(adapt_openie_arguments(propositions, completed, entities).arguments)
    families = tuple(reduce_openie_propositions(propositions, arguments).families)
    candidates = tuple(compile_openie_predicates(families, propositions, arguments).candidates)
    assertions = tuple(assemble_openie_assertions(candidates, arguments).assertions)
    fast_path = run_relation_fast_path([document], [survey], completed, entities)

    # FACT merge — the exact promotion contract the pipeline applies
    # (evidence-scoped: shared helper openie_fact_merge_disposition).
    from services.extraction.graphify_relations import openie_fact_merge_disposition

    merged_keys = {
        (m.subject_mention_id, m.object_mention_id, m.canonical_candidate)
        for m in fast_path.mapped_relations if m.terminal_state.value == "accepted"
    }
    qualified_spans_by_key: dict[tuple, list[tuple[int, int]]] = {}
    for m in fast_path.mapped_relations:
        if m.terminal_state.value == "qualified":
            qualified_spans_by_key.setdefault(
                (m.subject_mention_id, m.object_mention_id, m.canonical_candidate), [],
            ).append((m.evidence_start, m.evidence_end))
    merge_fate: dict[str, str] = {}
    for assertion in assertions:
        if assertion.lane != "FACT":
            continue
        if not (assertion.subject_mention_id and assertion.object_mention_id
                and assertion.canonical_predicate):
            merge_fate[assertion.assertion_id] = "fact_without_promotable_endpoints"
            continue
        key = (assertion.subject_mention_id, assertion.object_mention_id,
               assertion.canonical_predicate)
        disposition = openie_fact_merge_disposition(
            key, (assertion.evidence_start, assertion.evidence_end),
            merged_keys, qualified_spans_by_key,
        )
        if disposition == "blocked_same_evidence_qualified":
            merge_fate[assertion.assertion_id] = "blocked_by_qualified_syntax"
        elif disposition == "duplicate":
            merge_fate[assertion.assertion_id] = "already_promoted_by_syntax_or_duplicate"
        else:
            merged_keys.add(key)
            merge_fate[assertion.assertion_id] = "promoted"
    return {
        "document": document, "entities": entities, "completed": completed,
        "propositions": propositions, "arguments": arguments, "families": families,
        "candidates": candidates, "assertions": assertions, "fast_path": fast_path,
        "merge_fate": merge_fate,
    }


def waterfall(state: dict) -> dict:
    propositions = state["propositions"]
    total = len(propositions)
    args_by_prop: dict[str, dict[str, object]] = {}
    for argument in state["arguments"]:
        args_by_prop.setdefault(argument.proposition_id, {})[argument.role] = argument

    def kinds(pid: str) -> tuple[object | None, object | None]:
        pair = args_by_prop.get(pid, {})
        return pair.get("subject"), pair.get("object")

    aligned = pairs = 0
    unresolved_roles = Counter()
    for item in propositions:
        subject, obj = kinds(item.proposition_id)
        s_ok = subject is not None and subject.kind != OpenIEArgumentKind.UNRESOLVED
        o_ok = obj is not None and obj.kind != OpenIEArgumentKind.UNRESOLVED
        if not s_ok:
            unresolved_roles["subject"] += 1
        if not o_ok:
            unresolved_roles["object"] += 1
        aligned += s_ok and o_ok
        pairs += (
            subject is not None and obj is not None
            and subject.kind == OpenIEArgumentKind.ENTITY
            and obj.kind == OpenIEArgumentKind.ENTITY
        )

    family_of_prop: dict[str, str] = {}
    for family in state["families"]:
        for pid in family.rendering_ids:
            family_of_prop[pid] = family.family_id
    retained = sum(1 for item in propositions if item.proposition_id in family_of_prop)

    assertion_by_family = {a.family_id: a for a in state["assertions"]}
    candidate_by_family = {c.family_id: c for c in state["candidates"]}
    mapping_counts: Counter[str] = Counter()
    lane_counts: Counter[str] = Counter()
    graph_eligible = 0
    fates: dict[str, str] = {}
    for item in propositions:
        family_id = family_of_prop.get(item.proposition_id)
        assertion = assertion_by_family.get(family_id)
        candidate = candidate_by_family.get(family_id)
        if assertion is None:
            fates[item.proposition_id] = "LOST_NO_ASSERTION"
            continue
        mapping_counts[candidate.mapping_status if candidate else "?"] += 1
        lane_counts[assertion.lane] += 1
        fate = assertion.lane
        if assertion.lane == "FACT":
            fate = f"FACT:{state['merge_fate'].get(assertion.assertion_id, 'unaccounted')}"
            if state["merge_fate"].get(assertion.assertion_id) == "promoted":
                graph_eligible += 1
        subject, obj = kinds(item.proposition_id)
        if (subject is not None and subject.kind == OpenIEArgumentKind.UNRESOLVED) or (
            obj is not None and obj.kind == OpenIEArgumentKind.UNRESOLVED
        ):
            fate += "+ALIGNMENT_FAILURE"
        fates[item.proposition_id] = fate

    conserved = len(fates) == total and "LOST_NO_ASSERTION" not in fates.values()
    return {
        "raw_propositions": total,
        "arguments_aligned": aligned,
        "entity_pairs_resolved": pairs,
        "family_retained": retained,
        "unresolved_roles": dict(unresolved_roles),
        "mapping_status_by_prop": dict(mapping_counts),
        "assertion_lane_by_prop": dict(lane_counts),
        "graph_eligible_props": graph_eligible,
        "conservation_holds": conserved,
        "fates": fates,
    }


def first_loss(state: dict, gold_path: str) -> list[dict]:
    gold = json.loads(Path(gold_path).read_text())
    triples = gold.get("positive_canonical_triples") or gold
    rows = []
    for t in triples:
        rows.append((
            t.get("subject") or t.get("subject_name") or t.get("head"),
            t.get("predicate") or t.get("relation"),
            t.get("object") or t.get("object_name") or t.get("tail"),
        ))
    args_by_prop: dict[str, dict[str, object]] = {}
    for argument in state["arguments"]:
        args_by_prop.setdefault(argument.proposition_id, {})[argument.role] = argument
    family_of_prop: dict[str, str] = {}
    for family in state["families"]:
        for pid in family.rendering_ids:
            family_of_prop[pid] = family.family_id
    assertion_by_family = {a.family_id: a for a in state["assertions"]}
    candidate_by_family = {c.family_id: c for c in state["candidates"]}
    syntax_accepted = {
        (m.subject_mention_id, m.object_mention_id, m.canonical_candidate)
        for m in state["fast_path"].mapped_relations
        if m.terminal_state.value == "accepted"
    }
    entity_name = {e.entity_id: e.canonical_name for e in state["entities"]}
    mention_entity = {m.mention_id: m.entity_id for m in state["completed"]}

    def syntax_promotes(gs: str, gp: str, go: str) -> bool:
        for s_mid, o_mid, pred in syntax_accepted:
            if pred != gp:
                continue
            s_name = entity_name.get(mention_entity.get(s_mid, ""), "")
            o_name = entity_name.get(mention_entity.get(o_mid, ""), "")
            if _name_match(s_name, gs) and _name_match(o_name, go):
                return True
        return False

    traces = []
    for index, (gs, gp, go) in enumerate(rows, start=1):
        matching = [
            p for p in state["propositions"]
            if _name_match(p.subject, gs) and _name_match(p.object, go)
        ]
        trace = {
            "gold": f"R{index}: {gs} -[{gp}]-> {go}",
            "raw_proposition_found": bool(matching),
            "subject_alignment": False, "object_alignment": False,
            "family_retained": False, "predicate_mapping": None,
            "assertion_lane": None, "graph_promotion": False,
            "syntax_lane_promotes_independently": syntax_promotes(gs, gp, go),
            "first_loss": None,
        }
        best = None
        for p in matching:
            pair = args_by_prop.get(p.proposition_id, {})
            subject, obj = pair.get("subject"), pair.get("object")
            s_ok = subject is not None and subject.kind == OpenIEArgumentKind.ENTITY
            o_ok = obj is not None and obj.kind not in (
                None, OpenIEArgumentKind.UNRESOLVED, OpenIEArgumentKind.EMBEDDED_CLAUSE,
            )
            family_id = family_of_prop.get(p.proposition_id)
            assertion = assertion_by_family.get(family_id)
            candidate = candidate_by_family.get(family_id)
            promoted = bool(
                assertion and assertion.lane == "FACT"
                and state["merge_fate"].get(assertion.assertion_id) == "promoted"
                and assertion.canonical_predicate == gp
            )
            score = (
                s_ok + o_ok + bool(family_id)
                + bool(candidate and candidate.canonical_predicate == gp)
                + bool(assertion and assertion.lane == "FACT") + promoted
            )
            if best is None or score > best[0]:
                best = (score, s_ok, o_ok, family_id, candidate, assertion, promoted)
        if best:
            _, s_ok, o_ok, family_id, candidate, assertion, promoted = best
            trace.update({
                "subject_alignment": s_ok, "object_alignment": o_ok,
                "family_retained": bool(family_id),
                "predicate_mapping": (
                    candidate.canonical_predicate or candidate.mapping_status
                ) if candidate else None,
                "assertion_lane": assertion.lane if assertion else None,
                "graph_promotion": promoted,
            })
        if not trace["raw_proposition_found"]:
            trace["first_loss"] = "upstream: no raw proposition (not a downstream bug)"
        elif not trace["subject_alignment"] or not trace["object_alignment"]:
            trace["first_loss"] = "argument alignment"
        elif not trace["family_retained"]:
            trace["first_loss"] = "proposition reduction"
        elif trace["predicate_mapping"] != gp:
            trace["first_loss"] = f"predicate mapping (got: {trace['predicate_mapping']})"
        elif trace["assertion_lane"] != "FACT":
            trace["first_loss"] = f"assertion status (lane: {trace['assertion_lane']})"
        elif not trace["graph_promotion"]:
            trace["first_loss"] = "promotion/merge contract"
        else:
            trace["first_loss"] = "none — promoted"
        traces.append(trace)
    return traces


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db")
    parser.add_argument("--frozen-dir")
    parser.add_argument("--export-frozen")
    parser.add_argument("--answer-key")
    parser.add_argument("--json-out")
    args = parser.parse_args()
    if not args.source_db and not args.frozen_dir:
        parser.error("need --source-db or --frozen-dir")
    frozen = (
        _load_frozen_from_db(args.source_db) if args.source_db
        else _load_frozen_from_dir(Path(args.frozen_dir))
    )
    if args.export_frozen:
        _export_frozen(frozen, Path(args.export_frozen))
        print(f"frozen observations exported to {args.export_frozen}")
    state = replay(frozen)
    flow = waterfall(state)
    total = flow["raw_propositions"] or 1

    def line(label: str, count: int) -> str:
        return f"{label:34}{count:>6}  {count / total:>6.1%}"

    print(line("RAW PROPOSITIONS", flow["raw_propositions"]))
    print(line("ARGUMENTS ALIGNED", flow["arguments_aligned"]))
    print(line("ENTITY PAIRS RESOLVED", flow["entity_pairs_resolved"]))
    print(line("PROPOSITION FAMILY RETAINED", flow["family_retained"]))
    print("PREDICATE (by proposition)")
    for status, count in sorted(flow["mapping_status_by_prop"].items()):
        print(line(f"  {status.lower()}", count))
    print("ASSERTION LANE (by proposition)")
    for lane, count in sorted(flow["assertion_lane_by_prop"].items()):
        print(line(f"  {lane.lower()}", count))
    print(line("GRAPH ELIGIBLE (openie stream)", flow["graph_eligible_props"]))
    print(f"\nCONSERVATION: {'HOLDS' if flow['conservation_holds'] else 'VIOLATED — propositions lost'}")
    fate_counts = Counter(flow["fates"].values())
    for fate, count in fate_counts.most_common():
        print(f"  {fate:44}{count:>6}")

    result = {"waterfall": {k: v for k, v in flow.items() if k != "fates"},
              "fate_counts": dict(fate_counts)}
    if args.answer_key:
        traces = first_loss(state, args.answer_key)
        lost = [t for t in traces if t["first_loss"] != "none — promoted"
                and not t["syntax_lane_promotes_independently"]]
        print(f"\nGOLD FIRST-LOSS ({len(lost)} lost of {len(traces)}; "
              f"{sum(t['syntax_lane_promotes_independently'] for t in traces)} carried by syntax lane)")
        for t in lost:
            print(f"\n{t['gold']}")
            for key in ("raw_proposition_found", "subject_alignment", "object_alignment",
                        "family_retained", "predicate_mapping", "assertion_lane",
                        "graph_promotion"):
                value = t[key]
                shown = ("YES" if value is True else "NO" if value is False else value)
                print(f"  {key:26} {shown}")
            print(f"  FIRST LOSS: {t['first_loss']}")
        result["first_loss_traces"] = traces
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(result, indent=1, default=str))
        print(f"\njson written: {args.json_out}")


if __name__ == "__main__":
    main()
