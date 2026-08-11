# RTX box ops artifacts (source of truth)

The RTX workstation runs exactly two Polymath-owned services, both defined
HERE and installed by script — never hand-built on the box:

| Service | Port | What | Cap |
|---|---|---|---|
| `vllm-qwen` | :8000 | native vLLM serving `polymath-extract` (owner's agent maintains serve.sh/venv) | `--gpu-memory-utilization <= 0.70` (~70GB) — OWNER CAP |
| `vllm-lane-controller` | :8086 | up/down/status for vllm-qwen; REFUSES /up above the cap | installed by `install_lane_controller.sh` |

The Mac reaches :8086 through the launchd SSH tunnel
`com.polymath.rtx-controller-tunnel` (no Windows port config needed).
Secrets: `controller-key.txt` is installed over SSH from the Mac's env —
never committed, never pasted in chat.

Retired: the docker-era controller on :8085 and the GLiNER relex sidecars
(:8737-:8740). Do not start `polymath-extract-vllm` (docker) — it would
double-allocate VRAM against the native unit.
