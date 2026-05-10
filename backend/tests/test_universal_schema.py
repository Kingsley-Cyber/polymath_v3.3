"""
Sanity checks for the baked universal schema (GHOST B).

The schema is a contract: Neo4j entity types and RELATES_TO predicates across
every corpus are derived from these two lists. Accidentally renaming or
reordering them breaks cross-corpus queries and (when either vocabulary list
crosses SCHEMA_INLINE_LIMIT) flips ghost_b into degraded retrieval mode.
"""

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from config import get_settings
from models.schemas import IngestionConfig
import services.ghost_b as ghost_b
from services.ghost_b import (
    EntityItem,
    ExtractionTask,
    FactItem,
    RelationItem,
    UNIVERSAL_ENTITY_SCHEMA,
    UNIVERSAL_RELATION_SCHEMA,
    SchemaContext,
    _apply_schema,
    _debug_log_raw_jsonl_lines,
    _merge_jsonl_items,
    _parse,
    _parse_jsonl_items,
    _parse_jsonl_lines,
    _select_extraction_output_mode,
    _validate_evidence,
    build_json_object_prompt,
    build_rescue_prompt,
    build_user_prompt,
    normalize_relation_predicate_alias,
)
from services.graph.neo4j_writer import (
    ONTOLOGY_VERSION,
    canonicalize_entity_name,
    entity_id_from_name,
    resolve_canonical_family,
    resolve_domain_type,
    resolve_facets,
    resolve_ontology_metadata,
    resolve_primary_entity_type,
    refine_related_to_predicate,
    relation_family_for_predicate,
)


def test_entity_schema_shape():
    assert len(UNIVERSAL_ENTITY_SCHEMA) == 12
    assert all(isinstance(t, str) and t.strip() for t in UNIVERSAL_ENTITY_SCHEMA)
    assert len(set(UNIVERSAL_ENTITY_SCHEMA)) == 12, "entity schema has duplicates"
    for required in ("Person", "Organization", "Rule", "Law"):
        assert required in UNIVERSAL_ENTITY_SCHEMA


def test_relation_schema_shape():
    # 30 entries: 12 universal entity types do not gate this; the relation
    # list got the canonicalization + missing-affiliation/ownership additions
    # while shedding `calls` (collapsed into `uses`) and `extracts` (merged
    # into `detects`). Net +3 brings the list to the SCHEMA_INLINE_LIMIT.
    assert len(UNIVERSAL_RELATION_SCHEMA) == 30
    assert all(isinstance(p, str) and p.strip() for p in UNIVERSAL_RELATION_SCHEMA)
    assert len(set(UNIVERSAL_RELATION_SCHEMA)) == 30, "relation schema has duplicates"
    assert UNIVERSAL_RELATION_SCHEMA[-1] == "related_to", (
        "related_to sentinel MUST be last"
    )
    for required in (
        "excepts", "overrides", "runs_on", "trained_on",
        "synonym_of", "instance_of", "owns", "affiliated_with", "overlaps",
        "detects",
    ):
        assert required in UNIVERSAL_RELATION_SCHEMA
    # Predicates that were collapsed must NOT reappear in the universal list.
    for removed in ("calls", "extracts"):
        assert removed not in UNIVERSAL_RELATION_SCHEMA


def test_each_vocab_stays_inline():
    # ghost_b decides inline-vs-retrieved separately for entity and relation
    # vocabularies. Keep both below SCHEMA_INLINE_LIMIT so fresh ingest never
    # needs schema-term vector retrieval before chunk embeddings exist.
    limit = get_settings().SCHEMA_INLINE_LIMIT
    assert len(UNIVERSAL_ENTITY_SCHEMA) + 1 <= limit  # + 'other' sentinel
    assert len(UNIVERSAL_RELATION_SCHEMA) <= limit


def test_default_ingestion_config_uses_universal():
    cfg = IngestionConfig()
    assert cfg.entity_schema == UNIVERSAL_ENTITY_SCHEMA
    assert cfg.relation_schema == UNIVERSAL_RELATION_SCHEMA
    assert cfg.schema_strict == "soft"


def test_default_ingestion_config_lists_are_copies():
    # Guard against accidentally sharing the module-level list — mutating
    # a corpus-level schema must not mutate every other corpus's config.
    cfg1 = IngestionConfig()
    cfg2 = IngestionConfig()
    assert cfg1.entity_schema is not cfg2.entity_schema
    assert cfg1.relation_schema is not cfg2.relation_schema


def test_prompt_renders_universal_vocab():
    cfg = IngestionConfig()
    ctx = SchemaContext(
        entity_schema=cfg.entity_schema,
        relation_schema=cfg.relation_schema,
        strict=cfg.schema_strict,
    )
    prompt = build_user_prompt(
        chunk_id="c1",
        doc_id="d1",
        corpus_id="corp1",
        text="sample",
        schema=ctx,
    )
    # Tightened format: `Name=gloss|Name=gloss|…` with no spaces around `|`.
    # The verbose paragraphs of the old prompt were dropped to halve the
    # per-chunk extraction prompt; only essential rules remain.
    assert (
        "entity_type one of: Person=human individual|Organization=formal group"
    ) in prompt
    assert (
        "predicate one of: part_of=X subcomponent of Y|member_of=X in group Y"
    ) in prompt
    # Sentinels surface as explicit fallbacks (with [FALLBACK] tag inline)
    assert "'other'" in prompt
    assert "'related_to'" in prompt
    assert "other=fallback [FALLBACK]" in prompt
    assert "related_to=use only when no specific predicate fits [FALLBACK]" in prompt
    assert "evidence_phrase" in prompt
    assert "Output JSONL only" in prompt
    assert '"t":"e"' in prompt
    assert '"t":"r"' in prompt
    assert '{"t":"x"}' in prompt
    assert "escape it as \\n" in prompt
    # Universal predicates still listed in the JSONL line shape
    assert "runs_on" in prompt
    assert "trained_on" in prompt
    # Canonicalization + missing-relation predicates added in the schema patch
    assert "synonym_of" in prompt
    assert "instance_of" in prompt
    assert "owns" in prompt
    assert "affiliated_with" in prompt
    assert "overlaps" in prompt
    # Removed predicates should no longer be advertised in the vocab block
    assert "extracts=" not in prompt
    # `calls` was collapsed into `uses` — ensure neither the vocab line nor
    # the JSON-example enum still advertises it.
    assert "calls=" not in prompt
    assert "|calls|" not in prompt
    # Ontology facet exclusion still enforced
    assert "ontology" in prompt


