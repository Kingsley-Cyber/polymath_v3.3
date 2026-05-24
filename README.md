```
    ____        __                          __  __
   / __ \____  / /_  ______ ___  ____ _____/ /_/ /_
  / /_/ / __ \/ / / / / __ `__ \/ __ `/ __  / __/ __ \
 / ____/ /_/ / / /_/ / / / / / / /_/ / /_/ / /_/ / / /
/_/    \____/_/\__, /_/ /_/ /_/\__,_/\__,_/\__/_/ /_/
              /____/        knowledge graph · v3.3
   ╔══════════════════════════════════════════════════════╗
   ║   local-first hierarchical RAG with auto-synthesis   ║
   ║   single GPU · cloudflare tunnel · cross-platform    ║
   ╚══════════════════════════════════════════════════════╝
```

> A polymath is someone whose knowledge spans many subjects. This is the
> tooling for one — corpora ingested locally, woven by a graph, surfaced
> as nuance not as bullet points.

```
                    ┌─────────────────┐
                    │  query: "what   │
                    │  patterns…?"    │
                    └────────┬────────┘
                             │
              ┌──────────────┴──────────────┐
              │   curating  ●●●  0.4s       │   ← bouncing dots in UI
              │   synthesizing  ●●●  18.2s  │     (live counter)
              └──────────────┬──────────────┘
                             │
                             ▼
   ┌─────────────────────────────────────────────────────────┐
   │                    LLM SYNTHESIS                        │
   │                                                         │
   │   evidence + parent summaries + edge rationale +        │
   │   schema-lens facets + concept groupings + gaps         │
   │   ──────────────────────────────────────────────►       │
   │                                                         │
   │   # Headline                                            │
   │   *Theme: tag · tag · tag*                              │
   │   **Bold TL;DR sentence.**                              │
   │   prose · prose · prose [1] [2]                         │
   │   prose · prose · prose [3]                             │
   └─────────────────────────────────────────────────────────┘
```

---

## What this is

Polymath is a **local-first hierarchical RAG (HRAG) system** built around the
idea that the most valuable insight isn't in any single document — it's in the
**bridges, contradictions, and gaps** between many. You ingest a corpus,
the system extracts entity/relation graphs and parent-chunk summaries, and
then the **graph synthesizer** turns scattered text into one woven analytical
brief with inline `[n]` citations.

It runs end-to-end on a single workstation with one GPU. Cloud LLMs are
optional and routed through a wildcard LiteLLM proxy so you can pick any
provider per query.

**It is not just** a chatbot, a vector-search-only RAG, or a one-prompt
summarizer. It tries to behave like a research assistant who has actually read
the corpus.

---

## Current build highlights

The latest repo history is summarized in [`commit_history.md`](commit_history.md).
Recent work focused on making the app feel less like a generic RAG demo and
more like a research workbench:

| Area | Current capability |
|---|---|
| **Chat RAG** | Agent-Zero-inspired synthesis style, live reasoning streams, source-aware rendering, and pressure-tested answers for design/research questions. |
| **Retrieval** | Vector, hybrid, and graph-augmented tiers with reranking, HyDE controls, facet-aware coverage, and evidence provenance. |
| **Graph Query** | Query-specific graph views, evidence packets for research/nuance/ideation, bridges/gaps/hubs, and richer graph visualization controls. |
| **Model routing** | LiteLLM wildcard routing with DeepSeek, GLM 5.1, MiMo, OpenRouter, Anthropic, OpenAI, Gemini, Mistral, Ollama, and custom providers. |
| **Web RAG** | Optional live-web retrieval with cache, trust signals, reranking, and visible trace events. |

---

## Download & install

Polymath does not ship as a signed `.exe` or `.dmg` installer yet. The supported
download is the GitHub repo, then the platform installer script. You can use
Git or GitHub's green **Code -> Download ZIP** button.

| Platform | Recommended path | Best for |
|---|---|---|
| **Windows 11** | Docker Desktop + PowerShell bootstrap | NVIDIA / WSL2 workstation installs |
| **Apple Silicon Mac** | Docker core + host-native MLX sidecars | M1/M2/M3/M4 Macs where Docker cannot access the Apple GPU |
| **Linux / NVIDIA** | Docker Compose + bash bootstrap | Single-GPU Linux boxes or servers |

