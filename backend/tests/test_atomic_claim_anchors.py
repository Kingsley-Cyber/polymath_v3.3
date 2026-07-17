from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from models.claim_record import ClaimArgumentV1, ClaimCompilationV1, ClaimRecordV1
from models.hash_taxonomy import namespace_hash
from models.schemas import SourceChunk
from models.semantic_artifacts import domain_hash, make_evidence_ref
from models.semantic_digest_claim_input import CompiledChildCandidateExportV1
from services.context_manager import context_manager
from services import chat_orchestrator as chat_orchestrator_module
from services.ingestion.claim_compiler import claim_compiler_recipe_hash
from services.ingestion.semantic_digest_claim_inputs import (
    PARSER_VERSION,
    SPACY_MODEL,
    _expected_observation_bundle_id,
    document_source_version_id,
    materialize_candidate_row,
)
from services.ingestion.semantic_observations import (
    load_normalization_identity,
    local_extraction_recipe_hash,
    semantic_observation_recipe_hash,
)
from services.retriever.atomic_claim_anchors import (
    attach_atomic_claim_anchors,
    maybe_attach_atomic_claim_anchors,
)


def _document():
    return {
        "corpus_id": "corpus:test",
        "doc_id": "doc:test",
        "source_identity": {"content_sha256": "a" * 64},
    }


def _child(text: str = "Feedback changes the operating baseline."):
    return {
        "corpus_id": "corpus:test",
        "doc_id": "doc:test",
        "chunk_id": "child:test",
        "text": text,
    }


def _row(text: str = "Feedback changes the operating baseline.") -> dict:
    document = _document()
    child = _child(text)
    source_version = document_source_version_id(document)
    evidence = make_evidence_ref(
        text=text,
        start=0,
        end=len(text),
        source_version_id=source_version,
        hierarchy_node_id=child["chunk_id"],
    )
    observation_recipe = semantic_observation_recipe_hash(
        parser_id=SPACY_MODEL,
        parser_version=PARSER_VERSION,
    )
    compiler_recipe = claim_compiler_recipe_hash(observation_recipe)
    claim = ClaimRecordV1(
        schema_version="claim_record.v1",
        claim_id="claim:test",
        document_id="doc:test",
        child_id="child:test",
        proposition_text=text,
        canonical_proposition="feedback changes operating baseline",
        claim_type="causal",
        predicate_observation_id="predicate-observation:test",
        predicate_id="predicate:test",
        predicate_surface="changes",
        predicate_lemma="change",
        normalized_predicate="INFLUENCES",
        typing_status="typed",
        arguments=[
            ClaimArgumentV1(
                role="subject",
                filler_kind="span_observation",
                filler_ref="span:feedback",
                span_observation_id="span:feedback",
                surface="Feedback",
                start_char=0,
                end_char=8,
                evidence_sentence_id=evidence.evidence_ref_id,
            ),
            ClaimArgumentV1(
                role="object",
                filler_kind="span_observation",
                filler_ref="span:baseline",
                span_observation_id="span:baseline",
                surface="operating baseline",
                start_char=21,
                end_char=39,
                evidence_sentence_id=evidence.evidence_ref_id,
            ),
        ],
        polarity="positive",
        modality="asserted",
        assertion_mode="reported",
        conditions=[],
        exceptions=[],
        temporal_cues=[],
        evidence_sentence_ids=[evidence.evidence_ref_id],
        source_relation_ids=[],
        scope_hash=namespace_hash("scope", {"child": "child:test"}),
        knowledge_status="candidate",
        validation_status="candidate",
    )
    compilation = ClaimCompilationV1(
        schema_version="claim_compilation.v1",
        document_id="doc:test",
        child_id="child:test",
        claims=[claim],
        links=[],
        rejected_relation_ids=[],
        unresolved_coreference_spans=[],
        skipped_predicate_observation_ids=[],
        same_sentence_repeated_claim_count=0,
        cross_sentence_candidate_count=0,
        cross_sentence_rejected_count=0,
        compiler_recipe_hash=compiler_recipe,
    )
    source_text_hash = domain_hash("normalized-text", text)
    candidate = CompiledChildCandidateExportV1(
        schema_version="semantic_digest_claim_compilation_export.v1",
        corpus_id="corpus:test",
        document_id="doc:test",
        source_version_id=source_version,
        child_id="child:test",
        source_text_hash=source_text_hash,
        observation_bundle_id=_expected_observation_bundle_id(
            source_version_id_=source_version,
            child_id="child:test",
            source_text_hash=source_text_hash,
            observation_recipe_hash=observation_recipe,
        ),
        observation_recipe_hash=observation_recipe,
        local_extraction_recipe_hash=local_extraction_recipe_hash(),
        normalization_registry_hash=load_normalization_identity()["hash"],
        compiler_version="claim_compiler.v2",
        compiler_recipe_hash=compiler_recipe,
        spacy_library_version="3.8.14",
        spacy_model="en_core_web_sm",
        spacy_model_version="3.8.0",
        parser_version=PARSER_VERSION,
        evidence_refs=[evidence],
        compilation=compilation,
    )
    row = materialize_candidate_row(
        candidate,
        corpus_id="corpus:test",
        document=document,
        child=child,
        run_id="run:test",
        now=dt.datetime(2026, 7, 16, tzinfo=dt.timezone.utc),
    ).model_dump(mode="python", by_alias=True)
    row["_current_children"] = [child]
    row["_current_documents"] = [document]
    return row


