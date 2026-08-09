import json, sys, hashlib
from collections import Counter
sys.path.insert(0,"/app")
from services.extraction.relex_gate import gate_relations, new_gate_counters
import spacy
nlp = spacy.load("en_core_web_sm", disable=["ner","textcat"])
data = json.load(open(sys.argv[1]))
ctr = new_gate_counters()
raw_rows, kept_rows = [], []
for c in data:
    doc = nlp(c["text"])
    gated = gate_relations(c["relations"], text=c["text"], doc=doc,
                           chunk_id=c["chunk_id"], doc_id=c.get("doc_id",""),
                           counters=ctr)
    for g in gated:
        row = {"chunk_id": g.chunk_id, "corpus": c.get("corpus_id",""),
               "s": g.subject, "p": g.predicate or g.extras.get("raw_label"),
               "o": g.object, "s_type": g.subject_type, "o_type": g.object_type,
               "score": round(g.score,3), "ev": g.evidence[:300],
               "dropped": g.dropped_reason}
        raw_rows.append(row)
        if g.kept: kept_rows.append(row)
n_raw, n_kept = len(raw_rows), len(kept_rows)
print(json.dumps({"chunks": len(data), "raw": n_raw, "kept": n_kept,
                  "kept_share": round(n_kept/max(1,n_raw),4),
                  "raw_per_chunk": round(n_raw/len(data),2),
                  "kept_per_chunk": round(n_kept/len(data),2),
                  "gate_kills": {k:v for k,v in ctr.items() if v}}, indent=2))
with open("/tmp/relex_raw_rows.jsonl","w") as f:
    for r in raw_rows: f.write(json.dumps(r)+"\n")
with open("/tmp/relex_kept_rows.jsonl","w") as f:
    for r in kept_rows: f.write(json.dumps(r)+"\n")
# deterministic judge samples: hash-ordered, no RNG
def sample(rows, n, salt):
    return sorted(rows, key=lambda r: hashlib.sha256(
        (salt+r["s"]+str(r["p"])+r["o"]+r["chunk_id"]).encode()).hexdigest())[:n]
json.dump(sample([r for r in raw_rows if not r["dropped"]] or raw_rows, 0, ""), open("/dev/null","w"))
with open("/tmp/judge_raw.jsonl","w") as f:
    for r in sample(raw_rows, 150, "raw"): f.write(json.dumps(r)+"\n")
with open("/tmp/judge_kept.jsonl","w") as f:
    for r in sample(kept_rows, 150, "kept"): f.write(json.dumps(r)+"\n")
print(f"judge samples: raw=150 kept={min(150,n_kept)}", file=sys.stderr)
