from backend.scripts.run_openie_relation_kill_switch import (
    EndpointSpan,
    align_argument,
    compile_predicate,
    precision_reduce,
    surface_pair_candidates,
)
from backend.services.extraction.graphify_relations import PredicateCompiler


def test_argument_alignment_requires_one_exact_endpoint() -> None:
    endpoints = ["Harbor", "SQLite", "Industrial Sensor Telemetry"]
    assert align_argument("the Harbor platform", endpoints) == (
        "Harbor",
        "unique_endpoint_subspan",
    )
    assert align_argument("Harbor and SQLite", endpoints)[0] is None
    assert align_argument("it", endpoints)[0] is None


def test_predicate_compile_uses_frozen_synonyms_and_abstains() -> None:
    compiler = PredicateCompiler()
    mapped, reason = compile_predicate(
        compiler,
        surface="depends on",
        subject_type="Software",
        object_type="Software",
        subject_name="Harbor",
        object_name="SQLite",
        source="test",
    )
    assert mapped == "depends_on"
    assert reason.startswith("mapped:") or reason.startswith("review:endpoint_signature:")

    unmapped, reason = compile_predicate(
        compiler,
        surface="powers",
        subject_type="Software",
        object_type="Concept",
        subject_name="Qdrant",
        object_name="semantic retrieval",
        source="test",
    )
    assert unmapped is None
    assert "ambiguous_or_unmapped" in reason


def test_surface_recovery_handles_noun_tagged_technical_verb() -> None:
    text = "MongoDB stores canonical documents."
    spans = [
        EndpointSpan("MongoDB", 0, 7, "Software"),
        EndpointSpan("canonical documents", 15, 34, "Document"),
    ]
    assert surface_pair_candidates(spans, text) == {
        ("MongoDB", "stores", "canonical documents")
    }


def test_surface_recovery_carries_subject_across_coordinated_verb() -> None:
    text = "The API consumes JSON documents and produces normalized records."
    spans = [
        EndpointSpan("API", 4, 7, "Software"),
        EndpointSpan("JSON documents", 17, 31, "Document"),
        EndpointSpan("normalized records", 45, 63, "Artifact"),
    ]
    assert surface_pair_candidates(spans, text) == {
        ("API", "consumes", "JSON documents"),
        ("API", "and produces", "normalized records"),
    }


def test_precision_reducer_prefers_corroborated_direction() -> None:
    forward = ("microsoft", "owns", "github")
    reverse = ("github", "owns", "microsoft")
    assert precision_reduce(
        syntax={forward, reverse},
        openie={forward},
        surface={forward, reverse},
        promoted_names={"microsoft", "github"},
        alias_pairs=set(),
    ) == {forward}