### Windows 11 install

Prerequisites:

- Docker Desktop with WSL2 enabled
- Git for Windows
- PowerShell 7 or Windows PowerShell
- NVIDIA driver if you want the local embedder/reranker GPU profile
- A fast SSD path for runtime data, defaulting to `C:\PolymathRuntime`

```powershell
# 1. Download
git clone https://github.com/Kingsley-Cyber/polymath_v3.3.git
cd polymath_v3.3

# 2. Create .env, secrets, runtime folders, LiteLLM config, and local model folders
.\scripts\bootstrap-runtime.ps1 -GenerateSecrets -StageModels

# 3. Add at least one model provider key to .env
# Example keys supported: DEEPSEEK_API_KEY, OPENAI_API_KEY,
# ANTHROPIC_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY, Z_AI_API_KEY.
notepad .env

# 4. Validate before startup
.\scripts\check-install.ps1

# 5. Start Polymath
docker compose up -d --build

# 6. Verify and open
.\scripts\check-install.ps1 -CheckRunning
Start-Process http://localhost:3000
```

If model staging fails because Hugging Face is unavailable, rerun the bootstrap
without `-StageModels` and use a cloud provider key first. You can stage local
models later.

### Apple Silicon Mac install

Apple GPUs are not exposed to Docker Desktop. On M-series Macs, the core app
still runs in Docker, but embeddings/reranking/parsing run as host-native MLX
sidecars.

Prerequisites:

- Apple Silicon Mac (`arm64`)
- Docker Desktop for Mac, running
- Git and command-line tools (`xcode-select --install` if needed)
- Homebrew is recommended but not strictly required
- A fast local runtime path, defaulting to `~/PolymathRuntime`

```bash
# 1. Download
git clone https://github.com/Kingsley-Cyber/polymath_v3.3.git
cd polymath_v3.3

# 2. One-shot Apple setup:
#    - bootstraps .env and runtime folders
#    - installs host-native MLX sidecars
#    - pulls MLX embed/rerank models
#    - writes a launchd service
#    - starts Docker with the Apple override
#    - smoke-tests embeddings and reranking
bash scripts/setup_apple_mlx.sh

# 3. Add a chat/synthesis provider key if you have not already
nano .env

# 4. Open
open http://localhost:3000
```

Manual Apple mode is also available:

```bash
bash scripts/install_apple_mlx_runtime.sh
docker compose -f docker-compose.yml -f docker-compose.apple-mlx.yml up -d --build
bash scripts/smoke_apple_mlx.sh
```

### Linux / NVIDIA install

```bash
git clone https://github.com/Kingsley-Cyber/polymath_v3.3.git
cd polymath_v3.3
bash scripts/bootstrap-runtime.sh --generate-secrets --stage-models
bash scripts/check-install.sh
docker compose up -d --build
bash scripts/check-install.sh --check-running
xdg-open http://localhost:3000
```

---

## Architecture

```
                    ┌──────────────────────────────────────┐
                    │            Frontend (React + Vite)    │
                    │   • Chat                              │
                    │   • Mission Control (graph synthesis) │
                    │   • Sigma WebGL graph viz             │
                    └─────────────────┬────────────────────┘
                                      │ /api
                                      ▼
                    ┌──────────────────────────────────────┐
                    │     FastAPI Backend (port 8000)       │
                    │   ingestion · retrieval · synthesis   │
                    └─┬───────────────┬─────────────────┬──┘
                      │               │                 │
   ┌──────────┐  ┌────▼────┐    ┌─────▼─────┐    ┌─────▼──────┐
   │ MongoDB  │  │ Qdrant  │    │  Neo4j    │    │  LiteLLM   │
   │ chunks + │  │ vectors │    │ entities/ │    │ wildcard   │
   │ docs     │  │  (1024d)│    │ relations │    │ router     │
   └──────────┘  └─────────┘    └───────────┘    └─────┬──────┘
                                                       │
        ┌───────┬─────────┬────────┬────────┬─────────┼─────────┬───────┐
        ▼       ▼         ▼        ▼        ▼         ▼         ▼       ▼
     OpenAI  Anthropic Deepseek  Gemini  Mistral  OpenRouter  Ollama  Custom
                                                              (local)

   ┌─────────────────────────────┐   ┌─────────────────────────┐
   │   Embedder  (port 8082)     │   │  Reranker (port 8081)   │
   │   Qwen3-Embedding-0.6B      │   │  Qwen3-Reranker Q8 GGUF │
   │   1024d, GPU                │   │  llama.cpp / Qwen3 Q8   │
   └─────────────────────────────┘   └─────────────────────────┘

   ┌──────────────────────────────────────────────────────────┐
   │         Docling (port 8500)        Redis (port 6379)     │
   │         PDF/DOCX → markdown        cache + queues        │
   └──────────────────────────────────────────────────────────┘
```

