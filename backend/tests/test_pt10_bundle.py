"""Pt10 — bundle tests for citation gate, HyDE default, model_name validation.

Three separate fixes shipped together as Pt10. Each addresses a real
production failure observed on the Phase5_Luau_v4 corpus:

  Pt10a — citation filter
    Failure: Fowler book's bibliography produced entities like
    "Alpert, Brown and Woolf. Design Patterns Smalltalk Companion.
    Addison-Wesley, 1998." typed as Person. Author surnames like
    "Kai Yu" and "Knight and Dai" leaked from citation contexts.
    Fix: heuristic gate before evidence validation — drops anything
    matching (year + punctuation + (publisher OR length>=8 words)).

  Pt10c — HyDE on balanced query profile
    Failure: cross-domain queries like "how does generative AI apply
    to urban planning" produced wrong-domain retrieval because raw
    query embeddings matched on surface tokens ("design").
    Fix: balanced profile now has hyde_enabled=True (was False).
    Hypothetical-answer generation routes retrieval to actually-
    relevant docs.

  Pt10d — model_name save-time validation
    Failure: production saw two pool entries with bad model_names
    ("deepseek/admin" and "deepseek/DeepSeek-V4-Flash"). Both produced
    400 storms at every chat / synthesis call. Both were typed by hand
    via the UI which had no validation.
    Fix: regex-based validation in model_pool.create / update. Catches
    pool/account name typos and title-case marketing names. Escape
    hatch via extra_params.skip_model_validation=true.
"""
from __future__ import annotations

import sys
from types import ModuleType


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
                def encode(*_a, **_kw):
                    raise RuntimeError("stub")

                @staticmethod
                def decode(*_a, **_kw):
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
                def __init__(self, *a, **kw): pass
                def hash(self, *_a, **_kw): raise RuntimeError("stub")
                def verify(self, *_a, **_kw): raise RuntimeError("stub")

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
                def __init__(self, *a, **kw): pass
                def limit(self, *_a, **_kw):
                    def _d(fn): return fn
                    return _d

            def _get_remote_address(_request): return "0.0.0.0"

            slowapi_mod.Limiter = _Limiter
            util_mod.get_remote_address = _get_remote_address
            sys.modules["slowapi"] = slowapi_mod
            sys.modules["slowapi.util"] = util_mod


_install_stubs_if_missing()


import pytest  # noqa: E402


# ── Pt10a — citation entity filter ──────────────────────────────────


from services.ghost_b import _looks_like_citation  # noqa: E402


def test_citation_full_fowler_reference_is_detected():
    """The exact failure case from production — a Design Patterns book
    citation extracted as a Person entity."""
    name = "Alpert, Brown and Woolf. Design Patterns Smalltalk Companion. Addison-Wesley, 1998."
    assert _looks_like_citation(name) is True


def test_citation_with_oreilly_publisher_detected():
    assert _looks_like_citation(
        "Moroney, L. AI and Machine Learning for On-Device Development. O'Reilly Media, 2021."
    ) is True


def test_citation_with_springer_detected():
    assert _looks_like_citation(
        "Yang, Q., Wang, Z. PsychoGAT: A Novel Psychological Measurement Paradigm. Springer, 2024."
    ) is True


def test_long_text_with_year_no_publisher_detected():
    """8+ words + year + punctuation = looks like a citation even
    without a known publisher."""
    assert _looks_like_citation(
        "Smith, J., Doe, A., Roe, B. Some Paper About Something Important. Journal, 2020."
    ) is True


def test_real_person_with_year_not_dropped():
    """Person name with parenthetical year (e.g. 'Foo Bar (1998)') is
    short and shouldn't trip the citation gate."""
    assert _looks_like_citation("Foo Bar 1998") is False


def test_real_concept_with_no_year_passes():
    assert _looks_like_citation("Domain-Driven Design") is False
    assert _looks_like_citation("Generative AI") is False
    assert _looks_like_citation("TensorFlow Lite") is False


def test_short_name_with_year_not_a_citation():
    """A 3-word entity with a year (e.g. 'iPhone 1998') is not a
    bibliographic citation."""
    assert _looks_like_citation("iPhone 1998") is False
    assert _looks_like_citation("World Cup 2018") is False


def test_empty_input_safe():
    assert _looks_like_citation("") is False
    assert _looks_like_citation(None) is False  # type: ignore[arg-type]


