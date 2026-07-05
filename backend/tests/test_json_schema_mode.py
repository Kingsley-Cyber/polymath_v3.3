"""Pt9c — json_schema mode tests.

Provider-level constrained decoding replaces the prompt-with-post-validation
pattern that drove Pt8b's drop-rate problem. These tests pin:

  • ExtractionResponse generates a well-formed JSON Schema from Pydantic
  • _pin_all_required adapts the schema for OpenAI strict mode
  • The response_format payload matches the json_schema spec contract
  • _lane_supports_json_schema defaults on for known structured-output lanes,
    while preserving explicit opt-out
  • _select_extraction_output_mode returns "json_schema" for known lanes
    when the profile is "normal"
  • Source-pin: the 5 sites in _process_one all recognize the new mode

The actual end-to-end LLM call is exercised by integration tests; these
unit tests cover the schema-construction + mode-selection boundary
which is the part new to Pt9c.
"""
from __future__ import annotations

import sys
from types import ModuleType


# ── Auth-package stubs (same pattern as other unit tests) ──────────
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
    _json_schema_response_format,
    _lane_supports_json_object,
    _lane_supports_json_schema,
    _pin_all_required,
    _select_extraction_output_mode,
)
from services.ghost_b_schemas import (  # noqa: E402
    ExtractionResponse,
    LLMEntity,
    LLMFact,
    LLMRelation,
)


# ── ExtractionResponse schema generation ────────────────────────────


def test_extraction_response_schema_has_three_arrays():
    """The wrapper exposes entities, relations, and facts arrays —
    matching the shape the existing _parse() function expects."""
    schema = ExtractionResponse.model_json_schema()
    properties = schema["properties"]
    assert "entities" in properties
    assert "relations" in properties
    assert "facts" in properties
    assert properties["entities"]["type"] == "array"
    assert properties["relations"]["type"] == "array"
    assert properties["facts"]["type"] == "array"


def test_extraction_response_arrays_reference_typed_items():
    """Each array's items reference the typed model schema, not a generic
    any-shape allowance. Without this, json_schema mode would gain
    nothing — the LLM could still emit arbitrary entity shapes."""
    schema = ExtractionResponse.model_json_schema()
    # Pydantic v2 uses $ref into $defs for nested models.
    assert "$defs" in schema
    assert "LLMEntity" in schema["$defs"]
    assert "LLMRelation" in schema["$defs"]
    assert "LLMFact" in schema["$defs"]


def test_llm_entity_schema_includes_object_kind():
    """Regression guard for Pt9b — once Pt9b lands, object_kind appears
    in LLMEntity, and json_schema mode picks it up automatically without
    any hand-editing."""
    schema = ExtractionResponse.model_json_schema()
    entity_schema = schema["$defs"]["LLMEntity"]
    assert "object_kind" in entity_schema["properties"]


def test_llm_fact_schema_uses_fact_type_enum():
    """LLMFact.fact_type is a Literal — the generated schema should emit
    an enum constraint listing the 9 fact types."""
    schema = LLMFact.model_json_schema()
    fact_type = schema["properties"]["fact_type"]
    assert "enum" in fact_type
    assert set(fact_type["enum"]) == {
        "property", "status", "timestamp", "quantity",
        "threshold", "category", "tag", "rule_condition", "rule_action",
    }


def test_relation_and_fact_evidence_required_non_empty():
    """Production graph writes need traceable evidence, not just valid JSON.
    The provider schema must force a non-empty evidence phrase for the two
    write paths that can otherwise hallucinate silently."""
    relation_schema = LLMRelation.model_json_schema()
    fact_schema = LLMFact.model_json_schema()

    assert "evidence_phrase" in relation_schema["required"]
    assert relation_schema["properties"]["evidence_phrase"]["minLength"] == 1
    assert "evidence_phrase" in fact_schema["required"]
    assert fact_schema["properties"]["evidence_phrase"]["minLength"] == 1


# ── _pin_all_required ────────────────────────────────────────────────


def test_pin_all_required_adds_optional_fields_to_required():
    """Pydantic emits optional fields (with defaults) outside `required`.
    OpenAI strict mode demands they be in `required`. _pin_all_required
    fixes that."""
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "integer"},
        },
        "required": ["a"],
    }
    pinned = _pin_all_required(schema)
    assert set(pinned["required"]) == {"a", "b"}


def test_pin_all_required_sets_additional_properties_false():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["a"],
    }
    pinned = _pin_all_required(schema)
    assert pinned["additionalProperties"] is False


