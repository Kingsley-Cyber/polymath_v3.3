# Windows Scheduled Tasks — Polymath extraction sidecars (RTX box)

Import on a fresh box, logged in as the target user:

```
schtasks /Create /TN PolymathGhostBSidecar     /XML PolymathGhostBSidecar.xml
schtasks /Create /TN PolymathGhostBSidecarONNX /XML PolymathGhostBSidecarONNX.xml
```

- **PolymathGhostBSidecar** → 8084 torch-CUDA control; venv `.venv_sidecar`; runs `run_sidecar_windows.ps1`.
- **PolymathGhostBSidecarONNX** → 8086 ONNX-CUDA production; venv `.venv_onnx` (CUDA-13 ORT nightly); runs `run_sidecar_onnx.ps1 -Port 8086 -OnnxFile onnx/model.onnx -Forward 32`.

Both trigger AtLogOn (no stored password). Before importing on a different machine, edit each XML's `<UserId>` — the principal SID **and** the logon-trigger account — to the target box's user. Weights live outside the repo at `E:\Polymath_Training\...` (`GLIREL_CKPT_DIR`, `GHOST_B_GLINER_ONNX_REPO`); adjust those paths in the `.ps1` launchers if the rig layout differs.
