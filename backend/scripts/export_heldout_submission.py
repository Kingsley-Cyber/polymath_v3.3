"""Export held-out submission artifacts from the factory Mongo per contract.

Hard-gate discipline:
  * offsets emitted ONLY when they slice the raw corpus file exactly
  * qualified rows never carry assertion_status 'asserted'
  * OPEN rows never carry a canonical predicate
usage: export_heldout_submission.py <corpus_id>
"""
import json, re, sys, time
from pathlib import Path

sys.path.insert(0, "/Users/king/polymath_v3.3/backend")
from dotenv import dotenv_values  # noqa: E402
from pymongo import MongoClient  # noqa: E402

CORPUS_ID = sys.argv[1]
PACKET = Path("/Users/king/Downloads/polymath_hard_heldout_RUN_v1")
SUB = PACKET / "submission"
MANIFEST = json.load(open(PACKET / "LOCKED_MANIFEST.json"))
FILE_TO_DID = {d["filename"]: d["document_id"] for d in MANIFEST["documents"]}
RAW = {d["document_id"]: (PACKET / "corpus" / d["filename"]).read_text()
       for d in MANIFEST["documents"]}

PACKET_TYPES = set(json.load(open(PACKET / "public_contracts" / "canonical_schema.json"))["canonical_entity_types"])
_DIRECT_TYPE = {t.lower(): t for t in PACKET_TYPES}
_FACET_TYPE = {
    "service": "SERVICE", "data_store": "SYSTEM", "dataset": "DATASET",
    "model_architecture": "MODEL", "media_signal": "DATASET",
    "research_method": "METHOD", "procedure": "PROCESS",
    "metric": "METRIC", "quantity": "METRIC", "speaker": "PERSON",
    "interface": "SOFTWARE", "component": "COMPONENT",
    "system": "SYSTEM", "material": "MATERIAL", "process": "PROCESS",
}


def contract_type(internal_type: str, facet: str) -> str:
    mapped = _FACET_TYPE.get((facet or "").lower())
    if mapped:
        return mapped
    direct = _DIRECT_TYPE.get((internal_type or "").lower())
    if direct:
        return direct
    # no legitimate correspondence: emit internal type uppercased (honest miss)
    return (internal_type or "CONCEPT").upper()


ENV = dotenv_values("/Users/king/polymath_v3.3/.env")
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


def did_for(doc_id, filename_by_doc):
    return FILE_TO_DID.get(filename_by_doc.get(doc_id, ""), None)


filename_by_doc = {}
for d in db.documents.find({"corpus_id": CORPUS_ID}):
    filename_by_doc[d.get("doc_id") or str(d.get("_id"))] = (d.get("filename") or "").replace("-", "_")
# filenames were slugified on upload; match loosely against manifest stems
TITLE_TO_DID = {d["title"]: d["document_id"] for d in MANIFEST["documents"]}


def match_did(fname):
    stem = re.sub(r"[^a-z0-9]", "", fname.lower().removesuffix("md"))
    for manifest_name, did in FILE_TO_DID.items():
        if re.sub(r"[^a-z0-9]", "", manifest_name.lower().removesuffix("md")) == stem:
            return did
    for title, did in TITLE_TO_DID.items():
        if re.sub(r"[^a-z0-9]", "", title.lower()) == stem:
            return did
    for manifest_name, did in FILE_TO_DID.items():
        if stem[:20] and stem[:20] in re.sub(r"[^a-z0-9]", "", manifest_name.lower()):
            return did
    return None

doc_to_did = {doc_id: match_did(fname) for doc_id, fname in filename_by_doc.items()}
print("doc mapping:", {k[:8]: v for k, v in doc_to_did.items()})
assert all(doc_to_did.values()), "unmapped documents"

normalized = payloads("NORMALIZED")
reducer = payloads("ENTITY_REDUCTION_COMPLETE")
completion = payloads("MENTION_COMPLETION_COMPLETE")
relstage = payloads("RELATION_COMPILATION_COMPLETE")
validation = payloads("ASSERTION_VALIDATION_COMPLETE")
assembly = payloads("OPENIE_ASSERTION_ASSEMBLY_COMPLETE")
adaptation = payloads("OPENIE_ARGUMENT_ADAPTATION_COMPLETE")
census = payloads("ENTITY_CENSUS_COMPLETE")