One Docker Compose file runs the core app services: three persistent data
stores (Mongo / Qdrant / Neo4j), one LLM gateway (LiteLLM), local model
sidecars as needed (embedder, llama.cpp reranker, docling), one queue
(Redis), web search (SearXNG), and the app itself (backend + frontend).
Ollama is host-native only; it is not shipped as a Docker service.

---

## What makes it different

| Layer | What gets stored | What gets sent to the LLM |
|---|---|---|
| **Chunks** | raw text + heading path + chunk_kind | up to 360-char excerpt |
| **Parent summaries** | LLM-extracted abstractions of larger sections | 320-char summary alongside the excerpt |
| **Graph edges** | subject -predicate-> object + rationale chunk | the rationale text the extractor used to assert each edge |
| **Schema lens** | entity facets: `object_kind / domain_type / canonical_family` | typed entity catalog ("X :: kind=Y · domain=Z") |
| **Concept groupings** | community labels from graph | top 6 active community labels |
| **Synonym clusters** | canonical-form equivalents | `A ≡ B ≡ C` lines instead of three separate edges |

The synthesis call returns **prose** — not JSON cards. One `# headline`,
one `*Theme: tag · tag*` line, one bold TL;DR, then 3-5 short
bold-key-phrase paragraphs with inline `[n]` citations. ADHD-friendly,
ChatGPT-style reading flow.

---

## Advanced quickstart (reference NVIDIA rig)

> **Reference rig:** Windows 11 / Linux x86_64, NVIDIA GPU with ≥ 8 GB VRAM
> (RTX 3090, 4070, A4000, RTX Pro Blackwell, etc.), Docker Desktop with WSL2
> or Linux Docker Engine, 32 GB RAM, fast SSD. The system runs lighter than
> this — see "Cross-device setup" below for slimmed configs. If you only want
> the install commands, use "Download & install" above; this section explains
> the longer reference path and first-run signals.

```bash
# 1. Clone
git clone https://github.com/Kingsley-Cyber/polymath_v3.3.git
cd polymath_v3.3

# 2. Bootstrap runtime folders, .env, secrets, and bind-mounted config
# Windows PowerShell:
.\scripts\bootstrap-runtime.ps1 -GenerateSecrets -StageModels

# Linux/macOS:
bash scripts/bootstrap-runtime.sh --generate-secrets --stage-models

# Apple Silicon MLX instead:
bash scripts/setup_apple_mlx.sh

# 3. Edit .env and add at least one synthesis provider key, or configure Ollama.
# OPENAI_API_KEY / ANTHROPIC_API_KEY / DEEPSEEK_API_KEY /
# GEMINI_API_KEY / OPENROUTER_API_KEY are all supported.

# 4. Verify install shape before starting containers
.\scripts\check-install.ps1      # Windows
bash scripts/check-install.sh    # Linux/macOS

# 5. Bring it up
docker compose up -d --build
docker compose ps

# 6. Probe running services and open the app
.\scripts\check-install.ps1 -CheckRunning      # Windows
bash scripts/check-install.sh --check-running  # Linux/macOS
open http://localhost:3000
```

The bootstrap scripts do the production-critical setup that Docker Compose
cannot safely infer on its own:

- create `POLYMATH_DOCKER_DATA_ROOT`
- seed `POLYMATH_RUNTIME_BINDS_ROOT/litellm/config.yaml`
- seed `POLYMATH_RUNTIME_BINDS_ROOT/modal_embedder.py`
- generate strong local secrets when requested
- enable the local embedder, reranker, parser, and MCP profiles by default
- optionally download the two reference local models

**First-run signals you want to see:**

