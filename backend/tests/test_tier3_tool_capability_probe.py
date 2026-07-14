import json

import httpx

from scripts.probe_tier3_tool_capability import (
    TOOL_NAME,
    _validate_tool_success,
)


def _response(arguments, *, name=TOOL_NAME):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": name,
                                    "arguments": arguments,
                                }
                            }
                        ]
                    }
                }
            ]
        },
    )


def test_tier3_probe_accepts_one_exact_forced_tool_argument_object():
    assert _validate_tool_success(_response(json.dumps({"ok": True}))) == (
        True,
        "",
        "forced tool accepted and tiny arguments enforced",
    )


def test_tier3_probe_rejects_parameters_wrapper():
    assert _validate_tool_success(
        _response(json.dumps({"parameters": {"ok": True}}))
    ) == (
        False,
        "tool_arguments_not_strict",
        "2xx forced-tool arguments did not satisfy the tiny closed schema",
    )


def test_tier3_probe_rejects_wrong_or_missing_tool_call():
    assert _validate_tool_success(_response("{}", name="wrong")) == (
        False,
        "invalid_tool_output",
        "2xx response lacked one forced tool call",
    )