# ── Pt10c — HyDE default ────────────────────────────────────────────


import services.chat_orchestrator as chat_orchestrator_module  # noqa: E402
from models.schemas import ChatRequest, ModelOverrides, SourceChunk  # noqa: E402
from services.chat_orchestrator import (  # noqa: E402
    ChatOrchestrator,
    _build_chat_query_plan,
    _build_retrieval_answerability_gate,
    _build_polymath_system_prompt,
    _build_retrieval_nuance_digest,
    _format_evidence_plan_prompt_note,
    _format_retrieval_answerability_prompt_note,
    _format_answerability_short_circuit_response,
    _chat_source_is_low_value,
    _format_retrieval_nuance_contract,
    _format_chat_query_plan_trace,
    _format_retrieval_tier_synthesis_contract,
    _partition_known_tool_calls,
    _should_short_circuit_answerability,
    _should_skip_hyde_for_query,
)


def _make_tool_call(name: str) -> dict:
    return {"id": f"call_{name or 'empty'}", "type": "function",
            "function": {"name": name, "arguments": "{}"}}


def _source_chunk(text: str = "Personality and seduction evidence.") -> SourceChunk:
    return SourceChunk(
        chunk_id="chunk-1",
        parent_id="parent-1",
        doc_id="doc-1",
        corpus_id="corpus-1",
        text=text,
        score=0.9,
        source_tier="tier_b",
    )


def _web_schema() -> dict:
    return {"type": "function", "function": {"name": "web_search", "parameters": {}}}


def test_partition_known_tool_calls_drops_empty_name_calls():
    """minimax-m2.7 emits a spurious empty-name tool call alongside its answer.

    That bogus call must be dropped so the agentic loop does not 'execute' a
    not-found tool and regenerate a second answer that duplicates in the live
    stream. The persisted message was always clean; this guards the live stream.
    """
    calls = [_make_tool_call(""), _make_tool_call("web_search")]
    kept, dropped = _partition_known_tool_calls(calls, [_web_schema()])

    assert [c["function"]["name"] for c in kept] == ["web_search"]
    assert dropped == ["<empty>"]


def test_partition_known_tool_calls_drops_unknown_and_keeps_response():
    """Unknown tool names are dropped; the always-valid 'response' finish tool
    is kept even when it is not in the active schema list."""
    calls = [
        _make_tool_call("not_a_real_tool"),
        _make_tool_call("response"),
        _make_tool_call("web_search"),
    ]
    kept, dropped = _partition_known_tool_calls(calls, [_web_schema()])

    kept_names = [c["function"]["name"] for c in kept]
    assert "response" in kept_names
    assert "web_search" in kept_names
    assert "not_a_real_tool" not in kept_names
    assert dropped == ["not_a_real_tool"]


def test_partition_known_tool_calls_empty_only_yields_no_kept():
    """An iteration whose only tool call is malformed becomes a final answer
    (no kept calls), so the loop breaks instead of regenerating."""
    kept, dropped = _partition_known_tool_calls([_make_tool_call("")], [_web_schema()])

    assert kept == []
    assert dropped == ["<empty>"]


def test_balanced_profile_has_hyde_enabled():
    """The fix for cross-domain query retrieval. Pre-Pt10c this was
    False; that produced wrong-domain results when queries had
    overloaded surface tokens like 'design'."""
    presets = ChatOrchestrator._QUERY_PROFILE_PRESETS
    assert presets["balanced"]["hyde_enabled"] is True
    # v4 P1: pool widened toward the 64-doc listwise capacity.
    assert presets["balanced"]["rerank_top_n"] == 32


def test_fast_profile_still_has_hyde_disabled():
    """The Pt10c change is scoped to balanced. Fast stays cheap.
    Thorough was already True."""
    presets = ChatOrchestrator._QUERY_PROFILE_PRESETS
    assert presets["fast"]["hyde_enabled"] is False


def test_thorough_profile_unchanged():
    presets = ChatOrchestrator._QUERY_PROFILE_PRESETS
    assert presets["thorough"]["hyde_enabled"] is True
    assert presets["thorough"]["retrieval_k"] == 160
    assert presets["thorough"]["rerank_top_n"] == 40


