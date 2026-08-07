from types import SimpleNamespace
import ast
from pathlib import Path

import pytest

from services.extraction.graphify_normalization import normalize_document
from services.extraction.graphify_openie import (
    OpenIEUnit,
    TripletExtractCPUProvider,
    _reset_openie_provider_for_tests,
    run_openie_extraction,
)


class FakeLink:
    def to_dict(self):
        return {
            "asserter": "Maya",
            "verb": "deny",
            "construction": "ccomp",
            "speech_act": False,
            "negated": False,
            "cluster": None,
            "cluster_canonical": None,
        }


class FakeExtractor:
    def extract_triplet_objects(self, text):
        return [SimpleNamespace(
            subject="Harbor",
            relation="depends on",
            object="SQLite",
            confidence=0.9,
            from_clause_split=True,
            from_entailment=False,
            entailment_score=1.0,
            asserter_chain=["Maya"],
            asserter_links=[FakeLink()],
        )]


class EmptyExtractor:
    def extract_triplet_objects(self, text):
        return []


@pytest.fixture(autouse=True)
def reset_provider():
    _reset_openie_provider_for_tests()
    yield
    _reset_openie_provider_for_tests()


def test_raw_openie_preserves_attribution_and_evidence() -> None:
    text = "Maya denied that Harbor depends on SQLite."
    document = normalize_document("doc", text)
    unit = OpenIEUnit("unit:1", "doc", 0, len(text), text, True)
    provider = TripletExtractCPUProvider(loader=FakeExtractor)
    output = run_openie_extraction([document], [unit], provider)
    proposition = output.propositions[0]
    assert proposition.asserter_chain == ("Maya",)
    assert proposition.asserter_links[0].verb == "deny"
    assert proposition.evidence_text == text
    assert output.report["one_extract_call_per_eligible_unit"] is True
    assert output.report["exact_evidence_alignment"] is True
    assert output.report["no_graph_writes"] is True
    assert provider.load_count == 1


def test_ineligible_units_are_persisted_upstream_but_not_extracted() -> None:
    text = "No relation here."
    document = normalize_document("doc", text)
    unit = OpenIEUnit("unit:1", "doc", 0, len(text), text, False)
    output = run_openie_extraction(
        [document], [unit], TripletExtractCPUProvider(loader=FakeExtractor)
    )
    assert output.propositions == ()
    assert output.report["input_units"] == 1
    assert output.report["eligible_units"] == 0
    assert output.report["extract_calls"] == 0


def test_openie_stage_has_no_database_imports() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "services/extraction/graphify_openie.py"
    ).read_text(encoding="utf-8")
    imports = {
        alias.name.split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        (node.module or "").split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
    )
    assert imports.isdisjoint({"neo4j", "pymongo", "motor", "qdrant_client"})


def test_strict_surface_recovery_restores_explicit_technical_svo() -> None:
    text = "MongoDB stores canonical documents while Qdrant remains available."
    document = normalize_document("doc", text)
    unit = OpenIEUnit("unit:1", "doc", 0, len(text), text, True)
    output = run_openie_extraction(
        [document], [unit], TripletExtractCPUProvider(loader=EmptyExtractor),
    )
    assert [(item.subject, item.relation, item.object) for item in output.propositions] == [
        ("MongoDB", "stores", "canonical documents"),
    ]
    assert output.report["deterministic_surface_recoveries"] == 1


def test_attribution_cue_always_routes_through_triplet_extract() -> None:
    text = "Maya reported that Harbor uses SQLite."
    document = normalize_document("doc", text)
    unit = OpenIEUnit("unit:1", "doc", 0, len(text), text, True)
    output = run_openie_extraction(
        [document], [unit], TripletExtractCPUProvider(loader=FakeExtractor),
    )
    assert output.report["extract_calls"] == 1
    assert output.report["deterministic_surface_recoveries"] == 0
    assert output.propositions[0].asserter_chain == ("Maya",)


def test_strict_recovery_treats_markdown_emphasis_as_formatting() -> None:
    text = "Northstar Labs owns the Canonical Event Dataset, and the **Data Retention Guide** defines the retention policy."
    document = normalize_document("doc", text)
    unit = OpenIEUnit("unit:1", "doc", 0, len(text), text, True)
    output = run_openie_extraction(
        [document], [unit], TripletExtractCPUProvider(loader=EmptyExtractor),
    )
    assert ("Data Retention Guide", "defines", "retention policy") in {
        (item.subject, item.relation, item.object) for item in output.propositions
    }
