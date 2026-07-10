from __future__ import annotations

import httpx

from services.ghost_b import (
    ExtractionTask,
    _context_bounded_completion_tokens,
    _extract_balanced_json_object,
    _native_mode_http_error_type,
    _parse_object_with_repair,
)


def _http_error(message: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://provider.test/v1/chat/completions")
    response = httpx.Response(
        400,
        request=request,
        json={"error": {"message": message}},
    )
    return httpx.HTTPStatusError("bad request", request=request, response=response)


def test_private_vllm_output_budget_stays_inside_context_window():
    effective, meta = _context_bounded_completion_tokens(
        {
            "provider_preset": "rtx",
            "model": "openai/polymath-extract",
            "base_url": "http://192.168.1.83:8000/v1",
            "extra_params": {"managed_vllm": True},
        },
        system_prompt="extract",
        user_prompt="token " * 2050,
        requested_tokens=6144,
    )

    assert meta["context_window_tokens"] == 8192
    assert meta["capped"] is True
    assert effective < 6144
    assert effective + meta["prompt_token_estimate"] + meta["safety_margin_tokens"] <= 8192
    assert meta["prompt_token_estimate"] >= meta["tokenizer_prompt_token_estimate"]


def test_context_overflow_is_not_misclassified_as_schema_unsupported():
    exc = _http_error(
        "This model's maximum context length is 8192 tokens; input_tokens=2049"
    )

    assert _native_mode_http_error_type(exc, output_mode="json_schema") == (
        "context_window_exceeded"
    )


def test_actual_response_format_rejection_is_classified_for_downgrade():
    exc = _http_error("response_format json_schema is not supported")

    assert _native_mode_http_error_type(exc, output_mode="json_schema") == (
        "json_schema_unsupported"
    )


def test_compiler_extracts_fenced_object_and_ignores_trailing_prose():
    raw = """```json
{"entities":[{"canonical_name":"orbitcamera","surface_form":"OrbitCamera","entity_type":"Software","confidence":0.9}],"relations":[],"facts":[]}
```
This is cleaner and more defensible."""
    task = ExtractionTask(
        chunk_id="chunk-1",
        doc_id="doc-1",
        corpus_id="corpus-1",
        text="OrbitCamera controls camera movement.",
    )

    result = _parse_object_with_repair(raw, task, 0.2)

    assert result is not None
    assert [entity.canonical_name for entity in result.entities] == ["orbitcamera"]


def test_compiler_accepts_json_payload_envelope_but_rejects_two_objects():
    wrapped = (
        "<json_payload>{\"entities\":[],\"relations\":[],\"facts\":[]}"
        "</json_payload>"
    )
    ambiguous = (
        '{"entities":[],"relations":[],"facts":[]}'
        '{"entities":[],"relations":[],"facts":[]}'
    )

    assert _extract_balanced_json_object(wrapped) is not None
    assert _extract_balanced_json_object(ambiguous) is None
