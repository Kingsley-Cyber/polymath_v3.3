"""
Docling adapter — thin httpx client for the docling sidecar.

The sidecar (`docling_svc/`) wraps IBM Docling so the backend image stays
free of torch / transformers / accelerate. Backend code only ever sees the
adapter's `parse_document(...) -> DoclingParseResult` contract.

Responsibilities split:
  • Sidecar parses bytes → DoclingDocument → flat sections + markdown.
  • PDF uploads are text-first. Local pypdf extraction is the only PDF text
    recovery path; OCR is disabled by policy and `do_ocr=True` is ignored.
  • Adapter does ONE pre-processing step: when the upload looks like a
    structurally-implicit `.txt` file, run `inject_synthetic_headers` first
    so docling sees real `#`/`##` markers and can promote tier_b_plus.
  • Adapter then asks the sidecar to parse the (possibly augmented) bytes
    and packages the response into a `DoclingParseResult` ready for
    `source_classifier` and `tier_chunker` to consume.
"""

from __future__ import annotations

import logging
import os
import re
from io import BytesIO
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from models.schemas import SourceTier
from services.ingestion.b_plus_normalizer import inject_synthetic_headers

logger = logging.getLogger(__name__)

DOCLING_URL = os.getenv("DOCLING_URL", "http://docling:8500")
# Sidecar timeout. OCR is disabled; this mainly protects large layout parses
# for non-PDF formats and unusual PDFs routed to Docling without OCR.
DOCLING_TIMEOUT_SECONDS = float(os.getenv("DOCLING_TIMEOUT_SECONDS", "600"))
DOCLING_SIDECAR_POLICY = os.getenv("DOCLING_SIDECAR_POLICY", "auto").strip().lower()
DOCLING_AUTO_UNLOAD_AFTER_PARSE = (
    os.getenv("DOCLING_AUTO_UNLOAD_AFTER_PARSE", "false").strip().lower()
    in {"1", "true", "yes", "on"}
)

_PLAIN_TEXT_MIMES = {"text/plain"}
_PLAIN_TEXT_EXTS = {".txt", ".text", ".log"}
_MARKDOWN_MIMES = {"text/markdown", "text/x-markdown"}
_MARKDOWN_EXTS = {".md", ".markdown", ".mdown", ".mkd"}
_HTML_MIMES = {"text/html", "application/xhtml+xml"}
_HTML_EXTS = {".html", ".htm", ".xhtml"}
_DOCX_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_BINARY_DOC_EXTS = {
    ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".epub", ".odt",
    ".ods", ".odp", ".rtf", ".msg", ".eml",
}
# Code lane Phase 1 — extension → tree-sitter language tag. Files matching
# any of these extensions bypass the Docling sidecar entirely via the early-
# intercept gate at the top of parse_document(). The chunker then routes
# them through code_splitter.pack() which respects EMBEDDER_SAFE_MAX_TOKENS.
# Detection is O(1) (single suffix lookup); doc_id is sha256 of raw text
# downstream, so idempotency is preserved across runs.
_CODE_EXT_TO_LANGUAGE: dict[str, str] = {
    # ─── Mainstream programming languages ───────────────────────────────
    ".py": "python",   ".pyi": "python",
    ".rs": "rust",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".go": "go",
    ".lua": "lua",     ".luau": "luau",
    ".cpp": "cpp",     ".cc": "cpp",       ".cxx": "cpp",      ".hpp": "cpp",
    ".c": "c",         ".h": "c",
    ".cu": "cuda",     ".cuh": "cuda",                          # CUDA kernels
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",   ".kts": "kotlin",
    ".scala": "scala",
    ".sh": "bash",     ".bash": "bash",    ".zsh": "bash",
    ".ps1": "powershell",
    ".sql": "sql",
    ".r": "r",
    ".cs": "csharp",
    ".ex": "elixir",   ".exs": "elixir",
    ".hs": "haskell",
    ".ml": "ocaml",
    ".cljs": "clojure",".clj": "clojure",  ".edn": "clojure",
    ".dart": "dart",
    ".zig": "zig",
    ".nix": "nix",
    ".m": "objc",      ".mm": "objc",                           # Objective-C / Objective-C++
    # ─── Shaders ────────────────────────────────────────────────────────
    ".glsl": "glsl",   ".frag": "glsl",    ".vert": "glsl",
    ".hlsl": "hlsl",
    # ─── Web frameworks ─────────────────────────────────────────────────
    ".vue": "vue",
    ".svelte": "svelte",
    # ─── Styling ────────────────────────────────────────────────────────
    # HTML uploads default to the local_html prose extractor below. That is
    # the safer RAG default for web/document exports: strip navigation,
    # scripts, style, and markup before chunking. Source-code HTML should be
    # wrapped in a code fence inside markdown or renamed before ingest.
    ".css": "css",     ".scss": "css",
    # ─── XML family ─────────────────────────────────────────────────────
    # Apple property lists, Storyboards, Xibs, entitlements; Roblox place
    # & model files (decomposed XML format from rbx-dom).
    ".xml": "xml",
    ".plist": "xml",   ".storyboard": "xml",
    ".xib": "xml",     ".entitlements": "xml",
    ".rbxmx": "xml",   ".rbxlx": "xml",
    # ─── Data / config ──────────────────────────────────────────────────
    # Parsed by the pack so we get atomic packing (never mid-record splits)
    # and Ghost B skips them. symbols_defined will usually be empty —
    # these are data, not callable code.
    ".json": "json",   ".jsonl": "json",
    ".ipynb": "json",  # Jupyter notebooks (cell-level extraction is Phase 2)
    ".yaml": "yaml",   ".yml": "yaml",
    ".toml": "toml",   # pyproject.toml, Cargo.toml, etc.
    ".ini": "ini",     ".cfg": "ini",
    # ─── IaC / build tooling ────────────────────────────────────────────
    ".tf": "hcl",      ".tfvars": "hcl",   ".hcl": "hcl",
    # ─── API schemas ────────────────────────────────────────────────────
    ".proto": "proto",                                          # Protocol Buffers
    ".graphql": "graphql", ".gql": "graphql",
}

