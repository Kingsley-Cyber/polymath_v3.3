from __future__ import annotations

from services.research.context import pack_research_context


def test_pack_research_context_caps_tokens_and_receipts_drops():
    evidence = [
        {
            "citation_id": f"C{index}",
            "subquestion_id": "sq1",
            "corpus_id": "corp-1",
            "doc_id": f"doc-{index}",
            "chunk_id": f"chunk-{index}",
            "quote": "alpha beta gamma " * 160,
        }
        for index in range(1, 12)
    ]
    graph_traces = [
        {
            "subquestion_id": "sq1",
            "corpus_id": "corp-1",
            "status": "ok",
            "seed_count": 25,
            "node_count": 80,
            "edge_count": 120,
            "seeds": [{"entity_id": f"e-{item}", "score": 0.5} for item in range(25)],
            "graph": {
                "nodes": [{"id": f"n-{item}", "label": "node"} for item in range(80)],
                "links": [{"source": "a", "target": f"b-{item}"} for item in range(120)],
            },
        }
        for _ in range(4)
    ]

    packed = pack_research_context(
        evidence=evidence,
        graph_traces=graph_traces,
        token_budget=1200,
    )

    assert packed.receipt["input_evidence"] == 11
    assert packed.receipt["included_evidence"] < 11
    assert packed.receipt["dropped_evidence"] > 0
    assert packed.receipt["input_graph_traces"] == 4
    assert packed.receipt["estimated_tokens"] <= packed.receipt["token_budget"]
    assert [row["citation_id"] for row in packed.evidence] == [
        f"C{index}" for index in range(1, packed.receipt["included_evidence"] + 1)
    ]
