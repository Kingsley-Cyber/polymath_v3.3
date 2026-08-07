from scripts.run_relex_fixture_baseline import _quality_metrics


def test_quality_metrics_accepts_openie_pack_gold_schema():
    results = [
        {
            "entities": [{"surface_form": "Harbor", "entity_type": "software"}],
            "relations": [{"subject": "Harbor", "predicate": "uses", "object": "Qdrant"}],
        }
    ]
    gold = {
        "entities": [{"text": "Harbor", "label": "Software", "start": 0, "end": 6}],
        "relations": [
            {"subject": "Harbor", "predicate": "uses", "object": "Qdrant", "decision": "FACT"},
            {"subject": "Harbor", "predicate": "owns", "object": "Qdrant", "decision": "QUALIFIED_CLAIM"},
        ],
    }

    metrics = _quality_metrics(results, gold)

    assert metrics["entity"]["surface_type_true_positives"] == 1
    assert metrics["relation"]["directed_triple_true_positives"] == 1
    assert metrics["relation"]["gold_accepted_directed_triples"] == 1


def test_quality_metrics_accepts_sample_relations_key():
    results = [
        {
            "entities": [],
            "relations": [{"subject": "A", "predicate": "supports", "object": "B"}],
        }
    ]
    gold = {
        "entities": [],
        "sample_relations": [
            {"subject": "A", "predicate": "supports", "object": "B", "decision": "FACT"}
        ],
    }

    metrics = _quality_metrics(results, gold)

    assert metrics["relation"]["directed_triple_true_positives"] == 1
