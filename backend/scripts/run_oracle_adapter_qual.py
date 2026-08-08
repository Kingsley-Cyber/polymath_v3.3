"""Oracle-adapter qualification run (owner-authorized 2026-08-08).

Same source, windows, checkpoint, OpenIE, gates, canonicalization — ONLY the
active entity schema changes (the pack's own labels via the sanctioned
corpus-adapter interface, adapter_release cpcs-research-v1). Rescores A–D.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

os.environ["GRAPHIFY_FORCED_ADAPTERS"] = "cpcs_research_v1"

RAW = Path("/Users/king/Downloads/rag_graph_entity_relation_test/source_document.md").read_text()
GOLD = "/Users/king/Downloads/rag_graph_entity_relation_test/expected"
OUT = Path("/Users/king/polymath_v3.3/data_eval/final_qual_oracle")
OUT.mkdir(parents=True, exist_ok=True)


def _matches(row, query):
    return all(row.get(k) == v for k, v in query.items())


class _Collection:
    def __init__(self):
        self.rows = []
    async def find_one(self, query, projection=None):
        return next((deepcopy(r) for r in self.rows if _matches(r, query)), None)
    async def update_one(self, query, update, upsert=False):
        row = next((r for r in self.rows if _matches(r, query)), None)
        if row is None and upsert:
            row = dict(query); self.rows.append(row)
            for k, v in (update.get("$setOnInsert") or {}).items():
                row[k] = deepcopy(v)
        if row is not None:
            for k, v in (update.get("$set") or {}).items():
                row[k] = deepcopy(v)
        return SimpleNamespace(modified_count=1 if row else 0)
    async def insert_one(self, row):
        self.rows.append(deepcopy(row)); return SimpleNamespace(inserted_id=len(self.rows))
    async def count_documents(self, query):
        return sum(_matches(r, query) for r in self.rows)


class _Db(dict):
    def __missing__(self, key):
        v = _Collection(); self[key] = v; return v


async def run():
    from services.extraction.graphify_pipeline import run_graphify_pipeline
    from services.extraction.gliner2_cpu_provider import get_gliner2_cpu_provider
    db = _Db()
    await run_graphify_pipeline(
        db=db, corpus_id="oracle", doc_id="doc", text=RAW,
        children=[SimpleNamespace(chunk_id="doc:1", text=RAW)],
        provider=get_gliner2_cpu_provider(),
    )
    return db


def offset_mapper(ours, theirs):
    blocks = difflib.SequenceMatcher(None, ours, theirs, autojunk=False).get_matching_blocks()
    def map_span(start, end):
        for b in blocks:
            if b.a <= start and end <= b.a + b.size:
                return start + (b.b - b.a), end + (b.b - b.a)
        return None
    return map_span


def main() -> int:
    db = asyncio.run(run())
    def payload(stage):
        return next(r["payload"] for r in db["graphify_stage_artifacts"].rows if r["stage"] == stage)
    normalized = payload("NORMALIZED")["document"]["normalized_text"]
    reducer = payload("ENTITY_REDUCTION_COMPLETE")
    completion = payload("MENTION_COMPLETION_COMPLETE")["mentions"]
    relstage = payload("RELATION_COMPILATION_COMPLETE")
    validation = payload("ASSERTION_VALIDATION_COMPLETE")
    census_report = payload("ENTITY_CENSUS_COMPLETE")["report"]
    print("adapters active:", census_report.get("adapters") or census_report.get("schema_release"))

    map_span = offset_mapper(normalized, RAW)
    ent = {e["entity_id"]: e for e in reducer["entities"]}
    promotable = {i for i, e in ent.items() if e["state"] in ("promoted", "document_local")}
    rows = []
    for idx, m in enumerate(completion):
        if m["entity_id"] not in promotable or m.get("normalized_start") is None:
            continue
        mapped = map_span(m["normalized_start"], m["normalized_end"])
        if mapped is None:
            continue
        e = ent[m["entity_id"]]
        facet = (e.get("facet") or "").strip()
        label = facet.upper() if facet else e["entity_type"].upper()
        rows.append({"id": f"p{idx}", "text": m["surface"], "label": label,
                     "char_start": mapped[0], "char_end": mapped[1],
                     "canonical_id": e["canonical_name"], "entity_id": m["entity_id"]})
    (OUT / "entities.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    m2e = {m["mention_id"]: m["entity_id"] for m in completion}
    for m in relstage.get("endpoint_mentions") or []:
        m2e.setdefault(m["mention_id"], m["entity_id"])
    name_of = {i: e["canonical_name"] for i, e in ent.items()}
    for e in relstage.get("endpoint_entities") or []:
        name_of.setdefault(e["entity_id"], e["canonical_name"])
    rels, seen = [], set()
    for r in validation["mapped_relations"]:
        if r["terminal_state"] not in ("accepted", "open"):
            continue
        s_, o_ = m2e.get(r["subject_mention_id"]), m2e.get(r["object_mention_id"])
        if not s_ or not o_:
            continue
        pred = r.get("canonical_candidate") or r.get("surface_predicate") or ""
        if "structured_data" in ";".join(r.get("reasons") or []) and r.get("surface_predicate"):
            pred = "HAS_" + re.sub(r"[^A-Za-z0-9]+", "_", r["surface_predicate"]).upper()
        row = {"subject": name_of.get(s_, ""), "predicate": pred.upper().replace(" ", "_"),
               "object": name_of.get(o_, ""), "state": r["terminal_state"]}
        key = (row["subject"].casefold(), row["predicate"], row["object"].casefold())
        if key not in seen:
            seen.add(key); rels.append(row)
    (OUT / "relations.jsonl").write_text("\n".join(json.dumps(r) for r in rels) + "\n")
    print(f"exported entities {len(rows)} relations {len(rels)}")

    # A–D waterfall
    def load(p): return [json.loads(l) for l in open(p) if l.strip()]
    gold_e, gold_r = load(f"{GOLD}/entities.jsonl"), load(f"{GOLD}/relations.jsonl")
    def iou(a, b):
        ov = max(0, min(a["char_end"], b["char_end"]) - max(a["char_start"], b["char_start"]))
        un = max(a["char_end"], b["char_end"]) - min(a["char_start"], b["char_start"])
        return ov / un if un else 0.0
    cands = sorted(((iou(p, g), pi, gi) for pi, p in enumerate(rows) for gi, g in enumerate(gold_e) if iou(p, g) >= 0.8), reverse=True)
    mp, mg, pair = set(), set(), {}
    for _s, pi, gi in cands:
        if pi not in mp and gi not in mg:
            mp.add(pi); mg.add(gi); pair[gi] = pi
    A = len(mg)
    B = sum(1 for gi, pi in pair.items() if rows[pi]["label"] == gold_e[gi]["label"])
    proposed = set()
    for r in validation["mapped_relations"]:
        s_, o_ = m2e.get(r["subject_mention_id"]), m2e.get(r["object_mention_id"])
        if s_ and o_:
            proposed.add(frozenset((s_, o_)))
    gid2e = {gold_e[gi]["id"]: rows[pi]["entity_id"] for gi, pi in pair.items()}
    C_both = C_link = 0
    for r in gold_r:
        se, oe = gid2e.get(r["subject_id"]), gid2e.get(r["object_id"])
        if se and oe:
            C_both += 1
            if frozenset((se, oe)) in proposed:
                C_link += 1
    print(f"A entity discovery: {A}/60 = {A/60:.0%}")
    print(f"B typing|discovery: {B}/{max(A,1)} = {B/max(A,1):.0%}")
    print(f"C endpoints found:  {C_both}/38 = {C_both/38:.0%} | pair proposed: {C_link}/38 = {C_link/38:.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