```
backend    Up 30 seconds (healthy)
frontend   Up 10 seconds (healthy)
mongodb    Up 1 minute (healthy)
qdrant     Up 1 minute (healthy)
neo4j      Up 1 minute (healthy)
redis      Up 1 minute (healthy)
litellm    Up 50 seconds (healthy)
embedder   Up 40 seconds (healthy)   ← model loaded on GPU
reranker   Up 35 seconds (healthy)   ← model loaded on GPU
docling    Up 25 seconds (healthy)
mcp        Up 20 seconds (healthy)
```

If `embedder` is restarting → 99% of the time it's the model files not being
where the volume mount expects. If `reranker` is restarting, check that the
llama.cpp image can download or read
`ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF/qwen3-reranker-0.6b-q8_0.gguf`.
The Docker llama.cpp path returns bounded `0..1` relevance scores, so its
default `RERANKER_SCORE_SCALE` is `probability`. Its default runtime is also
kept lean for local installs: one 4k context slot, no prompt RAM cache, and
model sleep after four idle minutes. See "Local model staging."

---

## Cross-device setup

The reference rig is one shape this can take. Below are sketches for other
common shapes. **Do not blindly run any of these — read the agent setup
prompt at the bottom and let an agent adapt it to your specific machine.**

### NVIDIA workstation (Linux / Windows + WSL2)

Default. Works as-is. Set `POLYMATH_DOCKER_DATA_ROOT` to a fast SSD outside
Docker's WSL VHDX so vector indexes don't bloat your Docker virtual disk.

```bash
# .env
POLYMATH_DOCKER_DATA_ROOT=/mnt/fast-ssd/polymath
POLYMATH_MODELS_ROOT=/mnt/fast-ssd/polymath/models
```

### NVIDIA server / single-purpose box

Same as workstation. Add `restart: always` to backend/frontend in
`docker-compose.override.yml` and put it behind a reverse proxy (Cloudflare
Tunnel, Caddy, Traefik). The included Cloudflare config publishes
`kingsleylab.xyz` — you'll want your own.

### Apple Silicon (M1 / M2 / M3 / M4 / Pro / Max / Ultra)

Apple GPUs are **not** accessible from Docker Desktop. Two adaptation paths:

**Option A — Ollama on the host, everything else in Docker:**

```yaml
# Remove embedder/reranker from Docker or use the Apple MLX override.
# Ollama is already host-native only:
brew install ollama
ollama pull nomic-embed-text          # embedding
ollama pull qwen2.5:14b-instruct      # synthesis
ollama serve                           # listens on :11434

# Point the backend at host Ollama:
# .env  →  OLLAMA_URL=http://host.docker.internal:11434
# litellm/config.yaml is already wildcard-routed to ollama/*
```

The `embedder` service can be replaced by an Ollama-served embedding model
(set `EMBEDDING_MODEL=nomic-embed-text` and use the LiteLLM `ollama/*` route
for embeddings). **Caveat:** Qwen3-Embedding-0.6B at 1024d is the reference
embedding shape; switching to `nomic-embed-text` (768d) means re-ingesting
your corpus. Don't mix dimensions in the same Qdrant collection.

**Option B — MLX-native embedder/reranker/docling on the host (recommended):**

The repo ships an end-to-end installer that stages three host-native FastAPI
sidecars (embedder, reranker, docling), pre-pulls the MLX model weights
(~1 GB), wires a LaunchAgent for auto-restart, starts Docker with the Apple
override, and runs a real embedding/reranking smoke. **One command:**

```bash
# from repo root, on a Darwin/arm64 host
bash scripts/setup_apple_mlx.sh
```

Manual mode is still available: run `bash scripts/install_apple_mlx_runtime.sh`,
then `docker compose -f docker-compose.yml -f docker-compose.apple-mlx.yml up -d --build`,
then `bash scripts/smoke_apple_mlx.sh`.

What the installer does:

| step | result |
|---|---|
| platform gate | hard-rejects non-Darwin / non-arm64 hosts |
| stage code | `rsync` to `~/PolymathRuntime/apple_ml_services/` |
| venv | `uv` + `requirements.txt` |
| **model pull** | `pull_apple_mlx_models.py` warms and verifies `~/PolymathRuntime/volumes/hf-cache`, then writes `polymath-apple-mlx-models.json` |
| LaunchAgent | `~/Library/LaunchAgents/com.polymath.apple-ml.plist` with `RunAtLoad` + `KeepAlive` |
| smoke | checks `/embeddings` returns 1024-d vectors and `/rerank` separates relevant docs from an irrelevant one |

