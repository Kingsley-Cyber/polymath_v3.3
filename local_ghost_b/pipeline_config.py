"""Locked pipeline constants — single source of truth.

Both training-data prep (RTX) and inference (Mac) MUST import from here.
If a value drifts between the two paths, GLiREL's training distribution
won't match inference and quality silently degrades.

Bump PIPELINE_VERSION when changing any of these. Don't change values
in place without a version bump.
"""

from __future__ import annotations

PIPELINE_VERSION = "v1.2026.06"

# ---------- DEFAULT CLASSIFIER (LOCAL_GHOST_B_CLASSIFIER) ----------------
#
# As of 2026-06-07, fine-tuned GLiREL Ghost B v1 ships at
# models/glirel_ghost_b_v1/best/ and is the production default.
#
# DEFAULT_CLASSIFIER: "glirel" (sentence-windowed) | "existing" (BERT cascade)
#
# THRESHOLD IS LOAD-BEARING: fine-tuned GLiREL's best-F1 threshold on the
# held-out test (EVAL_REPORT.txt) is ~0.4, NOT the 0.5 we used at base-model
# validation. Always pass --threshold 0.4 or set the env var.
#
# Rollback to cascade: LOCAL_GHOST_B_CLASSIFIER=existing
DEFAULT_CLASSIFIER = "glirel"
DEFAULT_GLIREL_THRESHOLD = 0.40

# ---------- GLiNER entity tagger -------------------------------------------

GLINER_MODEL = "urchade/gliner_medium-v2.1"
GLINER_THRESHOLD = 0.45

# 11 Ghost B entity types. Derived from polymath_local_extractor.py's
# TYPE_CONSTRAINTS / TYPE_RULES + ghost_b_cascade_infer.py's high-value
# type pairs. DO NOT REORDER — labels are passed to GLiNER as a list
# and order can affect zero-shot calibration.
GHOST_B_ENTITY_TYPES: list[str] = [
    "Person",
    "Organization",
    "Software",
    "Product",
    "Method",
    "Artifact",
    "Concept",
    "Location",
    "Document",
    "Standard",
    "Event",
]

# ---------- Chunker --------------------------------------------------------

# Paragraph-merge chunker (see tools/chunk_with_gliner.py). Code blocks,
# URLs, file paths, version strings, and citation patterns ("et al.",
# "&amp;") are stripped before GLiNER tagging — that's the v1.2 fix.
CHUNKER_TARGET_CHARS = 400
CHUNKER_MIN_CHARS = 150

# ---------- relation_exists gate ------------------------------------------

GATE_THRESHOLD = 0.70           # LOCAL_GHOST_B_RELEXIST_THRESHOLD env override
GATE_BUNDLE = "relation_exists_v1"

# ---------- BERT cascade --------------------------------------------------

CASCADE_BACKBONE = "backbone_v1"
CASCADE_EASY = "easy_predicate_v1"
CASCADE_FAMILY = "family_v1"
CASCADE_T_BACKBONE = 0.80
CASCADE_T_EASY = 0.85
CASCADE_T_FAMILY = 0.80
CASCADE_T_RELATED = 0.50

# ---------- GLiREL classifier ---------------------------------------------

GLIREL_BUNDLE = "glirel_ghost_b_v1"
GLIREL_THRESHOLD = DEFAULT_GLIREL_THRESHOLD   # 0.40 — calibrated for fine-tune
GLIREL_ZERO_SHOT_FALLBACK = "jackboyla/glirel-large-v0"
GLIREL_LABELS_FILE = "labels.json"            # shipped flat list, 30 labels (incl. related_to)
# Sentence-windowing: GLiREL was trained on sentence units ≤128 tokens.
# Whole-chunk inference silently truncates AND breaks the train/infer
# distribution; canonical glirel_infer.py splits chunks into sentences first.
GLIREL_MAX_TOKENS_PER_SENTENCE = 160          # leaves headroom above 128 train cap

# ---------- Pair gate (candidate_pairs) -----------------------------------

PAIR_MAX_ENTITIES_PER_CHUNK = 16
PAIR_MAX_PAIRS_PER_CHUNK = 24
PAIR_REQUIRE_CUE = True              # LOCAL_GHOST_B_REQUIRE_CUE env override
PAIR_SAME_SENTENCE = True
MAX_RELATED_TO_PER_CHUNK = 3         # LOCAL_GHOST_B_MAX_RELATED_TO_PER_CHUNK

# ---------- Safety rules --------------------------------------------------

APPLY_TYPE_CONSTRAINTS = True        # LOCAL_GHOST_B_TYPE_CONSTRAINTS
APPLY_DANGER_GUARD = False           # LOCAL_GHOST_B_DANGER_GUARD


def summary() -> dict:
    """Snapshot of every config value — for logging at startup."""
    return {
        "version": PIPELINE_VERSION,
        "gliner": {
            "model": GLINER_MODEL,
            "threshold": GLINER_THRESHOLD,
            "n_entity_types": len(GHOST_B_ENTITY_TYPES),
        },
        "chunker": {
            "target_chars": CHUNKER_TARGET_CHARS,
            "min_chars": CHUNKER_MIN_CHARS,
        },
        "gate": {"threshold": GATE_THRESHOLD, "bundle": GATE_BUNDLE},
        "cascade": {
            "t_backbone": CASCADE_T_BACKBONE,
            "t_easy": CASCADE_T_EASY,
            "t_family": CASCADE_T_FAMILY,
            "t_related": CASCADE_T_RELATED,
        },
        "glirel": {
            "bundle": GLIREL_BUNDLE,
            "threshold": GLIREL_THRESHOLD,
            "zero_shot_fallback": GLIREL_ZERO_SHOT_FALLBACK,
        },
        "pairs": {
            "max_entities_per_chunk": PAIR_MAX_ENTITIES_PER_CHUNK,
            "max_pairs_per_chunk": PAIR_MAX_PAIRS_PER_CHUNK,
            "require_cue": PAIR_REQUIRE_CUE,
            "same_sentence": PAIR_SAME_SENTENCE,
            "max_related_to_per_chunk": MAX_RELATED_TO_PER_CHUNK,
        },
        "safety": {
            "type_constraints": APPLY_TYPE_CONSTRAINTS,
            "danger_guard": APPLY_DANGER_GUARD,
        },
    }


if __name__ == "__main__":
    import json
    print(json.dumps(summary(), indent=2))
