import os

os.environ.setdefault("LITELLM_MASTER_KEY", "test-litellm-master-key")
os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret-key")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "test-admin-password")

from services.chat_orchestrator import (
    _MAX_WEB_SEARCH_CALLS_PER_TURN,
    _MAX_WEB_SEARCH_RESULTS_PER_CALL,
    _append_deduped_web_sources,
    _available_tool_schemas,
    _annotate_web_evidence_scores,
    _build_backend_retry_query,
    _build_budgeted_augmented_prompt,
    _cap_web_sources_for_turn,
    _classify_web_evidence_sufficiency,
    _dedupe_sources_for_context,
    _filter_facts_to_selected_corpora,
    _filter_sources_to_selected_corpora,
    _format_evidence_packet_block,
    _looks_like_raw_tool_request_content,
    _limit_tool_calls_for_turn,
    _resolve_web_evidence_options,
)
from models.schemas import ChatRequest, ModelOverrides, SourceChunk, SourceFact


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


def _corpus_chunk(chunk_id: str, corpus_id: str, text: str) -> SourceChunk:
    return SourceChunk(
        chunk_id=chunk_id,
        parent_id=f"parent-{chunk_id}",
        doc_id=f"doc-{chunk_id}",
        corpus_id=corpus_id,
        text=text,
        summary=text,
        score=0.8,
        source_tier="qdrant_mongo_graph",
        doc_name=f"{chunk_id}.md",
    )


def test_final_source_scope_keeps_selected_corpus_and_web_only():
    selected = _corpus_chunk("a", "corpus-a", "Selected corpus evidence about GLiNER.")
    leaked = _corpus_chunk("b", "corpus-b", "Deselected corpus evidence about GLiNER.")
    web = SourceChunk(
        chunk_id="web:https://example.test",
        parent_id="web-parent",
        doc_id="https://example.test",
        corpus_id="live-web",
        text="Web evidence stays available when web/tool mode is active.",
        summary="web",
        score=0.6,
        source_tier="web_search",
        doc_name="example.test",
    )

    filtered = _filter_sources_to_selected_corpora(
        [selected, leaked, web],
        ["corpus-a"],
    )

    assert [source.chunk_id for source in filtered] == [
        "a",
        "web:https://example.test",
    ]


def test_evidence_packet_never_formats_deselected_corpus_source():
    selected = _corpus_chunk("a", "corpus-a", "Allowed source survives.")
    leaked = _corpus_chunk("b", "corpus-b", "Forbidden source must not appear.")
    request = ChatRequest(message="What did the selected corpus say?", corpus_ids=["corpus-a"])

    packet = _format_evidence_packet_block(sources=[selected, leaked], request=request)

    assert "Allowed source survives" in packet
    assert "Forbidden source must not appear" not in packet
    assert "Corpus sources included: 1" in packet


def test_final_fact_scope_keeps_only_selected_corpus_facts():
    selected = SourceFact(
        fact_id="f1",
        subject="GLiNER",
        fact_type="property",
        value="selected",
        corpus_id="corpus-a",
    )
    leaked = SourceFact(
        fact_id="f2",
        subject="GLiNER",
        fact_type="property",
        value="leaked",
        corpus_id="corpus-b",
    )

    assert _filter_facts_to_selected_corpora([selected, leaked], ["corpus-a"]) == [selected]


def test_budgeted_augmented_prompt_compacts_oversized_current_rag_turn():
    repeated = (
        "Python is a programming language used around AI systems. "
        "Artificial intelligence is not essentially Python. "
    ) * 5000
    sources = [
        SourceChunk(
            chunk_id=f"chunk-{idx}",
            parent_id=f"parent-{idx}",
            doc_id=f"doc-{idx}",
            corpus_id="corpus-1",
            text=repeated,
            summary="Python and AI relation",
            score=0.9,
            source_tier="qdrant_mongo_graph",
            doc_name=f"doc-{idx}.md",
        )
        for idx in range(8)
    ]

    prompt, meta = _build_budgeted_augmented_prompt(
        query="what is python and is ai essentially python",
        sources=sources,
        facts=[],
        corpus_ids=["corpus-1"],
        reasoning_mode=None,
        reasoning_blend=None,
        active_skills=None,
        analysis="Use graph evidence to distinguish Python and AI.",
        decoration=[],
        model="unknown-test-model",
    )

    assert meta["compacted"] is True
    assert meta["before_tokens"] > meta["after_tokens"]
    assert meta["after_tokens"] <= meta["budget_tokens"]
    assert "source excerpt clipped" in prompt or "current-turn RAG prompt clipped" in prompt


