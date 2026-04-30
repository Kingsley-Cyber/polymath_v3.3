from pathlib import Path

import pytest

from services.ghost_b import (
    ExtractionTask,
    SchemaContext,
    UNIVERSAL_ENTITY_GLOSSES,
    UNIVERSAL_ENTITY_SCHEMA,
    UNIVERSAL_RELATION_GLOSSES,
    UNIVERSAL_RELATION_SCHEMA,
    _parse,
    build_user_prompt,
    normalize_relation_predicate_alias,
)
from services.ontology import (
    entity_gloss_map,
    entity_type_names,
    load_ontology,
    object_kind_compatible,
    ontology_version,
    relation_domain_range_map,
    relation_family_map,
    relation_gloss_map,
    relation_type_names,
)
from services.graph.neo4j_writer import ONTOLOGY_VERSION, relation_family_for_predicate


REPAIR_PREDICATES = {
    "measures",
    "defined_in",
    "follows_distribution",
    "tests",
    "applied_to",
    "illustrated_in",
    "parameter_of",
    "equivalent_to",
}
EXPANSION_PREDICATES = {
    "embodies",
    "symbolizes",
    "influences",
    "motivates",
    "struggles_with",
    "reinforces",
    "undermines",
    "frames_as",
    "conceals",
    "leverages",
}


def test_ontology_contract_drives_ghost_b_public_constants():
    data = load_ontology()

    assert ontology_version() == data["version"]
    assert UNIVERSAL_ENTITY_SCHEMA == entity_type_names()
    assert UNIVERSAL_RELATION_SCHEMA == relation_type_names()
    assert UNIVERSAL_ENTITY_GLOSSES == entity_gloss_map()
    assert UNIVERSAL_RELATION_GLOSSES == relation_gloss_map()
    assert ONTOLOGY_VERSION == data["version"]


def test_ontology_contract_has_relation_governance_for_every_predicate():
    relations = load_ontology()["relations"]
    names = [item["name"] for item in relations]

    assert names[-1] == "related_to"
    assert len(names) == len(set(names))

    for item in relations:
        assert item["family"]
        assert item["gloss"]
        assert item["definition"]
        assert item["canonical_direction"]
        assert "discrimination_tests" in item
        assert item["governance_scope"] in {
            "core",
            "related_to_repair",
            "ontology_expansion",
        }


def test_ontology_governance_scopes_classify_new_predicates():
    scopes = {
        item["name"]: item["governance_scope"]
        for item in load_ontology()["relations"]
    }

    assert {name for name, scope in scopes.items() if scope == "related_to_repair"} == REPAIR_PREDICATES
    assert {name for name, scope in scopes.items() if scope == "ontology_expansion"} == EXPANSION_PREDICATES
    assert scopes["related_to"] == "core"


def test_ontology_domain_range_and_families_are_machine_readable():
    domain_range = relation_domain_range_map()
    families = relation_family_map()

    assert domain_range["works_for"] == {
        "subject_types": ["Person"],
        "object_types": ["Organization"],
    }
    assert domain_range["depends_on"]["subject_types"]
    assert domain_range["measures"]["subject_types"]
    assert families["related_to"] == "WeakAssociation"
    assert relation_family_for_predicate("supports") == "Operational"
    assert relation_family_for_predicate("measures") == "Analytical"
    assert relation_family_for_predicate("embodies") == "Interpretive"


def test_prompt_includes_predicate_decision_rules():
    ctx = SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )
    prompt = build_user_prompt(
        chunk_id="c1",
        doc_id="d1",
        corpus_id="corp1",
        text="TensorFlow Lite runs on Android and uses ML Kit.",
        schema=ctx,
    )

    assert "Predicate decision rules:" in prompt
    assert "depends_on: The source requires the target to function" in prompt
    assert "If X can still work without Y but currently consumes it, use uses" in prompt
    assert "alternative_predicates_considered" in prompt
    assert "predicate_confidence" in prompt