@pytest.mark.asyncio
async def test_profile_rerank_caps_resolve_from_presets():
    orchestrator = ChatOrchestrator()

    balanced = await orchestrator._resolve_query_profile(
        ChatRequest(message="remoteevent validation")
    )
    thorough = await orchestrator._resolve_query_profile(
        ChatRequest(
            message="remoteevent validation",
            overrides=ModelOverrides(query_profile="thorough"),
        )
    )

    assert balanced["rerank_top_n"] == 32
    assert thorough["rerank_top_n"] == 40


def test_hyde_skips_source_constrained_direct_support_queries():
    query = (
        "Based on the retrieved excerpts from Fowler's Patterns of Enterprise "
        "Application Architecture and Myers/Briggs' Gifts Differing, identify "
        "any defensible intersection. Distinguish direct textual support from "
        "inferred design recommendations."
    )
    assert _should_skip_hyde_for_query(query) is True


def test_hyde_skips_specific_definition_relation_queries():
    query = "What is NLP and how does Python relate to it?"
    assert _should_skip_hyde_for_query(query) is True


def test_chat_query_plan_exposes_relationship_search_contract():
    query = (
        "how does different personality correlate to the art of seduction "
        "with people as men dating women"
    )
    plan = _build_chat_query_plan(
        query=query,
        retrieval_query=query,
        requested_tier="qdrant_mongo",
        corpus_ids=["corpus-1"],
        collections=None,
        profile_cfg={
            "query_profile": "balanced",
            "retrieval_k": 40,
            "top_k_summary": 20,
            "rerank_enabled": True,
            "rerank_top_n": 24,
            "final_top_k": 8,
            "source_cap": 8,
        },
        search_mode="local",
        hyde_applied=False,
    )

    concept_keys = [item["key"] for item in plan["concepts"]]
    assert concept_keys[:3] == [
        "personality_framework",
        "personality",
        "seduction",
    ]
    assert "correlate" not in concept_keys
    assert "art" not in concept_keys
    assert "relationship" in plan["operators"]
    assert "relationship" in plan["required_atoms"]
    assert "concept:personality_framework" in plan["required_atoms"]
    assert "concept:art" not in plan["required_atoms"]
    assert plan["evidence_plan"]["mode"] == "multi_concept_relationship"
    assert [lane["name"] for lane in plan["evidence_plan"]["lanes"]] == [
        "personality_framework",
        "seduction",
    ]
    assert plan["stores"] == ["qdrant_vectors", "mongo_lexical_hydration"]
    trace = _format_chat_query_plan_trace(plan)
    assert "scope=selected_corpora" in trace
    assert "evidence_lanes: personality_framework, seduction" in trace
    assert "answerability: enforce_retrieved_evidence" in trace


def test_answerability_gate_relationship_only_gap_is_partial_not_refusal():
    # Contract since 4e47cdb (RELATIONSHIP_GATE loosening): when the ONLY
    # missing critical atom is the relationship bridge and coverage is at or
    # above the partial floor, the gate answers PARTIALLY in every mode —
    # both grounded sides are present and the LLM synthesizes the link
    # (chat_orchestrator status branch: "also softens strict mode").
    query = "How does personality correlate with seduction?"
    diagnostics = {
        "selection": {
            "sufficiency": {
                "required_atoms": [
                    "concept:personality",
                    "concept:seduction",
                    "relationship",
                ],
                "covered_required_atoms": [
                    "concept:personality",
                    "concept:seduction",
                ],
                "missing_atoms": ["relationship"],
                "missing_critical_atoms": ["relationship"],
                "required_coverage": 0.6667,
                "answerable": False,
            }
        }
    }

    gate = _build_retrieval_answerability_gate(
        query=query,
        diagnostics=diagnostics,
        sources=[_source_chunk()],
        facts=[],
        corpus_ids=["corpus-1"],
        web_search_enabled=False,
    )

    assert gate["status"] == "partial"
    assert gate["answerable"] is False
    assert gate["missing_critical_atoms"] == ["relationship"]
    note = _format_retrieval_answerability_prompt_note(gate)
    assert "partial" in note.lower()


