"""
Broke-mode cascade inference: chunk -> candidate entity pairs -> ModernBERT
cascade -> Ghost B-compatible relation JSONL.

This is the local, no-API replacement for Ghost B's relation extraction. It does
NOT do entity recognition (that's GLiNER/Python upstream) — it consumes entities
and emits typed relations.

Input chunk shape (one JSON object per line, or pass a list):
    {
      "chunk_id": "...", "doc_id": "...", "text": "<full chunk text>",
      "entities": [{"canonical_name": "...", "entity_type": "...",
                    "surface_form": "...", "query_aliases": [...]}, ...]
    }

Candidate pairs are formed from co-occurring entities. Per pair we derive:
    - text : the sentence/window containing both mentions (evidence)
    - cue  : the text span between subject and object (heuristic relation cue)
These approximate the evidence_phrase/relation_cue the LLM used to produce.

Output (one per surviving relation):
    {"t":"r","sub":...,"pred":...,"obj":...,"ok":"entity","cf":...,"ev":...,"cue":...}

Set HF_HUB_OFFLINE=1.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from itertools import permutations
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from polymath_local_extractor import Edge, LocalExtractor, _Head, match_cue

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WS = re.compile(r"\s+")


class RelationExistsGate:
    """Learned pair-quality filter. A binary ModernBERT (none/related) that scores
    each candidate pair's probability of being a real relation, and drops the ones
    below threshold BEFORE the predicate cascade runs. Trained on real relations
    (positives) vs real non-relation same-sentence pairs (negatives)."""

    def __init__(self, ckpt_dir: str, threshold: float = 0.5, device: str = "cuda"):
        self.head = _Head(ckpt_dir, device)
        self.threshold = threshold
        # index of the "related" label in this head
        self.rel_id = self.head.labels.index("related")

    def related_probs(self, pairs: List[dict], batch_size: int = 256) -> List[float]:
        import torch
        out: List[float] = []
        with torch.inference_mode():
            for s in range(0, len(pairs), batch_size):
                chunk = pairs[s:s + batch_size]
                enc = self.head.tok([self.head._input(p) for p in chunk], padding=True,
                                    truncation=True, max_length=192, return_tensors="pt"
                                    ).to(self.head.device)
                probs = torch.softmax(self.head.model(**enc).logits.float(), dim=-1).cpu().numpy()
                out.extend(float(r[self.rel_id]) for r in probs)
        return out

    def filter(self, pairs: List[dict]) -> List[dict]:
        if not pairs:
            return pairs
        p = self.related_probs(pairs)
        return [pr for pr, prob in zip(pairs, p) if prob >= self.threshold]

# High-value type pairs that are relational even without a clean cue verb
# (the cascade's heads handle these well: works_for, located_in, created_by, ...).
TYPE_PAIR_ALLOW = {
    ("Person", "Organization"), ("Person", "Location"), ("Person", "Document"),
    ("Organization", "Location"), ("Organization", "Organization"),
    ("Software", "Location"), ("Product", "Location"), ("Artifact", "Location"),
    ("Person", "Product"), ("Organization", "Product"),
}


def _envi(name, default):
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default


def gate_pair(window: str, cue: str, st: str, ot: str,
              require_cue: bool = True) -> bool:
    """Strict candidate gate: a pair reaches the cascade only if a relation is
    plausible. Cuts the n^2 entity-permutation flood that becomes related_to noise.

      - a recognized cue verb/preposition sits between/near the entities, OR
      - the entity-type pair is a known high-value relational pair.
    """
    if match_cue(cue, window) is not None:
        return True
    if (st, ot) in TYPE_PAIR_ALLOW:
        return True
    return not require_cue


def norm(s: str) -> str:
    return _WS.sub(" ", str(s or "")).strip()


def entity_names(e: dict) -> List[str]:
    out = []
    for k in ("surface_form", "canonical_name"):
        v = norm(e.get(k, ""))
        if v:
            out.append(v)
    out.extend(norm(a) for a in (e.get("query_aliases") or []) if norm(a))
    return out


def find_window(text: str, a: str, b: str) -> Optional[str]:
    """Return the sentence containing both a and b (case-insensitive), else None."""
    tl = text.lower()
    al, bl = a.lower(), b.lower()
    if al not in tl or bl not in tl:
        return None
    for sent in _SENT_SPLIT.split(text):
        sl = sent.lower()
        if al in sl and bl in sl:
            return norm(sent)
    return norm(text)  # both present but not same sentence -> whole chunk


def inter_span_cue(window: str, a: str, b: str) -> str:
    """Heuristic cue = the words between the two entity mentions in the window."""
    wl = window.lower()
    ia, ib = wl.find(a.lower()), wl.find(b.lower())
    if ia < 0 or ib < 0:
        return ""
    if ia < ib:
        seg = window[ia + len(a): ib]
    else:
        seg = window[ib + len(b): ia]
    seg = norm(seg)
    # keep it short — a relation cue is a few words
    return " ".join(seg.split()[:6])


def find_same_sentence(text: str, a: str, b: str) -> Optional[str]:
    """Return the SENTENCE containing both (no whole-chunk fallback)."""
    al, bl = a.lower(), b.lower()
    for sent in _SENT_SPLIT.split(text):
        sl = sent.lower()
        if al in sl and bl in sl:
            return norm(sent)
    return None


def candidate_pairs(chunk: dict, max_entities: int = 16,
                    max_pairs: Optional[int] = None,
                    require_cue: Optional[bool] = None,
                    same_sentence: bool = True) -> List[dict]:
    """Gated candidate generation. Only entity pairs that (a) co-occur in the same
    sentence and (b) pass gate_pair() reach the cascade. Capped per chunk."""
    if max_pairs is None:
        max_pairs = _envi("LOCAL_GHOST_B_MAX_PAIRS_PER_CHUNK", 24)
    if require_cue is None:
        v = os.environ.get("LOCAL_GHOST_B_REQUIRE_CUE")
        require_cue = (v.strip().lower() in ("1", "true", "yes", "on")) if v else True

    text = norm(chunk.get("text", ""))
    ents = (chunk.get("entities") or [])[:max_entities]
    cued: List[dict] = []
    typed: List[dict] = []
    for s_ent, o_ent in permutations(ents, 2):
        s_names, o_names = entity_names(s_ent), entity_names(o_ent)
        if not s_names or not o_names:
            continue
        s_name, o_name = s_names[0], o_names[0]
        if s_name.lower() == o_name.lower():
            continue
        window = s_used = o_used = None
        for sn in s_names:
            for on in o_names:
                window = (find_same_sentence(text, sn, on) if same_sentence
                          else find_window(text, sn, on))
                if window:
                    s_used, o_used = sn, on
                    break
            if window:
                break
        if not window:
            continue
        cue = inter_span_cue(window, s_used, o_used)
        st = s_ent.get("entity_type", "Concept")
        ot = o_ent.get("entity_type", "Concept")
        if not gate_pair(window, cue, st, ot, require_cue):
            continue
        rec = {"text": window, "cue": cue,
               "subject": s_name, "subject_type": st,
               "object": o_name, "object_type": ot}
        # prioritize cue-bearing pairs over type-only pairs when capping
        (cued if match_cue(cue, window) is not None else typed).append(rec)

    pairs = cued + typed
    return pairs[:max_pairs]


def apply_related_cap(edges: List[Edge], max_related: int) -> List[Edge]:
    """Per chunk: keep only the top-N related_to edges by confidence; demote the
    rest to drop. Exact edges are never capped. Prevents related_to floods."""
    if max_related < 0:
        return edges
    related_idx = [i for i, e in enumerate(edges) if e.predicate == "related_to" and e.tier != "drop"]
    if len(related_idx) <= max_related:
        return edges
    related_idx.sort(key=lambda i: -edges[i].confidence)
    kill = set(related_idx[max_related:])
    for i in kill:
        e = edges[i]
        edges[i] = Edge(e.subject, e.predicate, e.object, e.confidence, "drop", "related_cap")
    return edges


# Predicate -> is this (subject_type, object_type) the VALID direction?
# Used to pick the correct direction when both A->B and B->A were generated.
DIRECTION_TYPE = {
    "created_by":   lambda st, ot: ot in ("Person", "Organization"),
    "works_for":    lambda st, ot: st == "Person" and ot == "Organization",
    "located_in":   lambda st, ot: ot == "Location",
    "member_of":    lambda st, ot: ot in ("Organization", "Concept"),
    "references":   lambda st, ot: ot in ("Document", "Concept", "Standard"),
    "instance_of":  lambda st, ot: ot == "Concept",
    "example_of":   lambda st, ot: ot == "Concept",
    "owns":         lambda st, ot: st in ("Person", "Organization"),
    # operational: subject is the active system/agent, object is the content/target
    "stores":       lambda st, ot: st in ("Software", "Product", "Method", "Organization", "Location")
                                   and ot in ("Artifact", "Concept", "Document", "Standard"),
    "detects":      lambda st, ot: st in ("Software", "Method", "Product")
                                   and ot in ("Concept", "Event", "Artifact", "Person"),
    "produces":     lambda st, ot: st in ("Software", "Method", "Person", "Organization", "Product")
                                   and ot in ("Artifact", "Document", "Concept", "Product"),
}


def _pos_in_text(text: str, name: str) -> int:
    return text.find(name) if text and name else -1


def resolve_directions(edges: List[Edge], pairs: List[dict]) -> List[int]:
    """candidate_pairs() permutes entities, so each unordered pair {A,B} produces
    both A->B and B->A. The reverse is usually a spurious, wrongly-directed edge.
    Collapse each unordered pair to ONE directed edge, scored by (in priority):
      1. type-direction valid for the predicate (DIRECTION_TYPE)
      2. READING ORDER — subject appears before object in the evidence text
         (active voice subject-verb-object; the general direction signal)
      3. committed (non-related) tier
      4. model confidence
    Returns the list of edge indices to KEEP."""
    groups: Dict[frozenset, List[int]] = {}
    for i, p in enumerate(pairs):
        key = frozenset((p["subject"].lower(), p["object"].lower()))
        groups.setdefault(key, []).append(i)

    def score(i):
        e, p = edges[i], pairs[i]
        rule = DIRECTION_TYPE.get(e.predicate)
        type_ok = 1 if (rule and rule(p.get("subject_type", "Concept"),
                                      p.get("object_type", "Concept"))) else 0
        text = (p.get("text") or "").lower()
        si = _pos_in_text(text, p["subject"].lower())
        oi = _pos_in_text(text, p["object"].lower())
        reading_order = 1 if (0 <= si < oi) else 0   # subject before object
        exact = 1 if e.tier not in ("tier3_related", "drop") else 0
        return (type_ok, reading_order, exact, e.confidence)

    keep = []
    for idxs in groups.values():
        keep.append(max(idxs, key=score))
    return sorted(keep)


def pairs_from_gold_relations(chunk: dict) -> List[dict]:
    """Alternative: use the chunk's existing relation (sub,obj) as candidate pairs,
    using the gold evidence_phrase + relation_cue. Simulates an upstream that
    already found the pairs; measures the cascade's ceiling."""
    text = norm(chunk.get("text", ""))
    tmap = {}
    for e in (chunk.get("entities") or []):
        for n in entity_names(e):
            tmap.setdefault(n.lower(), e.get("entity_type", "Concept"))
    out = []
    for rel in (chunk.get("relations") or []):
        subj, obj = norm(rel.get("subject", "")), norm(rel.get("object", ""))
        if not subj or not obj or subj.lower() == obj.lower():
            continue
        out.append({
            "text": norm(rel.get("evidence_phrase") or "") or text,
            "cue": norm(rel.get("relation_cue") or ""),
            "subject": subj,
            "subject_type": tmap.get(subj.lower(), "Concept"),
            "object": obj,
            "object_type": tmap.get(obj.lower(), "Concept"),
            "_gold": rel.get("predicate"),
        })
    return out