Models pulled (CC BY-NC 4.0 for the reranker — fine for personal/research):
- `mlx-community/Qwen3-Embedding-0.6B-mxfp8` — 1024-dim embeddings (matches Docker default)
- `mlx-community/jina-reranker-v3-4bit-mxfp4` — cosine reranker

**Critical env knob set by the override**: `RERANKER_SCORE_SCALE=cosine`. Jina v3
returns cosine in 0..1, not logits. Without this, the retriever's negative-logit
"low confidence" guard discards every result.

**Implementation note on the sidecars**: the wire contracts (`/info`, `/health`,
`/embeddings`, `/rerank`, `/parse`) are stable. The embedder and reranker now
load the MLX models directly through `mlx-embeddings`; the reranker scores by
embedding the query and candidate documents and taking the cosine/dot-product
similarity advertised by the MLX model card.

Logs / ops:
```bash
tail -f ~/PolymathRuntime/logs/apple_ml_services.log
launchctl kickstart -k gui/$(id -u)/com.polymath.apple-ml   # restart
launchctl bootout    gui/$(id -u)/com.polymath.apple-ml     # stop
```

MLX is significantly faster than CPU on Apple Silicon for both embedding and
reranking. The unified-memory architecture means batch sizes that are tight on
discrete GPUs (8GB+) run comfortably here.

**Mac mini / Claude handoff prompt:**

If you give this repo to Claude or another coding agent for a Mac mini setup,
do not let it blindly run the default NVIDIA workstation path. Paste this:

```text
Set up this Polymath repo on an Apple Silicon Mac mini. Do not use NVIDIA/CUDA
profiles. Run the core services in Docker: MongoDB, Qdrant, Neo4j, Redis,
LiteLLM, backend, frontend, and MCP. Use host Ollama or cloud providers for
LLMs. If local embedding/reranking is required, use a Mac-compatible host
adapter such as Ollama or MLX and point the backend at host.docker.internal.
If I provide a Polymath runtime export, import it before starting the stack.
Do not change the embedding model or vector dimension unless you clearly
explain that doing so requires re-ingestion or a new corpus. Start by running
the bootstrap/check scripts with Mac-safe profiles, then create a
docker-compose.override.yml if needed.
```

Good starting command on a Mac mini:

```bash
bash scripts/bootstrap-runtime.sh --generate-secrets --compose-profiles mcp
bash scripts/check-install.sh
docker compose up -d --build
```

Add parser/embedding/reranking services only after choosing a Mac-compatible
path. For a moved corpus, prefer importing the portable archive first so the
Mac continues from the existing Mongo/Qdrant/Neo4j state instead of
re-ingesting.

### AMD GPU (ROCm)

`nvidia` GPU specs in `docker-compose.yml` need to be replaced with ROCm
device passthrough. Replace:

```yaml
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          capabilities: [gpu]
```

with:

```yaml
devices:
  - /dev/kfd
  - /dev/dri
group_add:
  - video
```

The Dockerfiles for embedder/reranker/docling install `torch` from the
NVIDIA index — switch to the ROCm wheel:
`pip install torch --index-url https://download.pytorch.org/whl/rocm6.1`.

### CPU-only laptop / cloud VM without GPU

Strip GPU services entirely. Use Ollama on the host (CPU mode) or a cloud
LLM exclusively. The local embedder/reranker won't run usefully on CPU
(too slow); use cloud embeddings via LiteLLM:

```yaml
# litellm/config.yaml
- model_name: "openai/text-embedding-3-small"
    model: "openai/text-embedding-3-small"
    api_key: os.environ/OPENAI_API_KEY
```

Then point ingestion at `openai/text-embedding-3-small` (1536d — yes, this
also means re-ingesting). Cost is a few cents per million tokens.

### Tiny edge box (Jetson Orin / Mac mini / NUC)

The graph layer (Neo4j) needs ≥ 4 GB RAM and is a big chunk of the footprint
for a small box. Two ways to slim down:

- **Disable graph synthesis**, use vector-only retrieval. Set
  `NEO4J_ENABLED=false` and the orchestrator skips the graph plumbing.
- **Run Neo4j on a separate machine** and point `NEO4J_URI=bolt://<remote>`
  at it.

