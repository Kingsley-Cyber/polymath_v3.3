"""Agent-facing Polymath app guide shared by MCP surfaces.

This module keeps the "how to use the app" contract in one place so MCP
clients, the in-app Settings panel, and tests all see the same route names,
workflows, and safety rules.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal


MCP_APP_INSTRUCTIONS = """Polymath RAG - full read/write research toolkit.

You are connected to the Polymath app through MCP. Treat the app as a complete
research workspace, not a bag of isolated tools.

Canonical route names:
- Fast Search: use qdrant_only for quick semantic recall.
- Hybrid Search: use qdrant_mongo for exact text evidence plus semantic recall.
- Graph Augmentation: use qdrant_mongo_graph for the highest-quality route:
  vector + MongoDB text evidence + Neo4j graph evidence.

Recommended agent workflow:
1. Discover: call polymath_app_guide, then polymath_list_corpora and
   polymath_list_documents when scope matters.
2. Search before writing: use polymath_search or polymath_cross_corpus_search
   to avoid duplicate ingestion.
3. Answer: use polymath_chat_query when the user needs a grounded answer.
4. Inspect graph meaning: use polymath_graph_query for research, nuance,
   ideation, or gap synthesis. Use polymath_graph_map_query for visual graph
   payloads and polymath_graph_question_suggestions to refine questions.
5. Update the knowledge base: create a corpus if needed, ingest URL or upload
   document bytes, poll polymath_get_ingest_status until complete, then verify
   with search/chat before reporting success.

Evidence rules:
- Corpus claims should come from hydrated corpus chunks.
- Graph claims should preserve entity/fact/relation provenance where present.
- Current public claims should use explicit web/search options on chat_query.
- Do not treat parent summaries, graph edges, or web snippets as private corpus
  proof unless a hydrated source chunk supports the claim.

Write safety:
- All tools respect corpus ACLs.
- MCP URL ingestion blocks private/loopback targets unless the server is
  explicitly configured otherwise.
- Uploads are size-capped by MCP_INGEST_MAX_BYTES.
- Deletion is irreversible; use it only for confirmed failed or unwanted
  documents.

Remote connection rules:
- Remote agents connect to the streamable HTTP endpoint: MCP_PUBLIC_URL + /mcp.
- Do not add a trailing slash to the endpoint; use /mcp, not /mcp/.
- Use Authorization: Bearer <Polymath MCP key> for this app.
- The Polymath MCP key is not an OpenAI or Anthropic model-provider key.
  OPENAI_API_KEY / ANTHROPIC_API_KEY stay on the machine running the agent.
