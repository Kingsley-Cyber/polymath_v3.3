"""Compiler: the acceptance policy is multi-signal, never a bare score."""

from dataclasses import dataclass, field

from services.extraction import extraction_compiler as comp
from services.extraction.extraction_compiler import filter_results_to_compiled


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


# --- domain ontology signatures: deterministic + idempotent ---------------

DOMAIN_CORPORA = ("consumer_psychology", "film_production", "it_security")


def test_domain_signatures_load_and_are_nonempty():
    for domain in DOMAIN_CORPORA:
        sigs = comp.signatures(domain)
        assert sigs, f"{domain} loaded no signatures"


def test_domain_signature_loading_is_deterministic():
    # same domain, repeated loads -> identical frozen pair sets
    for domain in DOMAIN_CORPORA:
        first = comp._domain_signatures(domain)
        second = comp._domain_signatures(domain)
        assert first == second
        assert {k: sorted(v) for k, v in first.items()} == {
            k: sorted(v) for k, v in second.items()
        }


def test_domain_overlay_replaces_global_predicate_not_unions_it():
    # `uses` in film_production means film pairs, not the global ones
    film = comp.signatures("film_production")
    glob = comp.signatures(None)
    assert ("film", "shot type") in film["uses"]
    # a pair the GLOBAL `uses` allows but the film domain does not
    assert ("organization", "method") in glob["uses"]
    assert ("organization", "method") not in film["uses"]


def test_unknown_domain_falls_back_to_global_without_raising():
    assert comp.signatures("no_such_domain") == comp.signatures(None)


def test_domain_name_is_sanitised_against_path_traversal():
    assert comp._domain_signatures("../../etc/passwd") == {}
    assert comp._domain_signatures("") == {}


def test_domain_typed_fact_is_accepted_when_its_signature_exists():
    r = _Result(
        entities=[_Ent("the wizard of oz", "Film"), _Ent("circular journey", "Narrative Device")],
        relations=[_Rel("the wizard of oz", "uses", "circular journey", confidence=0.8)],
    )
    facts, report = comp.compile_extraction([r], domain="film_production")
    assert report.accepted == 1
    assert facts[0].subject_type == "film" and facts[0].object_type == "narrative device"


def test_domain_typed_fact_with_illegal_pair_is_rejected():
    # neither direction of Equipment/Narrative Device is legal for `uses`
    r = _Result(
        entities=[_Ent("dolly", "Equipment"), _Ent("circular journey", "Narrative Device")],
        relations=[_Rel("dolly", "uses", "circular journey", confidence=0.99)],
    )
    facts, report = comp.compile_extraction([r], domain="film_production")
    assert facts == [] and report.reject_reasons["illegal_type_signature"] == 1


def test_invertible_direction_is_flagged_for_review_never_silently_flipped():
    # film --uses--> narrative device is legal; the reverse is not. The
    # compiler must NOT rewrite the claim — it flags and routes to REVIEW.
    r = _Result(
        entities=[_Ent("circular journey", "Narrative Device"), _Ent("the wizard of oz", "Film")],
        relations=[_Rel("circular journey", "uses", "the wizard of oz", confidence=0.99)],
    )
    facts, report = comp.compile_extraction([r], domain="film_production")
    assert report.review == 1 and report.accepted == 0
    assert "direction_suspect_invertible" in facts[0].reasons
    # the stored claim keeps the ORIGINAL direction; adjudication decides
    assert facts[0].subject_name == "circular journey"


def test_compiling_twice_with_a_domain_is_idempotent():
    r = _Result(
        entities=[_Ent("the wizard of oz", "Film"), _Ent("circular journey", "Narrative Device")],
        relations=[_Rel("the wizard of oz", "uses", "circular journey", confidence=0.8)],
    )
    first, r1 = comp.compile_extraction([r], domain="film_production")
    second, r2 = comp.compile_extraction([r], domain="film_production")
    assert [(f.subject_id, f.predicate, f.object_id, f.verdict) for f in first] == [
        (f.subject_id, f.predicate, f.object_id, f.verdict) for f in second
    ]
    assert r1.as_metrics() == r2.as_metrics()


# --- the graph-write gate -------------------------------------------------

from services.ghost_b import ExtractionResult as _ER  # noqa: E402


def _real_result(chunk_id, entities, relations):
    from services.ghost_b import EntityItem, RelationItem

    return _ER(
        schema_version="v2",
        chunk_id=chunk_id,
        doc_id="d",
        corpus_id="c",
        entities=[
            EntityItem(canonical_name=n, surface_form=n, entity_type=t, confidence=0.9)
            for n, t in entities
        ],
        relations=[
            RelationItem(
                subject=s, predicate=p, object=o, object_kind="entity",
                confidence=conf, evidence_phrase="verbatim evidence here",
            )
            for s, p, o, conf in relations
        ],
        text="source text",
    )


def test_gate_keeps_only_compiled_content_and_never_rewrites_it():
    r = _real_result(
        "c1",
        [("the wizard of oz", "Film"), ("circular journey", "Narrative Device"),
         ("full listing", "other")],
        [("the wizard of oz", "uses", "circular journey", 0.9),
         ("hook", "part_of", "creative system", 0.9)],
    )
    out, metrics = filter_results_to_compiled(
        [r], domain="film_production", result_cls=_ER
    )
    kept = out[0]
    # the untypeable span and the endpoint-less edge are gone
    assert [e.canonical_name for e in kept.entities] == [
        "the wizard of oz", "circular journey"
    ]
    assert [(x.subject, x.predicate, x.object) for x in kept.relations] == [
        ("the wizard of oz", "uses", "circular journey")
    ]
    # surviving items are the ORIGINAL objects, not rewritten ones
    assert kept.relations[0].evidence_phrase == "verbatim evidence here"
    assert metrics["graph_written_facts"] == 1


def test_gate_preserves_one_result_per_chunk_even_when_all_content_is_dropped():
    r1 = _real_result("c1", [("full listing", "other")], [])
    r2 = _real_result("c2", [("the wizard of oz", "Film")], [])
    out, _ = filter_results_to_compiled([r1, r2], domain="film_production", result_cls=_ER)
    assert [o.chunk_id for o in out] == ["c1", "c2"]
    assert out[0].entities == [] and out[0].relations == []


def test_gate_excludes_review_facts_from_the_graph_by_default():
    r = _real_result(
        "c1",
        [("alex", "Person"), ("qdrant", "Software")],
        [("alex", "uses", "qdrant", 0.45)],  # review band
    )
    out, metrics = filter_results_to_compiled([r], result_cls=_ER)
    assert out[0].relations == []
    assert metrics["review"] == 1 and metrics["graph_written_facts"] == 0
    out2, m2 = filter_results_to_compiled([r], result_cls=_ER, include_review=True)
    assert len(out2[0].relations) == 1 and m2["graph_written_facts"] == 1


def test_gate_is_idempotent():
    r = _real_result(
        "c1",
        [("the wizard of oz", "Film"), ("circular journey", "Narrative Device")],
        [("the wizard of oz", "uses", "circular journey", 0.9)],
    )
    first, m1 = filter_results_to_compiled([r], domain="film_production", result_cls=_ER)
    # feeding the gate its own output must change nothing
    second, m2 = filter_results_to_compiled(first, domain="film_production", result_cls=_ER)
    assert [(x.subject, x.predicate, x.object) for x in first[0].relations] == [
        (x.subject, x.predicate, x.object) for x in second[0].relations
    ]
    assert m1["graph_written_facts"] == m2["graph_written_facts"]