# Filenames with no extension that should route through the code lane.
# Looked up case-insensitively before the extension map fires.
_CODE_FILENAME_TO_LANGUAGE: dict[str, str] = {
    "dockerfile":     "dockerfile",
    "containerfile":  "dockerfile",  # Podman / OCI alias
    "makefile":       "make",
    "gnumakefile":    "make",
    "cmakelists.txt": "cmake",
}
_FAST_PDF_MIN_TOTAL_CHARS = 1200
_FAST_PDF_MIN_AVG_CHARS_PER_PAGE = 80
_FAST_PDF_MIN_NONEMPTY_PAGE_RATIO = 0.25
_FAST_PDF_MAX_REPLACEMENT_RATIO = 0.03


def _looks_like_pdf(filename: str, mime: str) -> bool:
    return (mime or "").lower() == "application/pdf" or (filename or "").lower().endswith(".pdf")


def _extension(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def _looks_like_markdown(filename: str, mime: str) -> bool:
    return (mime or "").lower() in _MARKDOWN_MIMES or _extension(filename) in _MARKDOWN_EXTS


# YAML frontmatter: `---` on line 1, closing `---`/`...` within the first 4 KB.
# Scraped/exported markdown (e.g. the merged corpus: source_url / site_name /
# priority / extracted headers) carries it on every file. Left in place it
# leaks into the first chunk as embedding noise AND fires fact cues — the
# `extracted: 2026-03-24` line became a confident-but-junk timestamp fact in
# the Phase A smoke. It is metadata, not content; strip before sectioning.
_FRONTMATTER_RE = re.compile(
    r"\A﻿?---[ \t]*\r?\n.{0,4096}?\r?\n(?:---|\.\.\.)[ \t]*\r?\n",
    re.DOTALL,
)


def _strip_yaml_frontmatter(text: str) -> str:
    if not text or not text.lstrip("﻿").startswith("---"):
        return text
    stripped = _FRONTMATTER_RE.sub("", text, count=1)
    if stripped is not text:
        logger.info("local_markdown: stripped YAML frontmatter (%d chars)",
                    len(text) - len(stripped))
    return stripped


# Bold-key metadata line: `**Source:** https://…`, `**Extracted:** 2026-03-24`.
# Scraper/export tooling writes a run of these right under the document title —
# a second metadata layer alongside YAML frontmatter. Like frontmatter, it is
# provenance, not content: it embeds as a junk chunk and its dates/URLs sit one
# entity-mention away from becoming junk facts.
_BOLD_KEY_LINE = re.compile(r"^\*\*[^*\n]{1,32}:\*\*\s")
_MD_TITLE = re.compile(r"^#{1,6}\s")


def _strip_leading_metadata_block(text: str) -> str:
    """Drop a doc-START run of 2+ bold-key metadata lines (blank lines allowed
    between them), preserving an optional leading title heading. A single bold
    note is left alone — only a BLOCK signals export metadata."""
    if not text:
        return text
    lines = text.split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and _MD_TITLE.match(lines[i].strip()):
        i += 1
    j, n_meta = i, 0
    while j < len(lines):
        s = lines[j].strip()
        if not s:
            j += 1
            continue
        if _BOLD_KEY_LINE.match(s):
            n_meta += 1
            j += 1
            continue
        break
    if n_meta < 2:
        return text
    logger.info("local_markdown: stripped %d leading metadata lines", n_meta)
    return "\n".join(lines[:i] + [""] + lines[j:])


def _looks_like_html(filename: str, mime: str) -> bool:
    return (mime or "").lower() in _HTML_MIMES or _extension(filename) in _HTML_EXTS


def _looks_like_docx(filename: str, mime: str) -> bool:
    return (mime or "").lower() in _DOCX_MIMES or _extension(filename) == ".docx"


def _looks_like_code(filename: str) -> bool:
    basename = Path(filename or "").name.lower()
    return basename in _CODE_FILENAME_TO_LANGUAGE or _extension(filename) in _CODE_EXT_TO_LANGUAGE


def docling_sidecar_needed(filename: str, mime: str) -> bool:
    """True only for formats that cannot use the local parser path."""
    if _looks_like_code(filename):
        return False
    if _looks_like_pdf(filename, mime):
        return False
    if _looks_like_markdown(filename, mime):
        return False
    if _looks_like_html(filename, mime):
        return False
    if _looks_like_docx(filename, mime):
        return False
    if _looks_like_plain_text(filename, mime):
        return False
    return True


def parser_strategy(filename: str, mime: str) -> str:
    """Human-readable parse lane for diagnostics and tests."""
    if _looks_like_code(filename):
        return "local_code"
    if _looks_like_pdf(filename, mime):
        return "local_pdf_fast_text"
    if _looks_like_markdown(filename, mime):
        return "local_markdown"
    if _looks_like_html(filename, mime):
        return "local_html"
    if _looks_like_docx(filename, mime):
        return "local_docx"
    if _looks_like_plain_text(filename, mime):
        return "local_text"
    return "docling_sidecar"


@dataclass
class Section:
    heading_path: list[str]
    text: str
    element_type: str  # "section_heading" | "paragraph" | "code_block" | ...
    level: int | None = None
    # Code lane: language tag (e.g. "python", "luau"); None for prose sections.
    language: str | None = None
    # Structured element metadata. Used by local markdown/table parsing while
    # keeping sidecar-produced prose/code sections backward-compatible.
    metadata: dict = field(default_factory=dict)


@dataclass
class DoclingParseResult:
    text: str
    markdown: str
    sections: list[Section]
    pages: list[str] | None
    has_structure: bool
    source_tier: SourceTier
    h1_count: int = 0
    h2_count: int = 0
    num_pages: int = 1
    source_format: str = ""
    augmented_with_synthetic_headers: bool = False
    injected_headers_audit: list[dict] = field(default_factory=list)
    # Code lane: filled by the early-intercept gate for code files (.py/.rs/etc).
    # None for prose / markdown / PDF / EPUB ingest.
    language: str | None = None
    # Original upload filename — needed by tier_chunker to stamp file_path
    # into per-chunk metadata for the code lane.
    filename: str | None = None


def _looks_like_plain_text(filename: str, mime: str) -> bool:
    ext = _extension(filename)
    mime_l = (mime or "").lower()
    if mime_l in _PLAIN_TEXT_MIMES:
        return True
    if ext in _PLAIN_TEXT_EXTS:
        return True
    # Browsers sometimes upload markdown/text as octet-stream. Accept only
    # known text-like extensions here so office files do not get decoded as
    # mojibake just because the client supplied a vague MIME.
    return mime_l == "application/octet-stream" and ext in (_PLAIN_TEXT_EXTS | _MARKDOWN_EXTS)


_FENCE_OPEN_RE = re.compile(r"^```([a-zA-Z0-9_+\-]*)\s*$")
_FENCE_CLOSE_RE = re.compile(r"^```\s*$")
_HEADING_ANCHOR_RE = re.compile(r"\s*\{#[^\n}]*\}\s*$")
_TABLE_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")
_TABLE_CAPTION_RE = re.compile(
    r"^(?:table|tbl\.?)\s*[\w.\-]+(?:\s*[:.\-]\s+|\s+).+",
    re.IGNORECASE,
)


def _split_markdown_table_row(line: str) -> list[str]:
    """Split a GitHub-style pipe table row into cleaned cells."""
    row = (line or "").strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [
        cell.replace(r"\|", "|").strip()
        for cell in re.split(r"(?<!\\)\|", row)
    ]


# Inline markdown link `[text](url)` / `[text](url "title")` — keep the
# visible text, drop the target. Scraped docs carry anchor links on every
# heading (`Getting Started[¶](https://…#x "Link to this heading")`) and the
# URLs/pilcrows were the last embedding-noise class left after the
# frontmatter/metadata strips. Applied to prose paragraphs, heading titles,
# and table cells — NEVER to code blocks (they're sectioned separately and
# must stay verbatim).
_IMG_MD_RE = re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)")  # drop images entirely (alt incl.)
_INLINE_MD_LINK_RE = re.compile(r"\[([^\]\n]*)\]\((?:[^)\s]+(?:\s+\"[^\"]*\")?)\)")
_BARE_URL_RE = re.compile(r"https?://\S+")
_TRANSCRIPT_HEADER_RE = re.compile(
    r"^\s*(Video|URL|Duration|Segments|Source|Date)\s*:\s*(.*?)\s*$",
    re.IGNORECASE,
)
_TRANSCRIPT_SEGMENT_RE = re.compile(
    r"^\s*\[(?P<time>(?:\d+:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?)\]\s*(?P<text>.+?)\s*$"
)


