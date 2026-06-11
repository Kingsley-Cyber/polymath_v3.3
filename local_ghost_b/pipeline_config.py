"""Locked pipeline constants — single source of truth.

Both training-data prep (RTX) and inference (Mac) MUST import from here.
If a value drifts between the two paths, GLiREL's training distribution
won't match inference and quality silently degrades.

Bump PIPELINE_VERSION when changing any of these. Don't change values
in place without a version bump.
"""

from __future__ import annotations

PIPELINE_VERSION = "v1.2026.06e"  # batched inference (GLiNER/GLiREL/facets)

# ---------- Batched inference (throughput) ----------------------------------
#
# Per-chunk model calls leave the GPU idle between tiny forward passes; the
# Mac Studio's 32 GB unified memory comfortably fits batched activations.
# Defaults are conservative for DeBERTa-large (GLiREL) + medium GLiNER at
# 128-token chunks. Env overrides exist for backfill-time tuning under the
# reclaimed-memory envelope (scripts/ingest_reclaim_memory.sh guarantees
# ≥~25 GB; measured stack peak ≈ 20-22 GB at DOUBLED batches, so 2x these
# defaults is the validated raise). Slicing follows chunk order, so batch
# grouping is deterministic per document. NOTE: batched softmax scores can
# differ from per-chunk mode in the last float digits (padding / reduction
# order) — each mode is self-deterministic, but don't expect bit-identical
# scores across modes.
import os as _os


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(_os.environ.get(name, "") or default))
    except ValueError:
        return default


GLINER_BATCH_SIZE = _env_int("GHOST_B_GLINER_BATCH", 32)   # chunks / OUTER slice (Python amortization)
GLIREL_UNIT_BATCH = _env_int("GHOST_B_GLIREL_BATCH", 64)   # sentence units / forward
FACET_BATCH = _env_int("GHOST_B_FACET_BATCH", 32)          # contexts / OUTER slice
# GPU forward size for GLiNER pass-1 AND the facet pass. Measured on CUDA
# (256 real book chunks): 8 -> 328 ms/chunk total, 32 -> 654, 256 -> 842 —
# big forwards pad length-varied texts to the batch max and lose more than
# they save in launch overhead. Keep small; tune per machine via env.
GLINER_FORWARD = _env_int("GHOST_B_GLINER_FORWARD", 8)

# The facet vocabulary (vector_database/game_engine/library/...) only ever
# applies to artifact-like types — running pass-2 for Person/Location/Event/
# TimeReference entities is pure waste (profiled at ~50% of extraction wall on
# entity-dense books: hundreds of people/places, none facetable). Eligible
# types keep facets; the rest skip straight to the downstream taxonomy.
FACET_ELIGIBLE_TYPES: frozenset[str] = frozenset({
    "Software", "Product", "Method", "Standard", "Artifact", "Concept",
    "Document", "Organization",
})

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

# ---------- GLiNER ONNX Runtime lane (GHOST_B_GLINER_ONNX=1) ----------------
#
# Swaps the GLiNER forward (entity pass-1 AND facet pass-2 — same shared
# instance) from torch onto ONNX Runtime. Target: CUDA kernel-launch overhead
# on the RTX box, where torch runs ~280-330 ms/chunk with the GPU mostly idle
# and gliner+facets = 89% of the wall. GLiREL stays torch either way.
#
# Provider selection is gliner-internal: map_location "cuda" requests
# CUDAExecutionProvider, anything else CPUExecutionProvider. ORT may still
# place unsupported nodes on CPU silently — verify via sidecar /health
# (gliner.providers) AND nvidia-smi utilization during a bench; never trust
# logs alone (documented Blackwell sm_120 silent-fallback when ORT's CUDA
# libs mismatch torch's — ragflow#14565, onnxruntime#26177).
#
# GHOST_B_GLINER_ONNX_FILE: onnx/model.onnx (fp32) | onnx/model_fp16.onnx.
# Any non-fp32 file must pass local_ghost_b/onnx_equivalence_check.py before
# production use.
GLINER_ONNX = (_os.environ.get("GHOST_B_GLINER_ONNX", "").strip().lower()
               in ("1", "true", "yes", "on"))
