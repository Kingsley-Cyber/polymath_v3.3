"""
MLX fast-path Qwen resolver for Apple Silicon.

Same interface as qwen_resolver.QwenResolver (a .resolve(prompts)->List[str]),
but backed by mlx-lm instead of PyTorch. Use this AFTER converting the merged
model to MLX (see README_QWEN_CLAUDE.md):

    mlx_lm.convert --hf-path qwen_resolver_merged \
                   --mlx-path qwen_resolver_mlx -q --q-bits 4

Then:
    from qwen_resolver_mlx import QwenResolverMLX
    resolver = QwenResolverMLX("qwen_resolver_mlx")

Drop it into HybridExtractor by passing this instance (see README).

Requires: pip install mlx-lm
"""

from __future__ import annotations

import re
from typing import List

from mlx_lm import generate, load
from mlx_lm.sample_utils import make_sampler

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


class QwenResolverMLX:
    def __init__(self, mlx_path: str):
        self.model, self.tok = load(mlx_path)
        self.sampler = make_sampler(temp=0.0)  # greedy

    def resolve(self, prompts: List[str], **_) -> List[str]:
        out: List[str] = []
        for pr in prompts:
            text = self.tok.apply_chat_template(
                [{"role": "system", "content": SYSTEM},
                 {"role": "user", "content": pr}],
                tokenize=False, add_generation_prompt=True)
            resp = generate(self.model, self.tok, prompt=text,
                            max_tokens=12, sampler=self.sampler, verbose=False)
            m = PRED_RE.search(resp)
            pred = m.group(1) if m else "none"
            out.append(pred if pred in VALID else "none")
        return out