def _scrub_inline_links(text: str) -> str:
    if not text or ("[" not in text and "http" not in text and "¶" not in text):
        return text
    t = _IMG_MD_RE.sub("", text)  # before link rewrite, or `![x](u)` leaves a stray `!`
    t = _INLINE_MD_LINK_RE.sub(r"\1", t)
    t = _BARE_URL_RE.sub("", t)
    t = t.replace("¶", "")
    return re.sub(r"[ \t]{2,}", " ", t)


def _parse_transcript_text_document(text: str, filename: str) -> DoclingParseResult | None:
    """Parse timestamped transcript exports into bounded semantic blocks.

    Expected shape is deterministic YouTube/transcript text:

        Video: ...
        URL: ...
        Duration: ...
        Segments: ...
        Source: YouTube Transcript API
        Date: ...

        [0:00] speech text
        [0:03] more speech text

    Header fields become chunk metadata; timestamped speech becomes the only
    substantive retrieval text. This prevents the first child chunk from being
    mostly `URL`/`Duration`/`Segments` metadata while preserving time ranges
    for source display and later citation jumps.
    """
    if not text or "[" not in text:
        return None

    headers: dict[str, str] = {}
    segments: list[dict[str, str]] = []
    seen_segment = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        match = _TRANSCRIPT_SEGMENT_RE.match(line)
        if match:
            seen_segment = True
            spoken = re.sub(r"\s+", " ", match.group("text")).strip()
            if spoken:
                segments.append(
                    {
                        "time": match.group("time").replace(",", "."),
                        "text": spoken,
                    }
                )
            continue

        header = _TRANSCRIPT_HEADER_RE.match(line)
        if header and not seen_segment:
            key = header.group(1).lower()
            value = re.sub(r"\s+", " ", header.group(2)).strip()
            if value:
                headers[key] = value
            continue

        # Transcript continuation line. Some transcript tools wrap long
        # captions without repeating a timestamp. Attach those words to the
        # previous timed segment instead of creating an untimed junk paragraph.
        if seen_segment and segments:
            continuation = re.sub(r"\s+", " ", line).strip()
            segments[-1]["text"] = f"{segments[-1]['text']} {continuation}".strip()

    source_hint = f" {headers.get('source', '')} {headers.get('url', '')} {filename} ".lower()
    looks_like_transcript = (
        len(segments) >= 3
        and (
            "youtube transcript" in source_hint
            or "youtu.be" in source_hint
            or "youtube.com" in source_hint
            or "segments" in headers
            or "duration" in headers
        )
    )
    if not looks_like_transcript:
        return None

    title = headers.get("video") or filename or "Transcript"
    max_words_per_block = 120
    grouped: list[tuple[int, int, list[dict[str, str]]]] = []
    start = 0
    buf: list[dict[str, str]] = []
    word_count = 0
    for idx, segment in enumerate(segments):
        words = len(segment["text"].split())
        if buf and word_count + words > max_words_per_block:
            grouped.append((start, idx - 1, buf))
            start = idx
            buf = []
            word_count = 0
        buf.append(segment)
        word_count += words
    if buf:
        grouped.append((start, start + len(buf) - 1, buf))

    sections: list[Section] = [
        Section(
            heading_path=[title],
            text=title,
            element_type="section_heading",
            level=1,
        )
    ]

    for start_idx, end_idx, group in grouped:
        time_start = group[0]["time"]
        time_end = group[-1]["time"]
        speech = " ".join(item["text"] for item in group)
        block_text = (
            f"Video: {title}\n"
            f"Transcript range: {time_start}-{time_end}\n\n"
            f"{speech}"
        )
        sections.append(
            Section(
                heading_path=[title],
                text=block_text,
                element_type="transcript_block",
                metadata={
                    "source_format": "youtube_transcript",
                    "video_title": title,
                    "url": headers.get("url", ""),
                    "duration": headers.get("duration", ""),
                    "segments_declared": headers.get("segments", ""),
                    "source": headers.get("source", ""),
                    "date": headers.get("date", ""),
                    "time_start": time_start,
                    "time_end": time_end,
                    "segment_start": start_idx,
                    "segment_end": end_idx,
                },
            )
        )

    return DoclingParseResult(
        text="\n\n".join(section.text for section in sections),
        markdown="\n\n".join(section.text for section in sections),
        sections=sections,
        pages=None,
        has_structure=True,
        source_tier=SourceTier.tier_b,
        h1_count=1,
        h2_count=0,
        source_format="youtube_transcript",
        filename=filename,
    )


