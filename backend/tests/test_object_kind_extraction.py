"""Pt9b — object_kind second-axis facet extraction tests.

The 12-type entity_type vocab is the canonical bucket. object_kind is the
LLM-emitted free-form refinement inside that bucket (library/framework/
disorder/therapy/protocol/...). These tests pin:

  • EntityItem accepts object_kind with sensible default
  • _normalize_object_kind canonicalizes variants against the schema_lens
    object_kinds list when present, and passes through cleaned strings
    when not
  • _parse() extracts object_kind from both JSON_OBJECT and JSONL paths
  • _apply_schema soft-remap preserves object_kind even when entity_type
    is rewritten to the sentinel
  • LLMEntity Pydantic validation accepts the new field

The graph-layer plumbing (EntityItem.object_kind → entity_identity ->
Neo4j SET) is exercised end-to-end by integration tests; these unit
tests cover the LLM → EntityItem boundary which is the part new to Pt9b.
"""
from __future__ import annotations

import json
import sys
from types import ModuleType


# ── Auth-package stubs (same pattern as test_ingest_slot_ordering.py) ──
def _install_stubs_if_missing() -> None:
    if "jose" not in sys.modules:
        try:
            import jose  # noqa: F401
        except ImportError:
            jose_mod = ModuleType("jose")

            class JWTError(Exception):
                pass

            class _Jwt:
                @staticmethod
                def encode(*_a, **_kw):  # pragma: no cover
                    raise RuntimeError("stub")

                @staticmethod
                def decode(*_a, **_kw):  # pragma: no cover
                    raise RuntimeError("stub")

            jose_mod.JWTError = JWTError
            jose_mod.jwt = _Jwt()
            sys.modules["jose"] = jose_mod

    if "passlib.context" not in sys.modules:
        try:
            import passlib.context  # noqa: F401
        except ImportError:
            passlib_mod = ModuleType("passlib")
            ctx_mod = ModuleType("passlib.context")

            class _CryptContext:
                def __init__(self, *a, **kw):
                    pass

                def hash(self, *_a, **_kw):  # pragma: no cover
                    raise RuntimeError("stub")

                def verify(self, *_a, **_kw):  # pragma: no cover
                    raise RuntimeError("stub")

            ctx_mod.CryptContext = _CryptContext
            passlib_mod.context = ctx_mod
            sys.modules["passlib"] = passlib_mod
            sys.modules["passlib.context"] = ctx_mod

    if "slowapi" not in sys.modules:
        try:
            import slowapi  # noqa: F401
        except ImportError:
            slowapi_mod = ModuleType("slowapi")
            util_mod = ModuleType("slowapi.util")

            class _Limiter:
                def __init__(self, *a, **kw):
                    pass

                def limit(self, *_a, **_kw):
                    def _decorator(fn):
                        return fn
                    return _decorator

            def _get_remote_address(_request):  # pragma: no cover
                return "0.0.0.0"

            slowapi_mod.Limiter = _Limiter
            util_mod.get_remote_address = _get_remote_address
            sys.modules["slowapi"] = slowapi_mod
            sys.modules["slowapi.util"] = util_mod


_install_stubs_if_missing()


from services.ghost_b import (  # noqa: E402
    EntityItem,
    ExtractionTask,
    SchemaContext,
    _apply_schema,
    _jsonl_items_to_object,
    _normalize_object_kind,
    _normalize_relation_object_kind,
    _parse,
)
from services.ghost_b_schemas import LLMEntity  # noqa: E402


# ── EntityItem field default ────────────────────────────────────────


def test_entity_item_object_kind_defaults_to_empty():
    """Backwards-compat: existing EntityItem(**dict) call sites that don't
    pass object_kind still work."""
    e = EntityItem(
        canonical_name="react",
        surface_form="React",
        entity_type="Software",
        confidence=0.9,
    )
    assert e.object_kind == ""


def test_entity_item_accepts_object_kind():
    e = EntityItem(
        canonical_name="react",
        surface_form="React",
        entity_type="Software",
        confidence=0.9,
        object_kind="library",
    )
    assert e.object_kind == "library"