def test_limit_tool_calls_allows_bounded_web_searches_per_turn():
    calls = [
        _tool_call("web_search", "web_1"),
        _tool_call("web_search", "web_2"),
        _tool_call("web_search", "web_3"),
        _tool_call("web_search", "web_4"),
        _tool_call("calculator", "calc_1"),
    ]

    allowed, web_calls, dropped_for_tool_limit, dropped_for_web_limit = (
        _limit_tool_calls_for_turn(
            calls,
            remaining_tool_calls=5,
            web_search_call_count=0,
        )
    )

    assert [call["function"]["name"] for call in allowed] == [
        "web_search",
        "web_search",
        "web_search",
        "calculator",
    ]
    assert web_calls == _MAX_WEB_SEARCH_CALLS_PER_TURN
    assert dropped_for_tool_limit is False
    assert dropped_for_web_limit is True


def test_available_tool_schemas_keep_web_until_search_limit():
    schemas = [_tool_schema("web_search"), _tool_schema("calculator")]

    available = _available_tool_schemas(schemas, web_search_call_count=1)

    assert [schema["function"]["name"] for schema in available] == [
        "web_search",
        "calculator",
    ]


def test_available_tool_schemas_remove_web_after_search_limit():
    schemas = [_tool_schema("web_search"), _tool_schema("calculator")]

    available = _available_tool_schemas(
        schemas,
        web_search_call_count=_MAX_WEB_SEARCH_CALLS_PER_TURN,
    )

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


def test_web_search_result_cap_supports_user_source_budget():
    assert _MAX_WEB_SEARCH_RESULTS_PER_CALL == 20


def _source_chunk(chunk_id: str, source_tier: str, *, url: str | None = None) -> SourceChunk:
    return SourceChunk(
        chunk_id=chunk_id,
        parent_id=f"{chunk_id}-parent",
        doc_id=url or f"{chunk_id}-doc",
        corpus_id="live-web" if source_tier == "web_search" else "corpus-1",
        text=f"{chunk_id} text",
        score=1.0,
        source_tier=source_tier,
        metadata={"url": url} if url else {},
    )


def test_pending_web_sources_merge_with_local_rag_and_dedupe_urls():
    local_rag = [_source_chunk("local-1", "qdrant_only")]
    pending_web = [
        _source_chunk("web-1", "web_search", url="https://example.test/security"),
        _source_chunk("web-dup", "web_search", url="https://example.test/security"),
        _source_chunk("web-2", "web_search", url="https://example.test/docs"),
    ]

    merged = _append_deduped_web_sources(local_rag, pending_web)

    assert [chunk.source_tier for chunk in merged] == [
        "qdrant_only",
        "web_search",
        "web_search",
    ]
    assert len(merged) == 3
    assert merged[0].chunk_id == "local-1"
    assert {chunk.metadata.get("url") for chunk in merged if chunk.source_tier == "web_search"} == {
        "https://example.test/security",
        "https://example.test/docs",
    }


def test_turn_web_sources_are_capped_across_multiple_searches():
    local_rag = [_source_chunk("local-1", "qdrant_only")]
    web_sources = [
        _source_chunk(f"web-{i}", "web_search", url=f"https://example.test/{i}")
        for i in range(25)
    ]

    capped = _cap_web_sources_for_turn(local_rag + web_sources)

    assert capped[0].chunk_id == "local-1"
    assert sum(chunk.source_tier == "web_search" for chunk in capped) == 20
    assert [chunk.chunk_id for chunk in capped[-2:]] == ["web-18", "web-19"]


