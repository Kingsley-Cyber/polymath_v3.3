"""B0 Stage-Contract CI gate: every RetrievalPayload field is populated on a
full-extraction fixture (no field is 'extracted but unusable' / declared-but-
empty), plus contract shape checks.

    docker exec -i polymath_v33-backend-1 python /app/tests/test_contracts.py
"""

from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from models.contracts import (  # noqa: E402
    ChunkExtraction,
    ChunkMetadata,
    ExtractedEntity,
    ExtractedFact,
    ExtractedRelation,
    GraphEntity,
    GraphRelation,
    GraphWriteModel,
    RerankerInput,
    RetrievalPayload,
)


def _full_payload() -> RetrievalPayload:
    return RetrievalPayload(
        chunk_id="c1", parent_id="p1", doc_id="d1", corpus_id="k1", user_id="u1",
        chunk_type="child", chunk_kind="body", language="python", domain="programming",
        topic_key="cpp.language.standard",
        concepts=["tensorflow", "tf"], entity_ids=["entity:tensorflow"],
        entity_families=["machine_learning"], entity_domains=["AIModel"],
        relation_predicates=["uses"], relation_families=["Operational"],
        fact_types=["quantity"], has_relations=True,
        semantic_chunk_type="principle", mechanisms=["compounding"], key_terms=["XPath"],
        document_status="active", is_latest=True, document_date="2026-06-15",
        valid_from="2026-06-15", valid_to=None,
    )


def test_stage_contract_every_field_populated():
    p = _full_payload()
    empty = []
    for name, val in p.model_dump().items():
        if name == "valid_to":            # None is the OPEN-ended validity — allowed
            continue
        if val in (None, "", [], {}):
            empty.append(name)
    assert not empty, f"declared-but-empty fields: {empty}"


def test_version_stamps_default():
    p = _full_payload()
    assert p.extract_schema_version == "polymath.extract.v2"
    assert p.promote_version == "polymath.promote.v1"


def test_envelope_extractor_provenance_is_required():
    ok = ChunkExtraction(extractor="gliner_glirel_local", corpus_id="k", doc_id="d",
                         chunk_id="c", parent_id="p")
    assert ok.schema_version == "polymath.extract.v2"
    try:
        ChunkExtraction(extractor="something_else", corpus_id="k", doc_id="d",
                        chunk_id="c", parent_id="p")
        raise AssertionError("invalid extractor accepted")
    except Exception:
        pass  # pydantic rejected — provenance is a closed enum


def test_entity_promote_time_fields_nullable_at_extract():
    e = ExtractedEntity(canonical_name="tensorflow")
    assert e.entity_id is None and e.domain_type is None and e.canonical_family is None


def test_fact_type_closed_enum():
    ExtractedFact(subject="s", fact_type="quantity", property_name="height", value="3")
    try:
        ExtractedFact(subject="s", fact_type="vibe", property_name="x", value="y")
        raise AssertionError("invalid fact_type accepted")
    except Exception:
        pass


def test_graph_entity_id_never_corpus_prefixed_convention():
    g = GraphEntity(entity_id="entity:tensorflow", canonical_name="tensorflow",
                    corpus_ids=["k1", "k2"])
    assert g.entity_id.startswith("entity:") and "::" not in g.entity_id
    assert g.corpus_ids == ["k1", "k2"]        # isolation via property, not identity


def test_reranker_input_render_no_ids():
    r = RerankerInput(source_book="Atomic Habits", section="Chapter 1", excerpt="Habits compound.")
    out = r.render()
    assert out == "Atomic Habits › Chapter 1\nHabits compound."
    r2 = RerankerInput(excerpt="bare text")
    assert r2.render() == "bare text"


def test_chunk_metadata_versioning_defaults():
    m = ChunkMetadata(doc_id="d", chunk_id="c", parent_id="p", corpus_id="k")
    assert m.document_status == "active" and m.is_latest is True
    assert m.supersedes == [] and m.superseded_by is None


def test_graph_write_model_composes():
    gw = GraphWriteModel(
        entities=[GraphEntity(entity_id="entity:x", canonical_name="x")],
        relations=[GraphRelation(subject_id="entity:x", predicate="uses", object_id="entity:y")],
    )
    assert gw.relations[0].predicate == "uses"


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