GLINER_ONNX_REPO = (_os.environ.get("GHOST_B_GLINER_ONNX_REPO", "").strip()
                    or "onnx-community/gliner_medium-v2.1")
GLINER_ONNX_FILE = (_os.environ.get("GHOST_B_GLINER_ONNX_FILE", "").strip()
                    or "onnx/model.onnx")
GLINER_ONNX_DEVICE = (_os.environ.get("GHOST_B_GLINER_ONNX_DEVICE", "").strip().lower()
                      or "auto")  # auto -> cuda if available else cpu

# Ghost B entity types passed to GLiNER as zero-shot labels. The first 11 are
# the original locked set (derived from polymath_local_extractor.py's
# TYPE_CONSTRAINTS / TYPE_RULES + ghost_b_cascade_infer.py's high-value type
# pairs). Phase A appends Rule, Law, TimeReference to cover the rest of the
# cloud EntityType schema that GLiREL was trained against.
#
# DO NOT REORDER the existing entries — labels are passed to GLiNER as a list
# and order can affect zero-shot calibration. New types are APPENDED only.
#
# NOTE: the cloud EntityType Literal also has "other", but that is the
# code-level SENTINEL applied when a type can't be pinned down — it is NOT a
# GLiNER predict label (giving GLiNER "other" makes it tag ambiguous spans as
# "other", which is noise). So this list has 14 real types; "other" lives only
# in ghost_b_schemas.EntityType for downstream validation.
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
    "Rule",          # Phase A — appended (do not reorder above)
    "Law",           # Phase A
    "TimeReference",  # Phase A
]

# ---------- GLiNER pass-2 facet vocabulary (object_kind) -------------------
#
# Second GLiNER pass over the SAME model: re-tag each unique entity with the
# open facet vocabulary below to refine the coarse entity_type ("Software")
# into a fine object_kind ("vector_database"). Drives EntityItem.object_kind.
# Deduped — runs once per unique canonical_name across the doc.
#
# Starting set (refine against the smoke test in A.6). DO NOT REORDER — same
# zero-shot calibration concern as the entity types above.
GHOST_B_FACET_VOCAB: list[str] = [
    "vector_database",
    "web_framework",
    "embedding_model",
    "dataset",
    "algorithm",
    "protocol",
    "language",
    "game_engine",
    "library",
    "framework",
    "platform",
    "model",
    "api",
    "schema",
    "format",
    "plugin",
    "extension",
    "package",
    "runtime",
    "server",
    "ide",
    "compiler",
    "tool",
    "service",
    "database",
    "ontology",
    "methodology",
    "paradigm",
]

# Facet pass runs a touch looser than entity tagging — the vocab is finer and
# zero-shot scores run lower. Only assign object_kind when a returned span
# matches the entity's own surface, so precision stays high regardless.
GLINER_FACET_THRESHOLD = 0.45

# ---------- Table chunks (kind=="table") -----------------------------------
#
# Table chunks route to the deterministic table-fact extractor
# (enrich.extract_table_facts) instead of the prose path: row label ->
# subject, column header -> property_name, cell -> verbatim value. GLiREL is
# skipped (rows aren't relational prose). The cap keeps a wide x tall chunk
# from emitting hundreds of facts; rows are processed in order so the cap is
# deterministic.
TABLE_MAX_FACTS_PER_CHUNK = 24

# ---------- Entity noise gates (post-GLiNER, pre-validation) ----------------
#
# Two deterministic precision gates applied after dedup in ghost_b_local:
#
# 1. FLOOR: drop an entity when its best GLiNER score is below the floor AND
#    the surface is a single all-lowercase word — low-confidence lowercase
#    singles are where GLiNER's generic-noun mistags live ("engine" @0.49,
#    "game" @0.64). Proper nouns ("World" — a real Flame class), acronyms,
#    and multi-word names are exempt regardless of score.
GLINER_ENTITY_CONF_FLOOR = 0.55