# ── _normalize_object_kind ──────────────────────────────────────────


def test_normalize_object_kind_empty_input():
    assert _normalize_object_kind("", ["library"]) == ""
    assert _normalize_object_kind(None, ["library"]) == ""  # type: ignore[arg-type]


def test_normalize_object_kind_no_canon_passes_through_cleaned():
    """No canonical list (None or empty) → cleaned pass-through."""
    assert _normalize_object_kind("Library", None) == "library"
    assert _normalize_object_kind("LIBRARY", []) == "library"
    assert _normalize_object_kind("library (python)", None) == "library"


def test_normalize_object_kind_exact_match_returns_canon_spelling():
    """'Library' / 'LIBRARY' / 'library' all converge to the canonical
    spelling stored in the schema_lens object_kinds list."""
    canon = ["Library", "Framework", "Application"]
    assert _normalize_object_kind("library", canon) == "Library"
    assert _normalize_object_kind("LIBRARY", canon) == "Library"
    assert _normalize_object_kind("Library", canon) == "Library"


def test_normalize_object_kind_strips_parens():
    canon = ["Library", "Framework"]
    assert _normalize_object_kind("Library (Python)", canon) == "Library"
    assert _normalize_object_kind("framework (cross-platform)", canon) == "Framework"


def test_normalize_object_kind_prefix_match():
    """'library (python)' should match canon 'library' via parens strip;
    'library-python' should match via prefix-with-hyphen."""
    canon = ["library"]
    assert _normalize_object_kind("library-python", canon) == "library"


def test_normalize_object_kind_substring_match():
    """'open-source library' contains 'library' — match to canonical."""
    canon = ["library"]
    assert _normalize_object_kind("open-source library tool", canon) == "library"


def test_normalize_object_kind_unknown_pass_through_bounded():
    """Term not in canon → cleaned string up to 100 chars, no error."""
    canon = ["library", "framework"]
    result = _normalize_object_kind("microservice", canon)
    assert result == "microservice"
    long_input = "a" * 200
    result = _normalize_object_kind(long_input, canon)
    assert len(result) == 100


# ── _parse() extracts object_kind from JSON_OBJECT path ─────────────


def test_parse_extracts_object_kind_from_json_object():
    """The JSON_OBJECT prompt path: data['entities'][i]['object_kind']."""
    import json
    task = ExtractionTask(
        chunk_id="c1", doc_id="d1", corpus_id="cor1", text="React is great",
    )
    raw = json.dumps({
        "entities": [
            {
                "canonical_name": "react",
                "surface_form": "React",
                "entity_type": "Software",
                "confidence": 0.95,
                "object_kind": "library",
            }
        ],
        "relations": [],
        "facts": [],
    })
    result = _parse(raw, task, threshold=0.1, schema=None, schema_lens=None)
    assert result is not None
    assert len(result.entities) == 1
    assert result.entities[0].object_kind == "library"


def test_parse_extracts_object_kind_from_e_kind_alias():
    """When the LLM emits 'e_kind' (the alias for json_schema mode)
    instead of 'object_kind', the parser still picks it up."""
    import json
    task = ExtractionTask(
        chunk_id="c1", doc_id="d1", corpus_id="cor1", text="React",
    )
    raw = json.dumps({
        "entities": [
            {
                "canonical_name": "react",
                "surface_form": "React",
                "entity_type": "Software",
                "confidence": 0.95,
                "e_kind": "framework",
            }
        ],
        "relations": [],
        "facts": [],
    })
    result = _parse(raw, task, threshold=0.1, schema=None, schema_lens=None)
    assert result is not None
    assert result.entities[0].object_kind == "framework"