def test_prompt_renders_optional_fact_shape_when_enabled():
    prompt = build_user_prompt(
        chunk_id="c1",
        doc_id="d1",
        corpus_id="corp1",
        text="Orders over $500 require manager approval.",
        enable_facts=True,
        max_facts=5,
    )

    assert '"t":"f"' in prompt
    assert '"ft":"property|status|timestamp|threshold|category|tag|rule_condition|rule_action"' in prompt
    assert "max 5 facts" in prompt
    assert "fact subject must match an entity canonical_name" in prompt


def test_json_object_prompt_renders_strict_primary_contract():
    cfg = IngestionConfig()
    ctx = SchemaContext(
        entity_schema=cfg.entity_schema,
        relation_schema=cfg.relation_schema,
        strict=cfg.schema_strict,
    )
    prompt = build_json_object_prompt(
        chunk_id="c1",
        doc_id="d1",
        corpus_id="corp1",
        text="constexpr functions produce compile-time results.",
        schema=ctx,
        enable_facts=True,
        max_entities=8,
        max_relations=12,
        max_facts=4,
        evidence_max_chars=120,
        fact_value_max_chars=160,
    )

    assert "Return exactly one valid JSON object" in prompt
    assert "Do not use markdown, code fences, JSONL" in prompt
    assert '"entities"' in prompt
    assert '"relations"' in prompt
    assert '"facts"' in prompt
    assert "entities: max 8" in prompt
    assert "relations: max 12" in prompt
    assert "facts: max 4" in prompt
    assert "evidence_phrase <= 120 chars" in prompt
    assert "value <= 160 chars" in prompt
    assert "entity_type one of: Person=human individual" in prompt
    assert "predicate one of: part_of=X subcomponent of Y" in prompt


def test_output_mode_is_jsonl_primary_even_for_deepseek_and_legacy_config():
    assert (
        _select_extraction_output_mode(
            "auto",
            {"model": "deepseek/deepseek-v4-flash", "base_url": None},
            profile_name="normal",
        )
        == "jsonl"
    )
    assert (
        _select_extraction_output_mode(
            "auto",
            {"model": "test-model", "base_url": None},
            profile_name="normal",
        )
        == "jsonl"
    )
    assert (
        _select_extraction_output_mode(
            "json_object",
            {"model": "test-model", "base_url": None},
            profile_name="normal",
        )
        == "jsonl"
    )
    assert (
        _select_extraction_output_mode(
            "json_object",
            {"model": "deepseek/deepseek-v4-flash", "base_url": None},
            profile_name="rescue",
        )
        == "jsonl"
    )


def test_jsonl_parser_keeps_complete_lines_and_discards_partial_tail():
    task = ExtractionTask(
        chunk_id="c1",
        doc_id="d1",
        corpus_id="corp1",
        text="constexpr functions produce compile-time results.",
    )
    raw = "\n".join(
        [
            '{"t":"e","cn":"constexpr functions","sf":"constexpr functions","et":"Concept","cf":0.91}',
            '{"t":"r","sub":"constexpr functions","pred":"produces","obj":"compile-time results","ok":"literal","cf":0.86,"ev":"produce compile-time results"}',
            '{"t":"e","cn":"truncated"',
        ]
    )

    parsed = _parse_jsonl_lines(raw)
    result = _parse_jsonl_items(parsed.items, task, 0.0)

    assert parsed.finished is False
    assert parsed.valid_lines == 2
    assert parsed.invalid_line is not None
    assert result is not None
    assert [e.canonical_name for e in result.entities] == ["constexpr functions"]
    assert len(result.relations) == 1
    assert result.relations[0].predicate == "produces"


def test_jsonl_parser_skips_fences_and_preambles():
    raw = "\n".join(
        [
            "Here is the extraction:",
            "```jsonl",
            'prefix text {"t":"e","cn":"constexpr objects","sf":"constexpr objects","et":"Concept","cf":0.92}',
            '  {"t":"x"}',
            "```",
        ]
    )

    parsed = _parse_jsonl_lines(raw)

    assert parsed.finished is True
    assert parsed.valid_lines == 2
    assert parsed.invalid_line is None
    assert len(parsed.items) == 1
    assert parsed.items[0]["cn"] == "constexpr objects"


def test_jsonl_relation_cue_alias_is_preserved():
    task = ExtractionTask(
        chunk_id="c1",
        doc_id="d1",
        corpus_id="corp1",
        text="constexpr functions produce compile-time results.",
    )
    parsed = _parse_jsonl_lines(
        "\n".join(
            [
                '{"t":"e","cn":"constexpr functions","sf":"constexpr functions","et":"Concept","cf":0.91}',
                '{"t":"r","sub":"constexpr functions","pred":"produces","obj":"compile-time results","ok":"literal","cf":0.86,"ev":"produce compile-time results","cue":"produce"}',
                '{"t":"x"}',
            ]
        )
    )

    result = _parse_jsonl_items(parsed.items, task, 0.0)

    assert result is not None
    assert result.relations[0].relation_cue == "produce"


def test_raw_jsonl_debug_logging_is_flag_guarded(caplog):
    raw = '{"t":"e","cn":"constexpr objects"}\n{"t":"x"}'

    with caplog.at_level(logging.DEBUG, logger="services.ghost_b"):
        _debug_log_raw_jsonl_lines(
            raw,
            chunk_id="c1",
            lane=2,
            attempt=3,
            enabled=False,
        )
    assert "GHOST B raw JSONL" not in caplog.text

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="services.ghost_b"):
        _debug_log_raw_jsonl_lines(
            raw,
            chunk_id="c1",
            lane=2,
            attempt=3,
            enabled=True,
        )

    assert "GHOST B raw JSONL chunk_id=c1 lane=2 attempt=3 line=1" in caplog.text
    assert '"cn":"constexpr objects"' in caplog.text
    assert "line=2" in caplog.text


def test_jsonl_continuation_merge_dedupes_and_preserves_facts():
    task = ExtractionTask(
        chunk_id="c1",
        doc_id="d1",
        corpus_id="corp1",
        text="constexpr objects are initialized during compilation.",
    )
    first = _parse_jsonl_lines(
        '{"t":"e","cn":"constexpr objects","sf":"constexpr objects","et":"Concept","cf":0.92}'
    )
    second = _parse_jsonl_lines(
        "\n".join(
            [
                '{"t":"e","cn":"constexpr objects","sf":"constexpr objects","et":"Concept","cf":0.92}',
                '{"t":"f","sub":"constexpr objects","ft":"property","pn":"initialized_when","val":"during compilation","cf":0.82,"ev":"during compilation"}',
                '{"t":"x"}',
            ]
        )
    )

    merged = _merge_jsonl_items(first.items, second.items)
    result = _parse_jsonl_items(
        merged,
        task,
        0.0,
        enable_facts=True,
        max_facts=5,
    )

    assert second.finished is True
    assert len(merged) == 2
    assert result is not None
    assert len(result.entities) == 1
    assert len(result.facts) == 1
    assert result.facts[0].property_name == "initialized_when"