Embedder/reranker can run on Jetson's onboard GPU with the ARM64 PyTorch
wheels; a shim Dockerfile is needed (the reference one is x86_64).

---

## Configuration

**Required `.env` keys:**

```bash
# Secrets (generate strong values)
MONGO_PASSWORD=<strong-password>
NEO4J_PASSWORD=<strong-password>
AUTH_SECRET_KEY=<openssl rand -base64 64>
LITELLM_MASTER_KEY=<openssl rand -base64 32>

# At least one cloud LLM key (or use Ollama for everything local)
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
DEEPSEEK_API_KEY=...
GEMINI_API_KEY=...
OPENROUTER_API_KEY=...
```

**Optional `.env` knobs worth knowing:**

```bash
# Where bind-mounted data lives (fast SSD recommended)
POLYMATH_DOCKER_DATA_ROOT=C:/PolymathRuntime    # or /mnt/ssd/polymath
POLYMATH_RUNTIME_BINDS_ROOT=C:/PolymathRuntime/binds
POLYMATH_MODELS_ROOT=C:/PolymathRuntime/models

# Ingestion concurrency (lower for small machines)
INGEST_PRE_VECTOR_DOC_CAP=8
INGEST_GRAPH_DOC_CAP=8
INGEST_MAX_PARSE_JOBS=8

# Embed batch size (lower for 6-8 GB VRAM)
LOCAL_EMBED_BATCH_SIZE=64
EMBED_BATCH_SIZE=64

# Cloudflare tunnel (optional — for kingsleylab.xyz-style publishing)
CLOUDFLARE_TUNNEL_TOKEN=...
MCP_PUBLIC_URL=https://mcp.example.com
MCP_API_KEY=<openssl rand -hex 32>
```

**Ghost B (entity / relation extractor) — read this if you change the model:**

The default extraction model (`deepseek/deepseek-v4-flash`) is a reasoning
model that ships with thinking-mode ON. Polymath disables it per call by
sending `thinking={"type":"disabled"}` so reasoning tokens don't eat the
output budget — see `GOTCHAS.md` § "Ghost B + DeepSeek thinking-mode" for
the why. Two consequences for operators:

- **`EXTRACTION_MAX_TOKENS=6144`** is the post-fix default. With thinking
  disabled, real output is ~600–1500 tokens — the headroom is for safety
  on dense chunks. If you swap to a non-DeepSeek **reasoning** model
  (Claude extended thinking, o-series, QwQ, etc.) you must wire its own
  disable knob the same way, or budget for ~3× more max_tokens.
- **`EXTRACTION_MAX_TOTAL_LINES=55`** sits 15 lines above the per-type
  theoretical max (14 entities + 20 relations + 5 facts + 1 sentinel = 40).
  Don't drop below ~45 unless you've also reduced the per-type caps,
  or you'll start seeing `error_type=line_cap_exceeded` audit events on
  dense documents.

The system has a per-doc failure circuit (default trips at ≥25 % failed
after 20 chunks processed) that protects against runaway provider spend
on a misbehaving doc; documents that trip the circuit still complete the
Mongo + Qdrant write so vector RAG works for them. Tally per doc lives on
`documents.ghost_b_metrics`.

---

## Common operations

| Task | Command |
|---|---|
| Fresh bootstrap (Windows) | `.\scripts\bootstrap-runtime.ps1 -GenerateSecrets -StageModels` |
| Fresh bootstrap (Linux/macOS) | `bash scripts/bootstrap-runtime.sh --generate-secrets --stage-models` |
| Fresh bootstrap (Apple Silicon MLX) | `bash scripts/setup_apple_mlx.sh` |
| Validate install | `.\scripts\check-install.ps1` or `bash scripts/check-install.sh` |
| Bring up | `docker compose up -d --build` |
| Bring down | `docker compose down` |
| Tail one service | `docker compose logs -f backend` |
| Rebuild one service | `docker compose up -d --build backend` |
| Open Mongo Express UI | `docker compose --profile admin up -d mongo-express`  →  http://localhost:8083 |
| Open Neo4j browser | http://localhost:7474 |
| Open Qdrant dashboard | http://localhost:6333/dashboard |
| Start MCP sidecar | `docker compose --profile mcp up -d --build mcp` |
| Start Cloudflare tunnel | `docker compose --profile cloudflare up -d cloudflared` |
| Export portable archive | `.\scripts\export-runtime.ps1 -Destination E:\PolymathRuntime-export -IncludeEnv -Archive` |
| Import portable archive | `.\scripts\import-runtime.ps1 -Source E:\PolymathRuntime-export.zip -IncludeEnv` |
| Run backend tests | `cd backend && python -m pytest tests/graph -q` |
| Wipe a corpus | DELETE via the API or use the corpus selector → settings → delete |
| Clear Redis cache | `docker compose exec redis redis-cli FLUSHALL` |