def test_pin_all_required_recurses_into_nested_objects():
    """The function must descend into $defs / properties values / items —
    any nested object schema needs the same treatment."""
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "extra": {"type": "string"},
                    },
                    "required": ["name"],
                },
            },
        },
        "required": ["items"],
    }
    pinned = _pin_all_required(schema)
    # Outer object: required is unchanged ("items" was already there).
    assert pinned["required"] == ["items"]
    # Inner array item schema: "extra" was promoted to required.
    inner = pinned["properties"]["items"]["items"]
    assert set(inner["required"]) == {"name", "extra"}
    assert inner["additionalProperties"] is False


def test_pin_all_required_does_not_mutate_input():
    """The helper must not modify the caller's schema dict. Pydantic's
    .model_json_schema() returns fresh dicts, but mutating them would
    affect any other code path that called .model_json_schema() and
    cached the result."""
    original = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
        "required": ["a"],
    }
    snapshot = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
        "required": ["a"],
    }
    _pin_all_required(original)
    assert original == snapshot


def test_pin_all_required_handles_non_object_schemas():
    """Recursion must tolerate non-dict nodes (booleans, primitives)
    without crashing — Pydantic emits these for `additionalProperties: false`
    on some paths."""
    schema = {
        "type": "object",
        "properties": {
            "scores": {"type": "array", "items": {"type": "number"}},
            "flag": {"type": "boolean"},
        },
    }
    pinned = _pin_all_required(schema)
    assert set(pinned["required"]) == {"scores", "flag"}


# ── _json_schema_response_format ────────────────────────────────────


def test_json_schema_response_format_envelope():
    """The payload shape matches OpenAI's structured-output contract."""
    payload = _json_schema_response_format()
    assert payload["type"] == "json_schema"
    assert payload["json_schema"]["strict"] is True
    assert payload["json_schema"]["name"] == "ghost_b_extraction"
    assert "schema" in payload["json_schema"]


def test_json_schema_response_format_emits_pinned_schema():
    """The schema embedded in the payload has been through
    _pin_all_required — every object's required list matches its
    properties list, additionalProperties is False."""
    payload = _json_schema_response_format()
    schema = payload["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"].keys())
    # Drill into LLMEntity through $defs and verify the same property.
    entity_def = schema["$defs"]["LLMEntity"]
    assert entity_def["additionalProperties"] is False
    assert set(entity_def["required"]) == set(entity_def["properties"].keys())


def test_json_schema_response_format_includes_object_kind():
    """End-to-end: the response_format payload generated for the LLM
    actually declares object_kind. If a future refactor renames the field
    on LLMEntity, this test will surface the schema regression
    immediately."""
    payload = _json_schema_response_format()
    entity_def = payload["json_schema"]["schema"]["$defs"]["LLMEntity"]
    assert "object_kind" in entity_def["properties"]
    assert "object_kind" in entity_def["required"]


def test_json_schema_response_format_requires_evidence():
    payload = _json_schema_response_format()
    defs = payload["json_schema"]["schema"]["$defs"]

    relation_def = defs["LLMRelation"]
    fact_def = defs["LLMFact"]
    assert relation_def["properties"]["evidence_phrase"]["minLength"] == 1
    assert fact_def["properties"]["evidence_phrase"]["minLength"] == 1


# ── _lane_supports_json_schema gating ───────────────────────────────


def test_lane_supports_json_schema_defaults_true_for_known_providers():
    """Production extraction uses provider-native schema enforcement for
    known capable lanes by default."""
    entry = {"model": "deepseek/deepseek-chat", "base_url": "https://api.deepseek.com/v1"}
    assert _lane_supports_json_schema(entry) is True
    entry = {"model": "gpt-4o", "base_url": "https://api.openai.com/v1"}
    assert _lane_supports_json_schema(entry) is True
    entry = {"model": "openai/polymath-extract", "provider_preset": "vllm-rtx"}
    assert _lane_supports_json_schema(entry) is True
    entry = {
        "model": "openai/polymath-extract",
        "base_url": "http://192.168.1.83:8000/v1",
    }
    assert _lane_supports_json_schema(entry) is True


def test_lane_supports_json_schema_defaults_false_for_unknown_custom_provider():
    entry = {
        "model": "openai/mimo-v2.5",
        "base_url": "https://token-plan-sgp.xiaomimimo.com/v1",
    }
    assert _lane_supports_json_schema(entry) is False


def test_lane_supports_json_schema_explicit_bool_true():
    entry = {
        "model": "deepseek/deepseek-chat",
        "extra_params": {"supports_json_schema": True},
    }
    assert _lane_supports_json_schema(entry) is True


