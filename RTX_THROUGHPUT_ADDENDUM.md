# HANDOFF ADDENDUM — paste to Claude on the RTX workstation (after initial setup)

Three tasks: (1) max-throughput GPU batching with a large standing VRAM pool,
(2) wake-on-LAN so the Mac's agent can start this box with no human,
(3) clean stop controls. The serving contract and model pins DO NOT change.

## 1. GPU batching + standing VRAM pool (throughput v2)

The current server loops one text per forward pass. Upgrade `scripts/relex_sidecar_server.py`:

- In `_infer`, process `texts` in GPU batches: `batch = int(os.environ.get("RELEX_GPU_BATCH", "64"))`,
  and call `_STATE["model"].inference(texts=chunk, ...)` with per-chunk lists,
  concatenating results IN ORDER. Keep the lock; keep per-text output shape identical.
- **Auto-probe the ceiling**: at startup, binary-search the largest stable batch
  (64 → 96 → 128 → 192) with synthetic ~512-token texts, backing off on OOM.
  Pin the discovered value as the effective batch and print it. Target: put a
  real fraction of the 96GB to work — tens of GB resident under load is the
  goal, not a 3GB idle model.
- Standing VRAM pool: set env in the systemd unit:
  `Environment=PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` and warm the pool at
  startup by running one max-batch synthetic inference right after model load,
  so the allocator grows once and stays hot permanently.
  Do NOT hard-reserve memory with dummy tensors — the allocator keeps grown
  segments, which achieves the "always allocated" goal without starving the driver.
- **Runtime VRAM budget endpoint** (`POST /config`): body
  `{"vram_budget_gb": <int>}`, clamped to **8..60**, DEFAULT **32** at boot.
  From the startup probe, build a small table mapping batch size → measured
  VRAM; on /config, pick the largest probed batch whose VRAM fits the budget
  and make it the effective batch. Respond with
  `{"vram_budget_gb", "effective_batch", "probed_ceiling"}` and include the
  same three fields in `GET /health`. The Mac's MCP exposes this to agents
  (bounded, write-scoped) so throughput scales on demand.
- Bump the release name to `relex-large-cuda-sidecar-v2-batched` in a COPY of the
  config (`relex_sidecar_cuda_v2.yaml`) — the Mac's qualification gate must see a
  distinct release. Serve v2 on port 8738 (second systemd unit) so v1 serial stays
  untouched until v2 passes the Mac-side digest battery. NOTE: the Mac's battery
  will run at BOTH budget extremes (32GB and 60GB) — batch-size must not change
  any output, or v2 stays unqualified. After qualification, production routes
  to :8738 and v1 can be disabled.

## 2. Wake-on-LAN (no-human wake)

- BIOS/UEFI: enable Wake on Magic Packet / Power On By PCIe; if an ErP option
  exists, set it OFF.
- Windows NIC "Ethernet 6" → Advanced: "Wake on Magic Packet" = Enabled;
  Power Management tab: allow this device to wake the computer.
- Disable Windows Fast Startup (or standardize on Sleep instead of Shut Down).
- Change the `RelexSidecarBoot` task trigger from At-Logon to **At-Startup**,
  run as SYSTEM — a wake must reach serving without any login.
- REPORT the LAN adapter's MAC address (get-netadapter) so the Mac agent can
  store it in its routing control document.

## 3. Stop controls (report what you set up)

- Service stop:  `wsl -d Ubuntu-24.04 -- sudo systemctl stop relex-sidecar`
- Full release:  put the box to Sleep (agent wakes it via WoL later).
- Create `E:\relex-sidecar-host\stop-relex.ps1` wrapping the systemctl stop +
  optional sleep, and `start-relex.ps1` for the inverse, so the owner has
  one-click controls.

## Completion report

1. v2 batched server: port, release name, the AUTO-PROBED batch ceiling,
   warm-pool VRAM reading from `nvidia-smi` after warmup (expect tens of GB),
   and measured windows/second at the pinned batch vs batch=1.
2. WoL: settings changed, MAC ADDRESS (critical), test result if possible
   (sleep + wake from another device).
3. Stop/start scripts paths.
4. Any deviations, explicitly.