def _clean_heading_title(title: str) -> str:
    """Strip markdown anchor IDs + inline links from visible heading metadata."""
    return _scrub_inline_links(_HEADING_ANCHOR_RE.sub("", title or "")).strip()


def _is_markdown_table_separator(line: str) -> bool:
    cells = _split_markdown_table_row(line)
    if len(cells) < 2:
        return False
    return all(_TABLE_SEPARATOR_CELL_RE.match(cell.strip()) for cell in cells)


def _is_markdown_table_start(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    header = lines[index]
    separator = lines[index + 1]
    if "|" not in header or "|" not in separator:
        return False
    header_cells = _split_markdown_table_row(header)
    if len([cell for cell in header_cells if cell.strip()]) < 2:
        return False
    if not _is_markdown_table_separator(separator):
        return False
    separator_cells = _split_markdown_table_row(separator)
    return len(separator_cells) == len(header_cells)


def _consume_markdown_table(
    lines: list[str],
    index: int,
) -> tuple[list[str], list[str], list[list[str]], int]:
    """Return raw table lines, columns, rows, and next index."""
    raw_lines = [lines[index], lines[index + 1]]
    columns = _split_markdown_table_row(lines[index])
    rows: list[list[str]] = []
    j = index + 2
    while j < len(lines):
        row_line = lines[j]
        if not row_line.strip() or "|" not in row_line:
            break
        cells = _split_markdown_table_row(row_line)
        if len(cells) < 2:
            break
        raw_lines.append(row_line)
        rows.append(cells)
        j += 1
    return raw_lines, columns, rows, j


def _pop_table_caption(paragraph: list[str]) -> str:
    """Detach an immediate `Table N. ...` caption from the prose buffer."""
    if not paragraph:
        return ""
    i = len(paragraph) - 1
    while i >= 0 and not paragraph[i].strip():
        i -= 1
    if i < 0:
        return ""
    candidate = paragraph[i].strip()
    if len(candidate) > 240 or not _TABLE_CAPTION_RE.match(candidate):
        return ""
    del paragraph[i:]
    while paragraph and not paragraph[-1].strip():
        paragraph.pop()
    return candidate


def _table_label(caption: str, table_index: int) -> str:
    if caption:
        match = re.match(r"^((?:table|tbl\.?)\s*[\w.\-]+)", caption, re.IGNORECASE)
        if match:
            return match.group(1).replace("tbl.", "Table").strip()
    return f"Table {table_index}"


def _linearize_markdown_table(
    *,
    heading_path: list[str],
    caption: str,
    table_index: int,
    columns: list[str],
    rows: list[list[str]],
) -> str:
    """Render a markdown table as row-wise text for embeddings and extraction."""
    cleaned_columns = [
        re.sub(r"\s+", " ", col).strip() or f"column_{i + 1}"
        for i, col in enumerate(columns)
    ]
    lines: list[str] = [f"Table: {_table_label(caption, table_index)}"]
    if heading_path:
        lines.append(f"Section: {' > '.join(heading_path)}")
    if caption:
        lines.append(f"Caption: {caption}")
    lines.append(f"Columns: {' | '.join(cleaned_columns)}")
    lines.append("")

    for row_idx, row in enumerate(rows, start=1):
        padded = list(row[: len(cleaned_columns)])
        if len(padded) < len(cleaned_columns):
            padded.extend([""] * (len(cleaned_columns) - len(padded)))
        pairs = []
        for col, cell in zip(cleaned_columns, padded):
            value = _scrub_inline_links(re.sub(r"\s+", " ", cell)).strip()
            if value:
                pairs.append(f"{col}={value}")
        if pairs:
            lines.append(f"Row {row_idx}: " + "; ".join(pairs))

    return "\n".join(lines).strip()


def _markdown_sections(markdown: str) -> tuple[list[Section], int, int]:
    """Local Markdown section walker, code-fence-aware.

    Headings define the hierarchy; paragraph text inherits the active heading
    path. Triple-backtick fenced code blocks are emitted as separate
    Section(element_type="code_block", language=<tag>) entries so the code lane
    in tier_chunker can route them through code_splitter.pack() instead of the
    prose sentence/token splitters. Tilde fences (~~~) and indented fences fall
    through as prose — Phase 1 limitation, documented for later expansion.
    """
    sections: list[Section] = []
    heading_stack: list[str] = []
    current_path: list[str] = []
    paragraph: list[str] = []
    h1_count = 0
    h2_count = 0
    table_count = 0

    def flush_paragraph() -> None:
        nonlocal paragraph
        text = _scrub_inline_links("\n".join(paragraph)).strip()
        if text:
            sections.append(
                Section(
                    heading_path=list(current_path),
                    text=text,
                    element_type="paragraph",
                )
            )
        paragraph = []

    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]

        # Fence open? Consume until matching close (or EOF).
        fence_open = _FENCE_OPEN_RE.match(line.strip())
        if fence_open is not None:
            flush_paragraph()
            language = (fence_open.group(1) or "").lower()
            fence_lines: list[str] = [line]
            j = i + 1
            while j < len(lines):
                fence_lines.append(lines[j])
                if _FENCE_CLOSE_RE.match(lines[j].strip()):
                    j += 1
                    break
                j += 1
            sections.append(
                Section(
                    heading_path=list(current_path),
                    text="\n".join(fence_lines),
                    element_type="code_block",
                    language=language or None,
                )
            )
            i = j
            continue

        if _is_markdown_table_start(lines, i):
            caption = _pop_table_caption(paragraph)
            flush_paragraph()
            table_count += 1
            _raw_lines, columns, rows, j = _consume_markdown_table(lines, i)
            metadata = {
                "table_index": table_count,
                "caption": caption,
                "columns": [
                    re.sub(r"\s+", " ", col).strip() or f"column_{idx + 1}"
                    for idx, col in enumerate(columns)
                ],
                "row_count": len(rows),
                "source_format": "markdown_pipe_table",
            }
            sections.append(
                Section(
                    heading_path=list(current_path),
                    text=_linearize_markdown_table(
                        heading_path=list(current_path),
                        caption=caption,
                        table_index=table_count,
                        columns=columns,
                        rows=rows,
                    ),
                    element_type="table",
                    metadata=metadata,
                )
            )
            i = j
            continue

        match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line.strip())
        if not match:
            paragraph.append(line)
            i += 1
            continue

        flush_paragraph()
        level = len(match.group(1))
        title = _clean_heading_title(match.group(2).strip())
        if not title:
            i += 1
            continue
        if level == 1:
            h1_count += 1
        elif level == 2:
            h2_count += 1
        heading_stack = heading_stack[: level - 1]
        while len(heading_stack) < level - 1:
            heading_stack.append("")
        heading_stack.append(title)
        current_path = [part for part in heading_stack if part]
        sections.append(
            Section(
                heading_path=list(current_path),
                text=title,
                element_type="section_heading",
                level=level,
            )
        )
        i += 1

    flush_paragraph()
    return sections, h1_count, h2_count


