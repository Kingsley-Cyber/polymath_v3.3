#!/usr/bin/env python3
"""publish_glirel_to_hf.py — upload the fine-tuned Ghost B GLiREL to HF Hub.

ONE-TIME, MAINTAINER-ONLY. Run this once (with your Hugging Face write token)
to publish the custom relation model so users can `bootstrap_models.py
--glirel-custom` it. The 1.7 GB weights never go into git — HF Hub is the
distribution channel, exactly like the stock GLiNER the pipeline already pulls.

What it uploads (from models/glirel_ghost_b_v1/best/ by default):
    glirel_config.json   model config (base encoder = microsoft/deberta-v3-large)
    labels.json          the 30 Ghost B predicate labels
    pytorch_model.bin    ~1.7 GB fine-tuned weights
plus a generated README.md model card.

Auth (one of):
    export HF_TOKEN=hf_xxx          # a WRITE token from hf.co/settings/tokens
    huggingface-cli login

Usage:
    pip install huggingface_hub
    python scripts/publish_glirel_to_hf.py                       # -> default repo, public
    python scripts/publish_glirel_to_hf.py --repo me/glirel-ghost-b-v1 --private
    python scripts/publish_glirel_to_hf.py --src /path/to/best   # non-default checkpoint
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

DEFAULT_REPO = os.environ.get("GHOST_B_GLIREL_HF_REPO") or "Sambenja1/glirel-ghost-b-v1"
REQUIRED_FILES = ["glirel_config.json", "labels.json", "pytorch_model.bin"]

_MODEL_CARD = """---
license: mit
library_name: glirel
tags:
  - relation-extraction
  - gliner
  - glirel
  - knowledge-graph
base_model: microsoft/deberta-v3-large
---

# GLiREL — Ghost B v1 (Polymath)

Fine-tuned [GLiREL](https://github.com/jackboyla/GLiREL) relation-extraction
model for Polymath's local "Ghost B" ingestion lane. Predicts the 30-predicate
Ghost B schema over GLiNER-extracted entities, fully on-device (no cloud).

- **Base encoder:** microsoft/deberta-v3-large
- **Predicates:** 30 (see `labels.json`)
- **Best-F1 threshold:** 0.40
- **Pairs with:** GLiNER `urchade/gliner_medium-v2.1` (entity pass)

## Use in Polymath

```
python scripts/bootstrap_models.py --gliner torch --glirel-custom
```

This downloads the checkpoint to `models/glirel_ghost_b_v1/best/`, where the
extraction sidecar loads it by default. Or point `GLIREL_CKPT_DIR` at this repo
id directly.
"""


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=DEFAULT_REPO,
                    help=f"target HF repo id (default: {DEFAULT_REPO})")
    ap.add_argument("--src", default=None,
                    help="checkpoint dir (default: <repo>/models/glirel_ghost_b_v1/best)")
    ap.add_argument("--private", action="store_true", help="create the repo private")
    args = ap.parse_args()

    try:
        from huggingface_hub import HfApi
    except ImportError:
        print("huggingface_hub is required:  pip install huggingface_hub")
        return 1

    repo_root = Path(__file__).resolve().parents[1]
    src = Path(args.src).resolve() if args.src else (
        repo_root / "models" / "glirel_ghost_b_v1" / "best")
    if not src.is_dir():
        print(f"checkpoint dir not found: {src}")
        return 1
    missing = [f for f in REQUIRED_FILES if not (src / f).exists()]
    if missing:
        print(f"checkpoint incomplete — missing {missing} in {src}")
        return 1

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
    api = HfApi(token=token)
    try:
        whoami = api.whoami()
    except Exception as exc:  # noqa: BLE001
        print(f"HF auth failed ({exc}). Set HF_TOKEN (a WRITE token) or run "
              "`huggingface-cli login`.")
        return 1
    print(f"authenticated as {whoami.get('name', '?')}; target repo {args.repo} "
          f"({'private' if args.private else 'public'})")

    api.create_repo(repo_id=args.repo, repo_type="model",
                    private=args.private, exist_ok=True)

    # Write the model card next to the checkpoint, then upload the folder.
    card = src / "README.md"
    if not card.exists():
        card.write_text(_MODEL_CARD)

    print(f"uploading {src} → {args.repo} (this pushes ~1.7 GB) ...")
    api.upload_folder(
        repo_id=args.repo,
        repo_type="model",
        folder_path=str(src),
        commit_message="Publish Ghost B GLiREL v1 checkpoint",
    )
    print(f"\nDONE → https://huggingface.co/{args.repo}")
    print("Users can now run:  python scripts/bootstrap_models.py "
          "--gliner torch --glirel-custom")
    if args.repo != DEFAULT_REPO:
        print(f"NOTE: you used a non-default repo. Set GHOST_B_GLIREL_HF_REPO="
              f"{args.repo} (or pass --glirel-repo) when bootstrapping.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
