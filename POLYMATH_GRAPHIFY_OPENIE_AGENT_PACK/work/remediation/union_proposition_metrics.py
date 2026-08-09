"""Pre- vs post-union proposition-level comparison on burned corpora (dev data)."""
import re, json, sys
from pymongo import MongoClient
from dotenv import dotenv_values

env = dotenv_values("/Users/king/polymath_v3.3/.env")
uri = re.sub(r"@mongodb:", "@localhost:", env.get("MONGODB_URI") or env.get("MONGO_URI") or "")
client = MongoClient(uri, serverSelectionTimeoutMS=8000)

def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s).casefold()).strip()

def name_match(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    return a == b or re.search(rf"(?:^| ){re.escape(b)}(?: |$)", a) or re.search(rf"(?:^| ){re.escape(a)}(?: |$)", b)

def analyze(dbname, gold_path):
    db = client[dbname]
    art = lambda st: db["graphify_stage_artifacts"].find_one({"stage": st})["payload"]
    ext = art("OPENIE_EXTRACTION_COMPLETE")
    props, rep = ext["propositions"], ext["report"]
    openie_rows = [p for p in props if "strict_surface_recovery" not in p["extractor_release"]]
    rec_rows = [p for p in props if "strict_surface_recovery" in p["extractor_release"]]
    malformed = [p for p in props if not norm(p["subject"]) or not norm(p["object"]) or not norm(p["relation"]) or norm(p["subject"]) == norm(p["object"])]

    gold = json.load(open(gold_path))
    triples = gold["positive_canonical_triples"]
    def g(t, *keys):
        for k in keys:
            if k in t: return t[k]
        raise KeyError(t)
    gold_rows = [(g(t,"subject","subject_name","head"), g(t,"predicate","relation"), g(t,"object","object_name","tail")) for t in triples]

    def pair_hit(gs, go, rows):
        return any(name_match(p["subject"], gs) and name_match(p["object"], go) for p in rows)
    pair_all = sum(1 for gs, _, go in gold_rows if pair_hit(gs, go, props))
    pair_openie = sum(1 for gs, _, go in gold_rows if pair_hit(gs, go, openie_rows))
    pair_rec = sum(1 for gs, _, go in gold_rows if pair_hit(gs, go, rec_rows))

    fam = {f["family_id"]: f for f in art("OPENIE_PROPOSITION_REDUCTION_COMPLETE")["families"]}
    prop_by_id = {p["proposition_id"]: p for p in props}
    cands = art("OPENIE_PREDICATE_COMPILATION_COMPLETE")["candidates"]
    prop_recall = 0
    for gs, gp, go in gold_rows:
        hit = False
        for c in cands:
            if c.get("canonical_predicate") != gp:
                continue
            f = fam.get(c["family_id"])
            p = prop_by_id.get(f["representative_proposition_id"]) if f else None
            if p and name_match(p["subject"], gs) and name_match(p["object"], go):
                hit = True
                break
        prop_recall += hit
    n = len(gold_rows)
    inv = {k: rep.get(k) for k in ("eligible_prose_units", "openie_successes", "explicit_openie_failures", "union_invariant_holds", "deterministic_only_units")}
    return {
        "db": dbname, "gold": n,
        "raw_propositions": len(props), "openie_rows": len(openie_rows), "recovery_rows": len(rec_rows),
        "malformed": len(malformed),
        "directed_pair_recall": f"{pair_all}/{n} = {pair_all/n:.3f}",
        "pair_from_openie_lane": pair_openie, "pair_from_recovery_lane": pair_rec,
        "proposition_recall(pair+compiled predicate)": f"{prop_recall}/{n} = {prop_recall/n:.3f}",
        "invariant": inv,
    }

BOOK_GOLD = "/Users/king/Downloads/technical_book_graphrag_stress_test/answer_key/gold_triples.json"
SEALED_GOLD = "/Users/king/Downloads/sealed_qualification_v1/answer_key/gold_triples.json"
for dbname, gold in json.loads(sys.argv[1]):
    print(json.dumps(analyze(dbname, gold), indent=1, default=str))