def _parse_local_text_document(raw_bytes: bytes, filename: str, mime: str) -> DoclingParseResult | None:
    """Parse text/markdown/html without the Docling sidecar.

    Local parsing keeps default Docker startup API-first. Docling remains an
    explicit profile for formats that truly need layout-aware conversion.
    """
    if _looks_like_html(filename, mime):
        from services.ingestion.format_router import route

        decoded = route(raw_bytes, filename=filename, mime_hint=mime)
        text = decoded.text or ""
        return DoclingParseResult(
            text=text,
            markdown=text,
            sections=[],
            pages=None,
            has_structure=False,
            source_tier=SourceTier.tier_b,
            source_format="local_html",
        )

    if _looks_like_markdown(filename, mime):
        markdown = _strip_leading_metadata_block(
            _strip_yaml_frontmatter(raw_bytes.decode("utf-8", errors="replace")))
        sections, h1, h2 = _markdown_sections(markdown)
        has_tables = any(s.element_type == "table" for s in sections)
        has_structure = (h1 + h2) > 0 or has_tables
        source_tier = (
            SourceTier.tier_a
            if (h1 + h2) > 0
            else SourceTier.tier_b if has_tables else SourceTier.tier_c
        )
        return DoclingParseResult(
            text=markdown,
            markdown=markdown,
            sections=sections,
            pages=None,
            has_structure=has_structure,
            source_tier=source_tier,
            h1_count=h1,
            h2_count=h2,
            source_format="local_markdown",
            filename=filename,
        )

    if _looks_like_docx(filename, mime):
        docx_result = _parse_local_docx_document(raw_bytes, filename)
        if docx_result is not None:
            return docx_result

    if not _looks_like_plain_text(filename, mime):
        return None

    raw_text = raw_bytes.decode("utf-8", errors="replace")
    transcript_result = _parse_transcript_text_document(raw_text, filename)
    if transcript_result is not None:
        return transcript_result

    aug_bytes, aug_filename, aug_mime, augmented, audit = _maybe_augment_plaintext(
        raw_bytes, filename, mime
    )
    text = aug_bytes.decode("utf-8", errors="replace")
    sections, h1, h2 = _markdown_sections(text)
    has_tables = any(s.element_type == "table" for s in sections)
    has_structure = (augmented and (h1 + h2) > 0) or has_tables
    source_tier = (
        SourceTier.tier_b_plus
        if augmented and (h1 + h2) > 0
        else SourceTier.tier_b if has_tables else SourceTier.tier_c
    )
    return DoclingParseResult(
        text=text,
        markdown=text,
        sections=sections,
        pages=None,
        has_structure=has_structure,
        source_tier=source_tier,
        h1_count=h1,
        h2_count=h2,
        source_format="local_text",
        augmented_with_synthetic_headers=augmented,
        injected_headers_audit=audit,
        filename=filename,
    )


