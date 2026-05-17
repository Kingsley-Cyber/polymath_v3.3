import os

os.environ.setdefault("LITELLM_MASTER_KEY", "test-litellm-master-key")
os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret-key")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "test-admin-password")

from services.chat_orchestrator import (
    _MAX_WEB_SEARCH_RESULTS_PER_CALL,
    _available_tool_schemas,
    _looks_like_raw_tool_request_content,
    _limit_tool_calls_for_turn,
)


def _tool_call(name: str, call_id: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def _tool_schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": "", "parameters": {}},
    }


def test_limit_tool_calls_allows_only_one_web_search_per_turn():
    calls = [
        _tool_call("web_search", "web_1"),
        _tool_call("web_search", "web_2"),
        _tool_call("calculator", "calc_1"),
    ]

    allowed, web_calls, dropped_for_tool_limit, dropped_for_web_limit = (
        _limit_tool_calls_for_turn(
            calls,
            remaining_tool_calls=3,
            web_search_call_count=0,
        )
    )

    assert [call["function"]["name"] for call in allowed] == [
        "web_search",
        "calculator",
    ]
    assert web_calls == 1
    assert dropped_for_tool_limit is False
    assert dropped_for_web_limit is True


def test_available_tool_schemas_remove_web_after_one_search():
    schemas = [_tool_schema("web_search"), _tool_schema("calculator")]

    available = _available_tool_schemas(schemas, web_search_call_count=1)

    assert [schema["function"]["name"] for schema in available] == ["calculator"]


def test_limit_tool_calls_still_reports_global_tool_limit():
    calls = [
        _tool_call("calculator", "calc_1"),
        _tool_call("weather", "weather_1"),
    ]

    allowed, web_calls, dropped_for_tool_limit, dropped_for_web_limit = (
        _limit_tool_calls_for_turn(
            calls,
            remaining_tool_calls=1,
            web_search_call_count=0,
        )
    )

    assert [call["id"] for call in allowed] == ["calc_1"]
    assert web_calls == 0
    assert dropped_for_tool_limit is True
    assert dropped_for_web_limit is False


def test_raw_dsml_tool_request_text_is_detected():
    content = (
        '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="web_search">'
        '<｜｜DSML｜｜parameter name="query">Roblox RemoteEvent</｜｜DSML｜｜parameter>'
    )

    assert _looks_like_raw_tool_request_content(content) is True


def test_normal_answer_with_web_search_words_is_not_raw_tool_request():
    content = "The web_search result says Roblox RemoteEvents need server validation."

    assert _looks_like_raw_tool_request_content(content) is False


def test_web_search_result_cap_is_seven_sources():
    assert _MAX_WEB_SEARCH_RESULTS_PER_CALL == 7
