"""Embedder-safe AST packing + metadata extraction for code chunks.

Code lane Phase 1 — wraps `tree_sitter_language_pack.process()` (v1.8+ high-
level batch API) so callers get a list of (slice_text, metadata_dict) tuples
where every slice fits the embedder's token cap and metadata carries the
AST-derived signals (`symbols_defined`, `imports`, `ast_signature`).

The pack already does the heavy lifting:
  - `chunk_max_size` (in bytes) drives AST-boundary splitting at the top
    level (function / class / import / module) with statement-block fallback
    when a single definition exceeds the cap.
  - `symbols=True` returns named definitions with span + kind.
  - `imports=True` returns import statements with source text + span.

Our wrapper adds:
  - cl100k token budgeting (callers think in tokens; pack thinks in bytes)
  - fence-wrapper preservation when input is ```lang\n...\n```
  - per-chunk filtering of symbols/imports by byte span
  - postcondition check (every slice ≤ cap, else signal failure for caller
    to hard-split)

Fallback contract: if the pack is unavailable, the language is unsupported,
or no chunks come back, return `[(source, {})]`. Caller (tier_chunker)
then hard-splits with a logged WARNING so the embedder-safety contract
holds even on the failure path.

Note on `symbols_called`: the v1.8 ProcessResult exposes `symbols` (defined),
`imports`, `exports`, `structure`, but NOT call-site references. Phase 1
leaves `symbols_called` empty and documents this gap. Cross-file call
graphs land in Phase 5 via SCIP indexers (sourcegraph/scip).
"""

from __future__ import annotations

import logging
from typing import Any

import tiktoken

logger = logging.getLogger(__name__)
_TOKENIZER = tiktoken.get_encoding("cl100k_base")

# Code tends to encode at ~3.3-4 chars per cl100k token. Use 3.5 as a
# conservative byte-to-token ratio when translating the embedder's token
# cap into the pack's byte-based chunk_max_size knob. We re-verify each
# returned chunk's actual token count afterwards.
_BYTES_PER_TOKEN_ESTIMATE = 3.5


def _count_tokens(text: str) -> int:
    return len(_TOKENIZER.encode(text, disallowed_special=()))


# Lazy pack import — module-import must succeed even when the dep is
# missing so unit tests and prose-only ingestion paths don't crash.
_PACK: Any = None
_PACK_TRIED = False
_PACK_ERR: Exception | None = None


def _pack():
    global _PACK, _PACK_TRIED, _PACK_ERR
    if _PACK_TRIED:
        return _PACK
    _PACK_TRIED = True
    try:
        import tree_sitter_language_pack as p

        _PACK = p
    except Exception as exc:  # pragma: no cover - import-time failure path
        _PACK_ERR = exc
        logger.warning("tree_sitter_language_pack unavailable: %s", exc)
    return _PACK


# SymbolKind values we consider "defined" for the symbols_defined metadata.
# The pack exposes a SymbolKind enum with lowercase members:
#   class, constant, enum, function, interface, module, other, type, variable
# We pick the ones a retrieval consumer would search for by name.
_DEFINED_KIND_NAMES: frozenset[str] = frozenset(
    {"function", "class", "module", "interface", "type", "enum"}
)


def _is_defined_kind(kind: Any) -> bool:
    name = getattr(kind, "name", None) or str(kind).rsplit(".", 1)[-1]
    return name.lower() in _DEFINED_KIND_NAMES


def _split_fence(source: str) -> tuple[str, str, str]:
    """If `source` starts with a triple-backtick fence and ends with one,
    return (open_fence_with_newline, body_without_fences, close_fence).
    Otherwise return ('', source, '')."""
    if not source.startswith("```"):
        return "", source, ""
    first_nl = source.find("\n")
    if first_nl < 0:
        return "", source, ""
    open_line = source[: first_nl + 1]
    rest = source[first_nl + 1 :]
    # Tolerate optional trailing whitespace before/after the close fence.
    if rest.rstrip().endswith("```"):
        close_idx = rest.rstrip().rfind("```")
        body = rest[:close_idx].rstrip()
        close = "\n```"
        return open_line, body, close
    return "", source, ""


def _wrap(body: str, open_line: str, close: str) -> str:
    if not open_line:
        return body
    return f"{open_line}{body.strip()}{close}"


def _filter_by_span(items, start_byte: int, end_byte: int):
    """Return items whose `.span.start_byte` falls within [start_byte, end_byte)."""
    out = []
    for item in items:
        span = getattr(item, "span", None)
        if span is None:
            continue
        sb = getattr(span, "start_byte", None)
        if sb is None:
            continue
        if start_byte <= sb < end_byte:
            out.append(item)
    return out