def test_repair_prompt_carries_accepted_jsonl_without_open_ended_continuation():
    previous = [
        {"t": "e", "cn": "a", "sf": "A", "et": "Concept", "cf": 0.9},
        {"t": "e", "cn": "b", "sf": "B", "et": "Concept", "cf": 0.9},
        {"t": "e", "cn": "c", "sf": "C", "et": "Concept", "cf": 0.9},
        {"t": "e", "cn": "d", "sf": "D", "et": "Concept", "cf": 0.9},
    ]

    prompt = build_rescue_prompt(
        accepted_items=previous,
        failure_reason="jsonl_incomplete",
        chunk_id="c1",
        doc_id="d1",
        corpus_id="corp1",
        text="sample",
        max_entities=8,
        max_relations=8,
        max_total_lines=16,
    )

    assert "REPAIR MODE" in prompt
    assert "one repair attempt" in prompt
    assert "Do not repeat accepted lines" in prompt
    assert "Accepted valid JSONL lines already kept" in prompt
    assert "Continue with the next" not in prompt
    assert '"cn":"a"' in prompt
    assert '"cn":"b"' in prompt
    assert '"cn":"d"' in prompt
    assert '{"t":"x"}' in prompt


@pytest.mark.asyncio
async def test_extraction_repairs_once_and_merges_accepted_primary_lines(monkeypatch):
    calls: list[dict] = []
    responses = [
        {
            "usage": {"total_tokens": 2400, "prompt_tokens": 1200, "completion_tokens": 1200},
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {
                        "content": '{"t":"e","cn":"alpha","sf":"Alpha","et":"Concept","cf":0.95}'
                    },
                }
            ],
        },
        {
            "usage": {"total_tokens": 180, "prompt_tokens": 110, "completion_tokens": 70},
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": "\n".join(
                            [
                                '{"t":"e","cn":"beta","sf":"Beta","et":"Concept","cf":0.9}',
                                '{"t":"r","sub":"alpha","pred":"produces","obj":"beta","ok":"entity","cf":0.86,"ev":"Alpha produces Beta","cue":"produces"}',
                                '{"t":"x"}',
                            ]
                        )
                    },
                }
            ],
        },
    ]

    class FakeResponse:
        def __init__(self, body: dict):
            self._body = body

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._body

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json, headers):
            calls.append(json)
            return FakeResponse(responses[len(calls) - 1])

    fake_settings = SimpleNamespace(
        ENTITY_CONFIDENCE_THRESHOLD=0.0,
        SCHEMA_INLINE_LIMIT=30,
        SCHEMA_RETRIEVAL_TOP_K=10,
        EXTRACTION_MAX_TOKENS=1200,
        EXTRACTION_OUTPUT_MODE="jsonl",
        EXTRACTION_JSON_OBJECT_MAX_ENTITIES_PER_CHUNK=8,
        EXTRACTION_JSON_OBJECT_MAX_RELATIONS_PER_CHUNK=12,
        EXTRACTION_JSON_OBJECT_MAX_FACTS_PER_CHUNK=4,
        EXTRACTION_EVIDENCE_MAX_CHARS=120,
        EXTRACTION_FACT_VALUE_MAX_CHARS=160,
        EXTRACTION_RESCUE_MAX_TOKENS=900,
        EXTRACTION_ENABLE_FACTS=True,
        EXTRACTION_MAX_FACTS_PER_CHUNK=5,
        EXTRACTION_JSONL_MAX_CALLS=8,
        EXTRACTION_FOREGROUND_MAX_CALLS=2,
        EXTRACTION_JSONL_DEBUG_RAW=False,
        EXTRACTION_MAX_INPUT_TOKENS=700,
        EXTRACTION_MAX_TOTAL_LINES=20,
        EXTRACTION_RESCUE_MAX_TOTAL_LINES=16,
        EXTRACTION_MAX_ENTITIES_PER_CHUNK=14,
        EXTRACTION_MAX_RELATIONS_PER_CHUNK=20,
        EXTRACTION_RESCUE_MAX_ENTITIES_PER_CHUNK=8,
        EXTRACTION_RESCUE_MAX_RELATIONS_PER_CHUNK=8,
        LITELLM_MASTER_KEY="test-key",
        LITELLM_URL="http://litellm",
        DEFAULT_COMPLETION_MODEL="test-model",
        EXTRACTION_MAX_CONCURRENT=1,
        EXTRACTION_GLOBAL_MAX_CONCURRENT=180,
        EXTRACTION_FAILURE_PAUSE_PERCENT=100.0,
        EXTRACTION_FAILURE_PAUSE_MIN_CHUNKS=20,
    )
    monkeypatch.setattr(ghost_b, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(ghost_b.httpx, "AsyncClient", FakeClient)

    report = await ghost_b.extract_entities(
        [
            ExtractionTask(
                chunk_id="c1",
                doc_id="d1",
                corpus_id="corp1",
                text="Alpha produces Beta.",
            )
        ],
        pool=[{
            "model": "test-model",
            "base_url": None,
            "api_key": None,
            "max_concurrent": 1,
            "extra_params": {},
        }],
        return_report=True,
        enable_facts=False,
    )

    assert len(calls) == 2
    assert calls[0]["max_tokens"] == 1200
    assert '"t":"f"' not in calls[0]["messages"][1]["content"]
    assert "max 20 total extraction item lines" in calls[0]["messages"][1]["content"]
    assert calls[1]["max_tokens"] == 900
    assert "REPAIR MODE" in calls[1]["messages"][1]["content"]
    assert "Accepted valid JSONL lines already kept" in calls[1]["messages"][1]["content"]
    assert '"cn":"alpha"' in calls[1]["messages"][1]["content"]
    assert "Do not repeat accepted lines" in calls[1]["messages"][1]["content"]
    assert "one repair attempt" in calls[1]["messages"][1]["content"]
    assert "max 8 entities" in calls[1]["messages"][1]["content"]
    assert report.results
    result = report.results[0]
    assert [e.canonical_name for e in result.entities] == ["alpha", "beta"]
    assert len(result.relations) == 1
    assert result.relations[0].relation_cue == "produces"
    assert report.metrics["attempt_count"] == 2
    assert report.metrics["completion_tokens"] == 1270


@pytest.mark.asyncio
async def test_deepseek_auto_uses_jsonl_payload_on_primary(monkeypatch):
    calls: list[dict] = []
    content = "\n".join(
        [
            '{"t":"e","cn":"alpha","sf":"Alpha","et":"Concept","cf":0.95}',
            '{"t":"x"}',
        ]
    )

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "usage": {"total_tokens": 120, "prompt_tokens": 90, "completion_tokens": 30},
                "choices": [
                    {"finish_reason": "stop", "message": {"content": content}}
                ],
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json, headers):
            calls.append(json)
            return FakeResponse()

    fake_settings = SimpleNamespace(
        ENTITY_CONFIDENCE_THRESHOLD=0.0,
        SCHEMA_INLINE_LIMIT=30,
        SCHEMA_RETRIEVAL_TOP_K=10,
        EXTRACTION_MAX_TOKENS=1200,
        EXTRACTION_OUTPUT_MODE="auto",
        EXTRACTION_JSON_OBJECT_MAX_ENTITIES_PER_CHUNK=8,
        EXTRACTION_JSON_OBJECT_MAX_RELATIONS_PER_CHUNK=12,
        EXTRACTION_JSON_OBJECT_MAX_FACTS_PER_CHUNK=4,
        EXTRACTION_EVIDENCE_MAX_CHARS=120,
        EXTRACTION_FACT_VALUE_MAX_CHARS=160,
        EXTRACTION_RESCUE_MAX_TOKENS=900,
        EXTRACTION_ENABLE_FACTS=False,
        EXTRACTION_MAX_FACTS_PER_CHUNK=5,
        EXTRACTION_JSONL_MAX_CALLS=2,
        EXTRACTION_FOREGROUND_MAX_CALLS=2,
        EXTRACTION_JSONL_DEBUG_RAW=False,
        EXTRACTION_MAX_INPUT_TOKENS=700,
        EXTRACTION_MAX_TOTAL_LINES=20,
        EXTRACTION_RESCUE_MAX_TOTAL_LINES=16,
        EXTRACTION_MAX_ENTITIES_PER_CHUNK=14,
        EXTRACTION_MAX_RELATIONS_PER_CHUNK=20,
        EXTRACTION_RESCUE_MAX_ENTITIES_PER_CHUNK=8,
        EXTRACTION_RESCUE_MAX_RELATIONS_PER_CHUNK=8,
        LITELLM_MASTER_KEY="test-key",
        LITELLM_URL="http://litellm",
        DEFAULT_COMPLETION_MODEL="test-model",
        EXTRACTION_MAX_CONCURRENT=1,
        EXTRACTION_GLOBAL_MAX_CONCURRENT=180,
        EXTRACTION_FAILURE_PAUSE_PERCENT=100.0,
        EXTRACTION_FAILURE_PAUSE_MIN_CHUNKS=20,
    )
    monkeypatch.setattr(ghost_b, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(ghost_b.httpx, "AsyncClient", FakeClient)

    report = await ghost_b.extract_entities(
        [
            ExtractionTask(
                chunk_id="c1",
                doc_id="d1",
                corpus_id="corp1",
                text="Alpha is a compact test concept.",
            )
        ],
        pool=[{
            "model": "deepseek/deepseek-v4-flash",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": None,
            "max_concurrent": 1,
            "extra_params": {},
        }],
        return_report=True,
        enable_facts=False,
    )

    assert len(calls) == 1
    assert "response_format" not in calls[0]
    assert "Output EXACTLY one JSON object per line" in calls[0]["messages"][0]["content"]
    assert "Output JSONL only" in calls[0]["messages"][1]["content"]
    assert "max 14 entities" in calls[0]["messages"][1]["content"]
    assert "max 20 relations" in calls[0]["messages"][1]["content"]
    assert '{"t":"x"}' in calls[0]["messages"][1]["content"]
    assert report.results and report.results[0].entities[0].canonical_name == "alpha"
    assert report.metrics["attempt_count"] == 1
    assert report.metrics["completion_tokens"] == 30


@pytest.mark.asyncio
async def test_global_extraction_budget_limits_simultaneous_provider_calls(monkeypatch):
    active = 0
    max_active = 0

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "usage": {"total_tokens": 20, "prompt_tokens": 10, "completion_tokens": 10},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"t":"e","cn":"alpha","sf":"Alpha","et":"concept","cf":0.95}\n{"t":"x"}'
                        },
                    }
                ],
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json, headers):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return FakeResponse()

    fake_settings = SimpleNamespace(
        ENTITY_CONFIDENCE_THRESHOLD=0.0,
        SCHEMA_INLINE_LIMIT=30,
        SCHEMA_RETRIEVAL_TOP_K=10,
        EXTRACTION_MAX_TOKENS=1200,
        EXTRACTION_OUTPUT_MODE="jsonl",
        EXTRACTION_JSON_OBJECT_MAX_ENTITIES_PER_CHUNK=8,
        EXTRACTION_JSON_OBJECT_MAX_RELATIONS_PER_CHUNK=12,
        EXTRACTION_JSON_OBJECT_MAX_FACTS_PER_CHUNK=4,
        EXTRACTION_EVIDENCE_MAX_CHARS=120,
        EXTRACTION_FACT_VALUE_MAX_CHARS=160,
        EXTRACTION_RESCUE_MAX_TOKENS=900,
        EXTRACTION_ENABLE_FACTS=False,
        EXTRACTION_MAX_FACTS_PER_CHUNK=5,
        EXTRACTION_JSONL_MAX_CALLS=2,
        EXTRACTION_FOREGROUND_MAX_CALLS=2,
        EXTRACTION_JSONL_DEBUG_RAW=False,
        EXTRACTION_MAX_INPUT_TOKENS=700,
        EXTRACTION_MAX_TOTAL_LINES=20,
        EXTRACTION_RESCUE_MAX_TOTAL_LINES=16,
        EXTRACTION_MAX_ENTITIES_PER_CHUNK=14,
        EXTRACTION_MAX_RELATIONS_PER_CHUNK=20,
        EXTRACTION_RESCUE_MAX_ENTITIES_PER_CHUNK=8,
        EXTRACTION_RESCUE_MAX_RELATIONS_PER_CHUNK=8,
        LITELLM_MASTER_KEY="test-key",
        LITELLM_URL="http://litellm",
        DEFAULT_COMPLETION_MODEL="test-model",
        EXTRACTION_MAX_CONCURRENT=3,
        EXTRACTION_GLOBAL_MAX_CONCURRENT=1,
        EXTRACTION_FAILURE_PAUSE_PERCENT=100.0,
        EXTRACTION_FAILURE_PAUSE_MIN_CHUNKS=20,
    )
    monkeypatch.setattr(ghost_b, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(ghost_b.httpx, "AsyncClient", FakeClient)

    report = await ghost_b.extract_entities(
        [
            ExtractionTask(chunk_id=f"c{i}", doc_id="d1", corpus_id="corp1", text="Alpha works.")
            for i in range(3)
        ],
        pool=[{
            "model": "test-model",
            "base_url": None,
            "api_key": None,
            "max_concurrent": 3,
            "extra_params": {},
        }],
        return_report=True,
        enable_facts=False,
    )

    assert max_active == 1
    assert len(report.results) == 3
    assert report.metrics["attempt_count"] == 3


@pytest.mark.asyncio
async def test_model_lane_concurrency_is_shared_across_concurrent_documents(monkeypatch):
    active = 0
    max_active = 0

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "usage": {"total_tokens": 20, "prompt_tokens": 10, "completion_tokens": 10},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"t":"e","cn":"alpha","sf":"Alpha","et":"concept","cf":0.95}\n{"t":"x"}'
                        },
                    }
                ],
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json, headers):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return FakeResponse()

    fake_settings = SimpleNamespace(
        ENTITY_CONFIDENCE_THRESHOLD=0.0,
        SCHEMA_INLINE_LIMIT=30,
        SCHEMA_RETRIEVAL_TOP_K=10,
        EXTRACTION_MAX_TOKENS=1200,
        EXTRACTION_OUTPUT_MODE="jsonl",
        EXTRACTION_JSON_OBJECT_MAX_ENTITIES_PER_CHUNK=8,
        EXTRACTION_JSON_OBJECT_MAX_RELATIONS_PER_CHUNK=12,
        EXTRACTION_JSON_OBJECT_MAX_FACTS_PER_CHUNK=4,
        EXTRACTION_EVIDENCE_MAX_CHARS=120,
        EXTRACTION_FACT_VALUE_MAX_CHARS=160,
        EXTRACTION_RESCUE_MAX_TOKENS=900,
        EXTRACTION_ENABLE_FACTS=False,
        EXTRACTION_MAX_FACTS_PER_CHUNK=5,
        EXTRACTION_JSONL_MAX_CALLS=2,
        EXTRACTION_FOREGROUND_MAX_CALLS=2,
        EXTRACTION_JSONL_DEBUG_RAW=False,
        EXTRACTION_MAX_INPUT_TOKENS=700,
        EXTRACTION_MAX_TOTAL_LINES=20,
        EXTRACTION_RESCUE_MAX_TOTAL_LINES=16,
        EXTRACTION_MAX_ENTITIES_PER_CHUNK=14,
        EXTRACTION_MAX_RELATIONS_PER_CHUNK=20,
        EXTRACTION_RESCUE_MAX_ENTITIES_PER_CHUNK=8,
        EXTRACTION_RESCUE_MAX_RELATIONS_PER_CHUNK=8,
        LITELLM_MASTER_KEY="test-key",
        LITELLM_URL="http://litellm",
        DEFAULT_COMPLETION_MODEL="test-model",
        EXTRACTION_MAX_CONCURRENT=3,
        EXTRACTION_GLOBAL_MAX_CONCURRENT=180,
        EXTRACTION_FAILURE_PAUSE_PERCENT=100.0,
        EXTRACTION_FAILURE_PAUSE_MIN_CHUNKS=20,
    )
    monkeypatch.setattr(ghost_b, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(ghost_b.httpx, "AsyncClient", FakeClient)

    pool = [{
        "model": "test-model",
        "base_url": None,
        "api_key": None,
        "max_concurrent": 3,
        "extra_params": {},
    }]

    async def run_doc(doc_id: str):
        return await ghost_b.extract_entities(
            [
                ExtractionTask(
                    chunk_id=f"{doc_id}-c{i}",
                    doc_id=doc_id,
                    corpus_id="corp1",
                    text="Alpha works.",
                )
                for i in range(3)
            ],
            pool=pool,
            return_report=True,
            enable_facts=False,
        )

    reports = await asyncio.gather(run_doc("d1"), run_doc("d2"))

    assert max_active == 3
    assert sum(len(report.results) for report in reports) == 6
    assert sum(report.metrics["attempt_count"] for report in reports) == 6


@pytest.mark.asyncio
async def test_jsonl_repair_is_hard_clamped_to_one_retry(monkeypatch):
    calls = 0

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "usage": {"total_tokens": 20, "prompt_tokens": 10, "completion_tokens": 10},
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "not jsonl"}}
                ],
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json, headers):
            nonlocal calls
            calls += 1
            return FakeResponse()

    fake_settings = SimpleNamespace(
        ENTITY_CONFIDENCE_THRESHOLD=0.0,
        SCHEMA_INLINE_LIMIT=30,
        SCHEMA_RETRIEVAL_TOP_K=10,
        EXTRACTION_MAX_TOKENS=1200,
        EXTRACTION_OUTPUT_MODE="jsonl",
        EXTRACTION_JSON_OBJECT_MAX_ENTITIES_PER_CHUNK=8,
        EXTRACTION_JSON_OBJECT_MAX_RELATIONS_PER_CHUNK=12,
        EXTRACTION_JSON_OBJECT_MAX_FACTS_PER_CHUNK=4,
        EXTRACTION_EVIDENCE_MAX_CHARS=120,
        EXTRACTION_FACT_VALUE_MAX_CHARS=160,
        EXTRACTION_RESCUE_MAX_TOKENS=900,
        EXTRACTION_ENABLE_FACTS=False,
        EXTRACTION_MAX_FACTS_PER_CHUNK=5,
        EXTRACTION_JSONL_MAX_CALLS=8,
        EXTRACTION_FOREGROUND_MAX_CALLS=3,
        EXTRACTION_JSONL_DEBUG_RAW=False,
        EXTRACTION_MAX_INPUT_TOKENS=700,
        EXTRACTION_MAX_TOTAL_LINES=20,
        EXTRACTION_RESCUE_MAX_TOTAL_LINES=16,
        EXTRACTION_MAX_ENTITIES_PER_CHUNK=14,
        EXTRACTION_MAX_RELATIONS_PER_CHUNK=20,
        EXTRACTION_RESCUE_MAX_ENTITIES_PER_CHUNK=8,
        EXTRACTION_RESCUE_MAX_RELATIONS_PER_CHUNK=8,
        LITELLM_MASTER_KEY="test-key",
        LITELLM_URL="http://litellm",
        DEFAULT_COMPLETION_MODEL="test-model",
        EXTRACTION_MAX_CONCURRENT=1,
        EXTRACTION_GLOBAL_MAX_CONCURRENT=180,
        EXTRACTION_FAILURE_PAUSE_PERCENT=100.0,
        EXTRACTION_FAILURE_PAUSE_MIN_CHUNKS=20,
    )
    monkeypatch.setattr(ghost_b, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(ghost_b.httpx, "AsyncClient", FakeClient)

    report = await ghost_b.extract_entities(
        [ExtractionTask(chunk_id="c1", doc_id="d1", corpus_id="corp1", text="bad")],
        pool=[{
            "model": "test-model",
            "base_url": None,
            "api_key": None,
            "max_concurrent": 1,
            "extra_params": {},
        }],
        return_report=True,
        enable_facts=False,
    )

    assert calls == 2
    assert len(report.results) == 0
    assert len(report.failures) == 1
    assert report.failures[0].attempts == 2


@pytest.mark.asyncio
async def test_failure_budget_pauses_remaining_foreground_queue(monkeypatch):
    calls = 0
    audit_events: list[dict] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "usage": {"total_tokens": 20, "prompt_tokens": 10, "completion_tokens": 10},
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "not jsonl"}}
                ],
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json, headers):
            nonlocal calls
            calls += 1
            return FakeResponse()

    fake_settings = SimpleNamespace(
        ENTITY_CONFIDENCE_THRESHOLD=0.0,
        SCHEMA_INLINE_LIMIT=30,
        SCHEMA_RETRIEVAL_TOP_K=10,
        EXTRACTION_MAX_TOKENS=1200,
        EXTRACTION_OUTPUT_MODE="jsonl",
        EXTRACTION_JSON_OBJECT_MAX_ENTITIES_PER_CHUNK=8,
        EXTRACTION_JSON_OBJECT_MAX_RELATIONS_PER_CHUNK=12,
        EXTRACTION_JSON_OBJECT_MAX_FACTS_PER_CHUNK=4,
        EXTRACTION_EVIDENCE_MAX_CHARS=120,
        EXTRACTION_FACT_VALUE_MAX_CHARS=160,
        EXTRACTION_RESCUE_MAX_TOKENS=900,
        EXTRACTION_ENABLE_FACTS=False,
        EXTRACTION_MAX_FACTS_PER_CHUNK=5,
        EXTRACTION_JSONL_MAX_CALLS=1,
        EXTRACTION_FOREGROUND_MAX_CALLS=1,
        EXTRACTION_JSONL_DEBUG_RAW=False,
        EXTRACTION_MAX_INPUT_TOKENS=700,
        EXTRACTION_MAX_TOTAL_LINES=20,
        EXTRACTION_RESCUE_MAX_TOTAL_LINES=16,
        EXTRACTION_MAX_ENTITIES_PER_CHUNK=14,
        EXTRACTION_MAX_RELATIONS_PER_CHUNK=20,
        EXTRACTION_RESCUE_MAX_ENTITIES_PER_CHUNK=8,
        EXTRACTION_RESCUE_MAX_RELATIONS_PER_CHUNK=8,
        LITELLM_MASTER_KEY="test-key",
        LITELLM_URL="http://litellm",
        DEFAULT_COMPLETION_MODEL="test-model",
        EXTRACTION_MAX_CONCURRENT=1,
        EXTRACTION_GLOBAL_MAX_CONCURRENT=180,
        EXTRACTION_FAILURE_PAUSE_PERCENT=50.0,
        EXTRACTION_FAILURE_PAUSE_MIN_CHUNKS=2,
    )
    monkeypatch.setattr(ghost_b, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(ghost_b.httpx, "AsyncClient", FakeClient)

    async def _audit(event: dict) -> None:
        audit_events.append(event)

    report = await ghost_b.extract_entities(
        [
            ExtractionTask(chunk_id=f"c{i}", doc_id="d1", corpus_id="corp1", text="bad")
            for i in range(5)
        ],
        pool=[{
            "model": "test-model",
            "base_url": None,
            "api_key": None,
            "max_concurrent": 1,
            "extra_params": {},
        }],
        return_report=True,
        enable_facts=False,
        audit_event_sink=_audit,
        audit_run_id="run-test",
    )

    assert calls == 2
    assert len(report.results) == 0
    assert len(report.failures) == 5
    assert report.metrics["error_counts"]["parse_error"] == 2
    assert report.metrics["error_counts"]["failure_budget_exceeded"] == 3
    failed_events = [
        event for event in audit_events
        if event["event"] == "ghost_b_attempt_failed"
    ]
    budget_events = [
        event for event in audit_events
        if event["event"] == "ghost_b_failure_budget_tripped"
    ]
    assert len(failed_events) == 2
    assert len(budget_events) == 1
    assert failed_events[0]["run_id"] == "run-test"
    assert failed_events[0]["error_type"] == "parse_error"
    assert failed_events[0]["raw"]["sha256"]
    assert failed_events[0]["raw"]["first"] == "not jsonl"
    assert failed_events[0]["jsonl"]["valid_lines"] == 0
    assert budget_events[0]["failed"] == 2
    assert budget_events[0]["queued_remaining"] == 3


def test_parse_facts_is_backward_compatible_when_missing():
    task = ExtractionTask(
        chunk_id="c1",
        doc_id="d1",
        corpus_id="corp1",
        text="The cache timeout is 30 seconds.",
    )
    raw = json.dumps({
        "schema_version": "polymath.extract.v1",
        "chunk_id": "c1",
        "doc_id": "d1",
        "corpus_id": "corp1",
        "entities": [
            {
                "canonical_name": "cache timeout",
                "surface_form": "cache timeout",
                "entity_type": "Concept",
                "confidence": 0.9,
            }
        ],
        "relations": [],
    })

    result = _parse(raw, task, 0.0, enable_facts=True, max_facts=5)

    assert result is not None
    assert result.facts == []
    assert result.fact_drop_count == 0


def test_parse_facts_keeps_valid_and_drops_invalid_without_failing_chunk():
    task = ExtractionTask(
        chunk_id="c1",
        doc_id="d1",
        corpus_id="corp1",
        text="Orders over $500 require manager approval. Status is active.",
    )
    raw = json.dumps({
        "schema_version": "polymath.extract.v1",
        "chunk_id": "c1",
        "doc_id": "d1",
        "corpus_id": "corp1",
        "entities": [
            {
                "canonical_name": "orders",
                "surface_form": "Orders",
                "entity_type": "Concept",
                "confidence": 0.9,
            },
            {
                "canonical_name": "manager approval",
                "surface_form": "manager approval",
                "entity_type": "Rule",
                "confidence": 0.9,
            },
        ],
        "relations": [],
        "facts": [
            {
                "subject": "orders",
                "fact_type": "threshold",
                "property_name": "approval_threshold",
                "value": "500",
                "unit": "USD",
                "condition": "order total exceeds 500",
                "confidence": 0.95,
                "evidence_phrase": "Orders over $500 require manager approval",
            },
            {
                "subject": "not in entities",
                "fact_type": "status",
                "property_name": "status",
                "value": "active",
                "confidence": 0.9,
                "evidence_phrase": "Status is active",
            },
            {
                "subject": "orders",
                "fact_type": "threshold",
                "property_name": "approval_threshold",
                "value": "750",
                "confidence": 0.9,
                "evidence_phrase": "not actually in source",
            },
        ],
    })

    result = _parse(raw, task, 0.0, enable_facts=True, max_facts=5)

    assert result is not None
    assert len(result.facts) == 1
    assert result.facts[0] == FactItem(
        subject="orders",
        fact_type="threshold",
        property_name="approval_threshold",
        value="500",
        unit="USD",
        condition="order total exceeds 500",
        confidence=0.95,
        evidence_phrase="Orders over $500 require manager approval",
    )
    assert result.fact_drop_count == 2


def test_schema_strict_legacy_values_deserialize():
    # Pre-migration Mongo docs may carry schema_strict="off" or "hard".
    # The Literal is intentionally left wide (soft|off|hard) so those records
    # still deserialize; the lifespan migration rewrites them to "soft".
    for legacy in ("soft", "off", "hard"):
        cfg = IngestionConfig.model_validate({"schema_strict": legacy})
        assert cfg.schema_strict == legacy


def test_domain_range_remaps_invalid_relation_softly():
    ctx = SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )
    entities = [
        EntityItem("campaign", "campaign", "Event", 0.9),
        EntityItem("sam", "Sam", "Person", 0.9),
    ]
    relations = [RelationItem("campaign", "depends_on", "sam", "entity", 0.9)]

    _, out_relations, counters = _apply_schema(entities, relations, ctx)

    assert out_relations[0].predicate == "related_to"
    assert out_relations[0].source_predicate == "depends_on"
    assert out_relations[0].validation_status == "domain_range_mismatch"
    assert counters["domain_range_remap_count"] == 1


