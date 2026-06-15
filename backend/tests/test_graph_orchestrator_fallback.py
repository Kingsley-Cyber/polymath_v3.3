import pytest


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