# 2. BLOCKLIST: single-word canonicals that are never a queryable entity on
#    their own, at any confidence. Multi-word names containing them survive
#    ("flame component system" stays; bare "components" dies). Generous on
#    purpose — graph cleanliness beats marginal recall, and the taxonomy /
#    facet layers still capture the concept via the multi-word forms.
GENERIC_ENTITY_BLOCKLIST: frozenset[str] = frozenset({
    # pronouns — GLiNER tags them as Person in book prose ("you" was the
    # TOP-mentioned pilot entity at 715 mentions) and they then anchor facts
    # and relations. A pronoun is never a queryable graph entity.
    "i", "you", "we", "they", "he", "she", "it", "me", "us", "them", "him",
    "her", "one", "someone", "anyone", "everyone", "everybody", "somebody",
    "anybody", "nobody", "yourself", "yourselves", "ourselves", "themselves",
    "himself", "herself", "itself", "myself", "others", "another",
    # discourse / document furniture
    "way", "kind", "type", "sort", "thing", "things", "stuff", "lot", "bit",
    "piece", "pieces", "part", "parts", "set", "sets", "group", "groups",
    "number", "numbers", "example", "examples", "sample", "samples",
    "overview", "introduction", "summary", "section", "sections", "page",
    "pages", "content", "contents", "item", "items", "list", "lists",
    "table", "tables", "figure", "figures", "image", "images", "note",
    "notes", "tip", "tips", "step", "steps", "guide", "guides", "tutorial",
    "tutorials", "documentation", "docs", "doc", "article", "articles",
    "post", "posts", "blog", "book", "books", "chapter", "chapters",
    "paper", "papers", "report", "reports", "detail", "details",
    "description", "information", "info", "question", "questions",
    "answer", "answers",
    # generic tech nouns (the multi-word forms carry the signal)
    "system", "systems", "engine", "engines", "game", "games", "world",
    "application", "applications", "app", "apps", "project", "projects",
    "tool", "tools", "library", "libraries", "framework", "frameworks",
    "platform", "platforms", "service", "services", "component",
    "components", "module", "modules", "function", "functions", "method",
    "methods", "class", "classes", "object", "objects", "feature",
    "features", "version", "versions", "release", "releases", "update",
    "updates", "model", "models", "software", "hardware", "product",
    "products", "device", "devices", "machine", "machines", "computer",
    "computers", "server", "servers", "database", "databases", "network",
    "networks", "language", "languages", "code", "data", "file", "files",
    "folder", "folders", "directory", "directories", "website", "site",
    "web", "internet", "online", "user", "users", "team", "teams",
    "community", "support", "help", "process", "processes", "task",
    "tasks", "job", "jobs", "work", "action", "actions", "event",
    "events", "result", "results", "output", "outputs", "input",
    "inputs", "value", "values", "name", "names", "text", "word",
    "words", "line", "lines", "field", "fields", "form", "forms",
    "term", "terms", "point", "points", "case", "cases", "issue",
    "issues", "error", "errors", "problem", "problems", "change",
    "changes", "state", "states", "status", "level", "levels", "order",
    "time", "times", "day", "days", "year", "years", "place", "area",
    "areas", "region", "regions", "people", "person", "study", "studies",
})

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
            "model": GLINER_ONNX_REPO if GLINER_ONNX else GLINER_MODEL,
            "backend": "onnx" if GLINER_ONNX else "torch",
            "threshold": GLINER_THRESHOLD,
            "n_entity_types": len(GHOST_B_ENTITY_TYPES),
            "facet_threshold": GLINER_FACET_THRESHOLD,
            "n_facets": len(GHOST_B_FACET_VOCAB),
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