def test_parse_object_kind_normalized_via_schema_lens_canon():
    """When schema_lens.object_kinds is provided, raw values get
    canonicalized to the matching list entry."""
    import json
    task = ExtractionTask(
        chunk_id="c1", doc_id="d1", corpus_id="cor1", text="React",
    )
    raw = json.dumps({
        "entities": [
            {
                "canonical_name": "react",
                "surface_form": "React",
                "entity_type": "Software",
                "confidence": 0.95,
                "object_kind": "LIBRARY (Open Source)",
            }
        ],
        "relations": [],
        "facts": [],
    })
    # schema_lens is provided as a dict (the to_dict() flavor) with
    # object_kinds list — same shape worker.py passes.
    lens = {"object_kinds": ["Library", "Framework"]}
    result = _parse(raw, task, threshold=0.1, schema=None, schema_lens=lens)
    assert result is not None
    assert result.entities[0].object_kind == "Library"


def test_parse_object_kind_missing_defaults_to_empty():
    """LLM omits object_kind entirely → EntityItem.object_kind is ''."""
    import json
    task = ExtractionTask(
        chunk_id="c1", doc_id="d1", corpus_id="cor1", text="React",
    )
    raw = json.dumps({
        "entities": [
            {
                "canonical_name": "react",
                "surface_form": "React",
                "entity_type": "Software",
                "confidence": 0.95,
            }
        ],
        "relations": [],
        "facts": [],
    })
    result = _parse(raw, task, threshold=0.1, schema=None, schema_lens=None)
    assert result is not None
    assert result.entities[0].object_kind == ""


# ── _jsonl_items_to_object: ek abbreviation ─────────────────────────


def test_jsonl_ek_abbreviation_is_translated():
    """The JSONL wire format uses 'ek' for entity object_kind to avoid
    collision with 'ok' (relation object_kind: entity|literal)."""
    task = ExtractionTask(
        chunk_id="c1", doc_id="d1", corpus_id="cor1", text="",
    )
    items = [
        {
            "t": "e",
            "cn": "react",
            "et": "Software",
            "cf": 0.9,
            "ek": "library",
        }
    ]
    out = _jsonl_items_to_object(items, task)
    assert len(out["entities"]) == 1
    assert out["entities"][0]["object_kind"] == "library"


def test_jsonl_relation_ok_is_entity_or_literal_unchanged():
    """RelationItem.object_kind keeps its 'entity'|'literal' semantics —
    Pt9b doesn't touch relation parsing."""
    task = ExtractionTask(
        chunk_id="c1", doc_id="d1", corpus_id="cor1", text="",
    )
    items = [
        {
            "t": "r",
            "sub": "react",
            "pred": "implements",
            "obj": "virtual_dom",
            "cf": 0.9,
            "ok": "entity",
        }
    ]
    out = _jsonl_items_to_object(items, task)
    assert len(out["relations"]) == 1
    assert out["relations"][0]["object_kind"] == "entity"


def test_jsonl_relation_entity_type_object_kind_is_normalized_to_entity():
    """Hy3/LongCat-style providers sometimes put an entity type in relation
    object_kind. The relation endpoint contract must still be entity|literal.
    """
    task = ExtractionTask(
        chunk_id="c1", doc_id="d1", corpus_id="cor1", text="",
    )
    items = [
        {
            "t": "r",
            "sub": "react",
            "pred": "implements",
            "obj": "virtual_dom",
            "cf": 0.9,
            "ok": "Method",
        }
    ]
    out = _jsonl_items_to_object(items, task)
    assert len(out["relations"]) == 1
    assert out["relations"][0]["object_kind"] == "entity"


def test_normalize_relation_object_kind_uses_endpoint_entity_names():
    assert (
        _normalize_relation_object_kind(
            "",
            object_name="virtual dom",
            entity_names={"virtual dom"},
        )
        == "entity"
    )
    assert _normalize_relation_object_kind("method") == "entity"
    assert _normalize_relation_object_kind("numeric value") == "literal"