def test_relation_aliases_load_from_ontology_contract():
    assert normalize_relation_predicate_alias("stored_in") == ("stores", True)
    assert normalize_relation_predicate_alias("powered_by") == ("runs_on", False)
    assert normalize_relation_predicate_alias("Uses") == ("uses", False)
    assert normalize_relation_predicate_alias("part-of") == ("part_of", False)
    assert normalize_relation_predicate_alias("measured_by") == ("measures", True)
    assert normalize_relation_predicate_alias("applied to") == ("applied_to", False)
    assert normalize_relation_predicate_alias("depicted-in") == ("illustrated_in", False)
    assert normalize_relation_predicate_alias("same_as") == ("equivalent_to", False)
    assert normalize_relation_predicate_alias("motivated_by") == ("motivates", True)
    assert normalize_relation_predicate_alias("uses strategically") == ("leverages", False)
    assert normalize_relation_predicate_alias("evaluates") == ("evaluates", False)
    assert normalize_relation_predicate_alias("defined_by") == ("defined_by", False)
    assert normalize_relation_predicate_alias("checks") == ("checks", False)
    assert normalize_relation_predicate_alias("scores") == ("scores", False)
    assert normalize_relation_predicate_alias("estimates") == ("estimates", False)


def test_graph_view_has_color_for_each_relation_family():
    graph_view = (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "components"
        / "chat"
        / "GraphView.tsx"
    )
    if not graph_view.exists():
        pytest.skip("frontend source is not present in this backend-only runtime")
    source = graph_view.read_text(encoding="utf-8")

    for family in sorted(set(relation_family_map().values()) | {"Discourse"}):
        assert f"{family}:" in source


def test_object_kind_compatibility_waits_for_required_facets():
    assert (
        object_kind_compatible(
            "runs_on",
            {"object_kind": "Model"},
            {},
        )
        is None
    )
    assert (
        object_kind_compatible(
            "runs_on",
            {"object_kind": "Model"},
            {"object_kind": "Report"},
        )
        is False
    )


def test_low_predicate_confidence_demotes_to_related_to_with_source_predicate():
    ctx = SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )
    raw = """
    {
      "schema_version": "polymath.extract.v1",
      "chunk_id": "c1",
      "doc_id": "d1",
      "corpus_id": "corp1",
      "entities": [
        {"canonical_name": "app", "surface_form": "app", "entity_type": "Product", "confidence": 0.9},
        {"canonical_name": "ml kit", "surface_form": "ML Kit", "entity_type": "Product", "confidence": 0.9}
      ],
      "relations": [
        {
          "subject": "app",
          "predicate": "depends_on",
          "object": "ml kit",
          "object_kind": "entity",
          "confidence": 0.9,
          "predicate_confidence": 0.42,
          "extraction_confidence": 0.9,
          "alternative_predicates_considered": ["uses"],
          "rejection_reasoning": "depends_on was too strong",
          "evidence_phrase": "the app integrates ML Kit"
        }
      ]
    }
    """

    parsed = _parse(
        raw,
        ExtractionTask("c1", "d1", "corp1", "text"),
        threshold=0.5,
        schema=ctx,
    )

    assert parsed is not None
    relation = parsed.relations[0]
    assert relation.predicate == "related_to"
    assert relation.source_predicate == "depends_on"
    assert "low_predicate_confidence" in (relation.validation_status or "")
    assert "review_required" in (relation.validation_status or "")
    assert relation.review_status == "needs_backfill"
    assert relation.predicate_confidence == 0.42
    assert relation.extraction_confidence == 0.9
    assert relation.alternative_predicates_considered == ["uses"]


def test_low_extraction_confidence_skips_relation_but_keeps_candidate_fact():
    ctx = SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )
    raw = """
    {
      "schema_version": "polymath.extract.v1",
      "chunk_id": "c1",
      "doc_id": "d1",
      "corpus_id": "corp1",
      "entities": [
        {"canonical_name": "app", "surface_form": "app", "entity_type": "Product", "confidence": 0.9},
        {"canonical_name": "ml kit", "surface_form": "ML Kit", "entity_type": "Product", "confidence": 0.9}
      ],
      "candidate_facts": [
        {
          "atomic_fact": "The app may use ML Kit.",
          "candidate_subject": "app",
          "candidate_predicate": "uses",
          "candidate_object": "ml kit",
          "object_kind": "entity",
          "predicate_confidence": 0.9,
          "extraction_confidence": 0.31,
          "evidence_phrase": "possibly integrates ML Kit"
        }
      ],
      "relations": []
    }
    """

    parsed = _parse(
        raw,
        ExtractionTask("c1", "d1", "corp1", "text"),
        threshold=0.5,
        schema=ctx,
    )

    assert parsed is not None
    assert len(parsed.candidate_facts) == 1
    assert parsed.relations == []