def _parse_local_docx_document(
    raw_bytes: bytes,
    filename: str,
) -> DoclingParseResult | None:
    """Parse DOCX headings/paragraphs locally when python-docx is installed."""
    try:
        from docx import Document
    except Exception:
        return None

    try:
        doc = Document(BytesIO(raw_bytes))
    except Exception:
        return None

    sections: list[Section] = []
    markdown_lines: list[str] = []
    text_blocks: list[str] = []
    heading_stack: list[str] = []
    h1_count = 0
    h2_count = 0

    for paragraph in doc.paragraphs:
        text = (paragraph.text or "").strip()
        if not text:
            continue
        style_name = str(getattr(paragraph.style, "name", "") or "")
        heading_match = re.match(r"Heading\s+([1-6])$", style_name, re.IGNORECASE)
        if heading_match:
            level = int(heading_match.group(1))
            if level == 1:
                h1_count += 1
            elif level == 2:
                h2_count += 1
            heading_stack = heading_stack[: level - 1]
            while len(heading_stack) < level - 1:
                heading_stack.append("")
            heading_stack.append(text)
            path = [part for part in heading_stack if part]
            markdown_lines.append(f"{'#' * level} {text}")
            sections.append(
                Section(
                    heading_path=list(path),
                    text=text,
                    element_type="section_heading",
                    level=level,
                )
            )
            continue

        path = [part for part in heading_stack if part]
        markdown_lines.append(text)
        text_blocks.append(text)
        sections.append(
            Section(
                heading_path=list(path),
                text=text,
                element_type="paragraph",
                level=None,
            )
        )

    for table_index, table in enumerate(doc.tables, start=1):
        rows: list[list[str]] = []
        for row in table.rows:
            cells = [re.sub(r"\s+", " ", (cell.text or "").strip()) for cell in row.cells]
            if any(cells):
                rows.append(cells)
        if not rows:
            continue
        width = max(len(row) for row in rows)
        padded = [row + [""] * (width - len(row)) for row in rows]
        header = padded[0]
        body = padded[1:]
        markdown_lines.append("| " + " | ".join(header) + " |")
        markdown_lines.append("| " + " | ".join(["---"] * width) + " |")
        for row in body:
            markdown_lines.append("| " + " | ".join(row) + " |")
        text_blocks.extend(" | ".join(row) for row in padded)
        sections.append(
            Section(
                heading_path=[part for part in heading_stack if part],
                text="\n".join(" | ".join(row) for row in padded),
                element_type="table",
                level=None,
                metadata={"table_index": table_index, "row_count": len(rows)},
            )
        )

    if not sections:
        return None

    markdown = "\n\n".join(line for line in markdown_lines if line.strip())
    text = "\n\n".join(text_blocks) or markdown
    has_structure = (h1_count + h2_count) > 0 or any(
        section.element_type == "table" for section in sections
    )
    return DoclingParseResult(
        text=text,
        markdown=markdown,
        sections=sections,
        pages=None,
        has_structure=has_structure,
        source_tier=SourceTier.tier_a if has_structure else SourceTier.tier_c,
        h1_count=h1_count,
        h2_count=h2_count,
        num_pages=1,
        source_format="local_docx",
        filename=filename,
    )