---

## Universal advice

- **Don't mix embedding dimensions in a single Qdrant collection.** If you
  switch from Qwen3-Embedding-0.6B (1024d) to anything else, re-ingest the
  whole corpus into a new collection.
- **Bind-mount your data root outside Docker's VHDX.** Vector indexes get
  big fast. Putting them inside the WSL VHDX or Docker's own data folder
  will balloon disk usage with no rotation.
- **Pre-stage local models before first run.** The embedder and reranker
  can read model files from the volume mount path. The Docker reranker also
  supports llama.cpp's Hugging Face download path and persists it under
  `POLYMATH_DOCKER_DATA_ROOT/volumes/hf-cache`, but pre-staging avoids
  first-run network failures.
- **Pick one cloud provider for synthesis to start.** DeepSeek V4-Flash is
  a strong default for its price-to-reasoning ratio, but any provider works
  through the wildcard LiteLLM router. Test one, then add others.
- **The synthesis is bound by the LLM call (~95% of total latency).**
  Hardware doesn't make graph queries faster — model speed does. Streaming
  the prose token-by-token is on the roadmap.
- **`/api/collections` is dead in v3.** It returns `[]` from the frontend
  helper to keep the legacy UI quiet. Don't depend on it.
- **Cache responses.** LiteLLM is configured with Redis-backed caching.
  Identical queries return in < 1s.
- **Moving machines does not require re-ingestion if you move the runtime
  stores.** Stop the stack and copy Mongo, Qdrant, and Neo4j from
  `POLYMATH_DOCKER_DATA_ROOT`. Today that is a whole-runtime mount, not a raw
  single-corpus folder copy.
- **MCP agents can choose retrieval, chat, graph maps, graph synthesis, or
  contextual question building.** The sidecar exposes cross-corpus search,
  `/api/chat` equivalent querying with the current retrieval/web/reasoning
  knobs, Mission Control `research` / `nuance` / `ideation` synthesis, the
  lightweight graph canvas query, and contextual follow-up question generation
  through the same backend services the UI uses.

---

## Project layout

```
polymath_v3.3/
├── backend/                  FastAPI app
│   ├── routers/              HTTP endpoints
│   ├── services/             ingestion, retrieval, graph orchestrator
│   │   └── graph/
│   │       ├── orchestrator.py     ← graph synthesis pipeline (the brain)
│   │       ├── analytics.py        ← Neo4j community detection
│   │       ├── neo4j_reader.py
│   │       └── neo4j_writer.py
│   ├── models/schemas.py     all Pydantic schemas
│   └── tests/                pytest test suite
├── frontend/                 React + Vite + Tailwind
│   └── src/
│       ├── components/
│       │   ├── chat/         ChatWindow, MessageBubble, GraphView
│       │   └── graph/        DiscoveryPanel (Mission Control)
│       └── stores/           Zustand state
├── embedder/                 GPU embedder service (Qwen3 1024d)
├── reranker/                 Legacy Python reranker service; Docker uses llama.cpp by default
├── docling_svc/              PDF/DOCX → markdown service
├── litellm/config.yaml       wildcard LLM router config
└── docker-compose.yml        all 11 services
```

---

## Agent setup prompt

> If you want an LLM agent (Claude, GPT-5, etc.) to walk you through setting
> this up on **your** machine, paste the block below into a fresh
> conversation. The agent will inspect your hardware, pick the right
> adaptation path, and emit the exact commands.