def test_answerability_gate_low_coverage_relationship_gap_still_refuses():
    # The refusal path survives the loosening: relationship-only missing with
    # coverage BELOW the partial floor (one of three atoms covered) must stay
    # unanswerable — the honesty floor.
    query = "How does personality correlate with seduction?"
    diagnostics = {
        "selection": {
            "sufficiency": {
                "required_atoms": [
                    "concept:personality",
                    "concept:seduction",
                    "relationship",
                ],
                "covered_required_atoms": ["concept:personality"],
                "missing_atoms": ["concept:seduction", "relationship"],
                "missing_critical_atoms": ["relationship"],
                "required_coverage": 0.3333,
                "answerable": False,
            }
        }
    }

    # Source text must NOT mention the missing concepts — the gate's text
    # fallback (test_answerability_text_fallback.py) would otherwise re-cover
    # them from the chunk text and lift coverage back above the floor.
    gate = _build_retrieval_answerability_gate(
        query=query,
        diagnostics=diagnostics,
        sources=[_source_chunk("An unrelated passage about database indexes.")],
        facts=[],
        corpus_ids=["corpus-1"],
        web_search_enabled=False,
    )

    assert gate["status"] == "unanswerable"
    assert gate["answerable"] is False
    note = _format_retrieval_answerability_prompt_note(gate)
    assert "HARD LIMIT" in note


def test_answerability_short_circuit_only_for_unrepairable_corpus_gaps():
    gate = {
        "status": "unanswerable",
        "corpus_scoped": True,
        "source_count": 1,
        "missing_atoms": ["relationship"],
    }

    assert _should_short_circuit_answerability(
        gate,
        web_search_enabled=False,
        selected_tools=None,
    )
    assert not _should_short_circuit_answerability(
        gate,
        web_search_enabled=True,
        selected_tools=None,
    )
    assert not _should_short_circuit_answerability(
        gate,
        web_search_enabled=False,
        selected_tools=["web_search"],
    )

    response = _format_answerability_short_circuit_response(
        gate,
        query="How does personality correlate with seduction?",
    )
    assert "cannot answer" in response
    assert "relationship" in response


def test_answerability_gate_allows_source_backed_answer():
    query = "How does personality correlate with seduction?"
    diagnostics = {
        "selection": {
            "sufficiency": {
                "required_atoms": [
                    "concept:personality",
                    "concept:seduction",
                    "relationship",
                ],
                "covered_required_atoms": [
                    "concept:personality",
                    "concept:seduction",
                    "relationship",
                ],
                "missing_atoms": [],
                "missing_critical_atoms": [],
                "required_coverage": 1.0,
                "answerable": True,
            }
        }
    }

    gate = _build_retrieval_answerability_gate(
        query=query,
        diagnostics=diagnostics,
        sources=[_source_chunk("A passage connects personality types to seductive roles.")],
        facts=[],
        corpus_ids=["corpus-1"],
        web_search_enabled=False,
    )

    assert gate["status"] == "answerable"
    assert gate["answerable"] is True
    note = _format_retrieval_answerability_prompt_note(gate)
    assert "Answer directly" in note


def test_answerability_gate_uses_evidence_plan_to_repair_stale_sufficiency():
    query = "how does different personality correlate to the art of seduction"
    diagnostics = {
        "selection": {
            "sufficiency": {
                "required_atoms": [
                    "concept:personality_framework",
                    "concept:seduction",
                    "relationship",
                    "cross_document_relationship_evidence",
                ],
                "covered_required_atoms": [
                    "concept:seduction",
                    "relationship",
                ],
                "missing_atoms": [
                    "concept:personality_framework",
                    "cross_document_relationship_evidence",
                ],
                "missing_critical_atoms": [
                    "cross_document_relationship_evidence",
                ],
                "required_coverage": 0.5,
                "answerable": False,
            }
        }
    }
    evidence_plan_meta = {
        "active": True,
        "required_lanes": ["personality_framework", "seduction"],
        "covered_lanes": ["personality_framework", "seduction"],
        "missing_lanes": [],
        "final": {
            "distinct_doc_count": 2,
            "lane_doc_ids": {
                "personality_framework": ["personality-book"],
                "seduction": ["seduction-book"],
            },
        },
    }

    gate = _build_retrieval_answerability_gate(
        query=query,
        diagnostics=diagnostics,
        sources=[_source_chunk()],
        facts=[],
        corpus_ids=["corpus-1"],
        web_search_enabled=False,
        evidence_plan_meta=evidence_plan_meta,
    )

    assert gate["status"] == "answerable"
    assert "concept:personality_framework" in gate["covered_required_atoms"]
    assert "cross_document_relationship_evidence" in gate["covered_required_atoms"]
    assert gate["missing_atoms"] == []
    note = _format_evidence_plan_prompt_note(evidence_plan_meta)
    assert "contains evidence for every required lane" in note


