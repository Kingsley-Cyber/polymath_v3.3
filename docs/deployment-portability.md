# Polymath Deployment And Portability

This repo can run as a local app, an MCP server, and a Cloudflare-published
service. The important rule is that ingestion state lives outside the repo in
the runtime root, not inside Docker's disposable internal volume store.

## Runtime State

For a fresh install, run the bootstrap script first. It creates the runtime
tree, seeds bind-mounted config files, and can generate local secrets.

Windows PowerShell:

```powershell
.\scripts\bootstrap-runtime.ps1 -GenerateSecrets -StageModels
```

Linux/macOS:

```bash
bash scripts/bootstrap-runtime.sh --generate-secrets --stage-models
```

Then verify before starting containers:

```powershell
.\scripts\check-install.ps1
```

```bash
bash scripts/check-install.sh
```

The runtime root is stored in `.env`:

```bash
POLYMATH_DOCKER_DATA_ROOT=C:/PolymathRuntime
POLYMATH_RUNTIME_BINDS_ROOT=C:/PolymathRuntime/binds
POLYMATH_MODELS_ROOT=C:/PolymathRuntime/models
```

On a Mac mini, use a native path:

```bash
POLYMATH_DOCKER_DATA_ROOT=$HOME/PolymathRuntime
POLYMATH_RUNTIME_BINDS_ROOT=$HOME/PolymathRuntime/binds
POLYMATH_MODELS_ROOT=$HOME/PolymathRuntime/models
```

The ingestion-critical stores are:

- `volumes/mongodb`: corpus metadata, documents, chunks, settings, staging.
- `volumes/qdrant`: vectors for naive, HRAG, graph, and schema collections.
- `volumes/neo4j`: extracted graph entities, relations, facts, chunks.

Copy those three while containers are stopped and you do not need to re-ingest.
Redis, n8n, models, and HF caches are helpful but not required for corpus
survival.

## Export From The Current Machine

Stop the stack first:

```powershell
docker compose down
```

Windows PowerShell:

```powershell
.\scripts\export-runtime.ps1 -Destination E:\PolymathRuntime-export -IncludeEnv -Archive
```

Bash:

```bash
INCLUDE_ENV=1 ARCHIVE=1 scripts/export-runtime.sh /Volumes/External/PolymathRuntime-export
```

The PowerShell command creates `E:\PolymathRuntime-export.zip`. The Bash
command creates `/Volumes/External/PolymathRuntime-export.tar.gz`. You can set
an exact output path with `-ArchivePath D:\polymath-backup.zip` or
`ARCHIVE_PATH=/Volumes/External/polymath-backup.tar.gz`.

Use `-IncludeModels` or `INCLUDE_MODELS=1` only if you want to copy local model
weights too. They are often large and can usually be re-staged or downloaded on
the new device.

The `-IncludeEnv` / `INCLUDE_ENV=1` option copies `.env` into the export. Treat
that export as secret material. It contains the keys needed to decrypt stored
provider credentials and authenticate Mongo/Neo4j.

## Import On A New Device

Clone the repo, copy the export directory to the new machine, then import.

PowerShell:

```powershell
$env:POLYMATH_DOCKER_DATA_ROOT="$HOME/PolymathRuntime"
.\scripts\import-runtime.ps1 -Source .\PolymathRuntime-export.zip -IncludeEnv
```

Bash:

```bash
export POLYMATH_DOCKER_DATA_ROOT="$HOME/PolymathRuntime"
INCLUDE_ENV=1 scripts/import-runtime.sh ./PolymathRuntime-export.tar.gz
```

If `.env` already exists and you intentionally want to replace it:

```powershell
.\scripts\import-runtime.ps1 -Source .\PolymathRuntime-export.zip -IncludeEnv -OverwriteEnv
```

```bash
INCLUDE_ENV=1 OVERWRITE_ENV=1 scripts/import-runtime.sh ./PolymathRuntime-export.tar.gz
```

Then start the stack:

```bash
docker compose up -d --build
```