def _maybe_augment_plaintext(
    raw_bytes: bytes, filename: str, mime: str
) -> tuple[bytes, str, str, bool, list[dict]]:
    """Pre-step for plain-text uploads: try `inject_synthetic_headers` and
    re-route the (now markdown) bytes to docling under a `.md` filename so
    docling promotes the structure into `section_header` items.

    Returns:
        (bytes, filename, mime, augmented_flag, audit_list)
    """
    if not _looks_like_plain_text(filename, mime):
        return raw_bytes, filename, mime, False, []
    try:
        text = raw_bytes.decode("utf-8", errors="replace")
    except Exception:
        return raw_bytes, filename, mime, False, []

    normalized, audit = inject_synthetic_headers(text)
    if not audit:
        return raw_bytes, filename, mime, False, []

    base = filename.rsplit(".", 1)[0] if "." in filename else filename
    augmented_filename = f"{base}.md"
    audit_dicts = [
        {
            "line_no": h.line_no,
            "level": h.level,
            "pattern": h.pattern,
            "original_line": h.original_line,
        }
        for h in audit
    ]
    logger.info(
        "Pre-augmented plain text %s with %d synthetic headers → %s",
        filename, len(audit), augmented_filename,
    )
    return normalized.encode("utf-8"), augmented_filename, "text/markdown", True, audit_dicts


def _classify_tier(
    *,
    original_mime: str,
    original_filename: str,
    augmented: bool,
    h1_count: int,
    h2_count: int,
    num_pages: int,
    has_structure: bool,
) -> SourceTier:
    """Mirror the rules of the legacy source_classifier but feed off docling
    output instead of regex hits.

    Order:
      1. Multi-page PDF → ocr_ast (page-layout chunking)
      2. HTML / XHTML  → tier_b
      3. Augmented plain text with structure → tier_b_plus
      4. Native MD/DOCX with structure → tier_a
      5. Otherwise → tier_c
    """
    mime = (original_mime or "").lower()
    fname = (original_filename or "").lower()

    if mime == "application/pdf" and num_pages > 1:
        return SourceTier.ocr_ast
    if mime in ("text/html", "application/xhtml+xml") or fname.endswith((".html", ".htm", ".xhtml")):
        return SourceTier.tier_b
    if augmented and has_structure:
        return SourceTier.tier_b_plus
    if has_structure:
        return SourceTier.tier_a
    return SourceTier.tier_c


def _parse_pdf_fast_text(raw_bytes: bytes, filename: str, mime: str) -> DoclingParseResult:
    """Fast local PDF text extraction for digital PDFs when OCR is disabled.

    This preserves the adapter contract while bypassing Docling's layout/OCR
    sidecar. The chunker still gets page text, so large digital books can be
    searched quickly without paying the layout/OCR cost.
    """
    from services.ingestion.format_router import route

    decoded = route(raw_bytes, filename=filename, mime_hint=mime)
    text = decoded.text or ""
    pages = decoded.pages or ([text] if text.strip() else [])
    num_pages = max(1, len(pages))
    source_tier = SourceTier.ocr_ast if num_pages > 1 else SourceTier.tier_c
    logger.info(
        "Fast PDF text path for %s pages=%d chars=%d",
        filename,
        num_pages,
        len(text),
    )
    return DoclingParseResult(
        text=text,
        markdown=text,
        sections=[],
        pages=pages,
        has_structure=False,
        source_tier=source_tier,
        num_pages=num_pages,
        source_format="pypdf_fast_text",
        filename=filename,
    )


def _fast_pdf_text_is_usable(result: DoclingParseResult) -> bool:
    """Heuristic gate retained for diagnostics.

    Digital PDFs usually expose plenty of text via pypdf; scanned PDFs and
    image-heavy documents return empty or sparse pages. OCR is disabled, so a
    sparse result is returned as sparse text rather than falling into OCR.
    """
    text = result.text or result.markdown or ""
    compact = "".join(text.split())
    if not compact:
        return False

    pages = result.pages or [text]
    page_count = max(1, len(pages))
    nonempty_pages = [
        p for p in pages if len("".join((p or "").split())) >= _FAST_PDF_MIN_AVG_CHARS_PER_PAGE
    ]
    avg_chars = len(compact) / page_count
    nonempty_ratio = len(nonempty_pages) / page_count
    replacement_ratio = text.count("\ufffd") / max(1, len(text))
    min_total_chars = 300 if page_count <= 2 else _FAST_PDF_MIN_TOTAL_CHARS

    return (
        len(compact) >= min_total_chars
        and avg_chars >= _FAST_PDF_MIN_AVG_CHARS_PER_PAGE
        and nonempty_ratio >= _FAST_PDF_MIN_NONEMPTY_PAGE_RATIO
        and replacement_ratio <= _FAST_PDF_MAX_REPLACEMENT_RATIO
    )


def _sidecar_disabled() -> bool:
    return DOCLING_SIDECAR_POLICY in {"0", "false", "off", "disabled", "none", "local"}