def test_answerability_gate_blocks_when_evidence_plan_lane_is_missing():
    query = "how does different personality correlate to the art of seduction"
    diagnostics = {
        "selection": {
            "sufficiency": {
                "required_atoms": [
                    "concept:personality_framework",
                    "concept:seduction",
                    "relationship",
                ],
                "covered_required_atoms": [
                    "concept:seduction",
                    "relationship",
                ],
                "missing_atoms": ["concept:personality_framework"],
                "missing_critical_atoms": [],
                "required_coverage": 0.6667,
                "answerable": False,
            }
        }
    }
    evidence_plan_meta = {
        "active": True,
        "required_lanes": ["personality_framework", "seduction"],
        "covered_lanes": ["seduction"],
        "missing_lanes": ["personality_framework"],
        "final": {
            "distinct_doc_count": 1,
            "lane_doc_ids": {"seduction": ["seduction-book"]},
        },
    }

    gate = _build_retrieval_answerability_gate(
        query=query,
        diagnostics=diagnostics,
        sources=[_source_chunk()],
        facts=[],
        corpus_ids=["corpus-1"],
        web_search_enabled=False,
        evidence_plan_meta=evidence_plan_meta,
    )

    assert gate["status"] == "unanswerable"
    assert "concept:personality_framework" in gate["missing_critical_atoms"]
    assert "cross_document_relationship_evidence" in gate["missing_atoms"]
    note = _format_evidence_plan_prompt_note(evidence_plan_meta)
    assert "HARD LIMIT" in note


def test_answerability_gate_does_not_require_cross_doc_for_plain_multi_concept():
    query = "personality seduction"
    diagnostics = {
        "selection": {
            "sufficiency": {
                "required_atoms": [
                    "concept:personality",
                    "concept:seduction",
                ],
                "covered_required_atoms": [
                    "concept:personality",
                    "concept:seduction",
                ],
                "missing_atoms": [],
                "missing_critical_atoms": [],
                "required_coverage": 1.0,
                "answerable": True,
            }
        }
    }
    evidence_plan_meta = {
        "active": True,
        "plan": {
            "mode": "multi_concept",
            "operators": [],
        },
        "required_lanes": ["personality", "seduction"],
        "covered_lanes": ["personality", "seduction"],
        "missing_lanes": [],
        "final": {
            "distinct_doc_count": 1,
            "lane_doc_ids": {
                "personality": ["same-book"],
                "seduction": ["same-book"],
            },
        },
    }

    gate = _build_retrieval_answerability_gate(
        query=query,
        diagnostics=diagnostics,
        sources=[_source_chunk()],
        facts=[],
        corpus_ids=["corpus-1"],
        web_search_enabled=False,
        evidence_plan_meta=evidence_plan_meta,
    )

    assert gate["status"] == "answerable"
    assert "cross_document_relationship_evidence" not in gate["required_atoms"]


def test_chat_evidence_filter_rejects_frontmatter_noise():
    noisy = SourceChunk(
        chunk_id="frontmatter",
        parent_id="frontmatter",
        doc_id="doc-frontmatter",
        corpus_id="corpus",
        text=(
            "## Join our book's Discord space\n"
            "# Table of Contents\n"
            "1. Introduction to Python and Code Editors\n"
        ),
        score=0.9,
        source_tier="tier_a",
    )
    substantive = SourceChunk(
        chunk_id="body",
        parent_id="body",
        doc_id="doc-body",
        corpus_id="corpus",
        text="Python code examples show how natural language processing models tokenize text.",
        score=0.9,
        source_tier="tier_a",
    )

    assert _chat_source_is_low_value(noisy, "What is NLP and how does Python relate to it?")
    assert not _chat_source_is_low_value(
        substantive,
        "What is NLP and how does Python relate to it?",
    )