For MCP and Cloudflare together:

```bash
docker compose --profile mcp --profile cloudflare up -d --build
```

## MCP

The MCP server is a sidecar profile. It shares Mongo, Qdrant, Neo4j, LiteLLM,
embedder, and reranker with the backend.

`.env`:

```bash
MCP_API_KEY=<openssl rand -hex 32>
MCP_REQUIRE_AUTH=true
MCP_PUBLIC_URL=https://mcp.example.com
```

Start locally:

```bash
docker compose --profile mcp up -d --build mcp
```

Health:

```bash
curl http://localhost:8765/health
```

Client URL:

```text
http://localhost:8765/mcp/
```

Use an `Authorization: Bearer <MCP_API_KEY>` header unless you set
`MCP_REQUIRE_AUTH=false` for a trusted local-only setup.

Agent-facing tools:

- `polymath_list_corpora`: discover accessible corpora.
- `polymath_cross_corpus_search`: retrieve evidence chunks across one, many, or
  all accessible corpora.
- `polymath_chat_query`: ask the same chat pipeline used by `/api/chat` and get
  a compact non-streamed answer plus source previews.
- `polymath_graph_query`: run Mission Control graph synthesis for a corpus.
- `polymath_get_chunk_extraction`: inspect extracted entities/relations for a
  chunk.
- `polymath_search_entities` and `polymath_get_entity_relations`: traverse the
  Neo4j layer directly.
- `polymath_list_documents`, `polymath_list_skills`, `polymath_get_skill`, and
  `polymath_list_tools`: orient the agent before it acts.

Generic streamable-HTTP MCP clients should point at the URL above and send the
API key as a bearer token. Cloudflare-published clients should use
`https://mcp.example.com/mcp/`.

## Corpus-Level Portability

There is a real mount today, but it is a **runtime mount**, not a single
corpus-as-one-folder mount. One ingested corpus is physically split across:

- MongoDB: corpus metadata, documents, parent chunks, child chunks, staging.
- Qdrant: per-corpus vector collections for naive, HRAG, graph, and schemas.
- Neo4j: Document/Chunk/Entity/Fact nodes plus graph edges scoped by
  `corpus_id` and/or evidence chunks.

Because of that split, copying only one folder named after a corpus is not
enough to avoid re-ingestion. To move without re-ingesting today, export/import
the whole runtime root with the scripts above. That preserves all corpora.

A true per-corpus portable package is possible, but it must be a logical export
that bundles:

- Mongo records filtered by `corpus_id`.
- Qdrant snapshots or point exports for the four `corpus_<id>_*` collections.
- Neo4j subgraph records for the corpus's documents/chunks/entities/facts and
  corpus-supported `RELATES_TO` edges.

That package can live as a folder, but it should be created by an exporter
rather than by raw-copying database internals while services are running.

## Cloudflare Tunnel

The compose-managed Cloudflare container is profile-gated so it never starts
unless you ask for it.

`.env`:

```bash
CLOUDFLARE_TUNNEL_TOKEN=<token from Cloudflare Zero Trust>
MCP_PUBLIC_URL=https://mcp.example.com
```

In Cloudflare Zero Trust, configure public hostnames to route to compose service
names on the tunnel network:

```text
app.example.com -> http://frontend:80
api.example.com -> http://backend:8000
mcp.example.com -> http://mcp:8765
```

Start:

```bash
docker compose --profile mcp --profile cloudflare up -d --build
```

If you already have an external `cloudflared-tunnel` container using the same
tunnel token, stop it first so two tunnel clients do not fight for the same
routes.

## What Must Stay The Same Across Devices

Keep these compatible when moving runtime state:

- `AUTH_SECRET_KEY`: needed to decrypt stored API keys.
- `MONGO_PASSWORD` and `NEO4J_PASSWORD`: used by existing database stores.
- Embedding dimension/model: changing embeddings requires re-ingestion or a new
  corpus.
- Docker image versions: major Mongo/Qdrant/Neo4j upgrades should be done after
  the import, not during the move.
