"""GLiREL relation classifier — drop-in alternative to the BERT cascade.

API mirrors LocalExtractor:
    classifier = GliRELClassifier(ckpt_dir, labels_path, device)
    edges = classifier.extract(pairs)        # pairs from candidate_pairs() + gate

Pairs are the same dicts the cascade consumes:
    {"text": ..., "cue": ..., "subject": ..., "subject_type": ...,
     "object": ..., "object_type": ...}

Edges are the same dataclass:
    Edge(subject, predicate, object, confidence, tier, source)

PLUG-AND-PLAY LOADER
- If `<ckpt_dir>/model.safetensors` exists, loads the fine-tuned checkpoint.
- Otherwise loads zero-shot `jackboyla/glirel-large-v0` (downloads on first
  use, then caches). Logs a loud WARNING so it's obvious the model isn't
  trained yet.

Either way, the loaded model classifies against the labels defined in
`<ckpt_dir>/label_descriptions.json` (or `labels_path` if provided
explicitly). That file ships with the bundle independent of training.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional, Tuple

from glirel import GLiREL

from polymath_local_extractor import Edge
from safety_rules import apply_safety
from pipeline_config import (
    GLIREL_ZERO_SHOT_FALLBACK,
    GLIREL_THRESHOLD as _DEFAULT_THRESHOLD,
    GLIREL_LABELS_FILE,
)

# ZS fallback — pipeline_config is the source of truth; env can override.
ZERO_SHOT_MODEL = os.environ.get("LOCAL_GHOST_B_GLIREL_ZS", GLIREL_ZERO_SHOT_FALLBACK)


def _envf(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default


class GliRELClassifier:
    """Per-pair relation classifier using GLiREL.

    Threshold semantics: predicate is committed only if score >= self.threshold
    AND the label is not `no_relation` / `none`. Below threshold or no-relation
    becomes `related_to` (not dropped — the pair survived the gate, so it's a
    real co-occurrence; we just don't know the predicate).
    """

    def __init__(
        self,
        ckpt_dir: str,
        labels_path: Optional[str] = None,
        device: str = "mps",
        threshold: Optional[float] = None,
    ):
        ckpt = Path(ckpt_dir)
        weights = ckpt / "model.safetensors"
        if weights.exists():
            self.model = GLiREL.from_pretrained(str(ckpt))
            self.source = "fine_tuned"
            print(f"[glirel] loaded fine-tuned checkpoint: {ckpt}", flush=True)
        else:
            self.model = GLiREL.from_pretrained(ZERO_SHOT_MODEL)
            self.source = "zero_shot"
            print(
                f"[glirel] WARNING: no fine-tuned weights at {ckpt}, "
                f"using zero-shot fallback {ZERO_SHOT_MODEL}",
                flush=True,
            )

        # Move to device when supported. GLiREL forwards .to() to its base.
        try:
            self.model = self.model.to(device)
            self.device = device
        except Exception as e:
            print(f"[glirel] device={device} failed ({e}); staying on cpu", flush=True)
            self.device = "cpu"

        # Label descriptions — REQUIRED. Without these GLiREL has no ontology
        # signal at zero-shot, and even fine-tuned needs the canonical list.
        if labels_path is None:
            labels_path = str(ckpt / "label_descriptions.json")
        lp = Path(labels_path)
        if not lp.exists():
            raise FileNotFoundError(
                f"label_descriptions.json not found at {lp}. "
                "Bundle should ship this file independent of model weights."
            )
        raw = json.loads(lp.read_text(encoding="utf-8"))
        # Drop schema/comment keys (any key starting with `_`).
        self.label_descriptions = {k: v for k, v in raw.items() if not k.startswith("_")}
        self.label_names = list(self.label_descriptions.keys())
        if not self.label_names:
            raise ValueError(f"no predicate labels in {lp}")
        print(
            f"[glirel] {len(self.label_names)} predicate labels "
            f"(includes no_relation: {'no_relation' in self.label_names})",
            flush=True,
        )

        self.threshold = (
            threshold if threshold is not None
            else _envf("LOCAL_GHOST_B_GLIREL_THRESHOLD", _DEFAULT_THRESHOLD)
        )
        print(f"[glirel] commit threshold = {self.threshold}", flush=True)

    # ---------- public API (mirrors LocalExtractor) ----------------------

    def extract(self, pairs: List[dict]) -> List[Edge]:
        edges = [self._extract_one(p) for p in pairs]
        return [apply_safety(e, p) for e, p in zip(edges, pairs)]

    def config_summary(self) -> dict:
        return {
            "classifier": "glirel",
            "source": self.source,
            "device": self.device,
            "threshold": self.threshold,
            "n_labels": len(self.label_names),
            "zs_model": ZERO_SHOT_MODEL if self.source == "zero_shot" else None,
        }

    # ---------- internals ------------------------------------------------

    def _extract_one(self, pair: dict) -> Edge:
        text = pair.get("text") or ""
        subj = pair["subject"]
        obj = pair["object"]
        tokens = text.split()

        subj_span = self._find_span(tokens, subj)
        obj_span = self._find_span(tokens, obj)
        if subj_span is None or obj_span is None:
            return self._fallback(pair, 0.0, "glirel:span_not_found")

        ner = [
            [subj_span[0], subj_span[1], pair.get("subject_type", "Concept"), subj],
            [obj_span[0], obj_span[1], pair.get("object_type", "Concept"), obj],
        ]

        try:
            preds = self.model.predict_relations(
                tokens, self.label_names, threshold=0.0, ner=ner, top_k=1
            )
        except Exception as e:
            return self._fallback(pair, 0.0, f"glirel:error:{type(e).__name__}")

        # GLiREL emits both directions. Pick the one matching head=subject.
        # Output positions are [start, end_exclusive]; our spans are
        # [start, end_inclusive] — comparing starts is safe and direction-
        # specific.
        for pr in preds:
            if pr.get("head_pos", [None])[0] == subj_span[0] and \
               pr.get("tail_pos", [None])[0] == obj_span[0]:
                label = pr.get("label") or "no_relation"
                score = float(pr.get("score") or 0.0)
                if label in ("no_relation", "none"):
                    return Edge(subj, "related_to", obj, round(score, 3),
                                "tier3_related", f"glirel:{self.source}:no_relation")
                if score < self.threshold:
                    return Edge(subj, "related_to", obj, round(score, 3),
                                "tier3_related",
                                f"glirel:{self.source}:below_thr({label}@{score:.2f})")
                return Edge(subj, label, obj, round(score, 3),
                            "tier1_exact", f"glirel:{self.source}")

        # No directional match found in output — shouldn't happen with our
        # 2-entity input, but be defensive.
        return self._fallback(pair, 0.0, "glirel:no_directional_match")

    @staticmethod
    def _fallback(pair: dict, score: float, source: str) -> Edge:
        return Edge(pair["subject"], "related_to", pair["object"],
                    round(score, 3), "tier3_related", source)

    @staticmethod
    def _find_span(tokens: List[str], name: str) -> Optional[Tuple[int, int]]:
        """Find a multi-token name in a whitespace-tokenized list.

        Case-insensitive, strips common surrounding punctuation. Returns
        (start, end_inclusive) or None. Tries exact match first, then a
        looser substring match per token (catches `flame,` / `flame.`).
        """
        name_tokens = [t.lower() for t in name.split() if t]
        if not name_tokens:
            return None
        strip = ".,;:!?\"'()[]{}"
        lower = [t.lower().strip(strip) for t in tokens]
        n = len(name_tokens)
        for i in range(len(lower) - n + 1):
            if all(lower[i + j] == name_tokens[j] for j in range(n)):
                return (i, i + n - 1)
        # Fallback: token-contains match (for "flame's" containing "flame")
        for i in range(len(lower) - n + 1):
            if all(name_tokens[j] in lower[i + j] for j in range(n)):
                return (i, i + n - 1)
        return None