def test_retrieval_tiers_have_distinct_synthesis_lenses():
    vector = _format_retrieval_tier_synthesis_contract(
        "qdrant_only",
        {"counts": {"lexical": 0, "facts": 0, "graph_expanded": 0}},
    )
    hybrid = _format_retrieval_tier_synthesis_contract(
        "qdrant_mongo",
        {"counts": {"lexical": 12, "facts": 0, "graph_expanded": 0}},
    )
    graph = _format_retrieval_tier_synthesis_contract(
        "qdrant_mongo_graph",
        {"counts": {"lexical": 12, "facts": 4, "graph_expanded": 20}},
    )

    assert "semantic overview" in vector.lower()
    assert "hydrated corpus synthesis" in hybrid.lower()
    assert "relationship map" in graph.lower()
    assert "broad_concept_rule" in vector
    assert "answer anyway" in hybrid
    assert "do not ask for clarification" in graph
    assert "source comparison" in vector
    assert "what the selected corpus evidence specifically says" in hybrid
    # Hybrid now opens naturally instead of with a fixed "Across the selected
    # sources" template, and must not emit a standing "does not establish" line.
    assert "do NOT use a fixed opener" in hybrid
    assert "default short-answer compression" in hybrid
    assert "core node, connected ideas" in graph
    # Graph keeps the relationship shape but must not paste the fixed labels
    # verbatim as section headers (de-templatized so weak models don't echo them).
    assert "do not paste the fixed labels" in graph
    assert len({vector, hybrid, graph}) == 3


def test_system_prompt_requires_answering_overloaded_concepts():
    prompt = _build_polymath_system_prompt()

    assert "broad or overloaded concept" in prompt
    assert "still answer the question" in prompt
    assert "do not silently pick one sense" in prompt


def test_retrieval_nuance_digest_surfaces_repeated_context():
    sources = [
        SourceChunk(
            chunk_id="nlp-1",
            parent_id="parent-1",
            doc_id="doc-nlp",
            corpus_id="corpus",
            doc_name="Computational Linguistics Handbook.md",
            heading_path=["Natural Language Processing"],
            text=(
                "Natural language processing studies language models, "
                "annotated corpora, information retrieval, and text analysis."
            ),
            score=1.0,
            source_tier="tier_a_summary",
        ),
        SourceChunk(
            chunk_id="nlp-2",
            parent_id="parent-2",
            doc_id="doc-nlp",
            corpus_id="corpus",
            doc_name="Computational Linguistics Handbook.md",
            heading_path=["Information Retrieval"],
            text=(
                "NLP and information retrieval both use annotated corpora. "
                "Language models connect natural language evidence to ranking."
            ),
            score=0.95,
            source_tier="tier_b_child",
        ),
        SourceChunk(
            chunk_id="python-1",
            parent_id="parent-3",
            doc_id="doc-python",
            corpus_id="corpus",
            doc_name="Python NLP Systems.md",
            heading_path=["Python"],
            text=(
                "Python libraries support natural language processing by "
                "tokenizing text, training language models, and evaluating "
                "information retrieval systems."
            ),
            score=0.9,
            source_tier="lexical",
        ),
    ]

    digest = _build_retrieval_nuance_digest(
        tier="qdrant_mongo",
        sources=sources,
        facts=[],
        decoration=[],
        diagnostics={
            "counts": {"lexical": 3, "funnel_a": 2, "funnel_b": 1},
            "final_source_tiers": {"tier_a_summary": 1, "tier_b_child": 1, "lexical": 1},
        },
    )
    contract = _format_retrieval_nuance_contract(digest)

    assert "natural language" in digest["high_frequency_context"]
    assert "language models" in digest["high_frequency_context"]
    assert digest["recurring_documents"][0]["name"] == "Computational Linguistics Handbook.md"
    assert contract is not None
    assert "<retrieval_nuance_digest>" in contract
    # Salient terms are surfaced to the model as a hint...
    assert "salient_terms" in contract
    # ...but leak-prone diagnostic counters are NOT sent into the prompt, and the
    # contract hard-forbids rendering the terms as a list / "Also X. Also Y." spam.
    assert "recurring_documents" not in contract
    assert "source_lane_mix" not in contract
    assert "retrieval_additions" not in contract
    assert "NEVER output these terms as a list" in contract


