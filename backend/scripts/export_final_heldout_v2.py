"""Final held-out v2 submission exporter (public export_contract.json only).

Same invariants as the qualified v1 exporter: surfaces are source-faithful
(surface == source[start:end], byte-verified); the export mirrors the graph
(assembly FACTs pass the same-evidence qualified-veto); OPEN keeps native
predicate with canonical null; qualified/negative rows never export as
asserted. No endpoint canonical-id substitution into surfaces.

usage: export_final_heldout_v2.py <corpus_id>
"""
from __future__ import annotations

import difflib
import json
import re
import sys
import time
from pathlib import Path

_BACKEND = str(Path(__file__).resolve().parents[1])
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from dotenv import dotenv_values  # noqa: E402
from pymongo import MongoClient  # noqa: E402

CORPUS_ID = sys.argv[1]
PACKET = Path("/Users/king/Downloads/polymath_final_heldout_RUN_v2")
SUB = PACKET / "submission"
CONTRACT = json.load(open(PACKET / "public_contracts" / "export_contract.json"))
MANIFEST = json.load(open(PACKET / "public_contracts" / "corpus_manifest.json"))
DOCS = MANIFEST if isinstance(MANIFEST, list) else MANIFEST["documents"]
TYPE_ENUM = set(CONTRACT["entities_jsonl"]["canonical_type_enum"])

RAW = {d["doc_id"]: (PACKET / d["file"]).read_text() for d in DOCS}

# Submission-contract type adapter: legitimate correspondences only.
_DIRECT = {t.lower(): t for t in TYPE_ENUM}
_INTERNAL = {
    "organization": "ORG", "software": "SOFTWARE", "person": "PERSON",
    "location": "LOCATION", "document": "DOCUMENT", "event": "EVENT",
    "concept": "CONCEPT", "method": "PROCESS", "artifact": "DATASET",
    "standard": "DOCUMENT", "product": "SOFTWARE", "timereference": "EVENT",
}
_FACET = {
    "service": "SERVICE", "dataset": "DATASET", "data_store": "SERVICE",
    "metric": "METRIC", "quantity": "METRIC", "speaker": "PERSON",
    "interface": "LIBRARY", "research_method": "PROCESS",
    "procedure": "PROCESS", "document_title": "DOCUMENT",
}


def contract_type(internal: str, facet: str) -> str:
    mapped = _FACET.get((facet or "").lower()) or _DIRECT.get((internal or "").lower()) \
        or _INTERNAL.get((internal or "").lower())
    return mapped if mapped in TYPE_ENUM else "CONCEPT"


PACKET_PREDICATES = set(json.load(open(
    PACKET / "public_contracts" / "canonical_schema.json"))["canonical_predicates"])
# Submission-contract predicate adapter (run-contract rule 6): exact
# correspondences map; exact-with-inversion swaps endpoints; anything
# without a packet equivalent stays OPEN with native preserved.
_PRED_EXACT = {"instance_of": "is_a"}
_PRED_INVERSE = {"preceded_by": "precedes"}


def contract_predicate(canonical):
    """Returns (packet_canonical | None, swap_endpoints: bool)."""
    if canonical is None:
        return None, False
    if canonical in PACKET_PREDICATES:
        return canonical, False
    if canonical in _PRED_EXACT and _PRED_EXACT[canonical] in PACKET_PREDICATES:
        return _PRED_EXACT[canonical], False
    if canonical in _PRED_INVERSE and _PRED_INVERSE[canonical] in PACKET_PREDICATES:
        return _PRED_INVERSE[canonical], True
    return None, False


ENV = dotenv_values(str(Path(_BACKEND).parent / ".env"))
uri = re.sub(r"@mongodb:", "@localhost:", ENV.get("MONGODB_URI") or "")
db = MongoClient(uri, serverSelectionTimeoutMS=8000)[ENV.get("MONGODB_DB", "polymath")]


def payloads(stage):
    from services.storage.graphify_artifact_codec import decode_stage_payload
    out = {}
    for row in db.graphify_stage_artifacts.find({"corpus_id": CORPUS_ID, "stage": stage}):
        if "payload" in row:
            payload = row["payload"]
        else:
            parts = list(db.graphify_stage_artifact_parts.find(
                {"artifact_id": row["artifact_id"]}).sort("part", 1))
            payload = decode_stage_payload(row, lambda a, n: [p["blob"] for p in parts])
        out[row["doc_id"]] = payload
    return out


