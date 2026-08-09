#!/usr/bin/env python3
"""Where does a CORRECT relation die? Per-stage attribution against blind gold.

THE QUESTION THIS ANSWERS
    Relation recall measured 0.028. Aggregate suppression counters say how many
    candidates each guard killed, but not whether the dead ones were RIGHT. A
    guard that kills 400 wrong candidates and a guard that kills 4 correct ones
    look identical in a counter. This walks each GOLD relation forward and names
    the exact stage that killed it.

THE MISTAKE THIS IS BUILT NOT TO REPEAT
    Earlier I compared textacy's RAW generator output (0.083) against my
    POST-FILTER output (0.028) and concluded textacy's generator was 3x better.
    Different layers. Measuring my generator at the same layer gave 0.222 — the
    opposite ranking, and it sent a design decision the wrong way for a turn.
    Here every generator is scored at the GENERATOR stage and nowhere else, and
    the filter stages are attributed separately.

    Second guard: this drives the REAL adapter (`extract_chunks`) with the REAL
    trace hook. It does not re-implement the guard order. A re-implementation
    measures a copy of the pipeline, and drifts from it silently — which is how
    the first draft of this file missed all four adapter-boundary filters
    (is_graph_edge, schema normalization, the second allowed_pairs gate, and the
    cap) that sit AFTER the extractor.

    Third guard: chunks are joined to gold by ORDER, not by ID prefix. A previous
    recall harness prefix-matched truncated 20-char chunk_ids, silently scored a
    different chunk, and reported a clean 0.000. The tell was ent_raw = 0.
    STAGE 0 below is that tell, made permanent: if a gold surface is not even
    present in the chunk text, the gold/chunk join is wrong (or the gold is), and
    that is reported as a HARNESS FAULT rather than absorbed as a filter loss.

STAGES — a gold relation is credited to the LAST stage it reached
    0 text_present      both surfaces occur in the chunk text at all
    1 generated         some candidate generator proposed the pair
    2 entity_emitted    GLiNER emitted both endpoints as entities
    3 anchor_gate       both survived the relaxed entity tier
    4..n <guard name>   died at a named guard, straight from the live trace
      emitted           reached the adapter's output

MATCHER
    --strict uses exact/containment only. Fuzzy (default) adds a 0.85 ratio.
    Both are reported so the reader can see how much rests on fuzziness. Every
    credited match is printable with --show-matches; nothing is scored on a
    similarity the reader cannot inspect.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, "/app")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Stages BEFORE the extractor's own guards. Everything after comes from the
# live trace, so this list never has to be kept in sync with the guard order.
PRE_STAGES = ["text_present", "generated", "entity_emitted", "anchor_gate"]


def make_matcher(strict: bool):
    def norm(s: str) -> str:
        return " ".join(str(s).lower().split()).strip(".,;:'\"()[]")

    def near(a, b) -> bool:
        a, b = norm(a), norm(b)
        if not a or not b:
            return False
        if a == b or a in b or b in a:
            return True
        if strict:
            return False
        return difflib.SequenceMatcher(None, a, b).ratio() > 0.85

    return near


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default="/tmp/gold30.json")
    ap.add_argument("--chunks", default="/tmp/recall_chunks30.json")
    ap.add_argument("--textacy", default="/tmp/textacy_triples.json")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--show", action="store_true", help="one row per gold relation")
    ap.add_argument("--show-matches", action="store_true",
                    help="print every credited fuzzy match for audit")
    ap.add_argument("--diagnose", action="store_true",
                    help="per missed relation: the sentence, my candidates, "
                         "textacy's candidates, and the entities in scope")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    import pymongo

    from services.extraction.entity_quality import relation_anchors
    from services.extraction.spacy_relation_adapter import get_spacy_extractor
    from services.extraction.frame_extractor import _find_frames
    from services.extraction.svo_candidates import svo_candidates

    db = pymongo.MongoClient(os.environ["MONGODB_URI"]).get_database("polymath")
    gold_doc = json.load(open(args.gold))
    gold_chunks = gold_doc["chunks"]
    chunk_meta = json.load(open(args.chunks))
    textacy = json.load(open(args.textacy)) if Path(args.textacy).exists() else None
    near = make_matcher(args.strict)

    if len(gold_chunks) != len(chunk_meta):
        print(f"HARNESS FAULT: gold has {len(gold_chunks)} chunks, chunk file has "
              f"{len(chunk_meta)}. Index join is invalid.", file=sys.stderr)
        return 2

    extractor = get_spacy_extractor()
    extractor._ensure_loaded()
    nlp = extractor._extractor._nlp

    reached = Counter()
    gen_hits = Counter()
    harness_faults: list[str] = []
    rows: list[dict] = []
    match_log: list[str] = []
    total = 0

    for i, (g, cm) in enumerate(zip(gold_chunks, chunk_meta)):
        text = cm["text"]
        cid = cm["chunk_id"]
        doc = nlp(text)

        stored = db.ghost_b_extractions.find_one(
            {"chunk_id": cid}, {"entities": 1, "_id": 0}) or {}
        ents = stored.get("entities") or []
        raw_surfaces = [e.get("surface_form") or e.get("surface") or "" for e in ents]
        anchor_ents = relation_anchors(ents)
        anchor_surfaces = [
            e.get("surface_form") or e.get("surface") or "" for e in anchor_ents]

        # ---- candidate generators, all scored at the SAME stage --------------
        # BOTH the head token AND its noun phrase are offered for matching.
        # My generator and svo_candidates yield HEAD TOKENS ("fields"); the
        # textacy dump yields SPANS ("CAPTCHA fields"). Scoring head tokens
        # against span gold marks "Companies -use-> fields" as a MISS for gold
        # (companies, uses, CAPTCHA) while crediting textacy for the identical
        # parse. That is the same apples-to-oranges error as before, pointed the
        # other way — so every generator is offered both forms.
        chunk_of = {}
        for nc in doc.noun_chunks:
            for t in nc:
                chunk_of[t.i] = nc.text

        def forms(tok):
            phrase = chunk_of.get(tok.i)
            return (tok.text,) if phrase is None else (tok.text, phrase)

        def gen_pairs(pairs_of_tokens):
            out = []
            for a, b in pairs_of_tokens:
                for fa in forms(a):
                    for fb in forms(b):
                        out.append((fa, fb))
            return out

        my_pairs = gen_pairs(
            [(f.subj_tok, f.obj_tok) for f in _find_frames(doc)])
        svo_pairs = gen_pairs(
            [(c.subject, c.object) for c in svo_candidates(doc)])
        tx_pairs = ([(t["s"], t["o"]) for t in textacy[i]]
                    if textacy and i < len(textacy) else [])

        # ---- the REAL pipeline, traced --------------------------------------
        trace: list[dict] = []
        edges = extractor.extract_chunks(
            [{"chunk_id": cid, "doc_id": cm.get("doc_id", ""), "text": text,
              "entities": ents}],
            docs=[doc],
            trace_list=[trace],
        )[0]

        for r in g["relations"]:
            total += 1
            s_txt, o_txt, p_txt = r["s"], r["o"], r["p"]
            label = f"{s_txt} -{p_txt}-> {o_txt}"

            def pairhit(pairs, log_as=""):
                for a, b in pairs:
                    fwd = near(s_txt, a) and near(o_txt, b)
                    rev = near(s_txt, b) and near(o_txt, a)
                    if fwd or rev:
                        if log_as and args.show_matches:
                            match_log.append(
                                f"  ch{i+1:02d} [{log_as}] gold({s_txt} | {o_txt})"
                                f"  <=  ({a} | {b}){' REVERSED' if rev else ''}")
                        return True
                return False

            # ---- STAGE 0 — harness integrity, NOT a pipeline stage -----------
            # Verbatim substring is too harsh: gold written as "S. Gaynor"
            # against a bibliography reading "Gaynor, S." is the SAME mention in
            # a different order. Presence is judged on content tokens, so only a
            # genuinely absent surface is flagged. A flagged relation is a
            # HARNESS or GOLD defect and is excluded from pipeline attribution
            # entirely — charging it to the extractor would understate recall
            # for a reason that has nothing to do with the extractor.
            lo = text.lower()

            def present(surface: str) -> bool:
                if surface.lower() in lo:
                    return True
                toks = [t for t in "".join(
                    c if c.isalnum() else " " for c in surface.lower()).split()
                    if len(t) > 1]
                return bool(toks) and all(t in lo for t in toks)

            if not (present(s_txt) and present(o_txt)):
                missing = [x for x in (s_txt, o_txt) if not present(x)]
                harness_faults.append(f"ch{i+1:02d}: {label} — absent: {missing}")
                reached["ABSENT_FROM_TEXT"] += 1
                rows.append({"chunk": i + 1, "relation": label,
                             "stage": "ABSENT_FROM_TEXT",
                             "mine": False, "svo": False, "textacy": False})
                continue
            stage = "text_present"

            # ---- STAGE 1 — generation ---------------------------------------
            in_mine = pairhit(my_pairs, "mine")
            in_svo = pairhit(svo_pairs, "svo")
            in_tx = pairhit(tx_pairs, "textacy")
            for k, v in (("mine", in_mine), ("native_svo", in_svo), ("textacy", in_tx)):
                if v:
                    gen_hits[k] += 1
            if in_mine or in_svo or in_tx:
                gen_hits["any"] += 1
            if in_mine:
                stage = "generated"

            # ---- STAGE 2/3 — the entity layer -------------------------------
            if stage == "generated":
                if (any(near(s_txt, x) for x in raw_surfaces)
                        and any(near(o_txt, x) for x in raw_surfaces)):
                    stage = "entity_emitted"
            if stage == "entity_emitted":
                if (any(near(s_txt, x) for x in anchor_surfaces)
                        and any(near(o_txt, x) for x in anchor_surfaces)):
                    stage = "anchor_gate"

            # ---- STAGE 4+ — straight from the live trace --------------------
            # A candidate can appear more than once (extractor survival, then an
            # adapter drop). Take the LAST record: that is its final fate.
            fate = None
            for rec in trace:
                cand = [rec.get("subject_entity") or rec.get("subject_token") or "",
                        rec.get("object_entity") or rec.get("object_token") or ""]
                if pairhit([tuple(cand)]):
                    fate = rec
            if fate is not None and stage == "anchor_gate":
                stage = fate["died_at"] or "emitted"

            if pairhit([(e["sub"], e["obj"]) for e in edges], "EMITTED"):
                stage = "emitted"

            reached[stage] += 1
            row = {"chunk": i + 1, "relation": label, "stage": stage,
                   "mine": in_mine, "svo": in_svo, "textacy": in_tx}

            # ---- side-by-side evidence for the relations nobody proposed ----
            # The whole point of the exercise: put the SENTENCE next to what
            # each generator saw in it, so the failure is readable rather than
            # inferred from a percentage.
            if args.diagnose and stage in ("text_present", "generated"):
                sent = next(
                    (s for s in doc.sents
                     if s.text.lower().find(s_txt.split()[0].lower()) >= 0
                     and o_txt.split()[0].lower() in s.text.lower()),
                    None)
                row["sentence"] = sent.text.strip() if sent else "(spans sentences)"
                sl = (sent.start, sent.end) if sent else (0, len(doc))
                row["mine_in_sentence"] = [
                    f"{f.subj_tok.text} -{f.pred_tok.text}({f.frame_type})-> "
                    f"{f.obj_tok.text}"
                    for f in _find_frames(doc) if sl[0] <= f.pred_tok.i < sl[1]]
                row["svo_in_sentence"] = [
                    f"{c.subject.text} -{c.verb.text}-> {c.object.text}"
                    for c in svo_candidates(doc) if sl[0] <= c.verb.i < sl[1]]
                row["textacy_in_chunk"] = [
                    f"{t['s']} -{t['v']}-> {t['o']}"
                    for t in (textacy[i] if textacy and i < len(textacy) else [])]
                row["entities_in_sentence"] = [
                    x for x in raw_surfaces
                    if sent is not None and x and x.lower() in sent.text.lower()]
            rows.append(row)

    # ---------------------------------------------------------------- report
    mode = "STRICT (exact/containment)" if args.strict else "FUZZY (+ratio>0.85)"
    print("=" * 74)
    print(f"GOLD RELATION STAGE TRACE — {total} gold relations, "
          f"{len(gold_chunks)} chunks")
    print(f"matcher: {mode}")
    print("=" * 74)

    absent = reached["ABSENT_FROM_TEXT"]
    scoreable = total - absent
    denom = max(1, scoreable)

    print("\nHARNESS INTEGRITY (stage 0) — checked BEFORE anything is blamed on code")
    if harness_faults:
        print(f"  {absent}/{total} gold relations name a surface not present in "
              f"the chunk.")
        print(f"  These are GOLD/HARNESS defects, EXCLUDED from the attribution "
              f"below.")
        print(f"  Scoreable gold: {scoreable}.")
        for f in harness_faults[:10]:
            print(f"    {f}")
        if len(harness_faults) > 10:
            print(f"    ... and {len(harness_faults) - 10} more")
    else:
        print(f"  clean — all {total} gold relations have both surfaces present.")

    print(f"\nCANDIDATE GENERATION — like-for-like, all at the generator stage "
          f"(n={scoreable})")
    for k in ("mine", "textacy", "native_svo", "any"):
        v = gen_hits[k]
        print(f"  {k:<12}{v:>3}/{scoreable} = {v/denom:.3f}   "
              f"{'#' * int(50*v/denom)}")
    print("  `any` is the CEILING a perfect filter could reach with today's")
    print("  generators. Nothing downstream can exceed it.")
    print("  NOTE: a generator 'hit' means the raw TOKEN pair was proposed. It")
    print("  does NOT mean a usable candidate existed — the slots still have to")
    print("  be filled by entities. Treating the two as the same is exactly the")
    print("  conflation that produced the earlier '87% filter loss' claim.")

    print("\nWHERE THE GOLD DIES — last stage reached")
    ordered = [s for s in PRE_STAGES if reached[s]]
    ordered += sorted((s for s in reached
                       if s not in PRE_STAGES and s not in
                       ("emitted", "ABSENT_FROM_TEXT")),
                      key=lambda s: -reached[s])
    for s in ordered:
        n = reached[s]
        note = ""
        if s == "text_present":
            note = "   (no generator proposed the pair)"
        elif s == "generated":
            note = "   (proposed, but the ENTITY layer missed an endpoint)"
        elif s == "entity_emitted":
            note = "   (entities existed, but the anchor gate dropped one)"
        elif s == "anchor_gate":
            note = "   (reached the extractor but produced no trace record)"
        print(f"  {n:>3}  {n/denom:>6.1%}  {s}{note}")
    n = reached["emitted"]
    print(f"  {n:>3}  {n/denom:>6.1%}  emitted  <-- END-TO-END RECALL")

    pre_gen = reached["text_present"]
    entity_loss = reached["generated"] + reached["entity_emitted"]
    filter_loss = sum(v for k, v in reached.items()
                      if k not in PRE_STAGES
                      and k not in ("emitted", "ABSENT_FROM_TEXT"))
    print("\nATTRIBUTION — which layer owns the loss")
    print(f"  {pre_gen:>3}/{scoreable}  GENERATOR never proposed the pair")
    print(f"  {entity_loss:>3}/{scoreable}  ENTITY layer missed an endpoint "
          f"(pair proposed, slots unfillable)")
    print(f"  {filter_loss:>3}/{scoreable}  FILTER killed a real candidate")
    print(f"  {n:>3}/{scoreable}  survived")
    print(f"\n  Only {filter_loss + n} of {scoreable} gold relations ever REACHED")
    print("  the filter. A filter cannot be blamed for what never arrives.")

    if args.diagnose:
        missed = [r for r in rows if r["stage"] in ("text_present", "generated")]
        print(f"\n{'=' * 74}\nSIDE BY SIDE — {len(missed)} relations the pipeline "
              f"never got a candidate for\n{'=' * 74}")
        for r in missed:
            print(f"\nch{r['chunk']:02d}  GOLD: {r['relation']}")
            print(f"      SENTENCE: {r.get('sentence', '')[:220]}")
            print(f"      GLiNER entities here: "
                  f"{r.get('entities_in_sentence') or '(none)'}")
            mine_c = r.get("mine_in_sentence") or []
            svo_c = r.get("svo_in_sentence") or []
            print(f"      MY frames ({len(mine_c)}): "
                  f"{mine_c[:4] if mine_c else '(none)'}")
            print(f"      textacy SVO ({len(svo_c)}): "
                  f"{svo_c[:4] if svo_c else '(none)'}")
            print(f"      -> died at: {r['stage']}")

    if args.show_matches and match_log:
        print(f"\nCREDITED MATCHES ({len(match_log)}) — audit these")
        for line in match_log:
            print(line)
    if args.show:
        print(f"\n{'ch':>3}  {'gold relation':<50}{'stage':<34} M S T")
        for row in rows:
            print(f"{row['chunk']:>3}  {row['relation'][:48]:<50}"
                  f"{row['stage'][:32]:<34} "
                  f"{'M' if row['mine'] else '-'} "
                  f"{'S' if row['svo'] else '-'} "
                  f"{'T' if row['textacy'] else '-'}")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "total": total, "matcher": mode,
            "harness_faults": harness_faults,
            "generator_coverage": dict(gen_hits),
            "stage_counts": dict(reached),
            "rows": rows,
        }, indent=2))
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
