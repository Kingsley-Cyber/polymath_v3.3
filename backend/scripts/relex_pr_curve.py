"""Precision/recall trade-off for the relex->gate pipeline, at every threshold.

Recall uses the SAME matcher as relation_stage_trace.py, verbatim. Precision
comes from the 150 hand-judged relations, so the two axes are measured on
DISJOINT text and must not be pooled -- they are reported side by side.
"""
import difflib, json, math, sys
sys.path.insert(0,"/app")
import spacy
from services.extraction.relex_gate import gate_relations, new_gate_counters

def near(a,b):
    n=lambda s:" ".join(str(s).lower().split()).strip(".,;:'\"()[]")
    a,b=n(a),n(b)
    if not a or not b: return False
    if a==b or a in b or b in a: return True
    return difflib.SequenceMatcher(None,a,b).ratio()>0.85

nlp=spacy.load("en_core_web_sm",disable=["ner","textcat"])
data=json.load(open("/tmp/relex_raw_GOLD.json"))
gold=json.load(open("/tmp/gold30.json"))["chunks"]
assert len(data)==len(gold)

per_chunk=[]
for c,g in zip(data,gold):
    doc=nlp(c["text"])
    kept=[x for x in gate_relations(c["relations"],text=c["text"],doc=doc,
          chunk_id=c["chunk_id"],counters=new_gate_counters()) if x.kept]
    per_chunk.append((kept,g["relations"]))

def wilson(k,n,z=1.96):
    if n==0: return (0.0,0.0)
    p=k/n; d=1+z*z/n; ctr=p+z*z/(2*n); m=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))
    return ((ctr-m)/d,(ctr+m)/d)

# precision from the judged sample, keyed by score
judged=[json.loads(l) for l in open("/tmp/RELEX_GATE_JUDGEMENTS.jsonl")]
tot_gold=sum(len(g) for _,g in per_chunk)
print(f"{'thr':>6}{'edges/ch':>10}{'RECALL':>9}{'  recall 95% CI':>20}"
      f"{'PRECISION':>11}{'  prec 95% CI':>20}{'  F1':>7}")
for t in [0.30,0.40,0.45,0.50,0.55,0.60,0.70,0.80,0.85,0.90]:
    hit=0; n_edges=0
    for kept,grels in per_chunk:
        ks=[k for k in kept if k.score>=t]
        n_edges+=len(ks)
        for gr in grels:
            if any((near(gr["s"],k.subject) and near(gr["o"],k.object)) or
                   (near(gr["s"],k.object) and near(gr["o"],k.subject)) for k in ks):
                hit+=1
    sub=[j for j in judged if j["score"]>=t]
    c=sum(1 for j in sub if j["judgement"]=="CORRECT")
    p=c/len(sub) if sub else 0.0
    r=hit/tot_gold
    rl,rh=wilson(hit,tot_gold); pl,ph=wilson(c,len(sub) if sub else 1)
    f1=2*p*r/(p+r) if (p+r) else 0.0
    print(f"{t:>6.2f}{n_edges/len(per_chunk):>10.2f}{r:>9.3f}   [{rl:.3f}, {rh:.3f}]"
          f"{p:>11.3f}   [{pl:.3f}, {ph:.3f}]{f1:>7.3f}")
print(f"\ngold relations = {tot_gold}; judged sample = {len(judged)}")
print("BASELINE frame extractor: recall 0.000, 0.03 edges/chunk, precision 0.8015 (gate v2)")