def test_retrieval_nuance_digest_groups_overloaded_ontology_frames():
    sources = [
        SourceChunk(
            chunk_id="kg-1",
            parent_id="parent-1",
            doc_id="doc-kg",
            corpus_id="corpus",
            doc_name="Knowledge Graphs.md",
            text=(
                "Ontologies in knowledge graphs use RDF, OWL, schema, and "
                "linked data to formally represent a domain model."
            ),
            score=1.0,
            source_tier="qdrant_mongo_graph",
        ),
        SourceChunk(
            chunk_id="phil-1",
            parent_id="parent-2",
            doc_id="doc-phil",
            corpus_id="corpus",
            doc_name="Philosophy of Mind.md",
            text=(
                "Ontology and epistemology ask about existence, being, "
                "reality, and what there is."
            ),
            score=0.9,
            source_tier="lexical",
        ),
        SourceChunk(
            chunk_id="self-1",
            parent_id="parent-3",
            doc_id="doc-self",
            corpus_id="corpus",
            doc_name="Self and Identity.md",
            text=(
                "Social ontology and self identity concern subjectivity, "
                "personal experience, and social construction."
            ),
            score=0.85,
            source_tier="tier_b_child",
        ),
    ]

    digest = _build_retrieval_nuance_digest(
        tier="qdrant_mongo_graph",
        query="why are ontologies so powerful",
        sources=sources,
        facts=[],
        decoration=[],
        diagnostics={},
    )
    frames = {item["frame"] for item in digest["broad_concept_frames"]}
    contract = _format_retrieval_nuance_contract(digest)

    assert "technical ontology / knowledge graph" in frames
    assert "philosophical ontology / being" in frames
    assert "social or self ontology" in frames
    assert contract is not None
    assert "broad_concept_frames" in contract
    assert "Do NOT ask the user to clarify" in contract
    assert "Answer the question directly first" in contract


def test_hyde_stays_available_for_open_cross_domain_discovery():
    query = "How could generative AI methods apply to urban planning?"
    assert _should_skip_hyde_for_query(query) is False


@pytest.mark.asyncio
async def test_source_constrained_profile_default_hyde_is_skipped(monkeypatch):
    query = (
        "Based on the retrieved excerpts from Fowler's Patterns of Enterprise "
        "Application Architecture and Myers/Briggs' Gifts Differing, identify "
        "any defensible intersection. Distinguish direct textual support from "
        "inferred design recommendations."
    )
    request = ChatRequest(
        message=query,
        overrides=ModelOverrides(query_profile="thorough"),
    )
    orchestrator = ChatOrchestrator()

    profile = await orchestrator._resolve_query_profile(request)
    assert profile["hyde_enabled"] is True
    assert profile["hyde_explicit"] is False

    async def fail_complete_sync(**_kwargs):
        raise AssertionError("profile-default HyDE should be skipped")

    monkeypatch.setattr(
        chat_orchestrator_module.llm_service,
        "complete_sync",
        fail_complete_sync,
    )

    retrieval_query, applied = await orchestrator._apply_hyde(
        request,
        hyde_explicit=profile["hyde_explicit"],
    )

    assert retrieval_query == query
    assert applied is False


@pytest.mark.asyncio
async def test_source_constrained_explicit_hyde_toggle_is_honored(monkeypatch):
    query = (
        "Based on the retrieved excerpts from Fowler's Patterns of Enterprise "
        "Application Architecture and Myers/Briggs' Gifts Differing, identify "
        "any defensible intersection. Distinguish direct textual support from "
        "inferred design recommendations."
    )
    request = ChatRequest(
        message=query,
        overrides=ModelOverrides(
            query_profile="thorough",
            hyde_enabled=True,
            hyde_model="test/hyde",
        ),
    )
    orchestrator = ChatOrchestrator()
    calls = {"count": 0}

    async def fake_complete_sync(**_kwargs):
        calls["count"] += 1
        return "A hypothetical answer for retrieval."

    monkeypatch.setattr(
        chat_orchestrator_module.llm_service,
        "complete_sync",
        fake_complete_sync,
    )
    chat_orchestrator_module._HYDE_FAILURE_CACHE.clear()

    profile = await orchestrator._resolve_query_profile(request)
    assert profile["hyde_enabled"] is True
    assert profile["hyde_explicit"] is True

    retrieval_query, applied = await orchestrator._apply_hyde(
        request,
        hyde_explicit=profile["hyde_explicit"],
    )

    assert retrieval_query == "A hypothetical answer for retrieval."
    assert applied is True
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_hyde_without_dedicated_model_inherits_active_chat_model(monkeypatch):
    request = ChatRequest(
        message="How could retrieval tables improve AWS architecture study notes?",
        overrides=ModelOverrides(
            hyde_enabled=True,
            model="deepseek/deepseek-v4-flash",
        ),
    )
    orchestrator = ChatOrchestrator()
    calls = {"model": None}

    async def no_hyde_pool(_user_id, kind):
        assert kind == "hyde"
        return None

    async def fake_complete_sync(**kwargs):
        calls["model"] = kwargs.get("model")
        return "A hypothetical AWS architecture answer for retrieval."

    monkeypatch.setattr(chat_orchestrator_module, "resolve_query_model_kind", no_hyde_pool)
    monkeypatch.setattr(chat_orchestrator_module.settings, "HYDE_MODEL", "env/hyde")
    monkeypatch.setattr(
        chat_orchestrator_module.llm_service,
        "complete_sync",
        fake_complete_sync,
    )
    chat_orchestrator_module._HYDE_FAILURE_CACHE.clear()

    retrieval_query, applied = await orchestrator._apply_hyde(
        request,
        user_id="user-1",
        hyde_explicit=True,
        fallback_model=request.overrides.model,
    )

    assert retrieval_query == "A hypothetical AWS architecture answer for retrieval."
    assert applied is True
    assert calls["model"] == "deepseek/deepseek-v4-flash"


