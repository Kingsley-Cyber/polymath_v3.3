"""Compiler: the acceptance policy is multi-signal, never a bare score."""

from dataclasses import dataclass, field

from services.extraction import extraction_compiler as comp


@dataclass
class _Ent:
    canonical_name: str
    entity_type: str
    confidence: float = 0.9
    surface_form: str = ""


@dataclass
class _Rel:
    subject: str
    predicate: str
    object: str
    confidence: float = 0.9
    evidence_phrase: str = "some verbatim evidence"


@dataclass
class _Result:
    chunk_id: str = "c1"
    entities: list = field(default_factory=list)
    relations: list = field(default_factory=list)


def test_untyped_spans_never_become_graph_nodes():
    r = _Result(entities=[_Ent("full listing", "other"), _Ent("harbor", "Software")])
    _facts, report = comp.compile_extraction([r])
    assert report.entities_kept == 1
    assert report.entity_drop_reasons["untyped_by_encoder"] == 1


def test_relation_with_unresolved_endpoint_is_rejected_not_stored():
    # the 'hook part_of creative system' class: neither endpoint is an entity
    r = _Result(relations=[_Rel("hook", "part_of", "creative system")])
    facts, report = comp.compile_extraction([r])
    assert facts == []
    assert report.reject_reasons["unresolved_endpoint"] == 1


def test_missing_evidence_is_rejected():
    r = _Result(
        entities=[_Ent("harbor", "Software"), _Ent("mongodb", "Software")],
        relations=[_Rel("harbor", "uses", "mongodb", evidence_phrase="  ")],
    )
    facts, report = comp.compile_extraction([r])
    assert facts == []
    assert report.reject_reasons["missing_evidence"] == 1


def test_self_loop_is_rejected():
    r = _Result(
        entities=[_Ent("harbor", "Software")],
        relations=[_Rel("harbor", "uses", "harbor")],
    )
    facts, report = comp.compile_extraction([r])
    assert facts == [] and report.reject_reasons["self_loop"] == 1


def test_low_confidence_routes_to_review_not_silent_drop():
    # Person->Software is a legal signature for `uses`, so the ONLY thing
    # separating this from acceptance is confidence — which must route to
    # REVIEW rather than being silently dropped.
    r = _Result(
        entities=[_Ent("alex", "Person"), _Ent("qdrant", "Software")],
        relations=[_Rel("alex", "uses", "qdrant", confidence=0.45)],
    )
    facts, report = comp.compile_extraction([r])
    assert report.review == 1 and report.accepted == 0
    assert facts and facts[0].verdict == "review"


def test_strong_evidence_with_legal_signature_is_accepted():
    # `uses` carries [Person, Software] in config/ontology.yaml
    r = _Result(
        entities=[_Ent("alex", "Person"), _Ent("qdrant", "Software")],
        relations=[_Rel("alex", "uses", "qdrant", confidence=0.8)],
    )
    facts, report = comp.compile_extraction([r])
    assert report.accepted == 1
    fact = facts[0]
    assert fact.verdict == "accept"
    assert fact.subject_type == "person" and fact.object_type == "software"
    assert fact.subject_id and fact.object_id and fact.subject_id != fact.object_id


def test_duplicate_facts_collapse_keeping_best_confidence():
    r = _Result(
        entities=[_Ent("alex", "Person"), _Ent("qdrant", "Software")],
        relations=[
            _Rel("alex", "uses", "qdrant", confidence=0.7),
            _Rel("alex", "uses", "qdrant", confidence=0.95),
        ],
    )
    facts, _report = comp.compile_extraction([r])
    assert len(facts) == 1 and facts[0].confidence == 0.95


def test_report_exposes_review_fraction_for_the_quality_loop():
    r = _Result(
        entities=[_Ent("alex", "Person"), _Ent("qdrant", "Software")],
        relations=[
            _Rel("alex", "uses", "qdrant", confidence=0.8),
            _Rel("alex", "uses", "qdrant", confidence=0.45),
        ],
    )
    _facts, report = comp.compile_extraction([r])
    metrics = report.as_metrics()
    assert "review_fraction" in metrics and "reject_reasons" in metrics
    assert metrics["relations_in"] == 2


def test_compilation_is_deterministic():
    r = _Result(
        entities=[_Ent("alex", "Person"), _Ent("qdrant", "Software")],
        relations=[_Rel("alex", "uses", "qdrant", confidence=0.8)],
    )
    first, _ = comp.compile_extraction([r])
    second, _ = comp.compile_extraction([r])
    assert [(f.subject_id, f.predicate, f.object_id) for f in first] == [
        (f.subject_id, f.predicate, f.object_id) for f in second
    ]


def test_hard_constraints_are_checked_before_confidence():
    # high confidence must NOT rescue an illegal type signature.
    # `uses` permits Software->Software but never Location->Location.
    r = _Result(
        entities=[_Ent("el paso", "Location"), _Ent("austin", "Location")],
        relations=[_Rel("el paso", "uses", "austin", confidence=0.99)],
    )
    facts, report = comp.compile_extraction([r])
    assert facts == [] and report.accepted == 0
    assert report.reject_reasons["illegal_type_signature"] == 1


def test_predicate_without_an_ontology_signature_routes_to_review():
    # fail closed on ontology gaps instead of accepting unconstrained edges
    r = _Result(
        entities=[_Ent("alex", "Person"), _Ent("acme", "Organization")],
        relations=[_Rel("alex", "affiliated_with", "acme", confidence=0.99)],
    )
    facts, report = comp.compile_extraction([r])
    assert report.accepted == 0 and report.review == 1
    assert "predicate_has_no_ontology_signature" in facts[0].reasons
