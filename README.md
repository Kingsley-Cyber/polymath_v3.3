# Polymath

Local-first RAG + knowledge-graph platform that runs as a two-machine
cluster: a Mac Studio orchestrator (system of record, query serving) and
an RTX GPU workstation (extraction engines, embedder, heavy ingest
workers) — controlled end-to-end by API and MCP, no cloud required.

## What it does

- **Ingest** books, transcripts, and documents at bulk speed: a fast lane
  (parse → chunk → deterministic summaries → embed → index) makes files
  searchable in minutes; a knowledge-graph enrichment pass (entities,
  relations, claims with evidence spans) runs strictly afterwards,
  corpus by corpus — enforced by the control plane, not convention.
- **Search** with hybrid vector+BM25+rerank across corpora, backed by
  per-corpus vocabulary/alias translation layers, summary trees, and a
  Neo4j graph for multi-hop questions.
- **Serve agents**: a full MCP toolset covers upload → status → verify →
  query, plus fleet operations (wake the GPU box, scale engines and
  workers, remount storage) with key-authenticated determinism.

## Architecture in one breath

| Plane | Owner | Where |
|---|---|---|
| Data (immutable runs, receipts, conservation checks) | graphify contracts | `backend/services/extraction/`, `backend/services/ingestion/` |
| Scheduling (ONE enrichment executor; planner-only reconciler) | `enrichment_executor.py` | `backend/services/ingestion/` |
| Engines (frozen GLiNER-Relex CUDA pool; qualification-gated LLM lane) | `engine_routing.py` + `extraction_contract.py` | `backend/services/extraction/` |
| Fleet control (wake/route/scale/remount, human + MCP) | lane manager :8085 + MCP tools | `backend/polymath_mcp/` |

Extraction engines are **release-pinned** (model id, revision, weights
sha256) and may only serve production after passing the extraction
battery — engine identity is governance, not configuration.

## Quickstart (Mac orchestrator)

```bash
cp .env.example .env            # fill secrets; see comments per key
docker compose -f docker-compose.yml -f docker-compose.override.yml \
  -f docker-compose.offline-ingest.yml --profile offline-ingest up -d
curl localhost:8000/api/health          # services
curl localhost:8000/api/health/fleet    # who is doing what right now
```

Upload via the UI (`localhost:3000`), API, or MCP
(`polymath_upload_document`); verify with `polymath_verify_ingestion`
(gates: safe_claims + vector conservation).

## The GPU box

Setup, wake-on-LAN, worker node, sidecar pool scaling, and SSH control
live in the operator skill: `~/.claude/skills/polymath-gpu-cluster/`
(runbook §A-§E). Capacity is an API call:
`POST :8085/sidecar/scale?replicas=N`, `POST :8085/worker/up?scale=N` —
or the MCP mirrors `polymath_engine_pool` / `polymath_worker_stack`.

## Memory doctrine

Container caps must sum under the Docker VM (bulk profile: 24 GB VM /
~23 GB caps; serve profile: 16 GB). Oversubscribed caps are the root of
random OOM kills — see `docs/audit/extraction_jobs_execution_map.md`
Part 3 for the measured budget.

## Development

```bash
local_ghost_b/.venv/bin/python -m pytest backend/tests -q   # full suite
```

- Extraction changes require the battery (packets under `~/Downloads/`,
  scorer `backend/scripts/audit_graphify_stress_frozen.py`) before any
  production routing — see `release/extraction-v1.yaml`.
- The audit maps in `docs/audit/` are the fastest way into the execution
  and memory architecture; extend them, don't let them rot.
- Ops drills (crash seams, idempotency, conservation) live in
  `backend/tests/` and `docker-compose.opskill.yml`.

## Repo map

```
backend/            FastAPI app, pipeline, MCP server, tests
frontend/           React UI (served behind the Cloudflare tunnel)
config/             Declarative graph/query configuration (mounted ro)
litellm/            Model-router config (wildcards only)
release/            Frozen extraction release manifests
docs/audit/         Atomic execution/memory maps (source of truth)
scripts/            Host-side maintenance (storage guardrail, e2e)
```
