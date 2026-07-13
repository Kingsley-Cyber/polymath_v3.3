#!/usr/bin/env python3
"""Relative-link check for git-tracked Markdown files (checklist P0.7).

Scans every git-tracked ``*.md`` file for inline Markdown links whose target
is a relative path (no scheme, not an anchor), resolves each against the
file's directory, and fails (non-zero exit) when a target does not exist.

Usage:
    python3 backend/scripts/check_markdown_links.py

Anchor-only links (``#section``), absolute URLs, and ``mailto:`` are ignored.
A ``path#anchor`` target is checked for the file part only.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def tracked_markdown() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "*.md", "**/*.md"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [REPO / line for line in out.splitlines() if line.strip()]


def main() -> int:
    broken: list[str] = []
    files = tracked_markdown()
    checked = 0
    for md in files:
        try:
            text = md.read_text(encoding="utf-8")
        except OSError as exc:
            broken.append(f"{md.relative_to(REPO)}: unreadable ({exc})")
            continue
        for match in LINK_RE.finditer(text):
            target = match.group(1)
            if SCHEME_RE.match(target) or target.startswith("#"):
                continue
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue
            if "/" not in path_part and "." not in path_part:
                # Bare tokens such as `[chunk](heading_path)` are payload-field
                # notation in design docs, not filesystem references.
                continue
            checked += 1
            resolved = (md.parent / path_part).resolve()
            if not resolved.exists():
                line = text[: match.start()].count("\n") + 1
                broken.append(
                    f"{md.relative_to(REPO)}:{line}: broken relative link -> {target}"
                )
    print(f"checked {checked} relative links across {len(files)} tracked .md files")
    if broken:
        print("BROKEN LINKS:")
        for item in broken:
            print(" -", item)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
