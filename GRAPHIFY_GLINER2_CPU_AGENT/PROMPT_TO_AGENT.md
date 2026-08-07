# Execute the Graphify GLiNER2 CPU refactor

Read `AGENTS.md` and treat it as the controlling instruction for this task.

Then run:

```bash
python3 controller.py init --repo-root ..
python3 controller.py verify-pack
python3 controller.py next
```

Execute every ready workflow stage until `S14_FINAL_VERIFY` passes. Do not broaden scope, do not add MPS/MLX/CUDA experiments, and do not stop after unit tests. Completion requires the canonical Graphify entrypoint to process both committed Markdown fixtures end to end, persist authoritative artifacts, build and rebuild an isolated graph projection, and pass the quality, speed, conservation, and idempotency gates.