def test_endpoint_completion_adds_missing_relation_entities():
    ctx = SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )
    entities = [EntityItem("unsloth", "Unsloth", "Product", 0.9)]
    relations = [
        RelationItem(
            "model",
            "uses",
            "unsloth",
            "entity",
            0.9,
            evidence_phrase="Fine-tune on RTX 3090 using Unsloth.",
        )
    ]

    out_entities, out_relations, counters = _apply_schema(entities, relations, ctx)

    assert any(e.canonical_name == "model" for e in out_entities)
    assert out_relations[0].predicate == "uses"
    assert counters["endpoint_completion_count"] == 1
    assert counters["domain_range_remap_count"] == 0


def test_domain_range_warning_preserves_evidence_backed_predicate():
    ctx = SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )
    entities = [
        EntityItem("ghost memory architecture", "Ghost Memory Architecture", "Concept", 0.9),
        EntityItem("context injection", "Context Injection", "Method", 0.9),
    ]
    relations = [
        RelationItem(
            "ghost memory architecture",
            "uses",
            "context injection",
            "entity",
            0.9,
            evidence_phrase="Context injection -- not RAG search.",
        )
    ]

    _, out_relations, counters = _apply_schema(entities, relations, ctx)

    assert out_relations[0].predicate == "uses"
    assert out_relations[0].source_predicate == "uses"
    assert "domain_range_warn" in (out_relations[0].validation_status or "")
    assert counters["domain_range_warn_count"] == 1
    assert counters["domain_range_remap_count"] == 0


