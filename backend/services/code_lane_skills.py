"""Code-lane retrieval glue — language-agnostic formatter + auto-selected skill.

Phase 2 of the code lane. The Phase 1 chunker already stamps every code chunk
with `language` and `metadata.{symbols_defined,symbols_called,imports,ast_signature,file_path}`.
This module does two things at retrieval time:

1. `format_code_source()` — render a SourceChunk whose `language` is set into
   a language-agnostic `<file path=… language=…>…</file>` XML block. One
   formatter handles every language in the pack; the LLM reads the
   `language` attribute and adjusts syntax.

2. `build_auto_code_skill()` — auto-detect the dominant language across
   retrieved code chunks and synthesize a skill dict that the orchestrator
   appends to `active_skills_dicts`. A small `LANGUAGE_PROMPT_OVERRIDES`
   table (10 entries today) injects per-language conventions where they
   actually matter; everything else falls through to a generic prompt.

Scales to every language wired into `_CODE_EXT_TO_LANGUAGE` without code
changes: any new extension that produces `chunk_kind=code` with a `language`
field automatically gets the XML formatter and the generic skill. Add an
override entry only when a language has conventions worth stating
explicitly (Rust borrow semantics, Luau Color3 vs BrickColor, etc.).
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable


# ─── Generic synthesis rules — apply to every language ──────────────────────

GENERIC_CODE_RULES = (
    "Output valid, runnable code inside fenced blocks tagged with the source language. "
    "Preserve existing imports, requires, module paths, and function/method signatures "
    "unless the user explicitly asks you to change them. Match casing and naming "
    "conventions visible in the retrieved context. If the context demonstrates a "
    "pattern (error handling, config passing, dependency injection, decorator usage), "
    "follow that pattern. After the code block, briefly explain what changed and why. "
    "If your change would break a cross-file reference visible in the context, "
    "call that out at the top of the explanation."
)


# ─── Per-language overrides — only languages where conventions matter ───────
# Keep this list small. The generic rules above handle 90% of cases. An
# override entry should state a specific convention that would otherwise be
# violated by a model defaulting to the loudest examples in its training set.
LANGUAGE_PROMPT_OVERRIDES: dict[str, str] = {
    "python": (
        "Follow PEP 8. Preserve type hints if present. Prefer pathlib over os.path "
        "when the codebase uses it. Respect existing dataclass / TypedDict / Pydantic "
        "boundaries."
    ),
    "rust": (
        "Follow ownership rules — prefer borrowing over cloning. Use `?` for error "
        "propagation. Match the crate's Error type. Don't introduce `unwrap()` in code "
        "paths that currently use Result."
    ),
    "go": (
        "Handle errors explicitly with `if err != nil`. Match the package's existing "
        "receiver-method style (value vs pointer receivers). Don't introduce generics "
        "in code that uses interfaces unless explicitly asked."
    ),
    "javascript": (
        "Match the module system in use (ESM `import` vs CommonJS `require`). Preserve "
        "async/await patterns; don't rewrite them as callbacks or .then() chains."
    ),
    "typescript": (
        "Preserve type annotations and generic constraints. Match the existing "
        "narrowing style (type guards vs assertion functions). Don't drop strict "
        "null checks."
    ),
    "tsx": (
        "JSX/TSX. Preserve component structure. Use the project's existing state and "
        "effect patterns (hooks vs class). Match prop-type / interface conventions."
    ),
    "luau": (
        "Roblox Luau. Prefer Color3 over BrickColor. Preserve RemoteEvent / "
        "RemoteFunction patterns and `game:GetService(...)` import style. Respect "
        "existing Luau type annotations (`local x: T = ...`)."
    ),
    "swift": (
        "Match optional handling (optional chaining vs guard let vs if let) to the "
        "surrounding code. Preserve protocol conformances and access modifiers "
        "(`private` / `internal` / `public`)."
    ),
    "kotlin": (
        "Preserve null-safety operators. Match coroutine usage if present "
        "(suspend / Flow / coroutineScope). Respect data class boundaries."
    ),
    "cpp": (
        "Preserve const-correctness. Match the project's smart-pointer style "
        "(unique_ptr / shared_ptr / raw). Don't introduce new allocations where the "
        "code uses RAII or stack objects."
    ),
}


# ─── Source classification — code vs prose ──────────────────────────────────

def is_code_source(s: Any) -> bool:
    """A SourceChunk is a code chunk iff its `language` field is set.

    The ingestion code lane stamps `language` on every CODE-kind chunk
    (code-file ingest or markdown fence detection). Prose chunks always
    have `language is None`. Using language presence as the gate keeps
    this function in sync with the chunker's own classification — no
    second source of truth.
    """
    return bool(getattr(s, "language", None))


# ─── Formatter — one function, every language ───────────────────────────────

def _escape_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def _format_signal_list(items: Iterable[str], cap: int) -> str:
    seen: list[str] = []
    for item in items:
        item = str(item).strip()
        if item and item not in seen:
            seen.append(item)
        if len(seen) >= cap:
            break
    return ", ".join(seen)


def format_code_source(
    s: Any,
    *,
    corpus_label: str = "",
    doc_label: str = "",
) -> str:
    """Render a code SourceChunk as a language-agnostic `<file>` XML block.

    The structure is identical across all 40+ supported languages — only
    the `language` attribute and the payload inside `<code>` change. LLMs
    read `language` and adjust their output syntax accordingly.

    No language-specific branching. Adding a new tree-sitter grammar
    upstream auto-enables this formatter for it.
    """
    meta = getattr(s, "metadata", None) or {}
    language = (getattr(s, "language", None) or "text").lower()
    file_path = (
        meta.get("file_path")
        or getattr(s, "doc_name", None)
        or getattr(s, "doc_id", None)
        or ""
    )
    heading_path = getattr(s, "heading_path", None) or []
    symbols_defined = meta.get("symbols_defined") or []
    symbols_called = meta.get("symbols_called") or []
    imports = meta.get("imports") or []

    attrs = [f'language="{_escape_attr(language)}"']
    if file_path:
        attrs.append(f'path="{_escape_attr(str(file_path))}"')
    if corpus_label:
        attrs.append(f'from="{_escape_attr(corpus_label)}"')
    if doc_label and (not file_path or doc_label != file_path):
        attrs.append(f'doc="{_escape_attr(doc_label)}"')

    lines: list[str] = [f"<file {' '.join(attrs)}>"]

    if heading_path:
        lines.append(
            f"  <section>{_escape_attr(' / '.join(str(h) for h in heading_path))}</section>"
        )

    sym_str = _format_signal_list(symbols_defined, cap=40)
    if sym_str:
        lines.append(f"  <symbols_defined>{_escape_attr(sym_str)}</symbols_defined>")

    called_str = _format_signal_list(symbols_called, cap=30)
    if called_str:
        lines.append(f"  <symbols_called>{_escape_attr(called_str)}</symbols_called>")

    imports_str = _format_signal_list(imports, cap=20)
    if imports_str:
        lines.append(f"  <imports>{_escape_attr(imports_str)}</imports>")

    lines.append("  <code>")
    lines.append(getattr(s, "text", "") or "")
    lines.append("  </code>")
    lines.append("</file>")
    return "\n".join(lines)


# ─── Auto-selected language skill ───────────────────────────────────────────

def detect_dominant_code_language(sources: Iterable[Any]) -> str | None:
    """Return the most common non-empty `language` across the retrieved
    sources, or None if no code chunks were retrieved."""
    counts: Counter[str] = Counter()
    for s in sources:
        lang = getattr(s, "language", None)
        if lang:
            counts[lang.lower()] += 1
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def already_has_code_skill(active_skills: Iterable[dict]) -> bool:
    """True if the user has manually activated a /code-like skill so we
    don't double-stack rules."""
    if not active_skills:
        return False
    for skill in active_skills:
        name = (skill.get("name") or "").lower()
        cmd = (skill.get("slash_command") or "").lstrip("/").lower()
        if name.startswith("code") or cmd.startswith("code") or cmd in LANGUAGE_PROMPT_OVERRIDES:
            return True
    return False


def build_auto_code_skill(language: str) -> dict:
    """Synthesize a skill dict (matches the shape of the skills_registry
    `Skill` model serialized to dict) for the auto-detected language. The
    orchestrator appends this to `active_skills_dicts` so it flows through
    the standard skill-envelope rendering."""
    lang = (language or "").lower()
    override = LANGUAGE_PROMPT_OVERRIDES.get(lang, "")
    instructions = (
        f"You are a {lang} code editor. {GENERIC_CODE_RULES}"
        + (f" {override}" if override else "")
        + f" Output valid {lang} inside ```{lang} fences."
    )
    return {
        "name": f"code-{lang}",
        "slash_command": f"/code-{lang}",
        "instructions": instructions,
        "auto_selected": True,
    }


def maybe_inject_code_skill(
    sources: Iterable[Any],
    active_skills: list[dict],
) -> list[dict]:
    """Inspect retrieved sources, append an auto-detected language skill
    when warranted, and return the (possibly extended) skill list. Safe to
    call with an empty `sources` iterable — returns the input unchanged.
    """
    if already_has_code_skill(active_skills):
        return active_skills
    lang = detect_dominant_code_language(sources)
    if not lang:
        return active_skills
    return list(active_skills) + [build_auto_code_skill(lang)]