entity_rows, relation_rows = [], []
schema_hashes = {}
for doc_id, did in doc_to_did.items():
    raw = RAW[did]
    import difflib
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
    rep = (census.get(doc_id) or {}).get("report") or {}
    hashes = rep.get("schema_hashes") or {}
    if hashes:
        schema_hashes[did] = list(hashes.values())[0]

    def offsets(mention):
        surface = mention.get("surface", "")
        o_start, o_end = mention.get("original_start"), mention.get("original_end")
        if o_start is not None and 0 <= o_start < (o_end or 0) <= len(raw) and raw[o_start:o_end] == surface:
            return (o_start, o_end)
        n_start, n_end = mention.get("normalized_start"), mention.get("normalized_end")
        if n_start is not None and n_end is not None:
            mapped = map_norm(n_start, n_end)
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
        entity = ent[m["entity_id"]]
        span = offsets(m)
        facet = (m.get("facet") or entity.get("facet") or "").strip()
        row = {
            "document_id": did,
            "start": span[0] if span else None,
            "end": span[1] if span else None,
            "text": m.get("surface", ""),
            "native_type": facet or entity.get("entity_type", ""),
            "canonical_type": contract_type(entity.get("entity_type", ""), facet),
            "canonical_name": entity.get("canonical_name", ""),
            "score": float(m.get("confidence") or entity.get("confidence") or 0.0),
            "provider_release": m.get("provider_release", ""),
            "decision": entity.get("state", ""),
        }
        if span is None:
            row.pop("start"); row.pop("end")
            continue  # contract requires start/end: skip unverifiable offsets
        entity_rows.append(row)

    def emit(subject_mid, object_mid, surface_pred, canonical, state, polarity, modality, ev_off=None):
        if state in ("rejected", "review"):
            return
        if state == "accepted":
            status, mapping = "asserted", ("exact" if canonical else "OPEN")
            if polarity == "negative":
                status = "negated"
            elif modality and modality not in ("asserted", "certain"):
                status = "possible"
        elif state == "qualified":
            mapping = "exact" if canonical else "OPEN"
            status = ("negated" if polarity == "negative"
                      else "reported" if modality in ("attributed", "reported")
                      else "conditional" if modality == "conditional"
                      else "possible")
        elif state == "open":
            mapping, canonical = "OPEN", None
            status = ("negated" if polarity == "negative"
                      else "possible" if modality and modality not in ("asserted", "certain")
                      else "asserted")
        else:
            return
        subject = {"text": msurf.get(subject_mid, "")}
        obj = {"text": msurf.get(object_mid, "")}
        if moff.get(subject_mid):
            subject["start"], subject["end"] = moff[subject_mid]
        if moff.get(object_mid):
            obj["start"], obj["end"] = moff[object_mid]
        if not subject["text"] or not obj["text"]:
            return
        row = {
            "document_id": did, "subject": subject, "object": obj,
            "predicate": surface_pred or (canonical or ""),
            "assertion_status": status, "mapping_status": mapping,
            "polarity": polarity or "positive", "modality": modality or "certain",
        }
        if canonical and mapping != "OPEN":
            row["canonical_predicate"] = canonical
        relation_rows.append(row)

    for r in val.get("mapped_relations") or []:
        emit(r.get("subject_mention_id"), r.get("object_mention_id"),
             r.get("surface_predicate"), r.get("canonical_candidate"),
             str(r.get("terminal_state")), r.get("polarity"), r.get("modality"))

    qualified_pairs = set()
    for r in val.get("mapped_relations") or []:
        if str(r.get("terminal_state")) == "qualified":
            s_e, o_e = m2e.get(r.get("subject_mention_id")), m2e.get(r.get("object_mention_id"))
            if s_e and o_e:
                qualified_pairs.add((s_e, o_e, r.get("canonical_candidate")))
                qualified_pairs.add((s_e, o_e, None))
    args = {a["argument_id"]: a for a in (adaptation.get(doc_id) or {}).get("arguments") or []}
    for a in (assembly.get(doc_id) or {}).get("assertions") or []:
        lane = a.get("lane")
        state = {"FACT": "accepted", "OPEN_RELATION": "open", "QUALIFIED_CLAIM": "qualified"}.get(lane)
        if state is None:
            continue
        sa, oa = args.get(a.get("subject_argument_id")) or {}, args.get(a.get("object_argument_id")) or {}
        if not sa.get("entity_id") or not oa.get("entity_id"):
            continue
        if state == "accepted" and (
            (sa["entity_id"], oa["entity_id"], a.get("canonical_predicate")) in qualified_pairs
            or (sa["entity_id"], oa["entity_id"], None) in qualified_pairs
        ):
            # Production fact-merge vetoes OpenIE FACTs when a qualified
            # syntax record holds the same pair; the export honors the veto.
            state = "qualified"
        subject_text = ent.get(sa["entity_id"], {}).get("canonical_name") or sa.get("surface", "")
        object_text = ent.get(oa["entity_id"], {}).get("canonical_name") or oa.get("surface", "")
        qualifiers = a.get("qualifiers") or {}
        row = {
            "document_id": did,
            "subject": {"text": subject_text}, "object": {"text": object_text},
            "predicate": a.get("surface_relation") or a.get("canonical_predicate") or "",
            "assertion_status": ("negated" if qualifiers.get("polarity") == "negative"
                                 else "reported" if qualifiers.get("attribution")
                                 else "asserted" if state in ("accepted", "open")
                                 and (qualifiers.get("modality") or "asserted") in ("asserted", "certain")
                                 else "possible" if state == "qualified" or qualifiers.get("modality")
                                 else "asserted"),
            "mapping_status": ("exact" if state == "accepted" and a.get("canonical_predicate") else "OPEN"),
            "polarity": qualifiers.get("polarity") or "positive",
            "modality": qualifiers.get("modality") or "certain",
        }
        if state == "accepted" and a.get("canonical_predicate"):
            row["canonical_predicate"] = a.get("canonical_predicate")
        if row["subject"]["text"] and row["object"]["text"] and row["predicate"]:
            relation_rows.append(row)

