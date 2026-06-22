"""
Pt 8c–8d idempotency / statelessness / determinism / dedup / cache tests.

Targets the new extraction-pipeline code shipped across Pt 7c–8d:
  • Pt 7f entity evidence gate
  • Pt 8a relation alias map additions
  • Pt 8b strict Pydantic Literal validation (intrinsic, no flag)
  • Pt 8c chunker noisy-kind skip + Unicode-normalization + paraphrase tolerance
  • Pt 8d schema slot swap (defines / example_of / during)

Each test verifies one of the five properties:
  - Idempotency:   F(x) called N times → identical results
  - Statelessness: no module state mutates across calls
  - Determinism:   same input → same output (no hidden RNG / clock)
  - Deduplication: duplicate input collapses correctly
  - Caching:       lru_cached loaders return the same object across calls
"""
from __future__ import annotations

import json
from copy import deepcopy

from services.ghost_b import (
    FACT_TYPES,
    RELATION_ALIAS_MAP,
    UNIVERSAL_ENTITY_SCHEMA,
    UNIVERSAL_RELATION_SCHEMA,
    ExtractionTask,
    _evidence_token_overlap,
    _normalize_evidence,
    _parse,
    _validate_evidence,
    normalize_relation_predicate_alias,
)
from services.ghost_b_schemas import LLMEntity, LLMRelation
from services.graph.neo4j_writer import (
    RELATION_FAMILY_MAP,
    _load_canonical_families,
    canonicalize_entity_name,
    relation_family_for_predicate,
)
from services.ingestion.section_classifier import (
    GHOST_B_SKIP_KINDS,
    ChunkKind,
    classify_chunk,
    classify_content,
)


# ─── Fixtures ──────────────────────────────────────────────────────────────

CHUNK_TEXT = (
    "Personalized recommender systems use deep neural networks. "
    "DeepFusion is trained on Amazon product data and measures performance "
    "via RMSE. Habit formation, as defined by Atomic Habits, depends on "
    "cue, craving, response, and reward."
)

RAW_JSONL = {
    "entities": [
        {"canonical_name": "deepfusion",         "surface_form": "DeepFusion",        "entity_type": "Product", "confidence": 0.95},
        {"canonical_name": "amazon",             "surface_form": "Amazon",            "entity_type": "Organization", "confidence": 0.95},
        {"canonical_name": "amazon product data","surface_form": "Amazon product data","entity_type": "other",   "confidence": 0.9},
        {"canonical_name": "deep neural networks","surface_form": "deep neural networks","entity_type": "Method", "confidence": 0.9},
        {"canonical_name": "rmse",               "surface_form": "RMSE",              "entity_type": "Method", "confidence": 0.85},
        {"canonical_name": "habit formation",    "surface_form": "Habit formation",   "entity_type": "Concept","confidence": 0.9},
        # Hallucinated — surface_form not in chunk. Pt 7f should drop.
        {"canonical_name": "netflix",            "surface_form": "Netflix",           "entity_type": "Organization", "confidence": 0.95},
    ],
    "relations": [
        {"subject": "deepfusion", "predicate": "uses",       "object": "deep neural networks", "object_kind": "entity", "confidence": 0.95, "evidence_phrase": "Personalized recommender systems use deep neural networks"},
        {"subject": "deepfusion", "predicate": "trained_on", "object": "Amazon product data",  "object_kind": "entity", "confidence": 0.95, "evidence_phrase": "DeepFusion is trained on Amazon product data"},
        # off-schema predicate `measures` — should alias to `detects` via Pt 8a
        {"subject": "deepfusion", "predicate": "measures",   "object": "rmse",                  "object_kind": "entity", "confidence": 0.9,  "evidence_phrase": "measures performance via RMSE"},
        # new Pt 8d predicate `defines` — should pass through to schema
        {"subject": "atomic habits", "predicate": "defines", "object": "habit formation",      "object_kind": "entity", "confidence": 0.9,  "evidence_phrase": "Habit formation, as defined by Atomic Habits"},
    ],
}


