"""
Qwen ambiguity-resolver + hybrid (BERT cascade -> Qwen fallback) extractor.

The ModernBERT cascade is fast and handles confident cases. Qwen is slower and
only runs on the AMBIGUOUS edges the cascade couldn't commit (tier3_related,
drop, or low max-head-confidence). It sees the same prompt the model was trained
on (evidence + types + cue + the three head guesses) and returns one predicate.

On Apple Silicon swap QwenResolver's backend for mlx_lm; the prompt/parse logic
is identical. Set HF_HUB_OFFLINE=1.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

from polymath_local_extractor import Edge, LocalExtractor, match_cue

GHOST_B_PREDICATES = [
    "part_of", "member_of", "located_in", "works_for", "created_by", "owns",
    "affiliated_with", "synonym_of", "instance_of", "example_of", "uses",
    "references", "implements", "depends_on", "produces", "stores", "detects",
    "supports", "defines", "represents", "maps_to", "preceded_by", "causes",
    "overlaps", "during", "derived_from", "contradicts", "excepts", "overrides",
    "related_to",
]
VALID = set(GHOST_B_PREDICATES) | {"none"}
PRED_RE = re.compile(r"PREDICATE=([a-z_]+)")

SYSTEM = ("You resolve the exact Ghost B predicate between two entities in a text, "
          "using the evidence and the fast classifier's guesses. "
          "Reply with exactly one line: PREDICATE=<one predicate> where the predicate "
          "is one of: " + ", ".join(GHOST_B_PREDICATES) + ", or none.")


def build_prompt(p: dict, bb, cb, pe, ce, pf, cf) -> str:
    return (
        f"TEXT: {p['text']}\n"
        f"SUBJECT: {p['subject']} ({p.get('subject_type','Concept')})\n"
        f"OBJECT: {p['object']} ({p.get('object_type','Concept')})\n"
        f"CUE: {p.get('cue','') or '(none)'}\n"
        f"FAST_CLASSIFIER:\n"
        f"  backbone={bb} ({cb:.2f})\n"
        f"  easy={pe} ({ce:.2f})\n"
        f"  family={pf} ({cf:.2f})\n"
        f"Choose the exact predicate."
    )


class QwenResolver:
    def __init__(self, model_dir: str, device: str = "cuda"):
        self.tok = AutoTokenizer.from_pretrained(model_dir)
        if self.tok.pad_token is None:
            self.tok.pad_token = self.tok.eos_token
        self.tok.padding_side = "left"  # for batched generation
        self.model = AutoModelForCausalLM.from_pretrained(
            model_dir, dtype=torch.bfloat16, device_map={"": 0} if device == "cuda" else None)
        if device != "cuda":
            self.model = self.model.to(device)
        self.model.eval()
        self.device = device

    @torch.inference_mode()
    def resolve(self, prompts: List[str], batch_size: int = 32) -> List[str]:
        out: List[str] = []
        for s in range(0, len(prompts), batch_size):
            chunk = prompts[s:s + batch_size]
            texts = [self.tok.apply_chat_template(
                [{"role": "system", "content": SYSTEM},
                 {"role": "user", "content": pr}],
                tokenize=False, add_generation_prompt=True) for pr in chunk]
            enc = self.tok(texts, return_tensors="pt", padding=True,
                           add_special_tokens=False).to(self.model.device)
            gen = self.model.generate(**enc, max_new_tokens=12, do_sample=False,
                                      pad_token_id=self.tok.pad_token_id)
            for i in range(len(chunk)):
                dec = self.tok.decode(gen[i][enc["input_ids"].shape[1]:],
                                      skip_special_tokens=True)
                m = PRED_RE.search(dec)
                pred = m.group(1) if m else "none"
                out.append(pred if pred in VALID else "none")
        return out


class HybridExtractor:
    """BERT cascade with Qwen fallback on ambiguous edges."""

    def __init__(self, runs_dir: str, qwen_dir: str = "",
                 resolver=None, lowconf: float = 0.80, device: str = "cuda",
                 backbone: str = "backbone_v1/best", easy: str = "easy_predicate_v1/best",
                 family: str = "family_v1/best"):
        self.bert = LocalExtractor(runs_dir, backbone=backbone, easy=easy,
                                   family=family, device=device)
        # resolver can be injected (e.g. QwenResolverMLX on Apple Silicon);
        # otherwise load the PyTorch QwenResolver from qwen_dir.
        self.qwen = resolver if resolver is not None else QwenResolver(qwen_dir, device=device)
        self.lowconf = lowconf

    def extract(self, pairs: List[dict]):
        bb = self.bert.backbone.predict(pairs)
        ez = self.bert.easy.predict(pairs)
        fm = self.bert.family.predict(pairs)

        edges: List[Edge] = []
        amb_idx: List[int] = []
        amb_prompts: List[str] = []
        for i, (p, (pb, cb), (pe, ce), (pf, cf)) in enumerate(zip(pairs, bb, ez, fm)):
            edge = self.bert._resolve(p, pb, cb, pe, ce, pf, cf)
            edges.append(edge)
            # ambiguous = cascade didn't commit an exact predicate
            ambiguous = edge.tier not in ("tier1_exact", "tier2_family")
            if ambiguous:
                amb_idx.append(i)
                amb_prompts.append(build_prompt(p, pb, cb, pe, ce, pf, cf))

        if amb_prompts:
            preds = self.qwen.resolve(amb_prompts)
            for idx, pred in zip(amb_idx, preds):
                if pred not in ("none", "related_to"):
                    e = edges[idx]
                    edges[idx] = Edge(e.subject, pred, e.object, 0.0, "qwen_resolved", "qwen")
        return edges
