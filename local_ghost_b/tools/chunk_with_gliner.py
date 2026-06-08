"""Chunk a markdown doc + tag entities with GLiNER, emit Ghost B chunks JSONL.

Usage:
    python tools/chunk_with_gliner.py \\
        --md /Volumes/Flash\\ Drive/merged/flame_engine_docs_complete.md \\
        --out flame_chunks.jsonl \\
        --model urchade/gliner_medium-v2.1 \\
        --threshold 0.45

Output schema (one JSON per line):
    {"chunk_id": "...", "doc_id": "...", "text": "...",
     "entities": [{"canonical_name": "...", "entity_type": "...",
                   "surface_form": "...", "query_aliases": []}, ...]}
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

# Locked entity types — single source in pipeline_config.py. Bump
# PIPELINE_VERSION if you change them.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline_config import (  # noqa: E402
    GHOST_B_ENTITY_TYPES as GHOST_B_TYPES,
    GLINER_MODEL as _DEFAULT_GLINER_MODEL,
    GLINER_THRESHOLD as _DEFAULT_GLINER_THRESHOLD,
    CHUNKER_TARGET_CHARS as _DEFAULT_TARGET_CHARS,
    CHUNKER_MIN_CHARS as _DEFAULT_MIN_CHARS,
)


def strip_frontmatter(md: str) -> str:
    if md.startswith("---"):
        end = md.find("\n---", 3)
        if end != -1:
            return md[end + 4 :].lstrip("\n")
    return md


# Fenced code blocks ```...``` (multi-line, any language tag) leak code
# identifiers like `onLoad`, `Vector2`, `SpriteComponent` into GLiNER as junk
# Software/Concept hits. Strip them before tagging.
_FENCED_CODE = re.compile(r"```[a-zA-Z0-9_+\-]*\n.*?```", re.DOTALL)
# Inline backticks `flame` / `pubspec.yaml` — short noisy spans. Strip them too.
_INLINE_CODE = re.compile(r"`[^`\n]{1,80}`")
# Markdown link `[text](url)` — keep text, drop url. URLs were tagging as Software.
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)\s]+)\)")
# Bare URL fragments.
_BARE_URL = re.compile(r"https?://\S+")


def strip_noise(md: str) -> str:
    """Remove markdown features that corrupt entity extraction."""
    md = _FENCED_CODE.sub(" ", md)
    md = _MD_LINK.sub(r"\1", md)
    md = _BARE_URL.sub(" ", md)
    md = _INLINE_CODE.sub(" ", md)
    return md


def chunk_markdown(md: str, target_chars: int = 600, min_chars: int = 150) -> list[str]:
    """Split markdown into chunks ~target_chars long.

    Strategy: split on blank lines (paragraphs / fences), then greedily pack
    paragraphs until target_chars, flushing on heading boundaries.
    Code blocks and link URLs are stripped first so GLiNER sees prose only.
    """
    md = strip_frontmatter(md)
    md = strip_noise(md)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", md) if p.strip()]

    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0

    def flush() -> None:
        nonlocal buf, buf_len
        if buf:
            text = "\n\n".join(buf).strip()
            if text:
                chunks.append(text)
            buf = []
            buf_len = 0

    for para in paragraphs:
        is_heading = para.lstrip().startswith("#")
        # Heading flushes the prior buffer (so headings start new chunks).
        if is_heading and buf_len >= min_chars:
            flush()
        buf.append(para)
        buf_len += len(para) + 2
        if buf_len >= target_chars:
            flush()
    flush()

    # Merge any orphan tiny chunks into the next.
    merged: list[str] = []
    for c in chunks:
        if merged and len(merged[-1]) < min_chars:
            merged[-1] = merged[-1] + "\n\n" + c
        else:
            merged.append(c)
    return merged


# Surface-form blocklist: surfaces that GLiNER tags but never represent a real
# Ghost B entity (URL hosts, file paths, bare versions, all-punct fragments).
# Intentionally narrow — does NOT touch camelCase or substring duplicates,
# which were over-aggressive in v1.
_URL_HOST = re.compile(r"^[a-z0-9][a-z0-9\-]*\.(?:com|org|io|net|dev|gov|edu|ai|app|co|xyz)(?:/.*)?$", re.I)
_FILE_PATH = re.compile(r"^[a-z0-9_\-/]+\.(?:py|js|ts|tsx|jsx|yaml|yml|json|toml|sh|md|html?|css|dart|kt|swift|rs|go|java|cpp|c|h|hpp|png|jpg|gif|svg|pdf)$", re.I)
_ALL_PUNCT = re.compile(r"^[^a-zA-Z0-9]+$")
_VERSION = re.compile(r"^v?\d+(\.\d+){1,3}$")


# Citation patterns: "Smith et al.", "Jones et al., 2024", "Liu & Wang" — never
# standalone entities, always references. (Broken HTML entities like "&amp;"
# also fail here.)
_CITATION = re.compile(r"\bet\s*al\b|&amp;|\(\d{4}\)|\b(19|20)\d{2}\b")
# Generic-noun "Person" mis-tags from academic prose.
_GENERIC_PERSON = {"researchers", "authors", "users", "developers", "engineers",
                   "scientists", "students", "owners", "experts"}


def is_junk_surface(s: str, label: str = "") -> bool:
    s = s.strip()
    if not s or len(s) < 2:
        return True
    if _ALL_PUNCT.match(s):
        return True
    if _URL_HOST.match(s):
        return True
    if _VERSION.match(s):
        return True
    if _FILE_PATH.match(s):
        return True
    if _CITATION.search(s):
        return True
    if label == "Person" and s.lower() in _GENERIC_PERSON:
        return True
    return False


def dedupe_entities(raw_ents: list[dict]) -> list[dict]:
    """Collapse GLiNER hits with same canonical form, drop junk surfaces."""
    by_key: dict[tuple[str, str], dict] = {}
    for e in raw_ents:
        surface = (e.get("text") or "").strip()
        label = e.get("label") or "Concept"
        if not surface or is_junk_surface(surface, label):
            continue
        canonical = surface.lower()
        key = (canonical, label)
        if key not in by_key:
            by_key[key] = {
                "canonical_name": canonical,
                "entity_type": label,
                "surface_form": surface,
                "query_aliases": [],
                "_score": float(e.get("score") or 0.0),
            }
        else:
            slot = by_key[key]
            if surface != slot["surface_form"] and surface not in slot["query_aliases"]:
                slot["query_aliases"].append(surface)
    out = []
    for v in by_key.values():
        v.pop("_score", None)
        out.append(v)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default=_DEFAULT_GLINER_MODEL)
    ap.add_argument("--threshold", type=float, default=_DEFAULT_GLINER_THRESHOLD)
    ap.add_argument("--target-chars", type=int, default=_DEFAULT_TARGET_CHARS)
    ap.add_argument("--min-chars", type=int, default=_DEFAULT_MIN_CHARS)
    args = ap.parse_args()

    md_path = Path(args.md)
    if not md_path.exists():
        sys.exit(f"missing: {md_path}")

    md_text = md_path.read_text(encoding="utf-8", errors="replace")
    chunks = chunk_markdown(md_text, target_chars=args.target_chars, min_chars=args.min_chars)
    print(f"[chunker] {len(chunks)} chunks from {md_path.name}", flush=True)

    # doc_id: stable sha256 of filename + first 64 bytes (deterministic, simple).
    doc_seed = (md_path.name + md_text[:64]).encode("utf-8")
    doc_id = hashlib.sha256(doc_seed).hexdigest()

    print(f"[gliner] loading {args.model} ...", flush=True)
    from gliner import GLiNER
    model = GLiNER.from_pretrained(args.model)
    print(f"[gliner] loaded; labels={GHOST_B_TYPES}", flush=True)

    n_ents = 0
    with Path(args.out).open("w", encoding="utf-8") as f:
        for i, text in enumerate(chunks):
            raw = model.predict_entities(text, GHOST_B_TYPES, threshold=args.threshold)
            ents = dedupe_entities(raw)
            n_ents += len(ents)
            chunk_id = f"{doc_id}_{i:04d}"
            row = {
                "chunk_id": chunk_id,
                "doc_id": doc_id,
                "text": text,
                "entities": ents,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"  chunk {i:02d}: {len(text)} chars, {len(ents)} entities", flush=True)
    print(f"[done] {len(chunks)} chunks, {n_ents} entities -> {args.out}", flush=True)


if __name__ == "__main__":
    main()