class _AggregateCursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length=None):
        return [dict(row) for row in self.rows]


class _Collection:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0
        self.pipeline = None

    def aggregate(self, pipeline):
        self.calls += 1
        self.pipeline = pipeline
        return _AggregateCursor(self.rows)


class _DB:
    def __init__(self, rows):
        self.collection = _Collection(rows)

    def __getitem__(self, name):
        assert name == "semantic_digest_claim_compilations"
        return self.collection


def _source() -> SourceChunk:
    return SourceChunk(
        chunk_id="child:test",
        parent_id="parent:test",
        doc_id="doc:test",
        corpus_id="corpus:test",
        text="A hydrated parent may contain more text than the exact child.",
        score=0.91,
        source_tier="tier_a",
        doc_name="Feedback.md",
        heading_path=["Mechanisms", "Feedback"],
    )


@pytest.mark.asyncio
async def test_claim_anchor_uses_one_bounded_aggregate_and_preserves_sources():
    db = _DB([_row()])
    source = _source()
    enriched, diagnostics = await attach_atomic_claim_anchors(
        db,
        [source],
        query="How does feedback change the baseline?",
        per_source=2,
        total=8,
    )

    assert db.collection.calls == 1
    assert diagnostics["aggregate_calls"] == 1
    assert diagnostics["anchors_attached"] == 1
    assert [item.chunk_id for item in enriched] == [source.chunk_id]
    assert enriched[0].text == source.text
    assert enriched[0].score == source.score
    anchor = enriched[0].metadata["atomic_claim_anchors"][0]
    assert anchor["exact_sentence"] == "Feedback changes the operating baseline."
    assert anchor["start"] == 0 and anchor["end"] == 40
    assert anchor["knowledge_status"] == "candidate_exact_sentence_anchor"


@pytest.mark.asyncio
async def test_motor_naive_bson_datetimes_are_restored_before_strict_parsing():
    def _strip_timezone(value):
        if isinstance(value, dt.datetime):
            return value.replace(tzinfo=None)
        if isinstance(value, dict):
            return {key: _strip_timezone(item) for key, item in value.items()}
        if isinstance(value, list):
            return [_strip_timezone(item) for item in value]
        return value

    db = _DB([_strip_timezone(_row())])
    enriched, diagnostics = await attach_atomic_claim_anchors(
        db,
        [_source()],
        query="How does feedback change the baseline?",
        per_source=2,
        total=8,
    )

    assert diagnostics["rows_valid"] == 1
    assert diagnostics["rows_rejected"] == 0
    assert diagnostics["anchors_attached"] == 1
    assert enriched[0].metadata["atomic_claim_anchors"][0]["claim_id"] == "claim:test"


