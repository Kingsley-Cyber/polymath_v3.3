"""Score RECALL against blind-authored gold. Gold was committed first."""
import os, json, sys, difflib
sys.path.insert(0,"/app")
import pymongo
from services.extraction.entity_quality import judge_entity, judge_relation_anchor
from services.extraction.dep_path_extractor import new_counters
from services.extraction.spacy_relation_adapter import get_spacy_extractor

gold=json.load(open("/tmp/gold.json"))["chunks"]
chunks={c["chunk_id"]: c for c in json.load(open("/tmp/recall_chunks.json"))}
db=pymongo.MongoClient(os.environ["MONGODB_URI"]).get_database("polymath")
ext=get_spacy_extractor()

def norm(s): return " ".join((s or "").lower().split()).strip(".,;:'\"")
def hit(target, produced):
    t=norm(target)
    for p in produced:
        pn=norm(p)
        if t==pn or t in pn or pn in t: return True
        if difflib.SequenceMatcher(None,t,pn).ratio()>0.85: return True
    return False

E_found=E_tot=R_found=R_tot=0
rows=[]
order=json.load(open("/tmp/recall_chunks.json"))
for g in gold:
    # Map by POSITION, not by the truncated chunk_id in the gold file. The
    # earlier prefix regex matched a DIFFERENT chunk of the same document
    # (_0000 instead of _0702), scoring gold against the wrong text and
    # producing a fake 0.000. Exact ids only.
    cid=order[g["n"]-1]["chunk_id"]
    doc=db.ghost_b_extractions.find_one({"chunk_id":cid},
        {"chunk_id":1,"doc_id":1,"text":1,"entities":1,"_id":0})
    if not doc: rows.append((g["n"],"NO CHUNK",0,0,0,0)); continue
    raw=[e.get("surface_form") or "" for e in (doc.get("entities") or [])]
    kept=[e.get("surface_form") or "" for e in (doc.get("entities") or []) if e.get("graph_eligible")]
    anchors=[e for e in (doc.get("entities") or [])
             if judge_relation_anchor(e.get("surface_form") or "", e.get("entity_type") or "").keep]
    pay=[{"chunk_id":doc["chunk_id"],"doc_id":doc.get("doc_id",""),"text":doc.get("text") or "",
          "entities":[{"surface":e.get("surface_form") or "","start_char":0,"end_char":0,
                       "entity_type":e.get("ontology_entity_type") or "",
                       "canonical_name":e.get("canonical_name") or ""} for e in anchors]}]
    edges=ext.extract_chunks(pay,max_related=10,suppression_counters_list=[new_counters()])[0]
    ef=sum(1 for x in g["entities"] if hit(x,kept))
    ef_raw=sum(1 for x in g["entities"] if hit(x,raw))
    produced_rel=[f"{e['sub']} {e['pred']} {e['obj']}" for e in edges]
    rf=sum(1 for r in g["relations"] if hit(r["s"],[e["sub"] for e in edges]) and hit(r["o"],[e["obj"] for e in edges]))
    E_found+=ef; E_tot+=len(g["entities"]); R_found+=rf; R_tot+=len(g["relations"])
    rows.append((g["n"],doc["chunk_id"][:12],ef,ef_raw,len(g["entities"]),rf,len(g["relations"]),len(edges)))

print(f"{'ch':>3}{'chunk':>14}{'ent_gated':>11}{'ent_raw':>9}{'ent_gold':>9}{'rel_hit':>9}{'rel_gold':>9}{'rel_out':>8}")
for r in rows:
    if r[1]=="NO CHUNK": print(f"{r[0]:>3}  NO CHUNK FOUND"); continue
    print(f"{r[0]:>3}{r[1]:>14}{r[2]:>11}{r[3]:>9}{r[4]:>9}{r[5]:>9}{r[6]:>9}{r[7]:>8}")
print()
print(f"ENTITY RECALL (gated)  : {E_found}/{E_tot} = {E_found/max(1,E_tot):.3f}")
print(f"RELATION RECALL        : {R_found}/{R_tot} = {R_found/max(1,R_tot):.3f}")
