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
import re
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


# Phase 5 — Roblox API regex extractor. Pack v1.8's ProcessResult does
# NOT expose the AST cursor (only chunks, symbols, imports, exports,
# structure), so we operate on chunk text directly. Patterns target the
# canonical Roblox surface: services, instance constructors, remote
# event firings, child lookups, humanoid methods, generic Play/Stop/
# Pause, require(), RunService event subscriptions. Each pattern is
# anchored loosely to tolerate Luau's whitespace flexibility.
#
# Output goes into `metadata.roblox_apis` (new field) AND `symbols_called`
# (closing the v1.8 ProcessResult gap deterministically for Luau, without
# depending on graphify being enabled).

_ROBLOX_PATTERN_GET_SERVICE = re.compile(
    r'game\s*:\s*GetService\s*\(\s*[\'"]([A-Za-z0-9_]+)[\'"]?\s*\)'
)
_ROBLOX_PATTERN_INSTANCE_NEW = re.compile(
    r'Instance\.new\s*\(\s*[\'"]([A-Za-z0-9_]+)[\'"]?\s*[,)]'
)
_ROBLOX_PATTERN_REMOTE_FIRE = re.compile(
    r'([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(FireServer|FireClient|FireAllClients'
    r'|InvokeServer|InvokeClient)\s*\('
)
_ROBLOX_PATTERN_WAIT_FOR_CHILD = re.compile(
    r':WaitForChild\s*\(\s*[\'"]([A-Za-z0-9_]+)[\'"]?\s*\)'
)
_ROBLOX_PATTERN_HUMANOID_METHOD = re.compile(
    r'(?:[Hh]umanoid)\s*:\s*(MoveTo|Jump|ChangeState|LoadAnimation|TakeDamage)\s*\('
)
_ROBLOX_PATTERN_TWEEN_METHOD = re.compile(
    r'([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(Play|Stop|Pause|Cancel|Destroy)\s*\('
)
_ROBLOX_PATTERN_REQUIRE = re.compile(r'require\s*\(\s*([^\)]{1,200})\s*\)')
_ROBLOX_PATTERN_RUNSERVICE = re.compile(
    r'RunService\s*\.\s*(Heartbeat|RenderStepped|Stepped|PreSimulation'
    r'|PostSimulation)\s*:\s*Connect'
)


def _extract_roblox_apis(body: str) -> dict[str, list[str]]:
    """Phase 5 regex extractor. Scans Luau body text for canonical Roblox
    API patterns and returns:

      roblox_apis      — high-confidence engine terms (TweenService,
                         Humanoid, Part, RemoteEvent.FireServer, etc.)
                         These get scoped Roblox entity types via
                         `roblox_ontology.resolve_code_entity_type`.
      called_methods   — short method names (Play, Connect, FireServer,
                         Destroy, etc.) without a fully-qualified
                         receiver. Useful for BM25 lexical hits when
                         the user queries with a method name alone.

    Document order is preserved: each pattern's matches are collected
    with their byte offset, and emissions are interleaved by offset so
    `Part` (Instance.new at byte 0) precedes `TweenService` (GetService
    at byte 30) even though pattern iteration order differs. Both lists
    are deduped case-sensitively (first occurrence wins) to avoid
    `TweenService`/`tweenservice` collisions — the worker side does
    case-insensitive dedup when merging into `symbols_called`.
    """
    # (start_byte, kind, name): kind == "api" → roblox_apis,
    #                          kind == "method" → called_methods
    events: list[tuple[int, str, str]] = []

    for m in _ROBLOX_PATTERN_GET_SERVICE.finditer(body):
        events.append((m.start(), "api", m.group(1)))      # e.g. TweenService
    for m in _ROBLOX_PATTERN_INSTANCE_NEW.finditer(body):
        events.append((m.start(), "api", m.group(1)))      # e.g. Part
        events.append((m.start(), "api", "Instance.new"))
    for m in _ROBLOX_PATTERN_REMOTE_FIRE.finditer(body):
        events.append((m.start(), "api", f"{m.group(1)}.{m.group(2)}"))
        events.append((m.start(), "method", m.group(2)))
    for m in _ROBLOX_PATTERN_WAIT_FOR_CHILD.finditer(body):
        events.append((m.start(), "api", m.group(1)))      # the child name as literal
        events.append((m.start(), "method", "WaitForChild"))
    for m in _ROBLOX_PATTERN_HUMANOID_METHOD.finditer(body):
        events.append((m.start(), "api", f"Humanoid.{m.group(1)}"))
        events.append((m.start(), "api", "Humanoid"))
        events.append((m.start(), "method", m.group(1)))
    for m in _ROBLOX_PATTERN_TWEEN_METHOD.finditer(body):
        events.append((m.start(), "method", m.group(2)))   # bare method name
    for m in _ROBLOX_PATTERN_REQUIRE.finditer(body):
        target = m.group(1).strip()
        if target:
            target = target.strip('\'"')
            events.append((m.start(), "api", target[:120]))
        events.append((m.start(), "method", "require"))
    for m in _ROBLOX_PATTERN_RUNSERVICE.finditer(body):
        events.append((m.start(), "api", f"RunService.{m.group(1)}"))
        events.append((m.start(), "method", "Connect"))

    events.sort(key=lambda ev: ev[0])

    apis: list[str] = []
    methods: list[str] = []
    seen_apis: set[str] = set()
    seen_methods: set[str] = set()
    for _, kind, name in events:
        name = name.strip()
        if not name:
            continue
        if kind == "api":
            if name not in seen_apis:
                apis.append(name)
                seen_apis.add(name)
        else:
            if name not in seen_methods:
                methods.append(name)
                seen_methods.add(name)

    return {"roblox_apis": apis, "called_methods": methods}


def _extract_metadata_for_chunk(
    chunk_content: str,
    chunk_start: int,
    chunk_end: int,
    all_symbols,
    all_imports,
    language: str = "",
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

    # Phase 5 Gate 1 — Roblox API regex extraction. Only fires on Luau/Lua
    # chunks. Other languages get the legacy empty-`symbols_called` shape;
    # Pt 11.1's graphify backfill remains the cross-language fallback.
    lang_lower = (language or "").lower()
    roblox = _extract_roblox_apis(chunk_content) if lang_lower in ("lua", "luau") else {}
    symbols_called = list(dict.fromkeys(
        list(roblox.get("roblox_apis", [])) + list(roblox.get("called_methods", []))
    ))[:60]

    return {
        # cap list sizes to keep Qdrant payloads compact
        "symbols_defined": defined[:40],
        "symbols_called": symbols_called,
        "imports": imports_src[:30],
        "ast_signature": ast_signature,
        # Phase 5 — new field, populated only for Lua/Luau chunks.
        # Downstream `roblox_ontology.resolve_code_entity_type` uses this
        # as one of its scope gates (alongside chunk.language).
        "roblox_apis": list(roblox.get("roblox_apis", []))[:30],
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
                    language=language,
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
        meta = _extract_metadata_for_chunk(content, sb, eb, all_symbols, all_imports, language=language)
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
