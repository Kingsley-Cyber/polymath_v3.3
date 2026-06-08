"""
Polymath broke-mode local predicate extractor.

Cascade (per entity pair, given evidence phrase + cue + entity types):

    1. Backbone head   -> 11 structural/operational predicates + none
    2. Easy head       -> 7 distinctive affiliation/spatial/temporal predicates + none
    3. Family head     -> 8-way coarse router (for predicates no exact head covers)
    + Python compiler  -> cue rules + type/direction rules resolve / confirm the
                          exact Ghost B predicate, and RECOVER part_of/uses that
                          the models can't confidently commit to.

Tiers (production rule):
    tier1_exact   high-confidence head OR cue-confirmed -> write exact predicate
    tier2_family  confident family + cue/type resolution -> write exact predicate
    tier3_related moderate signal, no exact resolution   -> write related_to
    drop          weak signal                            -> drop

All models load OFFLINE from local cache. Set HF_HUB_OFFLINE=1.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import json
import numpy as np
import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

# ---------------------------------------------------------------- cue rules
# Ordered: first match wins. Maps a regex over the cue/evidence to a predicate.
CUE_RULES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\b(?:consists? of|comprises?|made up of|component of|"
                r"section of|chapter of|subsystem of|module of)\b", re.I), "part_of"),
    (re.compile(r"\b(?:contains?|includes?|composed of|part[s]? of)\b", re.I), "part_of"),
    (re.compile(r"\b(?:member of|belongs? to|one of the|joined)\b", re.I), "member_of"),
    (re.compile(r"\b(?:such as|for example|for instance|e\.?g\.?)\b", re.I), "example_of"),
    (re.compile(r"\b(?:a type of|a kind of|an instance of|is one of the)\b", re.I), "instance_of"),
    (re.compile(r"\b(?:requires?|relies? on|depends? on|prerequisite|built on)\b", re.I), "depends_on"),
    (re.compile(r"\b(?:implements?|realizes?|conforms? to|provides? (?:an? )?interface)\b", re.I), "implements"),
    (re.compile(r"\b(?:produces?|generates?|outputs?|yields?|emits?)\b", re.I), "produces"),
    (re.compile(r"\b(?:stores?|persists?|caches?)\b", re.I), "stores"),
    (re.compile(r"\b(?:detects?|identifies?|recognizes?|classif|finds?)\b", re.I), "detects"),
    (re.compile(r"\b(?:supports?|enables?|allows?|facilitates?|helps?)\b", re.I), "supports"),
    (re.compile(r"\b(?:uses?|employs?|leverages?|utiliz|applies|via|through)\b", re.I), "uses"),
    (re.compile(r"\b(?:located in|found in|based in|situated|resides? in|stored at)\b", re.I), "located_in"),
    (re.compile(r"\b(?:created by|authored by|written by|developed by|designed by|made by)\b", re.I), "created_by"),
    (re.compile(r"\b(?:works? for|employed by|works? at|staff of)\b", re.I), "works_for"),
    (re.compile(r"\b(?:owns?|owned by|possess)\b", re.I), "owns"),
    (re.compile(r"\b(?:affiliated with|partnered with|a member organization)\b", re.I), "affiliated_with"),
    (re.compile(r"\b(?:during|while|throughout|amid|in the course of)\b", re.I), "during"),
    (re.compile(r"\b(?:before|after|precedes?|preceded by|followed by|then|subsequently)\b", re.I), "preceded_by"),
    (re.compile(r"\b(?:overlaps?|coincides?|concurrent)\b", re.I), "overlaps"),
    (re.compile(r"\b(?:causes?|leads? to|results? in|because of|due to|triggers?)\b", re.I), "causes"),
    (re.compile(r"\b(?:defines?|definition of|defined as|specifies?)\b", re.I), "defines"),
    (re.compile(r"\b(?:represents?|stands? for|denotes?|symboliz)\b", re.I), "represents"),
    (re.compile(r"\b(?:maps? to|corresponds? to|equivalent to|translates? to)\b", re.I), "maps_to"),
    (re.compile(r"\b(?:same as|synonym|also known as|a\.?k\.?a\.?)\b", re.I), "synonym_of"),
    (re.compile(r"\b(?:cites?|citation|references? the|as described in)\b", re.I), "references"),
    (re.compile(r"\b(?:derived from|based on|adapted from|extends?|inherits?)\b", re.I), "derived_from"),
    (re.compile(r"\b(?:contradicts?|conflicts? with|opposes?|disagrees?)\b", re.I), "contradicts"),
    (re.compile(r"\b(?:overrides?|supersedes?|replaces?)\b", re.I), "overrides"),
    (re.compile(r"\b(?:except|unless|excluding|other than)\b", re.I), "excepts"),
]

# Family -> predicates it can resolve to (for family-tier resolution).
FAMILY_PREDICATES: Dict[str, List[str]] = {
    "structural":   ["part_of", "member_of", "instance_of", "example_of"],
    "operational":  ["uses", "implements", "depends_on", "supports", "produces", "stores", "detects"],
    "definitional": ["synonym_of", "defines", "represents", "maps_to"],
    "affiliation":  ["works_for", "owns", "affiliated_with", "created_by", "located_in"],
    "temporal":     ["preceded_by", "during", "overlaps"],
    "causal":       ["causes", "contradicts", "overrides", "excepts"],
    "provenance":   ["references", "derived_from"],
    "sentinel":     ["related_to"],
}

# Soft type/direction rules: predicate -> function(subj_type, obj_type) -> bool plausible.
TYPE_RULES = {
    "located_in":   lambda s, o: o == "Location",
    "works_for":    lambda s, o: s == "Person" and o == "Organization",
    "member_of":    lambda s, o: o in ("Organization", "Concept"),
    "created_by":   lambda s, o: o in ("Person", "Organization"),
}

# ---------------------------------------------------- type-plausibility gate
# Post-classification false-positive filter. candidate_pairs() emits every
# same-sentence entity pair, so the model can type an implausible pair, e.g.
# "vectors stores HNSW index" (a passive Concept can't be the subject of stores).
# If a committed predicate's subject/object types are implausible, the pair is a
# false positive -> demote to related_to (keep the connection, drop the wrong type).
# Active-agent subject types for operational predicates:
_ACTIVE = {"Software", "Product", "Method", "Organization", "Person"}
TYPE_CONSTRAINTS: Dict[str, "callable"] = {
    # operational: subject must be an active system/agent, not a passive Concept/Artifact
    "stores":     lambda st, ot: st in (_ACTIVE | {"Artifact"}),
    "detects":    lambda st, ot: st in (_ACTIVE | {"Artifact"}),
    "produces":   lambda st, ot: st in _ACTIVE,
    "deploys":    lambda st, ot: st in _ACTIVE,
    "runs":       lambda st, ot: st in (_ACTIVE | {"Artifact"}),
    "trains":     lambda st, ot: st in _ACTIVE,
    "evaluates":  lambda st, ot: st in _ACTIVE,
    "implements": lambda st, ot: st in (_ACTIVE | {"Artifact"}),
    # agentive / spatial: object/subject role types
    "created_by": lambda st, ot: ot in ("Person", "Organization"),
    "works_for":  lambda st, ot: st == "Person" and ot == "Organization",
    "owns":       lambda st, ot: st in ("Person", "Organization"),
    "located_in": lambda st, ot: ot == "Location",
}


def type_plausible(pred: str, st: str, ot: str) -> bool:
    con = TYPE_CONSTRAINTS.get(pred)
    return con(st, ot) if con else True

# ----------------------------------------------------- dangerous-pair guard
# Different-meaning predicates that, if confidently CONFUSED, corrupt the graph
# (unlike semantic-equivalents like instance_of/example_of where either is fine).
# The guard requires a decisive cue/type signal to commit one of these; on a
# conflicting or absent signal it abstains to related_to (no wrong-meaning edge).
DANGEROUS_CLUSTERS = [
    {"causes", "contradicts", "overrides"},          # causation vs conflict vs supersede
    {"references", "created_by", "derived_from"},     # citation vs authorship vs derivation
    {"preceded_by", "during"},                        # sequence vs overlap
]
PRED_TO_DCLUSTER = {p: frozenset(c) for c in DANGEROUS_CLUSTERS for p in c}

# Cluster-scoped cue arbiter: predicate -> regex that POSITIVELY indicates it.
DANGER_CUE: Dict[str, re.Pattern] = {
    "causes":       re.compile(r"\b(?:causes?|caused|leads? to|lead to|results? in|because|due to|triggers?|so that|drives?|makes?)\b", re.I),
    "contradicts":  re.compile(r"\b(?:contradicts?|conflicts?|opposes?|refutes?|inconsistent|disagrees?|prevents?|negates?|violates?)\b", re.I),
    "overrides":    re.compile(r"\b(?:overrides?|supersedes?|takes? precedence|replaces?|overrules?|preempts?)\b", re.I),
    "references":   re.compile(r"\b(?:cites?|citation|references? the|as described in|refers? to|see also|et al|\(\d{4}\))\b", re.I),
    "created_by":   re.compile(r"\b(?:created by|authored by|written by|developed by|designed by|made by|wrote|invented by)\b", re.I),
    "derived_from": re.compile(r"\b(?:derived from|based on|adapted from|extends?|inherits?|builds? on|stems? from)\b", re.I),
    "preceded_by":  re.compile(r"\b(?:before|after|precedes?|preceded by|followed by|prior to|subsequently|then)\b", re.I),
    "during":       re.compile(r"\b(?:during|while|throughout|amid|in the course of|concurrent)\b", re.I),
}


def _danger_type_vote(cluster: frozenset, st: str, ot: str) -> Optional[str]:
    # provenance/authorship cluster: object type disambiguates
    if "references" in cluster:
        if ot in ("Person", "Organization"):
            return "created_by"
        if ot == "Document":
            return "references"
    return None


def guard_dangerous(pred: str, text: str, cue: str, st: str, ot: str) -> str:
    """Minimal-intervention guard for dangerous-cluster predicates.

    The model alone is fairly reliable here, so we do NOT 'correct' to the cue
    (that flips correct edges when the cue is noisy) and we do NOT abstain on
    silence (that throws away good predictions). We abstain to related_to ONLY
    when the cue/type actively CONFLICTS with the model -- pointing at a different
    cluster-mate and NOT at the model's pick. In that case neither signal is
    trustworthy, so we refuse to assert a possibly-wrong-meaning edge.
    Non-dangerous predicates pass through unchanged."""
    cluster = PRED_TO_DCLUSTER.get(pred)
    if cluster is None:
        return pred
    cue_votes = {cand for cand in cluster
                 if DANGER_CUE.get(cand) and
                 (DANGER_CUE[cand].search(cue or "") or DANGER_CUE[cand].search(text or ""))}
    if cue_votes and pred not in cue_votes:
        return "related_to"                # cue points elsewhere -> conflict -> abstain
    tv = _danger_type_vote(cluster, st, ot)
    if tv and tv != pred:
        return "related_to"                # type points elsewhere -> conflict -> abstain
    return pred                            # cue agrees or silent -> keep the model


def match_cue(*texts: str) -> Optional[str]:
    for t in texts:
        if not t:
            continue
        for pat, pred in CUE_RULES:
            if pat.search(t):
                return pred
    return None


def family_default(fam: str) -> str:
    # When cue doesn't resolve within a family, pick the family's most generic member.
    return {
        "structural": "part_of", "operational": "uses", "definitional": "defines",
        "affiliation": "affiliated_with", "temporal": "during", "causal": "causes",
        "provenance": "references", "sentinel": "related_to",
    }.get(fam, "related_to")


@dataclass
class Edge:
    subject: str
    predicate: str
    object: str
    confidence: float
    tier: str          # tier1_exact | tier2_family | tier3_related | drop
    source: str        # which head/rule decided


class _Head:
    def __init__(self, ckpt: str, device: str = "cuda"):
        lm = json.loads((Path(ckpt) / "label_map.json").read_text(encoding="utf-8"))
        self.labels = [l for l, _ in sorted(lm["label2id"].items(), key=lambda kv: kv[1])]
        self.tok = AutoTokenizer.from_pretrained(ckpt)
        self.sep = self.tok.sep_token or "[SEP]"
        self.model = AutoModelForSequenceClassification.from_pretrained(
            ckpt, dtype=torch.bfloat16).to(device).eval()
        self.device = device

    def _input(self, p: dict) -> str:
        base = (f"{p['text']} {self.sep} {p['subject']} {self.sep} {p['subject_type']} "
                f"{self.sep} {p['object']} {self.sep} {p['object_type']}")
        if p.get("cue"):
            base += f" {self.sep} {p['cue']}"
        return base

    @torch.inference_mode()
    def predict(self, pairs: List[dict], batch_size: int = 256) -> List[Tuple[str, float]]:
        out: List[Tuple[str, float]] = []
        for s in range(0, len(pairs), batch_size):
            chunk = pairs[s:s + batch_size]
            enc = self.tok([self._input(p) for p in chunk], padding=True, truncation=True,
                           max_length=192, return_tensors="pt").to(self.device)
            probs = torch.softmax(self.model(**enc).logits.float(), dim=-1).cpu().numpy()
            for row in probs:
                i = int(row.argmax())
                out.append((self.labels[i], float(row[i])))
        return out


def _envf(name, default):
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default


def _envb(name, default):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# Predicates that need extra evidence to be written exact (loose catch-alls).
GUARDED = {"part_of", "uses"}


class LocalExtractor:
    """Broke-mode cascade. Config from env (LOCAL_GHOST_B_*) with kwarg overrides:

        LOCAL_GHOST_B_MIN_EXACT_CONF          default 0.80  (easy/backbone exact gate)
        LOCAL_GHOST_B_FAMILY_CONF             default 0.80
        LOCAL_GHOST_B_PART_OF_USES_REQUIRE_CUE default false (true = part_of/uses
                                              exact ONLY when a strong cue agrees)
        LOCAL_GHOST_B_ALLOW_RELATED_TO        default true   (false = drop instead)
        LOCAL_GHOST_B_RELATED_MIN_CONF        default 0.50
    """
    def __init__(self, runs_dir: str,
                 backbone="backbone_v1/best", easy="easy_predicate_v1/best",
                 family="family_v1/best",
                 t_backbone=None, t_easy=None, t_family=None,
                 t_cue_recover=0.40, t_related=None,
                 part_of_uses_require_cue=None, allow_related_to=None,
                 device="cuda"):
        rd = Path(runs_dir)
        self.backbone = _Head(str(rd / backbone), device)
        self.easy = _Head(str(rd / easy), device)
        self.family = _Head(str(rd / family), device)
        min_exact = _envf("LOCAL_GHOST_B_MIN_EXACT_CONF", 0.80)
        self.t_backbone = t_backbone if t_backbone is not None else min_exact
        self.t_easy = t_easy if t_easy is not None else max(min_exact, 0.85)
        self.t_family = t_family if t_family is not None else _envf("LOCAL_GHOST_B_FAMILY_CONF", 0.80)
        self.t_cue_recover = t_cue_recover
        self.t_related = t_related if t_related is not None else _envf("LOCAL_GHOST_B_RELATED_MIN_CONF", 0.50)
        self.part_of_uses_require_cue = (part_of_uses_require_cue if part_of_uses_require_cue is not None
                                         else _envb("LOCAL_GHOST_B_PART_OF_USES_REQUIRE_CUE", False))
        self.allow_related_to = (allow_related_to if allow_related_to is not None
                                 else _envb("LOCAL_GHOST_B_ALLOW_RELATED_TO", True))
        self.danger_guard = _envb("LOCAL_GHOST_B_DANGER_GUARD", False)
        self.type_constraints = _envb("LOCAL_GHOST_B_TYPE_CONSTRAINTS", True)

    def extract(self, pairs: List[dict]) -> List[Edge]:
        bb = self.backbone.predict(pairs)
        ez = self.easy.predict(pairs)
        fm = self.family.predict(pairs)
        edges: List[Edge] = []
        for p, (pb, cb), (pe, ce), (pf, cf) in zip(pairs, bb, ez, fm):
            edges.append(self._resolve(p, pb, cb, pe, ce, pf, cf))
        return edges

    def _resolve(self, p, pb, cb, pe, ce, pf, cf) -> Edge:
        subj, obj = p["subject"], p["object"]
        st, ot = p.get("subject_type", "Concept"), p.get("object_type", "Concept")
        cue_pred = match_cue(p.get("cue", ""), p.get("text", ""))

        def edge(pred, conf, tier, src):
            return Edge(subj, pred, obj, round(conf, 3), tier, src)

        def commit(pred, conf, tier, src):
            # type-plausibility gate: a typed edge whose entity types are
            # implausible for the predicate is a candidate-pairing false positive
            # (e.g. "vectors stores HNSW index") -> demote to related_to.
            if self.type_constraints and not type_plausible(pred, st, ot):
                return edge("related_to", conf, "tier3_related", f"type_violation:{pred}")
            # dangerous-pair guard: a confident commit on a different-meaning
            # predicate must survive the cue/type arbiter, else -> related_to.
            if self.danger_guard and pred in PRED_TO_DCLUSTER:
                g = guard_dangerous(pred, p.get("text", ""), p.get("cue", ""), st, ot)
                if g != pred:
                    if g == "related_to":
                        return edge("related_to", conf, "tier3_related", f"danger_guard:{pred}")
                    return edge(g, conf, tier, f"{src}+corrected:{pred}->{g}")
            return edge(pred, conf, tier, src)

        # --- Tier 1: confident exact heads -------------------------------------
        candidates: List[Tuple[str, float, str]] = []
        if pb != "none" and cb >= self.t_backbone:
            candidates.append((pb, cb, "backbone"))
        if pe != "none" and ce >= self.t_easy:
            candidates.append((pe, ce, "easy"))
        if candidates:
            pred, conf, src = max(candidates, key=lambda c: c[1])
            # Guarded predicates (part_of/uses) are loose catch-alls: optionally
            # require a strong cue to agree before writing them exact, even at
            # high model confidence. If required and missing, defer to lower tiers.
            if pred in GUARDED and self.part_of_uses_require_cue and cue_pred != pred:
                pass  # fall through; don't write a guarded exact without cue
            else:
                return commit(pred, conf, "tier1_exact", src)

        # --- Tier 1b: cue-confirmed recovery (esp. part_of / uses) -------------
        # Model leans toward a backbone predicate but isn't confident enough;
        # if the deterministic cue agrees (or model is the catch-all part_of/uses
        # and cue names a backbone predicate), commit via the rule.
        if cue_pred:
            model_top = pb if pb != "none" else (pe if pe != "none" else None)
            if model_top == cue_pred and max(cb, ce) >= self.t_cue_recover:
                return commit(cue_pred, max(cb, ce, 0.70), "tier1_exact", "cue+model")
            # part_of/uses recovery: backbone weakly says part_of/uses and cue confirms
            if pb in ("part_of", "uses") and cue_pred in ("part_of", "uses") and cb >= self.t_cue_recover:
                return commit(cue_pred, max(cb, 0.65), "tier1_exact", "cue_recover")

        # --- Tier 2: confident family + cue/type resolution -------------------
        # Only commit an EXACT predicate when the cue (or a type rule) actually
        # resolves it within the family. An unresolved family is NOT enough to
        # guess an exact edge -> it falls through to related_to below.
        if pf != "sentinel" and cf >= self.t_family:
            fam_preds = FAMILY_PREDICATES.get(pf, [])
            chosen = None
            if cue_pred in fam_preds:
                chosen = cue_pred
            else:
                for cand in fam_preds:
                    rule = TYPE_RULES.get(cand)
                    if rule and rule(st, ot):
                        chosen = cand
                        break
            if chosen is not None:
                return commit(chosen, cf, "tier2_family", f"family:{pf}")
            # confident family but no exact resolution -> related_to (keep the edge)
            return edge("related_to", cf, "tier3_related", f"family_unresolved:{pf}")

        # --- Tier 3: related_to fallback --------------------------------------
        best = max(cb, ce, cf)
        if self.allow_related_to and (best >= self.t_related or cue_pred is not None):
            return edge("related_to", best, "tier3_related",
                        "cue_only" if cue_pred else "fallback")

        # --- drop --------------------------------------------------------------
        return edge("related_to", best, "drop", "weak")

    # ------------------------------------------------------------------ adapter
    @staticmethod
    def to_ghost_b_record(edge: "Edge", p: dict) -> Optional[dict]:
        """Existing Ghost B JSONL relation shape. Returns None for dropped edges."""
        if edge.tier == "drop":
            return None
        return {
            "t": "r",
            "sub": edge.subject,
            "pred": edge.predicate,
            "obj": edge.object,
            "ok": "entity",
            "cf": edge.confidence,
            "ev": p.get("text", ""),
            "cue": p.get("cue", ""),
        }

    def config_summary(self) -> dict:
        return {
            "t_backbone": self.t_backbone, "t_easy": self.t_easy,
            "t_family": self.t_family, "t_related": self.t_related,
            "part_of_uses_require_cue": self.part_of_uses_require_cue,
            "allow_related_to": self.allow_related_to,
        }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs_dir", default="runs")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    ex = LocalExtractor(args.runs_dir)
    demo = [
        {"text": "Qdrant stores vectors in an HNSW index", "cue": "stores",
         "subject": "qdrant", "subject_type": "Software", "object": "hnsw index", "object_type": "Artifact"},
        {"text": "The retriever module is part of the RAG pipeline", "cue": "part of",
         "subject": "retriever module", "subject_type": "Software", "object": "rag pipeline", "object_type": "Concept"},
        {"text": "Flutter uses the Dart language", "cue": "uses",
         "subject": "flutter", "subject_type": "Software", "object": "dart", "object_type": "Software"},
        {"text": "Alice Chen works for Meta AI", "cue": "works for",
         "subject": "alice chen", "subject_type": "Person", "object": "meta ai", "object_type": "Organization"},
    ]
    for e in ex.extract(demo):
        print(f"  {e.subject} --{e.predicate}--> {e.object}  [{e.tier}/{e.source}] conf={e.confidence}")
