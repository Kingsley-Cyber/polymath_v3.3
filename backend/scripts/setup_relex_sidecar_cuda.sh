#!/bin/bash
# One-shot setup for the CUDA relex sidecar on the GPU workstation
# (RTX 6000 Pro). Run ON THE GPU BOX (Linux or WSL2), from a copy of the
# repo (or a folder containing backend/scripts/relex_sidecar_server.py and
# config/relex_sidecar_cuda.yaml with the same relative layout).
#
#   bash backend/scripts/setup_relex_sidecar_cuda.sh
#
# Then from the Mac:  curl http://<gpu-box-ip>:8737/health
set -euo pipefail
cd "$(dirname "$0")/../.."

python3 -m venv .venv-relex-cuda
source .venv-relex-cuda/bin/activate
pip install --upgrade pip
# Same pinned stack as the MPS sidecar (config/relex_sidecar.yaml
# runtime.interpreter notes): gliner 0.2.28, transformers 5.13.1 — torch
# from the CUDA index for the local driver.
pip install "torch>=2.13" --index-url https://download.pytorch.org/whl/cu126
pip install "gliner==0.2.28" "transformers==5.13.1" pyyaml

python - << 'PYEOF'
import torch
assert torch.cuda.is_available(), "CUDA not available — check driver/torch build"
print("CUDA OK:", torch.cuda.get_device_name(0),
      f"{torch.cuda.get_device_properties(0).total_memory/1e9:.0f}GB")
PYEOF

# Warm the pinned model into the HF cache and verify the weights hash.
python - << 'PYEOF'
import hashlib, yaml
from pathlib import Path
from huggingface_hub import hf_hub_download
cfg = yaml.safe_load(Path("config/relex_sidecar_cuda.yaml").read_text())
pin = cfg["model"]
path = hf_hub_download(pin["id"], "pytorch_model.bin", revision=pin["revision"])
digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
assert digest == pin["weights_sha256"], f"weights hash mismatch: {digest}"
print("weights verified:", digest[:16])
PYEOF

echo "Starting sidecar (Ctrl-C to stop; use systemd/NSSM for supervision):"
RELEX_SIDECAR_CONFIG=config/relex_sidecar_cuda.yaml \
  exec .venv-relex-cuda/bin/python backend/scripts/relex_sidecar_server.py
