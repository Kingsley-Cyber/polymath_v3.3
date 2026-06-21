from services.graph.hop_pruning_validation import (
    compare_hop_summaries,
    summarize_graph_query_payload,
    validate_graph_hop_report,
)


def _payload(nodes, links, *, seeds=None, bridges=None, gaps=None):
    return {
        "nodes": nodes,
        "links": links,
        "bridges": bridges or [],
        "gaps": gaps or [],
        "seed_entities": seeds or [],
    }


def test_summarize_graph_payload_reports_edge_quality():
    payload = _payload(
        nodes=[{"id": "a"}, {"id": "b"}, {"id": "c"}],
        seeds=[{"id": "a", "display_name": "Python"}],
        links=[
            {
                "source": "a",
                "target": "b",
                "predicate": "uses",
                "confidence": 0.91,
                "edge_strength": "strong",
                "eligible_for_synthesis": True,
                "evidence_count": 2,
            },
            {
                "source": "a",
                "target": "c",
                "predicate": "related_to",
                "confidence": 0.30,
                "edge_strength": "weak",
                "eligible_for_synthesis": False,
                "evidence_count": 0,
            },
        ],
    )

    summary = summarize_graph_query_payload(payload, latency_s=0.12)

    assert summary["latency_s"] == 0.12
    assert summary["nodes"] == 3
    assert summary["links"] == 2
    assert summary["seeds"] == 1
    assert summary["edge_quality"]["generic_edges"] == 1
    assert summary["edge_quality"]["weak_or_thin_edges"] == 1
    assert summary["edge_quality"]["specific_edges"] == 1
    assert summary["edge_quality"]["eligible_edges"] == 1
    assert summary["context_bloat_score"] > 0


def test_compare_hop_summaries_reports_added_coverage():
    hop1 = summarize_graph_query_payload(
        _payload(
            nodes=[{"id": "a"}, {"id": "b"}],
            links=[{"source": "a", "target": "b", "predicate": "uses"}],
            seeds=[{"id": "a"}],
        )
    )
    hop2 = summarize_graph_query_payload(
        _payload(
            nodes=[{"id": "a"}, {"id": "b"}, {"id": "c"}],
            links=[
                {"source": "a", "target": "b", "predicate": "uses"},
                {"source": "b", "target": "c", "predicate": "supports"},
            ],
            seeds=[{"id": "a"}],
        )
    )

    comparison = compare_hop_summaries(hop1, hop2)

    assert comparison["new_nodes_from_hop2"] == 1
    assert comparison["new_edges_from_hop2"] == 1
    assert comparison["interpretation"] == "hop2_added_context"


def test_validate_graph_hop_report_flags_noisy_hop2():
    hop1 = summarize_graph_query_payload(
        _payload(nodes=[{"id": "a"}], links=[], seeds=[{"id": "a"}]),
        latency_s=0.1,
    )
    hop2 = summarize_graph_query_payload(
        _payload(
            nodes=[{"id": "a"}, {"id": "b"}],
            seeds=[{"id": "a"}],
            links=[
                {
                    "source": "a",
                    "target": "b",
                    "predicate": "related_to",
                    "confidence": 0.2,
                    "edge_strength": "weak",
                    "evidence_count": 0,
                }
            ],
        ),
        latency_s=0.2,
    )
    comparison = compare_hop_summaries(hop1, hop2)

    issues = validate_graph_hop_report(
        hop1=hop1,
        hop2=hop2,
        comparison=comparison,
        max_generic_ratio=0.5,
        max_weak_or_thin_ratio=0.5,
    )

    assert any("generic edge ratio" in issue for issue in issues)
    assert any("weak/thin edge ratio" in issue for issue in issues)