def test_web_evidence_options_default_to_normal_bounded_packet():
    request = ChatRequest(message="test", overrides=ModelOverrides(web_search_enabled=True))

    options = _resolve_web_evidence_options(request)

    assert options["fetch_depth"] == "normal"
    assert options["research_mode"] is False
    assert options["youtube_transcripts"] is True
    assert options["max_sources"] == 9


def test_web_evidence_options_research_promotes_normal_to_deep_and_doubles_sources():
    request = ChatRequest(
        message="test",
        overrides=ModelOverrides(
            web_search_enabled=True,
            web_fetch_depth="normal",
            web_research_mode=True,
            web_max_sources=8,
        ),
    )

    options = _resolve_web_evidence_options(request)

    assert options["fetch_depth"] == "deep"
    assert options["research_mode"] is True
    assert options["max_sources"] == 16


def test_evidence_sufficiency_grades_empty_web_as_insufficient():
    grade = _classify_web_evidence_sufficiency(
        chunks=[],
        scores=[],
        engine_errors=[],
        pipeline={"full_page_fetch_successes": 0, "snippet_sufficiency_score": 0.0},
    )

    assert grade["grade"] == "insufficient"
    assert grade["reason"] == "no_final_web_sources"


def test_evidence_scoring_attaches_relevance_completeness_intent_and_diversity():
    chunk = _source_chunk(
        "web-score",
        "web_search",
        url="https://create.roblox.com/docs/reference/engine/classes/GenerationService",
    )
    chunk.doc_name = "GenerationService"
    chunk.text = (
        "Title: GenerationService Content: GenerationService provides Roblox "
        "generation APIs and reference documentation."
    )
    chunk.metadata.update(
        {
            "url": chunk.doc_id,
            "source_type": "webpage",
            "evidence_mode": "full_page",
            "fetch_method": "raw_adapter",
        }
    )

    scores = _annotate_web_evidence_scores(
        "Roblox GenerationService API official docs",
        [chunk],
    )

    assert scores[0]["final"] > 0.7
    assert scores[0]["completeness"] == 0.92
    assert scores[0]["intent_fit"] >= 0.9
    assert chunk.metadata["evidence_score"]["final"] == scores[0]["final"]


def test_backend_retry_query_keeps_entities_and_adds_official_docs_when_needed():
    retry = _build_backend_retry_query(
        search_query="Roblox GenerationService",
        original_query="Roblox APIs such as GenerationService official docs",
    )

    assert retry is not None
    assert "GenerationService" in retry
    assert "official" in retry.lower()


def test_source_context_dedupes_exact_duplicate_chunk_cards():
    sources = [
        _source_chunk("local-1", "qdrant_only"),
        _source_chunk("local-1", "qdrant_mongo_graph"),
        _source_chunk("local-2", "qdrant_only"),
    ]

    deduped = _dedupe_sources_for_context(sources)

    assert [chunk.chunk_id for chunk in deduped] == ["local-1", "local-2"]


def test_source_context_dedupes_same_document_identical_text_chunks():
    duplicate_text = (
        "RemoteEvent validation must happen on the server. Never trust client "
        "arguments; verify type, ownership, and rate limits."
    )
    sources = [
        _source_chunk("local-1", "qdrant_mongo"),
        _source_chunk("local-2", "qdrant_mongo"),
        _source_chunk("local-3", "qdrant_mongo"),
    ]
    sources[0].doc_id = "same-doc"
    sources[1].doc_id = "same-doc"
    sources[2].doc_id = "other-doc"
    sources[0].text = duplicate_text
    sources[1].text = duplicate_text
    sources[2].text = duplicate_text

    deduped = _dedupe_sources_for_context(sources)

    assert [chunk.chunk_id for chunk in deduped] == ["local-1", "local-3"]
