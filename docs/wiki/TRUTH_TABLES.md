# TRUTH TABLES

Runtime facts as one-row lookups. Cross-check configs against these before acting.

## Services → port → status → deploy

| Service | Endpoint/port | Status | Citation | Deploy | Owner/notes |
|---|---|---|---|---|---|
| `backend` | `8000:8000` | active | docker-compose.yml:144,435 | docker compose up -d backend; image built from ./backend/Dockerfile (COPY . . at :67 — code baked in, not bind-mounted) | FastAPI app |
| `mcp` | `8765:8765` | active (profile mcp) | docker-compose.yml:696,701 | docker compose --profile mcp up -d; Dockerfile.mcp | MCP stdio+HTTP sidecar |
| `mongodb` | `27017 internal` | active | docker-compose.yml:28-35 | compose; data at POLYMATH_DOCKER_DATA_ROOT/volumes/mongodb | system of record |
| `qdrant` | `6333 internal` | active | docker-compose.yml:69-76 | compose; QDRANT_BINARY_QUANTIZATION_ENABLED=true (compose :451-457) | vector store |
| `neo4j` | `7687 bolt` | active if NEO4J_ENABLED=true | docker-compose.yml:104-108 | compose; NEO4J_ENABLED default false | graph store |
| `litellm` | `4000` | active | docker-compose.yml:180 | compose | model gateway; LITELLM_MASTER_KEY |
| `searxng` | `8080:8080` | active | docker-compose.yml:139-148 | compose; SEARXNG_BASE_URL http://localhost:8080/ | web search |
| `reranker` | `8081:8080` | active (native MLX override) | docker-compose.yml:302; override :65-70 | override runs llama.cpp Metal on host :8081; compose image is CUDA :8080-internal | cross-encoder |
| `embedder` | `8082:80` | active (native MLX override) | docker-compose.yml:230; override :3 | override runs MLX sidecar on host :8082; compose image :80-internal | bi-encoder |
| `RTX lane-manager (legacy)` | `http://192.168.1.83:8085` | partial: vLLM role RETIRED 2026-08-11 → :8086; sidecar lanes retired with GLiNER; worker lanes dormant | polymath_mcp/tools.py:3582 fallback; ops/rtx/README.md | Windows host EzePimpin; X-Api-Key | do NOT /up its vllm container (double-allocates vs native vllm-qwen) |
| `vllm-lane-controller` | `http://192.168.1.83:8086` (Mac: host.docker.internal:8086 via SSH tunnel) | ACTIVE — native vllm-qwen up/down/status, 70GB cap enforced at /up | ops/rtx/lane_controller.py; ops/rtx/README.md | same X-Api-Key | THE vLLM lifecycle authority since 2026-08-11 |
| `vLLM lane` | `http://192.168.1.83:8000/v1` | active when lane up | rtx-compute/SKILL.md:21 | lane-manager /up; lifecycle-managed (E:\AIStack\Services\) | extraction engine OpenAI-compatible |
| `GLiNER-Relex sidecars` | `:8737-8740` | active (systemd, NOT boot-persistent) | rtx-compute/SKILL.md:28-30 | scripts/setup_relex_sidecar_cuda.sh; RELEX_SIDECAR_URL default http://host.docker.internal:8737 (relex_sidecar_client.py:7,40) | CUDA sidecar; ~21 windows/s per replica |
| `Ollama` | `http://host.docker.internal:11434` | active (host-native) | docker-compose.yml:477 | no compose service — intentionally host bridge | local models |
| `8085-absent check` | `port 8085` | absent = FAIL-SAFE state (arbiter down) | docs/archive/COORDINATION.md:13660 | fail-safe wrapper verifies embed/rerank arbiter-disabled + 8085 absent before EXIT=0 | past deployment gate — different controller from lane-manager |

## Deploy semantics

- Backend code is BAKED into the image (`COPY . .` backend/Dockerfile:67) — `docker compose up -d` restart does NOT pick up code edits; rebuild required.
- `./config:/app/config:ro` (compose backend volumes) — config edits ARE live after restart.
- docker-compose.override.yml swaps embedder/reranker for host-native MLX sidecars on :8082/:8081 (override :3,:65-70) — compose image ports :80/:8080 are dead in that configuration.
- MCP runs under profile `mcp` (compose :696) — not started by default.

## Port-conflict past-failure class

- Lane-manager :8085 is LEGACY-PARTIAL since 2026-08-11: vLLM lifecycle moved to the native :8086 controller (ops/rtx/); :8085 keeps only dormant worker/sidecar lanes. '8085 absent' in COORDINATION.md:13660 is a DIFFERENT deployment gate.
- '4/6 retired' (override :68) refers to the OLD jina bi-encoder sidecar pool slots, not current services.