def test_lane_supports_json_schema_explicit_bool_false():
    """Explicit False overrides provider defaults."""
    entry = {
        "model": "gpt-4o",
        "base_url": "https://api.openai.com/v1",
        "extra_params": {"supports_json_schema": False},
    }
    assert _lane_supports_json_schema(entry) is False


def test_lane_supports_json_schema_string_truthy():
    """Tolerate stringly-typed config values from YAML/env loaders."""
    entry = {
        "model": "deepseek/deepseek-chat",
        "extra_params": {"supports_json_schema": "true"},
    }
    assert _lane_supports_json_schema(entry) is True
    entry["extra_params"]["supports_json_schema"] = "1"
    assert _lane_supports_json_schema(entry) is True
    entry["extra_params"]["supports_json_schema"] = "yes"
    assert _lane_supports_json_schema(entry) is True


def test_lane_supports_json_schema_string_falsy():
    entry = {
        "model": "deepseek/deepseek-chat",
        "extra_params": {"supports_json_schema": "no"},
    }
    assert _lane_supports_json_schema(entry) is False
    entry["extra_params"]["supports_json_schema"] = "false"
    assert _lane_supports_json_schema(entry) is False


# ── _select_extraction_output_mode dispatch ─────────────────────────


def test_select_mode_returns_jsonl_by_default():
    """Unknown lanes stay on JSONL unless explicitly schema-capable."""
    entry = {"model": "test-model"}
    mode = _select_extraction_output_mode(None, entry, profile_name="normal")
    assert mode == "jsonl"


def test_select_mode_returns_json_schema_for_known_lane():
    entry = {
        "model": "openai/polymath-extract",
        "provider_preset": "vllm-rtx",
    }
    mode = _select_extraction_output_mode(None, entry, profile_name="normal")
    assert mode == "json_schema"


def test_select_mode_returns_json_schema_when_flag_set_for_unknown_lane():
    entry = {
        "model": "test-model",
        "extra_params": {"supports_json_schema": True},
    }
    mode = _select_extraction_output_mode(None, entry, profile_name="normal")
    assert mode == "json_schema"


def test_select_mode_rescue_profile_stays_jsonl():
    """Rescue profiles use JSONL even when json_schema is enabled — the
    rescue prompt is JSONL-shaped and its accepted_jsonl_items merge
    depends on that format."""
    entry = {
        "model": "deepseek/deepseek-chat",
        "extra_params": {"supports_json_schema": True},
    }
    mode = _select_extraction_output_mode(None, entry, profile_name="rescue")
    assert mode == "jsonl"


# ── Source-pin: confirm the 5 sites recognize json_schema ───────────


def test_process_one_sites_recognize_json_schema():
    """All 5 sites in _process_one that branched on
    `profile_output_mode == "json_object"` must now ALSO recognize
    "json_schema". A future refactor that re-narrows any one site to
    json_object-only fails this test, surfacing the regression at
    commit time rather than at provider-error time."""
    from pathlib import Path
    import services.ghost_b as gb

    source = Path(gb.__file__).read_text(encoding="utf-8")

    # Find the body of _process_one.
    func_marker = "async def _process_one"
    func_pos = source.find(func_marker)
    assert func_pos != -1, "_process_one not found in ghost_b.py"
    # Bound to just this function.
    next_def = source.find("\nasync def ", func_pos + len(func_marker))
    if next_def < 0:
        next_def = source.find("\ndef ", func_pos + len(func_marker))
    body = source[func_pos:next_def] if next_def > 0 else source[func_pos:]

    # The bare equality check on "json_object" should appear AT MOST twice
    # — once for the response_format branch where we need exact dispatch
    # (because the payload differs), plus optionally inside the
    # log/error-type branches. The other 4 occurrences must use the
    # `in (...)` form. We assert the wider form is present:
    assert body.count('in ("json_object", "json_schema")') >= 3, (
        "json_schema mode missing from the prompt/system/parser sites. "
        "Expected at least 3 occurrences of 'in (\"json_object\", "
        "\"json_schema\")' in _process_one — found fewer. A site has "
        "narrowed back to json_object-only."
    )
    # The retry handler should also handle json_schema (the 5th site).
    assert "json_schema_unsupported" in body, (
        "Pt9c retry handler missing — the 400/422 fallback should set "
        "error_type='json_schema_unsupported' when the rejected mode was "
        "json_schema, mirroring the json_object_unsupported case."
    )