def test_evidence_cue_repair_flips_stored_in_language():
    ctx = SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )
    entities = [
        EntityItem("events", "events", "Artifact", 0.8),
        EntityItem("sqlite", "SQLite", "Product", 0.9),
    ]
    relations = [
        RelationItem(
            "events",
            "stores",
            "sqlite",
            "entity",
            0.9,
            evidence_phrase="events are stored in SQLite",
        )
    ]

    _, out_relations, counters = _apply_schema(entities, relations, ctx)

    assert out_relations[0].subject == "sqlite"
    assert out_relations[0].predicate == "stores"
    assert out_relations[0].object == "events"
    assert "evidence_cue_repair" in (out_relations[0].validation_status or "")
    assert counters["evidence_cue_repair_count"] == 1


def test_relation_aliases_normalize_before_soft_remap():
    ctx = SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )
    entities = [
        EntityItem("snapshot json", "Snapshot JSON", "Document", 0.9),
        EntityItem("sqlite", "SQLite", "Product", 0.9),
    ]
    relations = [
        RelationItem("snapshot json", "stored_in", "sqlite", "entity", 0.9)
    ]

    _, out_relations, counters = _apply_schema(entities, relations, ctx)

    assert out_relations[0].subject == "sqlite"
    assert out_relations[0].predicate == "stores"
    assert out_relations[0].object == "snapshot json"
    assert out_relations[0].source_predicate == "stored_in"
    assert counters["relation_remap_count"] == 0


