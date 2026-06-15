import pytest


def test_cache_warm_scheduler_uses_tracked_worker_when_legacy_missing(monkeypatch):
    from services.graph import orchestrator

    calls = []

    def fake_schedule_metrics_warmup_after_ingest(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(orchestrator, "_legacy", None)
    monkeypatch.setattr(
        "services.graph.cache_warmup.schedule_metrics_warmup_after_ingest",
        fake_schedule_metrics_warmup_after_ingest,
    )

    orchestrator.schedule_graph_discovery_cache_warm(
        qdrant="qdrant",
        neo4j_driver="neo4j",
        db="mongo",
        corpus_id="corp-a",
        user_id="user-a",
    )

    assert calls == [
        {
            "qdrant": "qdrant",
            "neo4j_driver": "neo4j",
            "db": "mongo",
            "corpus_id": "corp-a",
        }
    ]


@pytest.mark.asyncio
async def test_discover_uses_bounded_builder_when_legacy_missing(monkeypatch):
    from services.graph import orchestrator

    seen = {}

    async def fake_bounded_builder(**kwargs):
        seen.update(kwargs)
        return {"ok": True, "builder": "bounded_graph_query"}

    monkeypatch.setattr(orchestrator, "_legacy", None)
    monkeypatch.setattr(
        orchestrator,
        "_bounded_discover_without_legacy",
        fake_bounded_builder,
    )

    out = await orchestrator.discover(
        qdrant=object(),
        neo4j_driver=object(),
        db=None,
        corpus_ids=["corp-a"],
        query="Nash equilibrium",
        mode="auto",
        synthesis_mode="research",
        session_id="session-a",
        user_id="user-a",
    )

    assert out == {"ok": True, "builder": "bounded_graph_query"}
    assert seen["corpus_ids"] == ["corp-a"]
    assert seen["query"] == "Nash equilibrium"
    assert seen["synthesis_mode"] == "research"
