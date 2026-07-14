#!/usr/bin/env python3
"""BUILDLINE coverage checker: every checklist section with open work must
appear in BUILDLINE.md's COVERAGE MAP. Exits non-zero listing any misses.

Derivation rule (owner, 2026-07-14): the temporal plan is derived from the
checklist ON DISK, never from agent memory. This script is the enforcement.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CHECKLIST = REPO / "docs" / "RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md"
BUILDLINE = REPO / "BUILDLINE.md"

# Sections that are meta/log/receipt structure, not schedulable work.
EXEMPT_PATTERNS = [
    r"^Execution Order", r"^Standing Rules", r"^Status Legend",
    r"^Current Durable Baseline", r"^Audit Delta", r"^Governing Librarian",
    r"^Deterministic-First Librarian Build Order$", r"^Implementation Log",
    r"^Completion Rule", r"^Active PoC Scope", r"^P0 - ", r"^P1 - ", r"^P2 - ",
    r"^P3 - ",
]


def normalize(title: str) -> str:
    t = title.strip().strip("`").lower()
    t = re.sub(r"[`*_]", "", t)
    t = re.sub(r"\s*[—-]\s*(experiment|migration)\s*$", "", t)
    t = re.sub(r"\s*\(.*\)$", "", t)  # drop parenthetical suffixes
    t = re.sub(r"\s+", " ", t)
    return t


def checklist_sections() -> list[tuple[str, int]]:
    """(title, open_count) for every ##/### section with at least one open box."""
    out = []
    cur_title, cur_open = None, 0
    for line in CHECKLIST.read_text().splitlines():
        if line.startswith("## ") or line.startswith("### "):
            if cur_title is not None and cur_open > 0:
                out.append((cur_title, cur_open))
            cur_title, cur_open = line.lstrip("# ").strip(), 0
        elif line.lstrip().startswith("- [ ]"):
            cur_open += 1
    if cur_title is not None and cur_open > 0:
        out.append((cur_title, cur_open))
    return out


def buildline_map_keys() -> set[str]:
    keys = set()
    in_map = False
    for line in BUILDLINE.read_text().splitlines():
        if line.startswith("## COVERAGE MAP"):
            in_map = True
            continue
        if in_map and line.startswith("| ") and not line.startswith("|---"):
            cell = line.split("|")[1].strip()
            if cell and cell != "Checklist section":
                keys.add(normalize(cell))
    return keys


def main() -> int:
    mapped = buildline_map_keys()
    misses = []
    for title, open_count in checklist_sections():
        if any(re.search(p, title) for p in EXEMPT_PATTERNS):
            continue
        key = normalize(title)
        # a section is covered if any mapped key is a prefix match either way
        if not any(key.startswith(m) or m.startswith(key) for m in mapped):
            misses.append((title, open_count))
    if misses:
        print("BUILDLINE COVERAGE FAILURE — unmapped checklist sections:")
        for title, n in misses:
            print(f"  - {title}  (open boxes: {n})")
        return 1
    print(f"BUILDLINE coverage OK: {len(mapped)} mapped entries cover all "
          f"open checklist sections.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