def test_parse_normalizes_provider_relation_object_kind_before_strict_gate():
    task = ExtractionTask(
        chunk_id="c1",
        doc_id="d1",
        corpus_id="cor1",
        text="React implements the Virtual DOM.",
    )
    raw = json.dumps({
        "schema_version": "polymath.extract.v1",
        "entities": [
            {
                "canonical_name": "react",
                "surface_form": "React",
                "entity_type": "Software",
                "confidence": 0.95,
            },
            {
                "canonical_name": "virtual dom",
                "surface_form": "Virtual DOM",
                "entity_type": "Concept",
                "confidence": 0.92,
            },
        ],
        "relations": [
            {
                "subject": "react",
                "predicate": "implements",
                "object": "virtual dom",
                "object_kind": "Method",
                "confidence": 0.91,
                "evidence_phrase": "React implements the Virtual DOM",
            }
        ],
        "facts": [],
    })

    result = _parse(raw, task, threshold=0.1, schema=None, schema_lens=None)

    assert result is not None
    assert len(result.relations) == 1
    assert result.relations[0].object_kind == "entity"


# ── soft-remap preserves object_kind ────────────────────────────────


def test_soft_remap_preserves_object_kind():
    """If LLM emits an off-vocab entity_type but a valid object_kind, the
    soft-remap drops entity_type to 'other' but keeps object_kind intact.
    That way the graph still gets the usable refinement even when the
    bucket is the catch-all."""
    schema = SchemaContext(
        entity_schema=["Software", "Concept"],
        relation_schema=["uses"],
        strict="soft",
    )
    in_entity = EntityItem(
        canonical_name="react",
        surface_form="React",
        entity_type="Framework",  # off-vocab — will be remapped
        confidence=0.9,
        object_kind="library",
    )
    out_entities, _out_relations, counters = _apply_schema(
        [in_entity], [], schema,
    )
    assert len(out_entities) == 1
    assert out_entities[0].entity_type == SchemaContext.ENTITY_SENTINEL
    # The whole point: object_kind survives even though entity_type was
    # demoted. Downstream queries can still filter by object_kind="library".
    assert out_entities[0].object_kind == "library"
    assert counters["entity_remap_count"] == 1


# ── LLMEntity Pydantic validation accepts object_kind ───────────────


def test_llm_entity_pydantic_accepts_object_kind():
    e = LLMEntity(
        canonical_name="react",
        surface_form="React",
        entity_type="Software",
        confidence=0.9,
        object_kind="library",
    )
    assert e.object_kind == "library"


def test_llm_entity_pydantic_object_kind_defaults_to_empty():
    """Backwards-compat: pre-Pt9b validation calls that don't pass
    object_kind still validate."""
    e = LLMEntity(
        canonical_name="react",
        surface_form="React",
        entity_type="Software",
        confidence=0.9,
    )
    assert e.object_kind == ""


def test_llm_entity_pydantic_rejects_overlong_object_kind():
    """The Field(max_length=100) enforcement still catches the case where
    the LLM produces a paragraph instead of a tag."""
    from pydantic import ValidationError
    import pytest
    overlong = "library" + ("x" * 200)
    with pytest.raises(ValidationError):
        LLMEntity(
            canonical_name="react",
            surface_form="React",
            entity_type="Software",
            confidence=0.9,
            object_kind=overlong,
        )


# ── Source-pin: confirm the wire-format keys are unambiguous ────────


def test_jsonl_prompt_documents_ek_abbreviation():
    """The build_user_prompt JSONL abbreviation list must document `ek=
    object_kind` for entities, distinct from `ok=object_kind` for relations
    (which carries entity|literal semantics). If a future refactor merges
    the abbreviations, this test fails first."""
    from services.ghost_b import build_user_prompt
    task = ExtractionTask(
        chunk_id="c1", doc_id="d1", corpus_id="cor1",
        text="sample chunk for prompt assembly",
    )
    schema = SchemaContext(
        entity_schema=["Software", "Concept"],
        relation_schema=["uses"],
        strict="soft",
    )
    prompt = build_user_prompt(
        chunk_id=task.chunk_id,
        doc_id=task.doc_id,
        corpus_id=task.corpus_id,
        text=task.text,
        schema=schema,
        schema_lens=None,
        enable_facts=False,
    )
    # The abbreviation legend MUST document both keys with distinct meanings.
    assert "ek=object_kind" in prompt
    assert "ok=object_kind" in prompt
    # The 'ek' field appears in the entity line shape with refinement hints.
    assert '"ek"' in prompt