# ── Pt10d — model_name validation ───────────────────────────────────


from services.model_pool import (  # noqa: E402
    InvalidModelNameError,
    validate_model_name,
)


def test_admin_pool_name_typo_blocked():
    """The exact failure observed on the live system. User typed pool
    name into model field. Must raise."""
    with pytest.raises(InvalidModelNameError, match="pool/account name"):
        validate_model_name(provider="deepseek", model_name="admin")
    with pytest.raises(InvalidModelNameError, match="pool/account name"):
        validate_model_name(provider="deepseek", model_name="deepseek/admin")


def test_titlecase_capitalization_blocked_with_lowercase_hint():
    """Second observed failure — DeepSeek-V4-Flash typed instead of
    deepseek-v4-flash."""
    with pytest.raises(InvalidModelNameError, match="wrong capitalization"):
        validate_model_name(provider="deepseek", model_name="deepseek/DeepSeek-V4-Flash")
    with pytest.raises(InvalidModelNameError, match="wrong capitalization"):
        validate_model_name(provider="deepseek", model_name="DeepSeek-V4-Flash")


def test_valid_deepseek_model_names_pass():
    # All four production model ids must validate.
    for name in (
        "deepseek/deepseek-chat",
        "deepseek/deepseek-reasoner",
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
        "deepseek-chat",  # bare form
        "deepseek-v4-flash",
    ):
        # Should not raise.
        validate_model_name(provider="deepseek", model_name=name)


def test_valid_openai_model_names_pass():
    for name in (
        "openai/gpt-4o",
        "openai/gpt-4o-mini",
        "openai/o1",
        "openai/o3-mini",
        "gpt-4o",
        "o1-preview",
    ):
        validate_model_name(provider="openai", model_name=name)


def test_valid_anthropic_model_names_pass():
    for name in (
        "anthropic/claude-3-5-sonnet-20241022",
        "anthropic/claude-4-opus",
        "claude-3-7-sonnet",
    ):
        validate_model_name(provider="anthropic", model_name=name)


def test_skip_validation_bypass_works():
    """Escape hatch for novel models not yet in the registry."""
    # Without bypass, this raises.
    with pytest.raises(InvalidModelNameError):
        validate_model_name(provider="deepseek", model_name="deepseek-v99-future")
    # With bypass, it passes.
    validate_model_name(
        provider="deepseek",
        model_name="deepseek-v99-future",
        allow_skip=True,
    )


def test_unknown_provider_falls_through():
    """We don't enforce on providers we haven't catalogued. The
    provider's own API is the safety net for those."""
    validate_model_name(provider="exotic-provider", model_name="anything-goes")


def test_empty_model_name_blocked():
    with pytest.raises(InvalidModelNameError, match="required"):
        validate_model_name(provider="deepseek", model_name="")
    with pytest.raises(InvalidModelNameError, match="required"):
        validate_model_name(provider="deepseek", model_name=None)  # type: ignore[arg-type]


def test_unknown_deepseek_model_blocked():
    """Random unknown DeepSeek variant — caller must opt out via
    skip_model_validation if they intend it."""
    with pytest.raises(InvalidModelNameError, match="not a known"):
        validate_model_name(provider="deepseek", model_name="deepseek-vfoo")