def test_relation_alias_normalizer_reports_direction():
    assert normalize_relation_predicate_alias("used_by") == ("uses", True)
    assert normalize_relation_predicate_alias("provides") == ("supports", False)


def test_domain_range_keeps_valid_relation():
    ctx = SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )
    entities = [
        EntityItem("sam", "Sam", "Person", 0.9),
        EntityItem("openai", "OpenAI", "Organization", 0.9),
    ]
    relations = [RelationItem("sam", "works_for", "openai", "entity", 0.9)]

    _, out_relations, counters = _apply_schema(entities, relations, ctx)

    assert out_relations[0].predicate == "works_for"
    assert counters["domain_range_remap_count"] == 0


def test_relation_family_groups_raw_predicates():
    assert relation_family_for_predicate("part_of") == "Structural"
    assert relation_family_for_predicate("uses") == "Operational"
    assert relation_family_for_predicate("runs_on") == "Operational"
    assert relation_family_for_predicate("trained_on") == "Operational"
    assert relation_family_for_predicate("represents") == "Referential"
    assert relation_family_for_predicate("references") == "Referential"
    assert relation_family_for_predicate("causes") == "Causal"
    assert relation_family_for_predicate("contradicts") == "Conflict"
    assert relation_family_for_predicate("related_to") == "WeakAssociation"