def _extract_metadata_for_chunk(
    chunk_content: str,
    chunk_start: int,
    chunk_end: int,
    all_symbols,
    all_imports,
) -> dict[str, Any]:
    syms = _filter_by_span(all_symbols, chunk_start, chunk_end)
    imps = _filter_by_span(all_imports, chunk_start, chunk_end)
    defined: list[str] = []
    for s in syms:
        if _is_defined_kind(getattr(s, "kind", None)):
            name = getattr(s, "name", None)
            if name:
                defined.append(name)
    imports_src: list[str] = []
    for i in imps:
        src = getattr(i, "source", None)
        if src:
            imports_src.append(src)
    # First non-blank line of the chunk gives a reasonable ast_signature
    # for retrieval display (e.g. "def insert(vector, ef_construction=200):").
    ast_signature = ""
    for line in chunk_content.splitlines():
        s = line.strip()
        if s and not s.startswith("```"):
            ast_signature = s[:200]
            break
    return {
        # cap list sizes to keep Qdrant payloads compact
        "symbols_defined": defined[:40],
        "symbols_called": [],  # not surfaced by v1.8 ProcessResult; Phase 5 (SCIP) fills this
        "imports": imports_src[:30],
        "ast_signature": ast_signature,
    }


def pack(
    source: str,
    language: str,
    max_tokens: int,
) -> list[tuple[str, dict[str, Any]]]:
    """Pack `source` into embedder-safe slices with AST-derived metadata.

    Returns a list of (slice_text, metadata) tuples. Every slice_text is
    guaranteed `<= max_tokens` cl100k tokens — OR the function returns the
    single sentinel `[(source, {})]` so the caller can hard-split. Never a
    half-met contract.

    Empty / whitespace-only `source` returns `[]`.
    """
    if not source or not source.strip():
        return []

    pack_mod = _pack()
    open_fence, body, close_fence = _split_fence(source)

    # Fast path: under cap. Still attempt metadata so retrieval gets symbols.
    if _count_tokens(source) <= max_tokens:
        meta: dict[str, Any] = {}
        if pack_mod is not None and language:
            try:
                cfg = pack_mod.ProcessConfig(
                    language=language,
                    symbols=True,
                    imports=True,
                    structure=False,
                )
                result = pack_mod.process(body, cfg)
                # Treat the whole body as one chunk for metadata purposes.
                meta = _extract_metadata_for_chunk(
                    body, 0, len(body.encode("utf-8")),
                    list(result.symbols), list(result.imports),
                )
            except Exception as exc:
                logger.warning(
                    "code_splitter: metadata extraction failed lang=%r: %s",
                    language, exc,
                )
                meta = {}
        return [(source, meta)]

    # Heavy path: invoke pack with chunk_max_size so it splits at AST boundaries.
    if pack_mod is None or not language:
        return [(source, {})]

    byte_cap = max(64, int(max_tokens * _BYTES_PER_TOKEN_ESTIMATE))
    try:
        cfg = pack_mod.ProcessConfig(
            language=language,
            symbols=True,
            imports=True,
            structure=False,
            chunk_max_size=byte_cap,
        )
        result = pack_mod.process(body, cfg)
    except Exception as exc:
        logger.warning("code_splitter: pack.process(%r) failed: %s", language, exc)
        return [(source, {})]

    chunks = list(result.chunks)
    if not chunks:
        return [(source, {})]

    all_symbols = list(result.symbols)
    all_imports = list(result.imports)

    out: list[tuple[str, dict[str, Any]]] = []
    for ch in chunks:
        content = getattr(ch, "content", None)
        if content is None:
            continue
        sb = getattr(ch, "start_byte", 0)
        eb = getattr(ch, "end_byte", sb + len(content.encode("utf-8")))
        meta = _extract_metadata_for_chunk(content, sb, eb, all_symbols, all_imports)
        wrapped = _wrap(content.rstrip(), open_fence, close_fence) if open_fence else content
        out.append((wrapped, meta))

    # Postcondition: every slice must fit the token cap. If even one slipped
    # past (very long single statement, dense single-line code, language with
    # poor grammar coverage), signal failure so caller hard-splits.
    if any(_count_tokens(t) > max_tokens for t, _ in out):
        logger.warning(
            "code_splitter: pack produced slice over token cap; "
            "signaling failure lang=%r max_tokens=%d byte_cap=%d slices=%d",
            language, max_tokens, byte_cap, len(out),
        )
        return [(source, {})]

    return out