seen = set()
unique_relations = []
for r in relation_rows:
    key = (r["document_id"], r["subject"]["text"].casefold(), r["predicate"].casefold(),
           r["object"]["text"].casefold(), r["assertion_status"], r["mapping_status"])
    if key not in seen:
        seen.add(key)
        unique_relations.append(r)

invalid = 0
for e in entity_rows:
    raw_e = RAW[e["document_id"]]
    if raw_e[e["start"]:e["end"]] != e["text"]:
        invalid += 1
for r in unique_relations:
    raw_r = RAW[r["document_id"]]
    for ep in (r["subject"], r["object"]):
        if "start" in ep and raw_r[ep["start"]:ep["end"]] != ep["text"]:
            invalid += 1
assert invalid == 0, f"self-validation failed: {invalid} inconsistent offsets"
(SUB / "entities.jsonl").write_text("\n".join(json.dumps(r) for r in entity_rows) + "\n")
(SUB / "relations.jsonl").write_text("\n".join(json.dumps(r) for r in unique_relations) + "\n")
(SUB / "run_metadata.json").write_text(json.dumps({
    "run_id": f"heldout-run-v1-{CORPUS_ID[:8]}",
    "extractor_release": "relex-large-mps-sidecar-v1 single-pass @5a73eab",
    "adapter_release": "ontology-adapter-compiler-v1 (Matrix B frozen)",
    "model_id": "knowledgator/gliner-relex-large-v1.0",
    "model_revision": "4aedc9226a5ac9e2f6b5ea3e91c1ee577c88a290",
    "schema_hash": schema_hashes,
    "started_at": min(
        (d["created_at"].strftime("%Y-%m-%dT%H:%M:%SZ")
         for d in db.documents.find({"corpus_id": CORPUS_ID}) if d.get("created_at")),
        default="",
    ),
    "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "notes": "gold-blind per-document adapter compilation; thresholds pinned (0.30/0.30); "
             "no packs, thresholds, or code changed against this packet",
}, indent=1))
print(f"entities: {len(entity_rows)} | relations: {len(unique_relations)}")