def iter_chunks(path: str, limit: Optional[int]) -> Iterable[dict]:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if limit and i >= limit:
                break
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except Exception:
                    continue


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", required=True, help="JSONL of chunks (with entities)")
    ap.add_argument("--out", default="local_ghost_b_relations.jsonl")
    ap.add_argument("--runs_dir", default="runs")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--pair_mode", choices=["cooccur", "gold"], default="cooccur",
                    help="cooccur = derive pairs from entities (true broke-mode); "
                         "gold = reuse chunk's relation pairs (ceiling).")
    ap.add_argument("--batch_pairs", type=int, default=512)
    ap.add_argument("--relexist_dir", default=os.environ.get("LOCAL_GHOST_B_RELATION_EXISTS_DIR", ""),
                    help="relation_exists gate checkpoint dir; empty = gate off.")
    ap.add_argument("--relexist_threshold", type=float,
                    default=float(os.environ.get("LOCAL_GHOST_B_RELEXIST_THRESHOLD") or 0.70))
    args = ap.parse_args()

    ex = LocalExtractor(args.runs_dir)
    print(f"[config] {json.dumps(ex.config_summary())}", flush=True)
    max_related = _envi("LOCAL_GHOST_B_MAX_RELATED_TO_PER_CHUNK", 3)

    gate = None
    if args.relexist_dir and Path(args.relexist_dir).exists():
        gate = RelationExistsGate(args.relexist_dir, threshold=args.relexist_threshold)
        print(f"[gate] relation_exists ON (threshold={args.relexist_threshold})", flush=True)

    out_f = open(args.out, "w", encoding="utf-8")
    n_chunks = n_cand = n_pairs = n_written = 0
    for chunk in iter_chunks(args.chunks, args.limit or None):
        n_chunks += 1
        pairs = (pairs_from_gold_relations(chunk) if args.pair_mode == "gold"
                 else candidate_pairs(chunk))
        if not pairs:
            continue
        n_cand += len(pairs)
        # learned pair-quality gate: drop low-prob candidates before the cascade
        if gate is not None and args.pair_mode == "cooccur":
            pairs = gate.filter(pairs)
            if not pairs:
                continue
        edges = ex.extract(pairs)
        n_pairs += len(pairs)
        # collapse reverse/duplicate-direction edges (cooccur permutes entities)
        if args.pair_mode == "cooccur":
            keep = resolve_directions(edges, pairs)
            edges = [edges[i] for i in keep]
            kept_pairs = [pairs[i] for i in keep]
        else:
            kept_pairs = pairs
        edges = apply_related_cap(edges, max_related)
        for e, p in zip(edges, kept_pairs):
            rec = LocalExtractor.to_ghost_b_record(e, p)
            if rec is None:
                continue
            rec["chunk_id"] = chunk.get("chunk_id", "")
            rec["doc_id"] = chunk.get("doc_id", "")
            out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n_written += 1
    out_f.close()
    gate_msg = f" (gate kept {n_pairs}/{n_cand} = {100*n_pairs/max(n_cand,1):.0f}%)" if gate else ""
    print(f"[done] chunks={n_chunks} candidates={n_cand} classified={n_pairs}{gate_msg} "
          f"relations_written={n_written} -> {args.out}")


if __name__ == "__main__":
    main()