def test_related_to_refinement_uses_deterministic_facets():
    subject = {
        "canonical_name": "the council",
        "primary_entity_type": "Product",
        "domain_type": "Feature",
    }
    model_object = {
        "canonical_name": "local model",
        "primary_entity_type": "Product",
        "domain_type": "AIModel",
        "object_kind": "Model",
    }
    constraint_object = {
        "canonical_name": "message limit",
        "primary_entity_type": "Rule",
        "domain_type": "Constraint",
    }
    vague_object = {
        "canonical_name": "ambiguous idea",
        "primary_entity_type": "Concept",
    }

    assert refine_related_to_predicate("related_to", subject, model_object) == "uses"
    assert (
        refine_related_to_predicate("related_to", subject, constraint_object)
        == "depends_on"
    )
    assert (
        refine_related_to_predicate("related_to", subject, vague_object)
        == "implements"
    )
    assert refine_related_to_predicate("uses", subject, model_object) == "uses"


def test_related_to_refinement_recovers_source_predicate_with_evidence():
    subject = {
        "canonical_name": "module",
        "primary_entity_type": "Concept",
    }
    target = {
        "canonical_name": "monetization model",
        "primary_entity_type": "Concept",
    }

    assert (
        refine_related_to_predicate(
            "related_to",
            subject,
            target,
            source_predicate="part_of",
            evidence_phrase="module purchase, additional book generation, and annual me book",
        )
        == "part_of"
    )