@pytest.mark.asyncio
async def test_zero_overlap_and_stale_source_fail_closed_without_anchor():
    db = _DB([_row()])
    unchanged, diagnostics = await attach_atomic_claim_anchors(
        db,
        [_source()],
        query="lighthouse winter chronology",
        per_source=2,
        total=8,
    )
    assert unchanged[0].metadata == {}
    assert diagnostics["anchors_attached"] == 0

    stale = _row()
    stale["_current_children"][0]["text"] = "Changed source text."
    stale_db = _DB([stale])
    unchanged, diagnostics = await attach_atomic_claim_anchors(
        stale_db,
        [_source()],
        query="feedback baseline",
        per_source=2,
        total=8,
    )
    assert unchanged[0].metadata == {}
    assert diagnostics["rows_rejected"] == 1


@pytest.mark.asyncio
async def test_long_query_requires_more_than_one_generic_token_overlap():
    db = _DB([_row()])
    unchanged, diagnostics = await attach_atomic_claim_anchors(
        db,
        [_source()],
        query="How does lighthouse feedback relate to winter chronology?",
        per_source=2,
        total=8,
    )

    assert unchanged[0].metadata == {}
    assert diagnostics["anchors_attached"] == 0


@pytest.mark.asyncio
async def test_ambiguous_compilation_and_oversized_sentence_fail_closed():
    duplicated = _row()
    db = _DB([duplicated, duplicated])
    unchanged, diagnostics = await attach_atomic_claim_anchors(
        db,
        [_source()],
        query="feedback baseline",
        per_source=2,
        total=8,
    )
    assert unchanged[0].metadata == {}
    assert diagnostics["ambiguous_compilations"] == 1

    long_text = "Feedback changes the operating baseline. " + ("x" * 501)
    long_db = _DB([_row(long_text)])
    unchanged, diagnostics = await attach_atomic_claim_anchors(
        long_db,
        [_source()],
        query="feedback baseline",
        per_source=2,
        total=8,
    )
    assert unchanged[0].metadata == {}
    assert diagnostics["anchors_attached"] == 0


@pytest.mark.asyncio
async def test_flag_off_makes_zero_database_calls(monkeypatch):
    db = _DB([_row()])
    monkeypatch.setattr(
        "services.retriever.atomic_claim_anchors.get_settings",
        lambda: SimpleNamespace(ATOMIC_CLAIM_ANCHORS_ENABLED=False),
    )
    sources, diagnostics = await maybe_attach_atomic_claim_anchors(
        db, [_source()], query="feedback baseline"
    )
    assert sources[0] == _source()
    assert diagnostics == {"enabled": False, "aggregate_calls": 0}
    assert db.collection.calls == 0


def test_anchor_renders_in_legacy_and_waterfall_without_exposing_internal_ids(
    monkeypatch,
):
    monkeypatch.setattr(
        "services.context_manager.get_settings",
        lambda: SimpleNamespace(
            ATOMIC_CLAIM_ANCHORS_ENABLED=True,
            ATOMIC_CLAIM_ANCHORS_PER_SOURCE=2,
            ATOMIC_CLAIM_ANCHORS_TOTAL=8,
        ),
    )
    source = _source().model_copy(
        update={
            "metadata": {
                "atomic_claim_anchors": [
                    {
                        "claim_text": "feedback changes operating baseline",
                        "exact_sentence": "Feedback changes the operating baseline.",
                        "start": 0,
                        "end": 40,
                        "claim_id": "claim:secret",
                    }
                ]
            }
        }
    )
    legacy = context_manager.build_augmented_prompt("feedback baseline", [source])
    waterfall = context_manager.build_augmented_prompt(
        "feedback baseline",
        [source],
        packet={
            "items": [
                {
                    "kind": "child",
                    "ref_id": "child:test",
                    "doc_id": "doc:test",
                    "text": source.text,
                }
            ]
        },
    )
    for prompt in (legacy, waterfall):
        assert "<atomic_claim_anchors>" in prompt
        assert "Feedback changes the operating baseline." in prompt
        assert "claim:secret" not in prompt