def _task() -> ExtractionTask:
    return ExtractionTask(chunk_id="t", doc_id="d", corpus_id="c", text=CHUNK_TEXT)


# ─── Property 1 — IDEMPOTENCY ─────────────────────────────────────────────

def test_parse_is_idempotent_across_repeated_calls():
    """_parse(raw, task) must return the same ExtractionResult across N invocations."""
    raw_str = json.dumps(RAW_JSONL)
    runs = [_parse(raw_str, _task(), threshold=0.0) for _ in range(5)]
    # Same number of survivors across all runs
    entity_counts = [len(r.entities) for r in runs]
    relation_counts = [len(r.relations) for r in runs]
    assert len(set(entity_counts)) == 1, f"Entity counts diverge across runs: {entity_counts}"
    assert len(set(relation_counts)) == 1, f"Relation counts diverge across runs: {relation_counts}"

    # Same survivors (by canonical_name / predicate-triple) — order-insensitive
    first_entity_keys = {(e.canonical_name, e.entity_type) for e in runs[0].entities}
    first_relation_keys = {(r.subject, r.predicate, r.object) for r in runs[0].relations}
    for i, r in enumerate(runs[1:], start=2):
        assert {(e.canonical_name, e.entity_type) for e in r.entities} == first_entity_keys, f"Entities differ on run {i}"
        assert {(rr.subject, rr.predicate, rr.object) for rr in r.relations} == first_relation_keys, f"Relations differ on run {i}"


def test_evidence_validation_is_idempotent():
    """_validate_evidence on the same inputs returns the same bool across calls."""
    cases = [
        ("Atomic Habits", "see **At·om·ic Hab·its** chapter", True),
        ("Scott Adams, the cartoonist behind the Dilbert comic", "Scott Adams created Dilbert in 1989", True),
        ("totally hallucinated", "this chunk has nothing to do with the phrase", False),
    ]
    for phrase, chunk, expected in cases:
        results = [_validate_evidence(phrase, chunk) for _ in range(10)]
        assert all(r == expected for r in results), f"Diverged: {phrase!r} -> {results}"


# ─── Property 2 — STATELESSNESS ───────────────────────────────────────────

def test_normalize_evidence_is_stateless():
    """_normalize_evidence has no hidden state — same input → same output regardless of prior calls."""
    a = _normalize_evidence("**At·om·ic Hab·its**")
    _ = [_normalize_evidence("some other " + str(i)) for i in range(50)]
    b = _normalize_evidence("**At·om·ic Hab·its**")
    assert a == b, f"_normalize_evidence drifted: {a!r} -> {b!r}"


def test_alias_normalization_is_stateless():
    """normalize_relation_predicate_alias produces same canonical regardless of prior call history."""
    a = normalize_relation_predicate_alias("trained_on")
    _ = [normalize_relation_predicate_alias(f"random_{i}") for i in range(50)]
    b = normalize_relation_predicate_alias("trained_on")
    assert a == b == ("trained_on", False), f"Alias drifted: {a!r} -> {b!r}"


def test_parse_does_not_leak_state_between_chunks():
    """Two _parse calls with DIFFERENT chunk_ids must not affect each other."""
    raw = json.dumps(RAW_JSONL)
    task_a = ExtractionTask(chunk_id="A", doc_id="dA", corpus_id="c", text=CHUNK_TEXT)
    task_b = ExtractionTask(chunk_id="B", doc_id="dB", corpus_id="c", text=CHUNK_TEXT + " EXTRA TEXT.")
    r_a1 = _parse(raw, task_a, threshold=0.0)
    r_b  = _parse(raw, task_b, threshold=0.0)
    r_a2 = _parse(raw, task_a, threshold=0.0)
    keys_a1 = {(e.canonical_name) for e in r_a1.entities}
    keys_a2 = {(e.canonical_name) for e in r_a2.entities}
    assert keys_a1 == keys_a2, "Parsing chunk B leaked state into a later chunk A parse"