def test_related_to_refinement_uses_evidence_and_source_predicate():
    subject = {
        "canonical_name": "tensorflow lite",
        "primary_entity_type": "Product",
        "domain_type": "AIModel",
        "object_kind": "Model",
    }
    device = {
        "canonical_name": "android device",
        "primary_entity_type": "Product",
        "domain_type": "Device",
        "object_kind": "Device",
    }
    dataset = {
        "canonical_name": "fashion mnist",
        "primary_entity_type": "Document",
        "domain_type": "Dataset",
        "object_kind": "Dataset",
    }

    assert (
        refine_related_to_predicate(
            "related_to",
            subject,
            device,
            evidence_phrase="TensorFlow Lite runs on Android devices for on-device inference.",
        )
        == "runs_on"
    )
    assert (
        refine_related_to_predicate(
            "related_to",
            subject,
            dataset,
            source_predicate="trained_on",
        )
        == "trained_on"
    )


def test_entity_aliases_canonicalize_before_id_generation():
    assert canonicalize_entity_name("Open AI Inc.") == "openai"
    assert entity_id_from_name("Open AI Inc.", "Organization") == "entity:openai"


def test_entity_id_collapses_type_splits():
    ids = {
        entity_id_from_name("PVector", "Product"),
        entity_id_from_name("p-vector", "Method"),
        entity_id_from_name("p vector", "Concept"),
    }
    assert ids == {"entity:pvector"}


def test_primary_entity_type_uses_curated_override_then_observed_types():
    assert resolve_primary_entity_type(
        "pvector", ["Product", "Method", "Concept"]
    ) == "Artifact"
    assert resolve_primary_entity_type(
        "OpenAI", ["Concept", "Organization"]
    ) == "Organization"


def test_object_kind_facets_infer_library():
    assert resolve_facets("Box2D", "Artifact") == {
        "object_kind": "Library",
        "object_kind_parent": "CodeArtifact",
        "object_kind_root": "Artifact",
    }
    assert resolve_facets("Box2D", "Product") == {
        "object_kind": "Library",
        "object_kind_parent": "CodeArtifact",
        "object_kind_root": "Product",
    }


def test_object_kind_facets_infer_report():
    assert resolve_facets("Architecture_Feasibility_Report.docx", "Document") == {
        "object_kind": "Report",
        "object_kind_parent": "Document",
        "object_kind_root": "Document",
    }


def test_canonical_family_resolution():
    assert resolve_canonical_family("PBox2D") == "physics_simulation"
    assert resolve_canonical_family("gen ai") == "generative_ai"
    assert resolve_canonical_family("user profile extraction") == "identity_extraction"
    assert resolve_canonical_family("PVector") == "creative_coding"
    assert resolve_canonical_family("The Council") == "council_chat"
    assert resolve_canonical_family("Book JSON") == "book_generation"


def test_domain_type_facets_infer_prd_roles():
    assert resolve_domain_type("The Council", "Product") == {
        "domain_type": "Feature",
        "domain_type_parent": "ProductBehavior",
        "domain_type_root": "PRD",
    }
    assert resolve_domain_type("Book JSON", "Document") == {
        "domain_type": "DataObject",
        "domain_type_parent": "ProductData",
        "domain_type_root": "PRD",
    }
    assert resolve_domain_type("Gate C", "Rule") == {
        "domain_type": "Constraint",
        "domain_type_parent": "ProductRule",
        "domain_type_root": "PRD",
    }


def test_ontology_metadata_combines_facets_family_and_version():
    assert resolve_ontology_metadata("Box2D", "Product") == {
        "object_kind": "Library",
        "object_kind_parent": "CodeArtifact",
        "object_kind_root": "Product",
        "canonical_family": "physics_simulation",
        "ontology_version": ONTOLOGY_VERSION,
    }

    assert resolve_ontology_metadata("The Council", "Product") == {
        "object_kind": "App",
        "object_kind_parent": "Product",
        "object_kind_root": "Product",
        "domain_type": "Feature",
        "domain_type_parent": "ProductBehavior",
        "domain_type_root": "PRD",
        "canonical_family": "council_chat",
        "ontology_version": ONTOLOGY_VERSION,
    }


# ──────────────────────────────────────────────────────────────────────────
# Phase B — evidence-phrase validation gate
#
# `_validate_evidence(phrase, chunk_text)` powers the Phase B drop logic in
# `_parse`. The runtime path is integration-tested via a real ingest, but
# the cheap surface tests below pin the normalization rules so a future
# change to the regex / casefold / strip behavior surfaces here first.
# ──────────────────────────────────────────────────────────────────────────


def test_validate_evidence_exact_substring():
    chunk = "OpenAI is affiliated with Microsoft. GPT-4 runs on Microsoft Azure."
    assert _validate_evidence("GPT-4 runs on Microsoft Azure", chunk) is True


def test_validate_evidence_lowercase_match():
    chunk = "OpenAI is affiliated with Microsoft."
    assert _validate_evidence("openai is AFFILIATED with microsoft", chunk) is True


def test_validate_evidence_collapsed_whitespace():
    chunk = "GPT-4   runs\non\tMicrosoft  Azure"
    assert _validate_evidence("GPT-4 runs on Microsoft Azure", chunk) is True


def test_validate_evidence_phrase_with_extra_whitespace():
    chunk = "ChatGPT depends on GPT-4 for its responses."
    assert _validate_evidence("  ChatGPT  depends   on   GPT-4  ", chunk) is True


def test_validate_evidence_paraphrase_rejected():
    chunk = "OpenAI was founded in San Francisco in December 2015."
    # Same idea, different words → must be rejected.
    assert _validate_evidence("OpenAI started in SF in late 2015", chunk) is False


def test_validate_evidence_empty_phrase_rejected():
    chunk = "Sam Altman works for OpenAI as CEO."
    assert _validate_evidence("", chunk) is False
    assert _validate_evidence(None, chunk) is False
    assert _validate_evidence("   \n\t  ", chunk) is False


def test_validate_evidence_substring_not_found_rejected():
    chunk = "Microsoft owns a substantial stake in OpenAI."
    # The phrase is plausible English but doesn't appear in the chunk.
    assert _validate_evidence("Microsoft acquired OpenAI", chunk) is False


def test_validate_evidence_chunk_text_empty_rejected():
    # Without source text we can't verify anything — fail closed.
    assert _validate_evidence("anything", "") is False
    assert _validate_evidence("anything", None or "") is False
