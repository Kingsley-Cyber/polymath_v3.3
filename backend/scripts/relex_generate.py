"""Phase A: run GLiNER-Relex on host (has torch+gliner), dump raw relations.
Phase B applies the repo gate inside the container (has backend+ontology).

IMPORTANT (P1A): entity labels MUST be preserved in the output.  The
``inference()`` return includes a ``label`` field per entity.  Earlier
versions serialized this as ``type: null``, which caused 100% of gate
candidates to hit REVIEW_UNKNOWN_TYPE.  The fix below copies ``label``
into ``type`` so downstream adapters can resolve endpoint types.
"""
import json, sys, time
EL = ["person","organization","location","product","software","document",
      "method","concept","event","standard","artifact"]
RL = ["affiliated with","works for","created by","owns","part of","located in",
      "causes","detects","uses","produces","derived from","is an instance of",
      "synonym of","includes","supports","implements","has part","references",
      "depends on","member of","example of","evaluates","deploys","creates",
      "trains","runs on","quantizes"]
ent_thr, rel_thr = float(sys.argv[1]), float(sys.argv[2])
from gliner import GLiNER
m = GLiNER.from_pretrained("knowledgator/gliner-relex-large-v1.0"); m.eval()
chunks = json.load(open("prec_chunks.json"))
out=[]; t0=time.time(); nrel=0
for i,c in enumerate(chunks):
    try:
        e,r = m.inference(texts=[c["text"]], labels=EL, relations=RL,
                          threshold=ent_thr, relation_threshold=rel_thr,
                          return_relations=True, flat_ner=False)
    except Exception as ex:
        print(f"ch{i} FAIL {type(ex).__name__}: {ex}", file=sys.stderr); e,r=[[]],[[]]
    # P1A: preserve entity labels.  GLiNER-Relex returns ``label`` per
    # entity; copy it into ``type`` so the adapter offset-join can resolve
    # endpoint types.  Also stamp entity_idx on relation endpoints so the
    # adapter can join by identity, not text.
    ents = e[0] if e else []
    for ent in ents:
        if ent.get("label") and not ent.get("type"):
            ent["type"] = ent["label"]
    # Build offset → entity_idx map for relation endpoint stamping
    offset_to_idx = {}
    for idx, ent in enumerate(ents):
        offset_to_idx[(ent.get("start"), ent.get("end"))] = idx
    rels = r[0] if r else []
    for rel in rels:
        for endpoint_key in ("head", "tail"):
            ep = rel.get(endpoint_key)
            if isinstance(ep, dict):
                ep_idx = offset_to_idx.get((ep.get("start"), ep.get("end")))
                if ep_idx is not None:
                    ep["entity_idx"] = ep_idx
                    ep["type"] = ents[ep_idx].get("type") or ents[ep_idx].get("label") or ""
    nrel += len(rels)
    out.append({**c, "entities": ents, "relations": rels})
    if (i+1) % 10 == 0: print(f"  {i+1}/{len(chunks)}", file=sys.stderr)
fn = f"relex_raw_{ent_thr}_{rel_thr}.json"
json.dump(out, open(fn,"w"), indent=1, default=str)
print(f"{len(chunks)} chunks, {nrel} raw relations ({nrel/len(chunks):.1f}/chunk), "
      f"{time.time()-t0:.1f}s -> {fn}", file=sys.stderr)
