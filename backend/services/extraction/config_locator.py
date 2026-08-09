"""Locate the versioned config/ directory from any deployment layout.

Host layout:      <repo>/backend/services/extraction/x.py → <repo>/config
Container layout: /app/services/extraction/x.py           → /app/config

One fail-loud walk replaces every hardcoded ``parents[3]`` assumption
(which silently resolved to /config inside the container image — found by
the factory E2E, 2026-08-08).
"""
from __future__ import annotations

from pathlib import Path


def find_config_dir(anchor: str | Path) -> Path:
    anchor_path = Path(anchor).resolve()
    for parent in anchor_path.parents:
        candidate = parent / "config"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        f"FATAL: config/ directory not found on any parent of {anchor_path}"
    )