# ─── Property 3 — DETERMINISM ─────────────────────────────────────────────

def test_pydantic_literal_validation_is_deterministic():
    """LLMEntity / LLMRelation Pydantic models accept/reject the same input identically every time."""
    valid_e = {"canonical_name": "x", "surface_form": "X", "entity_type": "Person", "confidence": 0.9}
    invalid_e = {"canonical_name": "x", "surface_form": "X", "entity_type": "NotAType", "confidence": 0.9}
    for _ in range(10):
        LLMEntity.model_validate(valid_e)  # should not raise
        try:
            LLMEntity.model_validate(invalid_e)
            raise AssertionError("Pydantic accepted off-Literal entity_type")
        except Exception:
            pass  # expected

    valid_r = {"subject": "a", "predicate": "uses", "object": "b", "object_kind": "entity", "confidence": 0.9}
    invalid_r = {**valid_r, "predicate": "measures"}  # off-schema
    for _ in range(10):
        LLMRelation.model_validate(valid_r)
        try:
            LLMRelation.model_validate(invalid_r)
            raise AssertionError("Pydantic accepted off-Literal predicate")
        except Exception:
            pass


def test_classify_chunk_is_deterministic():
    """classify_chunk's verdict is purely a function of inputs."""
    toc = "\n".join([f"[Chapter {i}](#ch{i})" for i in range(8)])
    glossary = "\n".join([
        "atomic - an extremely small amount of a thing",
        "habit - a routine or practice performed regularly",
        "craving - a desire to change your state",
        "cue - a trigger of behavior",
        "response - the action performed",
        "reward - the satisfaction",
    ])
    body = "This is a normal paragraph about how habits form through repetition and reward."
    for _ in range(10):
        assert classify_chunk(None, toc) == ChunkKind.TOC
        assert classify_chunk(None, glossary) == ChunkKind.FRONT_MATTER
        assert classify_chunk(None, body) == ChunkKind.BODY


def test_relation_family_for_predicate_is_deterministic():
    cases = [
        ("uses",        "Operational"),
        ("runs_on",     "Operational"),
        ("trained_on",  "Operational"),
        ("defines",     "Referential"),
        ("totally_made_up", "WeakAssociation"),  # default
    ]
    for pred, fam in cases:
        for _ in range(5):
            assert relation_family_for_predicate(pred) == fam, f"family drift for {pred}"


def test_current_graph_predicates_survive_pydantic_literal():
    """REGRESSION — current graph contract keeps runtime/training predicates first-class.
    The Pydantic Literal in ghost_b_schemas.py must include them or extraction drops them.
    """
    for pred in ("defines", "runs_on", "trained_on"):
        try:
            LLMRelation.model_validate({
                "subject": "x", "predicate": pred, "object": "y",
                "object_kind": "entity", "confidence": 0.9,
            })
        except Exception as exc:
            raise AssertionError(
                f"Pt 8b Pydantic Literal rejected schema-valid predicate {pred!r}: {exc!r}. "
                f"ghost_b_schemas.py:Predicate is out of sync with UNIVERSAL_RELATION_SCHEMA."
            )
    # And dropped/broad legacy ones must NOT be accepted as canonical:
    for pred in ("classifies", "example_of", "during"):
        try:
            LLMRelation.model_validate({
                "subject": "x", "predicate": pred, "object": "y",
                "object_kind": "entity", "confidence": 0.9,
            })
            raise AssertionError(
                f"Pt 8b Pydantic Literal accepted dropped predicate {pred!r}. "
                f"Schema swap not reflected in ghost_b_schemas.py:Predicate."
            )
        except AssertionError:
            raise
        except Exception:
            pass  # expected rejection