def test_waterfall_omitted_source_does_not_render_its_anchor(monkeypatch):
    monkeypatch.setattr(
        "services.context_manager.get_settings",
        lambda: SimpleNamespace(
            ATOMIC_CLAIM_ANCHORS_ENABLED=True,
            ATOMIC_CLAIM_ANCHORS_PER_SOURCE=2,
            ATOMIC_CLAIM_ANCHORS_TOTAL=8,
        ),
    )
    source = _source().model_copy(
        update={
            "metadata": {
                "atomic_claim_anchors": [
                    {
                        "claim_text": "omitted claim",
                        "exact_sentence": "Omitted source sentence.",
                    }
                ]
            }
        }
    )

    prompt = context_manager.build_augmented_prompt(
        "feedback baseline",
        [source],
        packet={
            "items": [
                {
                    "kind": "child",
                    "ref_id": "child:other",
                    "doc_id": "doc:test",
                    "text": "A different rendered fragment.",
                }
            ]
        },
    )

    assert "<atomic_claim_anchors>" not in prompt
    assert "Omitted source sentence." not in prompt


def test_flag_off_ignores_injected_anchor_metadata_byte_identically(monkeypatch):
    monkeypatch.setattr(
        "services.context_manager.get_settings",
        lambda: SimpleNamespace(
            ATOMIC_CLAIM_ANCHORS_ENABLED=False,
            ATOMIC_CLAIM_ANCHORS_PER_SOURCE=2,
            ATOMIC_CLAIM_ANCHORS_TOTAL=8,
        ),
    )
    injected = _source().model_copy(
        update={
            "metadata": {
                "atomic_claim_anchors": [
                    {
                        "claim_text": "must remain dark",
                        "exact_sentence": "This stale metadata must not render.",
                    }
                ]
            }
        }
    )
    clean = injected.model_copy(update={"metadata": {}})

    assert context_manager.build_augmented_prompt(
        "feedback baseline", [injected]
    ) == context_manager.build_augmented_prompt("feedback baseline", [clean])

    monkeypatch.setattr(
        chat_orchestrator_module,
        "settings",
        SimpleNamespace(ATOMIC_CLAIM_ANCHORS_ENABLED=False),
    )
    assert chat_orchestrator_module._compact_source_previews(
        [injected]
    ) == chat_orchestrator_module._compact_source_previews([clean])


def test_prompt_rendering_obeys_runtime_anchor_caps(monkeypatch):
    monkeypatch.setattr(
        "services.context_manager.get_settings",
        lambda: SimpleNamespace(
            ATOMIC_CLAIM_ANCHORS_ENABLED=True,
            ATOMIC_CLAIM_ANCHORS_PER_SOURCE=1,
            ATOMIC_CLAIM_ANCHORS_TOTAL=1,
        ),
    )
    anchors = [
        {
            "claim_text": "first claim",
            "exact_sentence": "First exact sentence.",
        },
        {
            "claim_text": "second claim",
            "exact_sentence": "Second exact sentence.",
        },
    ]
    source = _source().model_copy(
        update={"metadata": {"atomic_claim_anchors": anchors}}
    )

    prompt = context_manager.build_augmented_prompt("feedback baseline", [source])

    assert "First exact sentence." in prompt
    assert "Second exact sentence." not in prompt


def test_budgeted_prompt_reports_surviving_anchor_render_count(monkeypatch):
    monkeypatch.setattr(
        "services.context_manager.get_settings",
        lambda: SimpleNamespace(
            ATOMIC_CLAIM_ANCHORS_ENABLED=True,
            ATOMIC_CLAIM_ANCHORS_PER_SOURCE=2,
            ATOMIC_CLAIM_ANCHORS_TOTAL=8,
        ),
    )
    source = _source().model_copy(
        update={
            "metadata": {
                "atomic_claim_anchors": [
                    {
                        "claim_text": "feedback changes operating baseline",
                        "exact_sentence": "Feedback changes the operating baseline.",
                    }
                ]
            }
        }
    )

    prompt, metadata = chat_orchestrator_module._build_budgeted_augmented_prompt(
        query="feedback baseline",
        sources=[source],
        facts=[],
        corpus_ids=["corpus:test"],
        reasoning_mode=None,
        reasoning_blend=None,
        active_skills=None,
        analysis=None,
        decoration=[],
        model="unknown-test-model",
    )

    assert "<atomic_claim_anchors>" in prompt
    assert metadata["atomic_claim_anchor_render_count"] == 1
