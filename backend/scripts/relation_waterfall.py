"""R0→R7 relation-loss waterfall (owner-demanded, 2026-08-08).

For each of the pack's 38 gold relations, under the composite encoder +
oracle adapter, report where the relation dies and why:

  R0 gold → R1 endpoints discovered → R2 pair proposed → R3 surface frame
  recovered → R4 native predicate identified → R5 native→canonical mapped
  → R6 survives gate (direction/type/scope) → R7 exact canonical triple
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
from collections import Counter

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
os.environ["GRAPHIFY_FORCED_ADAPTERS"] = "cpcs_research_v1"
os.environ.setdefault("GRAPHIFY_ENTITY_PROVIDER", "composite")

from run_oracle_adapter_qual import run, offset_mapper, RAW  # noqa: E402
import asyncio  # noqa: E402

GOLD_DIR = "/Users/king/Downloads/rag_graph_entity_relation_test/expected"


def norm(v):
    return re.sub(r"\s+", " ", str(v or "").casefold()).strip()


def norm_pred(v):
    return re.sub(r"[^A-Z0-9]+", "_", str(v or "").upper()).strip("_")


def main() -> int:
    def load(p): return [json.loads(l) for l in open(p) if l.strip()]
    gold_e = load(f"{GOLD_DIR}/entities.jsonl")
    gold_r = load(f"{GOLD_DIR}/relations.jsonl")
    gold_by_id = {g["id"]: g for g in gold_e}

    db = asyncio.run(run())
    def payload(stage):
        return next(r["payload"] for r in db["graphify_stage_artifacts"].rows if r["stage"] == stage)
    normalized = payload("NORMALIZED")["document"]["normalized_text"]
    reducer = payload("ENTITY_REDUCTION_COMPLETE")
    completion = payload("MENTION_COMPLETION_COMPLETE")["mentions"]
    relstage = payload("RELATION_COMPILATION_COMPLETE")
    validation = payload("ASSERTION_VALIDATION_COMPLETE")
    props = payload("OPENIE_EXTRACTION_COMPLETE")["propositions"]

    map_span = offset_mapper(normalized, RAW)
    ent = {e["entity_id"]: e for e in reducer["entities"]}
    promotable = {i for i, e in ent.items() if e["state"] in ("promoted", "document_local")}
    m2e = {m["mention_id"]: m["entity_id"] for m in completion}
    for m in relstage.get("endpoint_mentions") or []:
        m2e.setdefault(m["mention_id"], m["entity_id"])
    name_of = {i: e["canonical_name"] for i, e in ent.items()}
    for e in relstage.get("endpoint_entities") or []:
        name_of.setdefault(e["entity_id"], e["canonical_name"])
        ent.setdefault(e["entity_id"], e)

    # gold entity id -> our entity id via span IoU on completed mentions
    def iou(a_s, a_e, b_s, b_e):
        ov = max(0, min(a_e, b_e) - max(a_s, b_s))
        un = max(a_e, b_e) - min(a_s, b_s)
        return ov / un if un else 0.0
    gid2entity = {}
    for g in gold_e:
        best, best_iou = None, 0.0
        for m in completion:
            if m["entity_id"] not in promotable or m.get("normalized_start") is None:
                continue
            mapped = map_span(m["normalized_start"], m["normalized_end"])
            if not mapped:
                continue
            value = iou(mapped[0], mapped[1], g["char_start"], g["char_end"])
            if value >= 0.8 and value > best_iou:
                best, best_iou = m["entity_id"], value
        if best:
            gid2entity[g["id"]] = best

    # relation universes
    all_mapped = validation["mapped_relations"]
    pair_rows: dict[frozenset, list] = {}
    for r in all_mapped:
        s_, o_ = m2e.get(r["subject_mention_id"]), m2e.get(r["object_mention_id"])
        if s_ and o_:
            pair_rows.setdefault(frozenset((s_, o_)), []).append(r)
    # openie propositions by rough entity pair (surface containment on names)
    counts = Counter()
    stages = ["R1_endpoints", "R2_pair_proposed", "R3_surface_frame",
              "R4_native_predicate", "R5_canonical_mapped", "R6_gate", "R7_exact"]
    losses = Counter()
    details = []
    for g in gold_r:
        gid = g["id"]
        gs, go = g["subject_id"], g["object_id"]
        gsub, gobj = gold_by_id[gs], gold_by_id[go]
        gold_pred = norm_pred(g["predicate"])
        gold_ptext = norm(g.get("predicate_text"))
        se, oe = gid2entity.get(gs), gid2entity.get(go)
        if not se or not oe:
            losses["R1: endpoint(s) undiscovered"] += 1
            details.append((gid, "R1", f"missing {'subj' if not se else ''}{'+obj' if not oe else ''} "
                                       f"({gsub['text'][:20]} / {gobj['text'][:20]})"))
            continue
        counts["R1_endpoints"] += 1
        rows = pair_rows.get(frozenset((se, oe)), [])
        if not rows:
            losses["R2: no proposal between pair"] += 1
            details.append((gid, "R2", f"{name_of.get(se,'')[:22]} × {name_of.get(oe,'')[:22]}"))
            continue
        counts["R2_pair_proposed"] += 1
        # R3: any row whose evidence covers the gold predicate span or whose
        # surface predicate text appears in the gold predicate text (or v.v.)
        def surface_hit(r):
            sp = norm(r.get("surface_predicate"))
            return sp and (sp in gold_ptext or gold_ptext in sp or
                           norm_pred(sp) == gold_pred or
                           ("HAS_" + norm_pred(sp)) == gold_pred)
        frame_rows = [r for r in rows if surface_hit(r)]
        if not frame_rows:
            losses["R3: pair proposed, gold frame not recovered"] += 1
            details.append((gid, "R3", f"pred wanted '{g.get('predicate_text')}' got " +
                            "|".join(sorted({str(r.get('surface_predicate'))[:18] for r in rows})[:3])))
            continue
        counts["R3_surface_frame"] += 1
        counts["R4_native_predicate"] += 1  # frame hit implies native predicate here
        def canon_hit(r):
            c = norm_pred(r.get("canonical_candidate"))
            sp = norm_pred(r.get("surface_predicate"))
            return gold_pred in {c, sp, "HAS_" + sp}
        mapped_rows = [r for r in frame_rows if canon_hit(r)]
        if not mapped_rows:
            losses["R5: native predicate not mapped to gold canonical"] += 1
            details.append((gid, "R5", f"gold {gold_pred} vs " +
                            "|".join(sorted({str(r.get('canonical_candidate'))[:16] for r in frame_rows})[:3])))
            continue
        counts["R5_canonical_mapped"] += 1
        surviving = [r for r in mapped_rows if r["terminal_state"] in ("accepted", "open")]
        if not surviving:
            losses["R6: gate (state/direction/type)"] += 1
            details.append((gid, "R6", "|".join(sorted({f"{r['terminal_state']}:{r.get('mapping_rule','')[:24]}" for r in mapped_rows})[:2])))
            continue
        counts["R6_gate"] += 1
        def exact(r):
            s_id, o_id = m2e.get(r["subject_mention_id"]), m2e.get(r["object_mention_id"])
            return (norm(name_of.get(s_id)) in {norm(gsub["text"]), norm(gsub.get("canonical_id"))} and
                    norm(name_of.get(o_id)) in {norm(gobj["text"]), norm(gobj.get("canonical_id"))})
        if any(exact(r) for r in surviving):
            counts["R7_exact"] += 1
        else:
            losses["R7: endpoint canonical text differs from gold"] += 1
            details.append((gid, "R7", f"ours '{name_of.get(se,'')[:20]}'→'{name_of.get(oe,'')[:20]}' vs gold '{gsub['text'][:20]}'→'{gobj['text'][:20]}'"))

    entering = 38
    print(f"R0 gold relations: 38")
    for stage in stages:
        print(f"{stage:22} surviving {counts[stage]:>2}/38")
    print("\nLOSS REASONS:")
    for reason, n in losses.most_common():
        print(f"  {n:>2}  {reason}")
    print("\nSAMPLE DETAILS (first 14):")
    for gid, layer, msg in details[:14]:
        print(f"  {gid} @{layer}: {msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