```
You are setting up the Polymath RAG v3.3 stack
(https://github.com/Kingsley-Cyber/polymath_v3.3) on the user's machine.

CONTEXT
The reference rig is x86_64 Linux/Windows + NVIDIA GPU + Docker Desktop /
Docker Engine. The core Docker stack includes mongodb, qdrant, neo4j, redis,
searxng, litellm, embedder (Qwen3-Embedding-0.6B, 1024d, GPU), reranker
(llama.cpp Qwen3-Reranker-0.6B Q8 GGUF), docling, backend (FastAPI), frontend
(React+Vite), plus optional profile services such as MCP/n8n/admin tools.
Data is bind-mounted to a host-side root
(POLYMATH_DOCKER_DATA_ROOT, default C:/PolymathRuntime). LLM routing is
wildcard via LiteLLM — any cloud key in .env works.

YOUR JOB

1. Identify the user's machine:
   - OS (Linux / macOS / Windows + WSL2 / bare Windows)
   - CPU arch (x86_64 / arm64 / Apple Silicon)
   - GPU (NVIDIA + CUDA version / AMD + ROCm / Apple GPU / none)
   - Available RAM and free SSD space
   - Docker version (Desktop or Engine)

2. Match to one of these adaptation paths and explain the trade-offs:
   - NVIDIA workstation: as-is.
   - Apple Silicon: embedder/reranker leave Docker; use the MLX host
     sidecars. Ollama remains host-native; backend points at
     host.docker.internal:11434 when local chat models are used.
   - AMD ROCm: replace nvidia GPU spec with kfd/dri device passthrough;
     swap torch wheel to ROCm.
   - CPU-only: strip GPU services; use cloud embeddings via LiteLLM
     (e.g. openai/text-embedding-3-small, 1536d); flag the re-ingestion
     cost.
   - Edge box (Jetson / Mac mini / NUC): consider disabling Neo4j or
     pointing it at a separate machine; ARM64 wheels for local services.

3. Walk the user through:
   - Cloning the repo
   - Generating .env secrets (MONGO_PASSWORD, NEO4J_PASSWORD,
     AUTH_SECRET_KEY, LITELLM_MASTER_KEY) using openssl
   - Picking ONE cloud LLM provider to start (recommend DeepSeek V4-Flash
     or Anthropic Claude depending on user's existing keys)
   - Setting POLYMATH_DOCKER_DATA_ROOT to a fast SSD path
   - Pre-staging local models with huggingface-cli
   - First docker compose up
   - Verifying all 11 services are healthy
   - Opening http://localhost:3000 and running their first ingest+query

4. Do not make assumptions. If hardware/software details aren't given,
   ask one tight clarifying question per round (max 3 rounds). After 3
   rounds, pick the most likely path and proceed.

5. Critical things to NOT mess up:
   - The embedding model dimension (1024d for Qwen3-Embedding-0.6B). Mixing
     dimensions in one Qdrant collection breaks everything silently.
   - The chunk pipeline expects markdown input — Docling handles PDF/DOCX
     conversion. Don't bypass it.
   - Bind-mounted data root MUST be on a real filesystem (not a network
     share, not Docker's WSL VHDX) for vector index performance.
   - Auth: the backend uses JWT with HS256. AUTH_SECRET_KEY must be set
     before first startup, not after, or token validation breaks.

OUTPUT
Be concrete. Emit copy-paste-able commands. When the user is on a non-
NVIDIA path, also provide the docker-compose.override.yml diff inline
rather than describing it abstractly. Verify each step before proceeding
to the next.

START by asking the user for their OS, GPU, and how much VRAM/RAM/disk
they have available.
```

---

## License & credits

Built by Kingsley (`@Kingsley-Cyber`).

- **Embedding:** [Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)
- **Reranking:** [Qwen3-Reranker-0.6B Q8 GGUF](https://huggingface.co/ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF) served by [llama.cpp](https://github.com/ggml-org/llama.cpp)
- **PDF parsing:** [Docling](https://github.com/DS4SD/docling)
- **LLM routing:** [LiteLLM](https://github.com/BerriAI/litellm)
- **Graph:** [Neo4j Community](https://neo4j.com/) + Apache Pulsar (async)
- **Vector store:** [Qdrant](https://qdrant.tech/)
- **WebGL graph viz:** [sigma.js](https://www.sigmajs.org/) +
  [graphology](https://graphology.github.io/) (Louvain + ForceAtlas2)

---

```
        ●●●  thinking…  ─→  ●●●  curating  ─→  ●●●  synthesizing  ─→  ✓  done
```