"""


RETRIEVAL_ROUTES: list[dict[str, Any]] = [
    {
        "ui_name": "Fast Search",
        "retrieval_tier": "qdrant_only",
        "uses": ["Qdrant parent/chunk vectors", "MongoDB hydration when needed"],
        "best_for": [
            "quick broad recall",
            "first-pass corpus reconnaissance",
            "low-latency semantic neighborhoods",
        ],
        "avoid_for": [
            "exact passage recovery",
            "structured relationship reasoning",
            "final evidence when exact text matters",
        ],
    },
    {
        "ui_name": "Hybrid Search",
        "retrieval_tier": "qdrant_mongo",
        "uses": ["Qdrant semantic recall", "MongoDB lexical/text evidence"],
        "best_for": [
            "exact document passages",
            "multi-document evidence packs",
            "default grounded answers",
        ],
        "avoid_for": [
            "questions whose value depends on graph facts or relation paths",
        ],
    },
    {
        "ui_name": "Graph Augmentation",
        "retrieval_tier": "qdrant_mongo_graph",
        "uses": [
            "Qdrant semantic seeds",
            "MongoDB hydrated source text",
            "Neo4j entities/facts/relations",
        ],
        "best_for": [
            "relationships between concepts",
            "gap analysis",
            "why/how questions",
            "cross-document synthesis",
            "ontology or entity-level investigation",
        ],
        "avoid_for": [
            "tiny speed-only lookups where Fast Search is sufficient",
        ],
    },
]


GRAPH_MODES: list[dict[str, Any]] = [
    {
        "mode": "research",
        "use_for": "Build a thesis from local graph/corpus evidence.",
    },
    {
        "mode": "nuance",
        "use_for": "Surface disagreements, tensions, weak links, and caveats.",
    },
    {
        "mode": "ideation",
        "use_for": "Generate build ideas, analogies, transfers, and next moves.",
    },
    {
        "mode": "gap",
        "use_for": "Identify missing evidence, unresolved questions, and next research.",
    },
]


APP_CAPABILITY_MAP: dict[str, Any] = {
    "app_name": "Polymath",
    "app_views": [
        {
            "name": "Chat Query",
            "purpose": "Grounded question answering over selected corpora.",
            "mcp_entrypoint": "polymath_chat_query",
        },
        {
            "name": "Graph View",
            "purpose": "Visual corpus map, query graph, and four-lens graph synthesis.",
            "mcp_entrypoints": [
                "polymath_graph_query",
                "polymath_graph_map_query",
                "polymath_graph_question_suggestions",
            ],
        },
        {
            "name": "Corpus Management",
            "purpose": "Create corpora, ingest files/URLs, poll status, and delete documents.",
            "mcp_entrypoints": [
                "polymath_create_corpus",
                "polymath_ingest_from_url",
                "polymath_upload_document",
                "polymath_get_ingest_status",
                "polymath_delete_document",
            ],
        },
    ],
    "core_capabilities": {
        "discover": [
            "polymath_app_guide",
            "polymath_list_corpora",
            "polymath_list_documents",
            "polymath_list_skills",
            "polymath_list_tools",
        ],
        "retrieve": [
            "polymath_search",
            "polymath_cross_corpus_search",
        ],
        "answer": [
            "polymath_chat_query",
        ],
        "graph": [
            "polymath_graph_query",
            "polymath_graph_map_query",
            "polymath_graph_question_suggestions",
            "polymath_get_chunk_extraction",
            "polymath_search_entities",
            "polymath_get_entity_relations",
        ],
        "update_knowledge_base": [
            "polymath_create_corpus",
            "polymath_ingest_from_url",
            "polymath_upload_document",
            "polymath_get_ingest_status",
            "polymath_delete_document",
        ],
    },
}


AGENT_WORKFLOWS: list[dict[str, Any]] = [
    {
        "name": "answer_existing_corpus",
        "steps": [
            "polymath_list_corpora",
            "polymath_search with Hybrid Search or Graph Augmentation",
            "polymath_chat_query for final grounded prose",
            "report route, corpora, and evidence confidence",
        ],
    },
    {
        "name": "graph_investigation",
        "steps": [
            "polymath_graph_question_suggestions to sharpen the question",
            "polymath_graph_map_query for bounded visual graph payload",
            "polymath_graph_query with research/nuance/ideation/gap mode",
            "inspect entities or chunk extraction only when needed",
        ],
    },
    {
        "name": "ingest_and_verify",
        "steps": [
            "polymath_list_corpora",
            "polymath_create_corpus if no existing corpus fits",
            "polymath_ingest_from_url or polymath_upload_document",
            "poll polymath_get_ingest_status until complete or failed",
            "verify with polymath_search on known document terms",
            "summarize with polymath_chat_query",
        ],
    },
]


TOOL_PLAYBOOK: list[dict[str, Any]] = [
    {
        "tool": "polymath_app_guide",
        "when_to_use": "First call when an agent needs the app map or workflow rules.",
    },
    {
        "tool": "polymath_search",
        "when_to_use": "Raw evidence retrieval from selected corpora.",
    },
    {
        "tool": "polymath_cross_corpus_search",
        "when_to_use": "Search every accessible corpus or compare corpora.",
    },
    {
        "tool": "polymath_chat_query",
        "when_to_use": "Generate a final grounded answer through the same path as Chat Query.",
    },
    {
        "tool": "polymath_graph_query",
        "when_to_use": "Run four-lens graph synthesis: research, nuance, ideation, or gap.",
    },
    {
        "tool": "polymath_graph_map_query",
        "when_to_use": "Build a bounded visual graph payload for a query.",
    },
    {
        "tool": "polymath_graph_question_suggestions",
        "when_to_use": "Refine or generate better graph/RAG questions.",
    },
    {
        "tool": "polymath_get_chunk_extraction",
        "when_to_use": "Inspect entities and relations extracted from one chunk.",
    },
    {
        "tool": "polymath_search_entities",
        "when_to_use": "Find graph entities in a corpus.",
    },
    {
        "tool": "polymath_get_entity_relations",
        "when_to_use": "Inspect direct graph relations around one entity.",
    },
    {
        "tool": "polymath_create_corpus",
        "when_to_use": "Create a destination corpus before adding new knowledge.",
    },
    {
        "tool": "polymath_ingest_from_url",
        "when_to_use": "Queue public URL content for ingestion.",
    },
    {
        "tool": "polymath_upload_document",
        "when_to_use": "Queue base64 document bytes for ingestion through MCP.",
    },
    {
        "tool": "polymath_get_ingest_status",
        "when_to_use": "Poll async ingestion until complete before searching.",
    },
    {
        "tool": "polymath_delete_document",
        "when_to_use": "Remove confirmed failed or unwanted documents; irreversible.",
    },
]


WRITE_SAFETY_RULES: list[str] = [
    "Search existing corpora before ingesting duplicate material.",
    "Poll ingestion status until complete before claiming the app was updated.",
    "Verify retrieval with known terms from the ingested document.",
    "Do not delete unless the user asked or the ingest is confirmed unwanted/failed.",
    "Never expose or request API keys through guide metadata.",
]


REMOTE_AGENT_SETUP: dict[str, Any] = {
    "endpoint_rule": "Use MCP_PUBLIC_URL + /mcp without a trailing slash.",
    "auth": {
        "header": "Authorization",
        "scheme": "Bearer",
        "secret_type": "Polymath MCP API key",
        "notes": [
            "Generate user-scoped keys from Settings -> MCP Server when possible.",
            "Static MCP_API_KEY remains supported for trusted system agents.",
            "Never confuse the Polymath MCP key with model-provider keys.",
        ],
    },
    "model_provider_keys": {
        "openai": "Set OPENAI_API_KEY on the remote agent machine when the agent uses OpenAI models.",
        "anthropic": "Set ANTHROPIC_API_KEY on the remote agent machine when the agent uses Anthropic models.",
    },
    "smoke_test": [
        "POST initialize to the /mcp endpoint with Accept: application/json, text/event-stream.",
        "Capture Mcp-Session-Id from response headers.",
        "POST tools/list with the same Mcp-Session-Id.",
    ],
}


def get_app_guide(
    detail: Literal["summary", "full"] = "summary",
) -> dict[str, Any]:
    """Return a JSON-safe app guide payload for MCP clients."""
    payload: dict[str, Any] = {
        "app_name": "Polymath",
        "guide_version": 1,
        "default_agent_flow": [
            "discover scope",
            "retrieve evidence",
            "answer or synthesize",
            "ingest/update only when needed",
            "poll and verify writes",
        ],
        "retrieval_routes": deepcopy(RETRIEVAL_ROUTES),
        "graph_modes": deepcopy(GRAPH_MODES),
        "app_capabilities": deepcopy(APP_CAPABILITY_MAP),
        "agent_workflows": deepcopy(AGENT_WORKFLOWS),
        "remote_agent_setup": deepcopy(REMOTE_AGENT_SETUP),
        "tool_playbook": deepcopy(TOOL_PLAYBOOK),
        "write_safety": list(WRITE_SAFETY_RULES),
    }
    if detail == "full":
        payload["agent_instructions"] = MCP_APP_INSTRUCTIONS
    else:
        payload["agent_instructions_summary"] = (
            "Use Fast Search for speed, Hybrid Search for exact text, Graph "
            "Augmentation for full vector+Mongo+Neo4j synthesis. When updating "
            "the app, ingest asynchronously, poll until complete, and verify "
            "retrieval before reporting success."
        )
    return payload


__all__ = [
    "APP_CAPABILITY_MAP",
    "AGENT_WORKFLOWS",
    "GRAPH_MODES",
    "MCP_APP_INSTRUCTIONS",
    "REMOTE_AGENT_SETUP",
    "RETRIEVAL_ROUTES",
    "TOOL_PLAYBOOK",
    "WRITE_SAFETY_RULES",
    "get_app_guide",
]
