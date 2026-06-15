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
    assert "model_override" in seen


@pytest.mark.asyncio
async def test_bounded_builder_runs_mode_aware_packet_synthesis(monkeypatch):
    from services.graph import graph_query, orchestrator

    async def fake_extract_query_entities(*args, **kwargs):
        return [
            {
                "entity_id": "e-neural",
                "display_name": "Neural Network",
                "entity_type": "Technology",
                "mention_count": 5,
                "score": 10.0,
            }
        ]

    async def fake_expand_subgraph(*args, **kwargs):
        return {
            "nodes": [
                {
                    "id": "e-neural",
                    "display_name": "Neural Network",
                    "entity_type": "Technology",
                    "mention_count": 5,
                    "is_seed": True,
                },
                {
                    "id": "e-backprop",
                    "display_name": "Backpropagation",
                    "entity_type": "Method",
                    "mention_count": 3,
                },
            ],
            "links": [
                {
                    "source": "e-neural",
                    "target": "e-backprop",
                    "predicate": "uses",
                    "relation_family": "Mechanism",
                    "confidence": 0.9,
                }
            ],
        }

    async def fake_find_bridges(*args, **kwargs):
        return []

    async def fake_find_gaps(*args, **kwargs):
        return []

    def fake_find_hubs(nodes, links, metrics=None):
        return [{"entity_id": "e-neural", "display_name": "Neural Network", "degree": 1}]

    captured = {}

    async def fake_retrieve_packet_source_docs(*args, **kwargs):
        return [], {"path": "test"}

    async def fake_enrich_packet_with_extractions(**kwargs):
        return None

    async def fake_call_llm_synthesis(packet, **kwargs):
        captured["packet"] = packet
        captured["kwargs"] = kwargs
        return {
            "headline": "Nuance synthesis",
            "markdown": "Mode-aware graph synthesis.",
            "sources": [],
            "fallback": False,
        }, None

    monkeypatch.setattr(graph_query, "extract_query_entities", fake_extract_query_entities)
    monkeypatch.setattr(graph_query, "expand_subgraph", fake_expand_subgraph)
    monkeypatch.setattr(graph_query, "find_bridges", fake_find_bridges)
    monkeypatch.setattr(graph_query, "find_gaps", fake_find_gaps)
    monkeypatch.setattr(graph_query, "find_hubs", fake_find_hubs)
    monkeypatch.setattr(orchestrator, "_retrieve_packet_source_docs", fake_retrieve_packet_source_docs)
    monkeypatch.setattr(orchestrator, "_enrich_packet_with_extractions", fake_enrich_packet_with_extractions)
    monkeypatch.setattr(orchestrator, "_call_llm_synthesis", fake_call_llm_synthesis)

    out = await orchestrator._bounded_discover_without_legacy(
        qdrant=object(),
        neo4j_driver=object(),
        db=None,
        corpus_ids=["corp-a"],
        query="what is a neural network",
        synthesis_mode="nuance",
        user_id="user-a",
        model_override="deepseek-chat",
    )

    assert out.auto_synthesis["markdown"] == "Mode-aware graph synthesis."
    assert out.auto_synthesis["builder"] == "bounded_graph_query"
    assert captured["kwargs"]["synthesis_mode"] == "nuance"
    assert captured["kwargs"]["model_override"] == "deepseek-chat"
    assert captured["packet"]["entities"][0]["canonical_name"] == "Neural Network"


@pytest.mark.asyncio
async def test_build_corpus_suggestions_returns_bounded_prompts_without_legacy(monkeypatch):
    from services.graph import orchestrator

    class FakeCursor:
        def __init__(self, docs):
            self._docs = iter(docs)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._docs)
            except StopIteration:
                raise StopAsyncIteration

    class FakeCollection:
        def find(self, *args, **kwargs):
            return FakeCursor([
                {"corpus_id": "corp-a", "name": "Authentic Library", "doc_count": 498}
            ])

    class FakeDB:
        def __getitem__(self, name):
            assert name == "corpora"
            return FakeCollection()

    monkeypatch.setattr(orchestrator, "_legacy_build_corpus_suggestions", None)

    payload = await orchestrator.build_corpus_suggestions(
        qdrant=object(),
        neo4j_driver=object(),
        db=FakeDB(),
        corpus_ids=["corp-a"],
        user_id="user-a",
    )

    assert payload["corpus_id"] == "corp-a"
    assert payload["suggestions"]
    assert any(item["kind"] == "gaps" for item in payload["suggestions"])
    assert payload["domain_map_summary"][0]["builder"] == "bounded_graph_query"


@pytest.mark.asyncio
async def test_resolve_graph_model_matches_saved_pool_entry_by_plain_name(monkeypatch):
    from services.graph import orchestrator
    from services import query_model_resolver
    from services import settings as settings_module

    async def fake_get_models_raw(user_id):
        assert user_id == "user-a"
        return {
            "query_model_pool": [
                {
                    "entry_id": "entry-a",
                    "provider": "deepseek",
                    "model_name": "deepseek-chat",
                }
            ]
        }

    async def fake_resolve_by_entry_id(user_id, entry_id):
        assert user_id == "user-a"
        assert entry_id == "entry-a"
        return {
            "model": "deepseek/deepseek-chat",
            "api_base": "https://api.deepseek.com/v1",
            "api_key": "sk-test",
            "extra_params": {"temperature": 0.2},
        }

    monkeypatch.setattr(settings_module.settings_service, "get_models_raw", fake_get_models_raw)
    monkeypatch.setattr(query_model_resolver, "resolve_by_entry_id", fake_resolve_by_entry_id)

    resolved = await orchestrator._resolve_graph_model("user-a", "deepseek-chat")

    assert resolved == {
        "model": "deepseek/deepseek-chat",
        "api_base": "https://api.deepseek.com/v1",
        "api_key": "sk-test",
        "extra_params": {"temperature": 0.2},
        "source": "override_match:entry-a",
    }


@pytest.mark.asyncio
async def test_resolve_graph_model_matches_saved_pool_entry_by_tail(monkeypatch):
    from services.graph import orchestrator
    from services import query_model_resolver
    from services import settings as settings_module

    async def fake_get_models_raw(user_id):
        return {
            "query_model_pool": [
                {
                    "entry_id": "entry-custom",
                    "provider": "opencode-go-anthropic",
                    "model_name": "anthropic/minimax-m2.7",
                }
            ]
        }

    async def fake_resolve_by_entry_id(user_id, entry_id):
        assert entry_id == "entry-custom"
        return {
            "model": "anthropic/minimax-m2.7",
            "api_base": "https://opencode.ai/zen/go",
            "api_key": "custom-key",
            "extra_params": {},
        }

    monkeypatch.setattr(settings_module.settings_service, "get_models_raw", fake_get_models_raw)
    monkeypatch.setattr(query_model_resolver, "resolve_by_entry_id", fake_resolve_by_entry_id)

    resolved = await orchestrator._resolve_graph_model("user-a", "minimax-m2.7")

    assert resolved["model"] == "anthropic/minimax-m2.7"
    assert resolved["api_base"] == "https://opencode.ai/zen/go"
    assert resolved["api_key"] == "custom-key"
    assert resolved["source"] == "override_match:entry-custom"
