"""
glirel_infer.py
===============
Mac inference for the fine-tuned GLiREL Ghost B relation classifier.

Drop-in alternative to the cascade's run_on_mac.py: takes the SAME chunks JSONL
(chunk_id, doc_id, text, entities[]) and emits the SAME Ghost B relation records,
so you can A/B GLiREL vs the cascade on identical inputs.

Pipeline (per chunk):
  text -> sentences -> (tokenize with GLiREL's regex, locate entity spans) ->
  GLiREL.predict_relations(tokens, 30 labels, ner, threshold, top_k=1) ->
  map spans back to entities -> safety_rules (type-plausibility + dangerous guard)
  -> collapse reverse directions -> related_to cap -> Ghost B records.

The tokenizer + span-location here are byte-identical to the training converter,
which is byte-identical to GLiREL's own inference tokenizer -> zero train/infer skew.

Usage (Mac):
  python glirel_infer.py --demo
  python glirel_infer.py --chunks my_chunks.jsonl --out glirel_relations.jsonl \
         --ckpt glirel_ghost_b_v1/best --threshold 0.5
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Optional

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import torch
from glirel import GLiREL

from safety_rules import type_plausible, guard_dangerous

HERE = Path(__file__).resolve().parent
_TOK_RE = re.compile(r"\w+(?:[-_]\w+)*|\S")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WS = re.compile(r"\s+")


def tokenize(t: str): return _TOK_RE.findall(t or "")
def norm(s) -> str: return _WS.sub(" ", str(s or "")).strip()


def entity_names(e: dict):
    out = []
    for k in ("surface_form", "canonical_name"):
        v = norm(e.get(k, ""))
        if v:
            out.append(v)
    out.extend(norm(a) for a in (e.get("query_aliases") or []) if norm(a))
    seen, uniq = set(), []
    for n in out:
        if n.lower() not in seen:
            seen.add(n.lower()); uniq.append(n)
    return uniq


def locate_span(tokens_lower, mention):
    ment = [t.lower() for t in tokenize(mention)]
    n = len(ment)
    if n == 0:
        return None
    for i in range(len(tokens_lower) - n + 1):
        if tokens_lower[i:i + n] == ment:
            return (i, i + n - 1)
    return None


def out_name(e: dict) -> str:
    return norm(e.get("canonical_name") or e.get("surface_form") or "")


def pick_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class GliRELClassifier:
    def __init__(self, ckpt_dir: str, labels, device: str, threshold: float = 0.5,
                 max_entities: int = 16, max_tokens: int = 160,
                 type_gate: bool = True, danger_guard: bool = False):
        self.labels = labels
        self.device = device
        self.threshold = threshold
        self.max_entities = max_entities
        self.max_tokens = max_tokens
        self.type_gate = type_gate
        self.danger_guard = danger_guard
        self.model = GLiREL.from_pretrained(ckpt_dir)
        self.model.to(device)
        self.model.device = torch.device(device)
        self.model.config.fixed_relation_types = True
        self.model.eval()

    def _sentence_units(self, chunk: dict):
        """Yield (tokens, ner_spans, pos2ent, sentence_text) per sentence with >=2 located entities."""
        text = norm(chunk.get("text", ""))
        entities = chunk.get("entities") or []
        for sent in _SENT_SPLIT.split(text):
            toks = tokenize(sent)
            if len(toks) < 3 or len(toks) > self.max_tokens:
                continue
            tl = [t.lower() for t in toks]
            ner, pos2ent, taken = [], {}, set()
            for e in entities:
                etype = e.get("entity_type", "Concept")
                for nm in entity_names(e):
                    sp = locate_span(tl, nm)
                    if sp and sp not in taken:
                        taken.add(sp)
                        ner.append([sp[0], sp[1], etype])
                        pos2ent[sp] = e
                        break
                if len(ner) >= self.max_entities:
                    break
            if len(ner) >= 2:
                yield toks, ner, pos2ent, sent

    def extract_chunk(self, chunk: dict, max_related: int = 3):
        units = list(self._sentence_units(chunk))
        if not units:
            return []
        token_lists = [u[0] for u in units]
        ner_lists = [u[1] for u in units]
        batch = self.model.batch_predict_relations(
            token_lists, self.labels, flat_ner=True,
            threshold=self.threshold, ner=ner_lists, top_k=1,
        )
        edges = []  # (sub, pred, obj, score, ev)
        for (toks, ner, pos2ent, sent), preds in zip(units, batch):
            for r in preds:
                hp = (r["head_pos"][0], r["head_pos"][1] - 1)  # back to inclusive
                tp = (r["tail_pos"][0], r["tail_pos"][1] - 1)
                se = pos2ent.get(hp); oe = pos2ent.get(tp)
                if se is None or oe is None or hp == tp:
                    continue
                st = se.get("entity_type", "Concept"); ot = oe.get("entity_type", "Concept")
                pred = r["label"]; score = float(r["score"])
                if self.type_gate and not type_plausible(pred, st, ot):
                    pred = "related_to"
                if self.danger_guard:
                    pred = guard_dangerous(pred, sent, "", st, ot)
                edges.append({"sub": out_name(se), "pred": pred, "obj": out_name(oe),
                              "score": round(score, 4), "ev": sent,
                              "st": st, "ot": ot})
        edges = _collapse_directions(edges)
        edges = _cap_related(edges, max_related)
        return edges


def _collapse_directions(edges):
    """One directed edge per unordered entity pair: keep the higher score."""
    best = {}
    for e in edges:
        key = frozenset((e["sub"].lower(), e["obj"].lower()))
        if key not in best or e["score"] > best[key]["score"]:
            best[key] = e
    return list(best.values())


def _cap_related(edges, max_related):
    if max_related < 0:
        return edges
    rel = [e for e in edges if e["pred"] == "related_to"]
    if len(rel) <= max_related:
        return edges
    rel.sort(key=lambda e: -e["score"])
    keep_ids = {id(e) for e in rel[:max_related]}
    return [e for e in edges if e["pred"] != "related_to" or id(e) in keep_ids]


def to_record(e: dict, chunk: dict) -> dict:
    return {"t": "r", "sub": e["sub"], "pred": e["pred"], "obj": e["obj"],
            "ok": "entity", "cf": e["score"], "ev": e["ev"], "cue": "",
            "chunk_id": chunk.get("chunk_id", ""), "doc_id": chunk.get("doc_id", "")}


DEMO = {
    "chunk_id": "demo_0", "doc_id": "demo",
    "text": ("Flame is a modular game engine built on top of Flutter. "
             "It provides a game loop and sprite components that Flutter does not. "
             "Alice Chen, a researcher at Meta AI, created the FineLlama model."),
    "entities": [
        {"canonical_name": "flame", "surface_form": "Flame", "entity_type": "Software"},
        {"canonical_name": "flutter", "surface_form": "Flutter", "entity_type": "Software"},
        {"canonical_name": "game loop", "surface_form": "game loop", "entity_type": "Concept"},
        {"canonical_name": "sprite components", "surface_form": "sprite components", "entity_type": "Concept"},
        {"canonical_name": "alice chen", "surface_form": "Alice Chen", "entity_type": "Person"},
        {"canonical_name": "meta ai", "surface_form": "Meta AI", "entity_type": "Organization"},
        {"canonical_name": "finellama", "surface_form": "FineLlama", "entity_type": "Product"},
    ],
}


def iter_chunks(path, limit=0):
    with open(path, encoding="utf-8", errors="replace") as f:
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
    ap.add_argument("--ckpt", default=str(HERE / "glirel_ghost_b_v1" / "best"))
    ap.add_argument("--labels", default="")
    ap.add_argument("--chunks", default="")
    ap.add_argument("--out", default="glirel_relations.jsonl")
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--max_related", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--danger_guard", action="store_true",
                    help="enable the dangerous-cluster guard (default off; net-neutral on cascade)")
    args = ap.parse_args()

    device = pick_device()
    labels_path = args.labels or str(Path(args.ckpt) / "labels.json")
    if not Path(labels_path).exists():
        labels_path = str(HERE / "data_glirel_full" / "labels.json")
    labels = json.loads(Path(labels_path).read_text(encoding="utf-8"))
    print(f"[device] {device}  [labels] {len(labels)}  [ckpt] {args.ckpt}", flush=True)

    clf = GliRELClassifier(args.ckpt, labels, device, threshold=args.threshold,
                           danger_guard=args.danger_guard)

    if args.demo or not args.chunks:
        for e in clf.extract_chunk(DEMO, args.max_related):
            print(f"  {e['sub']} --{e['pred']}--> {e['obj']}  "
                  f"[{e['st']}->{e['ot']}] score={e['score']}  | {e['ev'][:60]}")
        return

    n_chunks = n_written = 0
    with open(args.out, "w", encoding="utf-8") as out_f:
        for chunk in iter_chunks(args.chunks, args.limit or 0):
            n_chunks += 1
            for e in clf.extract_chunk(chunk, args.max_related):
                out_f.write(json.dumps(to_record(e, chunk), ensure_ascii=False) + "\n")
                n_written += 1
    print(f"[done] chunks={n_chunks} relations={n_written} -> {args.out}", flush=True)


if __name__ == "__main__":
    main()
