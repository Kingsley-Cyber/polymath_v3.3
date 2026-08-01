"""Phase A: run GLiNER-Relex on host (has torch+gliner), dump raw relations.
Phase B applies the repo gate inside the container (has backend+ontology)."""
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
    rels = r[0] if r else []
    nrel += len(rels)
    out.append({**c, "entities": e[0] if e else [], "relations": rels})
    if (i+1) % 10 == 0: print(f"  {i+1}/{len(chunks)}", file=sys.stderr)
fn = f"relex_raw_{ent_thr}_{rel_thr}.json"
json.dump(out, open(fn,"w"), indent=1, default=str)
print(f"{len(chunks)} chunks, {nrel} raw relations ({nrel/len(chunks):.1f}/chunk), "
      f"{time.time()-t0:.1f}s -> {fn}", file=sys.stderr)