def slug(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


file_slugs = {slug(Path(d["file"]).stem): d["doc_id"] for d in DOCS}
title_slugs = {slug(d.get("title", "")): d["doc_id"] for d in DOCS}
doc_to_did = {}
for d in db.documents.find({"corpus_id": CORPUS_ID}):
    s = slug(Path(str(d.get("filename") or "")).stem)
    did = file_slugs.get(s) or title_slugs.get(s)
    if did is None:
        for k, v in file_slugs.items():
            if s and (s in k or k in s):
                did = v
                break
    if did is None:
        for k, v in title_slugs.items():
            if s and k and (s in k or k in s):
                did = v
                break
    doc_to_did[d.get("doc_id") or str(d.get("_id"))] = did
print("mapping:", {k[:8]: v for k, v in doc_to_did.items()})
assert all(doc_to_did.values()), f"unmapped: {[k for k, v in doc_to_did.items() if not v]}"

normalized = payloads("NORMALIZED")
reducer = payloads("ENTITY_REDUCTION_COMPLETE")
completion = payloads("MENTION_COMPLETION_COMPLETE")
relstage = payloads("RELATION_COMPILATION_COMPLETE")
validation = payloads("ASSERTION_VALIDATION_COMPLETE")
assembly = payloads("OPENIE_ASSERTION_ASSEMBLY_COMPLETE")
adaptation = payloads("OPENIE_ARGUMENT_ADAPTATION_COMPLETE")

entity_rows, relation_rows = [], []
for doc_id, did in doc_to_did.items():
    raw = RAW[did]
    norm_text = (normalized.get(doc_id) or {}).get("document", {}).get("normalized_text", "")
    blocks = difflib.SequenceMatcher(None, norm_text, raw, autojunk=False).get_matching_blocks()

    def map_norm(start, end):
        for b in blocks:
            if b.a <= start and end <= b.a + b.size:
                return start + (b.b - b.a), end + (b.b - b.a)
        return None

    red = reducer.get(doc_id) or {}
    comp = (completion.get(doc_id) or {}).get("mentions") or []
    rel = relstage.get(doc_id) or {}
    val = validation.get(doc_id) or {}
    ent = {e["entity_id"]: e for e in red.get("entities") or []}
    for e in (rel.get("endpoint_entities") or []):
        ent.setdefault(e["entity_id"], e)
    promotable = {i for i, e in ent.items() if e.get("state") in ("promoted", "document_local")}

    def offsets(m):
        surface = m.get("surface", "")
        o_s, o_e = m.get("original_start"), m.get("original_end")
        if o_s is not None and 0 <= o_s < (o_e or 0) <= len(raw) and raw[o_s:o_e] == surface:
            return (o_s, o_e)
        n_s, n_e = m.get("normalized_start"), m.get("normalized_end")
        if n_s is not None and n_e is not None:
            mapped = map_norm(n_s, n_e)
            if mapped and raw[mapped[0]:mapped[1]] == surface:
                return mapped
        if surface and raw.count(surface) == 1:
            i = raw.index(surface)
            return (i, i + len(surface))
        return None

    m2e, msurf, moff = {}, {}, {}
    for m in comp + (rel.get("endpoint_mentions") or []):
        m2e[m["mention_id"]] = m["entity_id"]
        msurf[m["mention_id"]] = m.get("surface", "")
        moff[m["mention_id"]] = offsets(m)

    for m in comp:
        if m["entity_id"] not in promotable:
            continue
        span = offsets(m)
        if span is None:
            continue
        entity = ent[m["entity_id"]]
        facet = (m.get("facet") or entity.get("facet") or "").strip()
        entity_rows.append({
            "doc_id": did, "start": span[0], "end": span[1],
            "surface": m.get("surface", ""),
            "canonical_name": entity.get("canonical_name", ""),
            "canonical_type": contract_type(entity.get("entity_type", ""), facet),
            "native_type": facet or entity.get("entity_type", ""),
            "confidence": float(m.get("confidence") or entity.get("confidence") or 0.0),
            "provenance": m.get("provider_release", ""),
        })

    def emit(subject_mid, object_mid, surface_pred, canonical, state, polarity, modality):
        if state in ("rejected", "review"):
            return
        if state == "accepted" and polarity == "negative":
            state = "qualified"
        if state == "accepted" and modality and modality not in ("asserted", "certain"):
            state = "qualified"
        s_off, o_off = moff.get(subject_mid), moff.get(object_mid)
        if s_off is None or o_off is None:
            return  # contract requires endpoint offsets; unverifiable rows abstain
        if state == "open":
            canonical = None
        status = {"accepted": "asserted", "qualified": "qualified", "open": "open"}.get(state)
        if status is None:
            return
        packet_canonical, swap = contract_predicate(canonical)
        if canonical is not None and packet_canonical is None:
            # no packet equivalent: run-contract rule 6 — OPEN, native kept
            status = "open"
        if swap:
            subject_mid, object_mid = object_mid, subject_mid
            s_off, o_off = o_off, s_off
        row = {
            "doc_id": did,
            "subject_start": s_off[0], "subject_end": s_off[1],
            "subject_surface": msurf.get(subject_mid, ""),
            "subject_canonical_name": ent.get(m2e.get(subject_mid), {}).get("canonical_name", ""),
            "object_start": o_off[0], "object_end": o_off[1],
            "object_surface": msurf.get(object_mid, ""),
            "object_canonical_name": ent.get(m2e.get(object_mid), {}).get("canonical_name", ""),
            "native_predicate": surface_pred or (canonical or ""),
            "canonical_predicate": packet_canonical if status != "open" else None,
            "status": status,
            "qualifier": f"{polarity or 'positive'}/{modality or 'asserted'}",
        }
        relation_rows.append(row)

    qualified_pairs = set()
    for r in val.get("mapped_relations") or []:
        if str(r.get("terminal_state")) == "qualified":
            s_e, o_e = m2e.get(r.get("subject_mention_id")), m2e.get(r.get("object_mention_id"))
            if s_e and o_e:
                qualified_pairs.add((s_e, o_e))

    for r in val.get("mapped_relations") or []:
        emit(r.get("subject_mention_id"), r.get("object_mention_id"),
             r.get("surface_predicate"), r.get("canonical_candidate"),
             str(r.get("terminal_state")), r.get("polarity"), r.get("modality"))

    args = {a["argument_id"]: a for a in (adaptation.get(doc_id) or {}).get("arguments") or []}
    for a in (assembly.get(doc_id) or {}).get("assertions") or []:
        lane = a.get("lane")
        state = {"FACT": "accepted", "OPEN_RELATION": "open", "QUALIFIED_CLAIM": "qualified"}.get(lane)
        if state is None:
            continue
        sa, oa = args.get(a.get("subject_argument_id")) or {}, args.get(a.get("object_argument_id")) or {}
        if not sa.get("entity_id") or not oa.get("entity_id"):
            continue
        if state == "accepted" and (sa["entity_id"], oa["entity_id"]) in qualified_pairs:
            state = "qualified"  # export honors the production fact-merge veto
        sa_m = sa.get("mention_id")
        oa_m = oa.get("mention_id")
        if sa_m not in moff and sa.get("normalized_start") is not None:
            moff[sa_m] = offsets({"surface": sa.get("surface", ""),
                                  "normalized_start": sa.get("normalized_start"),
                                  "normalized_end": sa.get("normalized_end")})
            msurf[sa_m] = sa.get("surface", "")
            m2e[sa_m] = sa["entity_id"]
        if oa_m not in moff and oa.get("normalized_start") is not None:
            moff[oa_m] = offsets({"surface": oa.get("surface", ""),
                                  "normalized_start": oa.get("normalized_start"),
                                  "normalized_end": oa.get("normalized_end")})
            msurf[oa_m] = oa.get("surface", "")
            m2e[oa_m] = oa["entity_id"]
        qualifiers = a.get("qualifiers") or {}
        emit(sa_m, oa_m, a.get("surface_relation"),
             a.get("canonical_predicate"), state,
             qualifiers.get("polarity"), qualifiers.get("modality"))

seen = set()
unique = []
for r in relation_rows:
    key = (r["doc_id"], r["subject_start"], r["subject_end"], r["object_start"],
           r["object_end"], r["native_predicate"].casefold(), r["status"])
    if key not in seen:
        seen.add(key)
        unique.append(r)

invalid = 0
for e in entity_rows:
    if RAW[e["doc_id"]][e["start"]:e["end"]] != e["surface"]:
        invalid += 1
for r in unique:
    raw = RAW[r["doc_id"]]
    if raw[r["subject_start"]:r["subject_end"]] != r["subject_surface"]:
        invalid += 1
    if raw[r["object_start"]:r["object_end"]] != r["object_surface"]:
        invalid += 1
open_bad = sum(1 for r in unique if r["status"] == "open" and r["canonical_predicate"])
assert invalid == 0 and open_bad == 0, (invalid, open_bad)

(SUB / "entities.jsonl").write_text("\n".join(json.dumps(r) for r in entity_rows) + "\n")
(SUB / "relations.jsonl").write_text("\n".join(json.dumps(r) for r in unique) + "\n")
(SUB / "run_metadata.json").write_text(json.dumps({
    "stack_commit": "2feaf26",
    "adapter_release": "ontology-adapter-compiler-v1 (Matrix B frozen)",
    "thresholds": {"entity": 0.30, "relation": 0.30, "relex_accept": 0.5},
    "tuning_performed_against_packet": False,
    "run_completed": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "extractor_release": "relex-large-mps-sidecar-v1 single-pass",
    "model_revision": "4aedc9226a5ac9e2f6b5ea3e91c1ee577c88a290",
}, indent=1))
print(f"entities: {len(entity_rows)} | relations: {len(unique)} | invalid: {invalid}")