def _docling_required_error(filename: str, mime: str) -> RuntimeError:
    return RuntimeError(
        "This upload needs the Docling sidecar because it is not markdown, "
        "plain text, code, HTML, or a fast-text PDF. Current parse strategy "
        f"for {filename!r} ({mime or 'unknown MIME'}) is docling_sidecar. "
        "Start it with `docker compose --profile local-parser up -d docling`, "
        "set DOCLING_SIDECAR_POLICY=auto, or convert the file to .md/.txt."
    )


async def unload_docling_sidecar() -> dict:
    """Ask the sidecar to release the heavy converter immediately."""
    async with httpx.AsyncClient(
        base_url=DOCLING_URL,
        timeout=httpx.Timeout(10.0, connect=3.0),
    ) as client:
        resp = await client.post("/unload")
        resp.raise_for_status()
        return resp.json()


async def parse_document(
    raw_bytes: bytes,
    filename: str,
    mime: str,
    do_ocr: bool = False,
) -> DoclingParseResult:
    """Hand the upload to the docling sidecar and return a structured
    DoclingParseResult. Plain-text uploads are pre-augmented with synthetic
    headers when `inject_synthetic_headers` finds qualifying markers.
    """
    if do_ocr:
        logger.warning("Ignoring do_ocr=True for %s; OCR is disabled by policy", filename)
        do_ocr = False

    # Code lane Phase 1 — early-intercept gate. Files matching a known code
    # extension (or a known filename like "Dockerfile" / "Makefile" /
    # "CMakeLists.txt") bypass the Docling sidecar entirely (no network hop,
    # no torch burn) and emit a synthetic DoclingParseResult carrying a single
    # code_block Section. tier_chunker routes this through code_splitter.pack().
    code_basename = Path(filename or "").name.lower()
    code_lang = (
        _CODE_FILENAME_TO_LANGUAGE.get(code_basename)
        or _CODE_EXT_TO_LANGUAGE.get(_extension(filename))
    )
    if code_lang:
        try:
            code_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            code_text = raw_bytes.decode("utf-8", errors="replace")
        return DoclingParseResult(
            text=code_text,
            markdown=code_text,
            sections=[
                Section(
                    heading_path=[filename] if filename else [],
                    text=code_text,
                    element_type="code_block",
                    level=1,
                    language=code_lang,
                )
            ],
            pages=None,
            has_structure=False,
            source_tier=SourceTier.tier_code,
            h1_count=0,
            h2_count=0,
            num_pages=1,
            source_format=f"code_{code_lang}",
            augmented_with_synthetic_headers=False,
            injected_headers_audit=[],
            language=code_lang,
            filename=filename,
        )

    if _looks_like_pdf(filename, mime):
        fast_result = _parse_pdf_fast_text(raw_bytes, filename, mime)
        if not do_ocr or _fast_pdf_text_is_usable(fast_result):
            return fast_result

    local_result = _parse_local_text_document(raw_bytes, filename, mime)
    if local_result is not None:
        return local_result

    aug_bytes, aug_filename, aug_mime, augmented, audit = _maybe_augment_plaintext(
        raw_bytes, filename, mime
    )

    if _sidecar_disabled():
        raise _docling_required_error(filename, mime)

    files = {"file": (aug_filename, aug_bytes, aug_mime)}
    data = {"do_ocr": "false"}

    try:
        async with httpx.AsyncClient(
            base_url=DOCLING_URL,
            timeout=httpx.Timeout(DOCLING_TIMEOUT_SECONDS, connect=30.0),
        ) as client:
            try:
                resp = await client.post("/parse", files=files, data=data)
            except httpx.RequestError as exc:
                raise RuntimeError(
                    "Docling parser sidecar is unavailable. Markdown, text, HTML, "
                    "and digital PDFs parse locally; this file type needs the "
                    "`local-parser` profile. Start it with "
                    "`docker compose --profile local-parser up -d docling`, or "
                    "convert the file to .md/.txt before ingest."
                ) from exc
            resp.raise_for_status()
            payload = resp.json()
    finally:
        if DOCLING_AUTO_UNLOAD_AFTER_PARSE:
            try:
                await unload_docling_sidecar()
            except Exception as exc:
                logger.debug("Docling sidecar auto-unload failed: %s", exc)

    sections = [
        Section(
            heading_path=list(s.get("heading_path") or []),
            text=s.get("text", "") or "",
            element_type=s.get("element_type", "paragraph"),
            level=s.get("level"),
            language=s.get("language"),
            metadata=s.get("metadata") or {},
        )
        for s in payload.get("sections", [])
    ]

    h1 = int(payload.get("h1_count", 0))
    h2 = int(payload.get("h2_count", 0))
    has_structure = bool(payload.get("has_structure", (h1 + h2) >= 2))
    num_pages = int(payload.get("num_pages", 1))

    tier = _classify_tier(
        original_mime=mime,
        original_filename=filename,
        augmented=augmented,
        h1_count=h1,
        h2_count=h2,
        num_pages=num_pages,
        has_structure=has_structure,
    )

    return DoclingParseResult(
        text=payload.get("text", "") or "",
        markdown=payload.get("markdown", "") or "",
        sections=sections,
        pages=payload.get("pages"),
        has_structure=has_structure,
        source_tier=tier,
        h1_count=h1,
        h2_count=h2,
        num_pages=num_pages,
        source_format=payload.get("source_format", "") or "",
        augmented_with_synthetic_headers=augmented,
        injected_headers_audit=audit,
        language=None,
        filename=filename,
    )
