"""Export locked extraction as GLiNER/GLiREL-style predictions for scoring.

Post-lock scoring adapter for the final qualification (Pegasus/CPCS pack).
GENERAL conversions only — nothing keyed to this fixture's content:

  * offsets: our normalized_text spans → the pack's raw file offsets via a
    difflib opcode map (exact on equal blocks; no fuzz)
  * labels: one general map from our frozen entity types (+facets) to the
    pack's schema labels where a 1:1 semantic correspondence exists;
    unmapped types keep our label (scored as a miss — ontology distance is
    part of the honest result)
  * predicates: canonical + OPEN surface predicates uppercased verbatim;
    kv-metadata relations use the general attribute convention HAS_<KEY>
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
from pathlib import Path

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from dotenv import dotenv_values  # noqa: E402
from pymongo import MongoClient  # noqa: E402

ENV = dotenv_values(os.path.join(_BACKEND, "..", ".env"))

CORPUS = sys.argv[1] if len(sys.argv) > 1 else "8e55b1a0-fac8-4d37-bdcb-ea9d59281782"
RAW = Path("/Users/king/Downloads/rag_graph_entity_relation_test/source_document.md").read_text()
OUT_DIR = Path("/Users/king/polymath_v3.3/data_eval/final_qual_predictions")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# General type→label map (1:1 semantic correspondences only).
LABEL_MAP = {
    ("artifact", "document_identifier"): "DOCUMENT_ID",
    ("document", ""): "DOCUMENT",
    ("timereference", ""): "TIME_REFERENCE",
    ("event", ""): "EVENT",
    ("method", ""): "METHOD",
    ("concept", ""): "CONCEPT",
    ("software", ""): "MODEL",
    ("standard", ""): "PROTOCOL",
    ("product", ""): "TOOL",
}


def _db():
    uri = re.sub(r"@mongodb:", "@localhost:", ENV.get("MONGODB_URI") or ENV.get("MONGO_URI") or "")
    return MongoClient(uri, serverSelectionTimeoutMS=8000)[ENV.get("MONGODB_DB", "polymath")]


def _payload(db, stage):
    row = db.graphify_stage_artifacts.find_one({"corpus_id": CORPUS, "stage": stage})
    if row is None:
        raise SystemExit(f"missing artifact {stage}")
    if "payload" in row:
        return row["payload"]
    from services.storage.graphify_artifact_codec import decode_stage_payload
    parts = list(db.graphify_stage_artifact_parts.find(
        {"artifact_id": row["artifact_id"]}).sort("part", 1))
    return decode_stage_payload(row, lambda a, n: [p["blob"] for p in parts])


def offset_mapper(ours: str, theirs: str):
    matcher = difflib.SequenceMatcher(None, ours, theirs, autojunk=False)
    blocks = matcher.get_matching_blocks()

    def map_span(start: int, end: int):
        for block in blocks:
            if block.a <= start and end <= block.a + block.size:
                delta = block.b - block.a
                return start + delta, end + delta
        return None
    return map_span


def main() -> int:
    db = _db()
    normalized = _payload(db, "NORMALIZED")["document"]["normalized_text"]
    reducer = _payload(db, "ENTITY_REDUCTION_COMPLETE")
    completion = _payload(db, "MENTION_COMPLETION_COMPLETE")["mentions"]
    validation = _payload(db, "ASSERTION_VALIDATION_COMPLETE")
    relation_stage = _payload(db, "RELATION_COMPILATION_COMPLETE")

    map_span = offset_mapper(normalized, RAW)
    entity_by_id = {e["entity_id"]: e for e in reducer["entities"]}
    promotable = {e["entity_id"] for e in reducer["entities"]
                  if e["state"] in ("promoted", "document_local")}

    rows, unmapped_spans = [], 0
    for index, m in enumerate(completion):
        if m["entity_id"] not in promotable:
            continue
        if m.get("normalized_start") is None:
            continue
        mapped = map_span(m["normalized_start"], m["normalized_end"])
        if mapped is None:
            unmapped_spans += 1
            continue
        entity = entity_by_id[m["entity_id"]]
        label = LABEL_MAP.get((entity["entity_type"], entity.get("facet") or "")) \
            or LABEL_MAP.get((entity["entity_type"], "")) \
            or entity["entity_type"].upper()
        rows.append({
            "id": f"p{index}",
            "text": m["surface"],
            "label": label,
            "char_start": mapped[0],
            "char_end": mapped[1],
            "canonical_id": entity["canonical_name"],
            "entity_id": m["entity_id"],
        })
    (OUT_DIR / "entities.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n")

    mention_entity = {m["mention_id"]: m["entity_id"] for m in completion}
    # Relation-local endpoints (structured/kv lane, discourse subjects,
    # relation-minted values) live in the validation payload, not in the
    # completion stage — index them too or every kv relation is dropped.
    for m in (relation_stage.get("endpoint_mentions") or []) + (validation.get("endpoint_mentions") or []):
        mention_entity.setdefault(m["mention_id"], m["entity_id"])
    name_of = {e["entity_id"]: e["canonical_name"] for e in reducer["entities"]}
    for e in (relation_stage.get("endpoint_entities") or []) + (validation.get("endpoint_entities") or []):
        name_of.setdefault(e["entity_id"], e["canonical_name"])
    rel_rows = []
    for rel in validation["mapped_relations"]:
        state = rel["terminal_state"]
        if state not in ("accepted", "open"):
            continue
        subject_entity = mention_entity.get(rel["subject_mention_id"])
        object_entity = mention_entity.get(rel["object_mention_id"])
        if not subject_entity or not object_entity:
            continue
        predicate = rel.get("canonical_candidate") or rel.get("surface_predicate") or ""
        source = ";".join(rel.get("reasons") or [])
        if "structured_data" in source and rel.get("surface_predicate"):
            predicate = "HAS_" + re.sub(r"[^A-Za-z0-9]+", "_", rel["surface_predicate"]).upper()
        rel_rows.append({
            "subject": name_of.get(subject_entity, ""),
            "predicate": predicate.upper().replace(" ", "_"),
            "object": name_of.get(object_entity, ""),
            "state": state,
        })
    seen = set()
    unique_rels = []
    for r in rel_rows:
        key = (r["subject"].casefold(), r["predicate"], r["object"].casefold())
        if key not in seen:
            seen.add(key)
            unique_rels.append(r)
    (OUT_DIR / "relations.jsonl").write_text(
        "\n".join(json.dumps(r) for r in unique_rels) + "\n")
    print(f"entities: {len(rows)} (unmapped spans: {unmapped_spans}) | relations: {len(unique_rels)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
