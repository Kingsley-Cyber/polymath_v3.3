import re, torch
from glirel import GLiREL
LABELS = ["part_of","member_of","located_in","works_for","created_by","owns",
"affiliated_with","synonym_of","instance_of","example_of","uses","references",
"implements","depends_on","produces","stores","detects","supports","defines",
"represents","maps_to","preceded_by","causes","overlaps","during","derived_from",
"contradicts","excepts","overrides","related_to"]
TOK = re.compile(r"\w+(?:[-_]\w+)*|\S")
dev = "mps" if torch.backends.mps.is_available() else "cpu"
print("device:", dev, "| torch:", torch.__version__)
import time; t=time.time()
m = GLiREL.from_pretrained("jackboyla/glirel-large-v0")
m.to(dev); m.device = torch.device(dev); m.config.fixed_relation_types = True
print("model load:", round(time.time()-t,1), "s")
text = ("Flame is a game engine built on top of Flutter. "
        "Alice Chen, a researcher at Meta AI, created the FineLlama model.")
ents = [("Flame","Software"),("Flutter","Software"),("Alice Chen","Person"),
        ("Meta AI","Organization"),("FineLlama","Product")]
toks = TOK.findall(text); tl=[x.lower() for x in toks]
def loc(name):
    w=[x.lower() for x in TOK.findall(name)]; n=len(w)
    for i in range(len(tl)-n+1):
        if tl[i:i+n]==w: return [i,i+n-1]
    return None
ner=[]
for nm,ty in ents:
    sp=loc(nm); print("located" if sp else "MISSING", nm, sp)
    if sp: ner.append([sp[0],sp[1],ty])
for r in m.predict_relations(toks, LABELS, ner=ner, threshold=0.5, top_k=1):
    print(r["head_text"],"--",r["label"],"-->",r["tail_text"], round(r["score"],3))