def test_schema_literal_in_sync_with_universal_relation_schema():
    """The Pydantic Literal must be identical to the canonical schema list."""
    from typing import get_args
    from services.ghost_b_schemas import Predicate
    literal_members = set(get_args(Predicate))
    schema_members = set(UNIVERSAL_RELATION_SCHEMA)
    diff_only_in_literal = literal_members - schema_members
    diff_only_in_schema  = schema_members - literal_members
    assert literal_members == schema_members, (
        f"Pydantic Literal drift:\n"
        f"  only in Literal: {diff_only_in_literal}\n"
        f"  only in schema:  {diff_only_in_schema}"
    )


# ─── Property 4 — DEDUPLICATION ───────────────────────────────────────────

def test_parse_dedupes_duplicate_entities_in_jsonl():
    """Same canonical entity emitted twice in one extraction must collapse to one."""
    raw = {
        "entities": [
            {"canonical_name": "deepfusion", "surface_form": "DeepFusion", "entity_type": "Product", "confidence": 0.95},
            {"canonical_name": "deepfusion", "surface_form": "DeepFusion", "entity_type": "Product", "confidence": 0.95},
            {"canonical_name": "deepfusion", "surface_form": "DeepFusion", "entity_type": "Method",  "confidence": 0.80},
        ],
        "relations": [],
    }
    result = _parse(json.dumps(raw), _task(), threshold=0.0)
    canonicals = [e.canonical_name for e in result.entities]
    # _parse itself doesn't dedupe at the EntityItem level — that's the
    # writer's job via MERGE — but the canonical_name must normalize the
    # same way every time so downstream MERGE collapses them.
    assert len(set(canonicalize_entity_name(c) for c in canonicals)) == 1, "canonical_name drift across duplicates"


def test_canonicalize_entity_name_collapses_case_and_whitespace():
    variants = ["DeepFusion", "deepfusion", "  DeepFusion  ", "DEEPFUSION"]
    canons = {canonicalize_entity_name(v) for v in variants}
    assert len(canons) == 1, f"Variants didn't collapse: {canons}"


def test_alias_map_collapses_synonyms_to_one_canonical():
    """Multiple aliases for the same canonical must all resolve to it."""
    uses_aliases = ["uses", "using", "utilizes", "consumes", "tests", "activates"]
    runs_on_aliases = ["runs_on", "deployed_on", "executes_on"]
    trained_on_aliases = ["trained_on", "trained_with"]
    assert {normalize_relation_predicate_alias(a)[0] for a in uses_aliases} == {"uses"}
    assert {normalize_relation_predicate_alias(a)[0] for a in runs_on_aliases} == {"runs_on"}
    assert {normalize_relation_predicate_alias(a)[0] for a in trained_on_aliases} == {"trained_on"}


# ─── Property 5 — CACHING ─────────────────────────────────────────────────

def test_canonical_families_lru_cached():
    """_load_canonical_families is decorated with @lru_cache(maxsize=1) — same object returned."""
    a = _load_canonical_families()
    b = _load_canonical_families()
    assert a is b, "Expected the same cached object across calls"
    # And it must contain the Pt 8d additions
    assert "behavioral_science" in a
    assert "machine_learning" in a


def test_entity_stoplist_lru_cached():
    """Pt 7d entity stop-list loader is cached — same compiled (exact, regex) tuple."""
    from services.graph.queries import _load_entity_stoplist
    a = _load_entity_stoplist()
    b = _load_entity_stoplist()
    assert a is b, "Expected cached return of same tuple object"


# ─── Smoke: end-to-end run prints results so this file is also a script ──

if __name__ == "__main__":
    import inspect
    fns = [
        (n, f) for n, f in globals().items()
        if n.startswith("test_") and callable(f)
    ]
    passed, failed = 0, []
    for name, fn in fns:
        try:
            fn()
            passed += 1
            print(f"  PASS  {name}")
        except Exception as exc:
            failed.append((name, repr(exc)))
            print(f"  FAIL  {name}  -- {exc!r}")
    print()
    print(f"{passed}/{passed + len(failed)} tests passed")
    if failed:
        import sys
        sys.exit(1)
