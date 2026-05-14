"""Tests for the Phase 2 code lane glue (formatter + auto-skill).

Scope: the formatter must scale to any language without per-language
branches, the auto-selector must pick the dominant language and skip
when the user already has a code skill active, and override entries
must be injected into the generated skill instructions when present.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.code_lane_skills import (
    GENERIC_CODE_RULES,
    LANGUAGE_PROMPT_OVERRIDES,
    already_has_code_skill,
    build_auto_code_skill,
    detect_dominant_code_language,
    format_code_source,
    is_code_source,
    maybe_inject_code_skill,
)


def _src(**kw):
    """Lightweight SourceChunk stand-in (real SourceChunk is a Pydantic
    BaseModel; this avoids the model overhead in unit tests)."""
    defaults = dict(
        chunk_id="c", parent_id="p", doc_id="d", corpus_id="cor",
        text="", score=0.0, source_tier="tier_code",
        corpus_name=None, doc_name=None, heading_path=None,
        language=None, metadata={}, provenance=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


# ─── is_code_source ─────────────────────────────────────────────────────────

def test_is_code_source_true_when_language_set():
    assert is_code_source(_src(language="python")) is True
    assert is_code_source(_src(language="luau")) is True


def test_is_code_source_false_for_prose():
    assert is_code_source(_src(language=None)) is False
    assert is_code_source(_src(language="")) is False


# ─── format_code_source ─────────────────────────────────────────────────────

def test_format_code_source_basic_python():
    s = _src(
        language="python",
        text="def foo():\n    return 1\n",
        metadata={
            "file_path": "src/util.py",
            "symbols_defined": ["foo"],
            "imports": ["numpy"],
        },
    )
    out = format_code_source(s, corpus_label="MyCorpus", doc_label="util.py")
    assert 'language="python"' in out
    assert 'path="src/util.py"' in out
    assert 'from="MyCorpus"' in out
    assert "<symbols_defined>foo</symbols_defined>" in out
    assert "<imports>numpy</imports>" in out
    assert "<code>" in out
    assert "def foo():" in out
    assert out.rstrip().endswith("</file>")


def test_format_code_source_scales_to_unknown_language_without_branching():
    """The formatter must NOT have per-language if-branches. A made-up
    language tag should still produce a well-formed block."""
    s = _src(language="brainfuck", text="++++.", metadata={})
    out = format_code_source(s)
    assert 'language="brainfuck"' in out
    assert "<code>" in out
    assert "++++." in out


def test_format_code_source_empty_metadata_omits_optional_tags():
    s = _src(language="rust", text="fn main() {}", metadata={})
    out = format_code_source(s)
    # Optional tags should be absent when their data is empty.
    assert "<symbols_defined>" not in out
    assert "<symbols_called>" not in out
    assert "<imports>" not in out
    # Required parts still present.
    assert "<code>" in out
    assert "fn main()" in out


def test_format_code_source_includes_heading_path_as_section():
    s = _src(
        language="typescript",
        text="export const x = 1;",
        heading_path=["Chapter 3", "Modules"],
        metadata={"file_path": "x.ts"},
    )
    out = format_code_source(s)
    assert "<section>Chapter 3 / Modules</section>" in out


def test_format_code_source_falls_back_to_doc_name_when_no_file_path():
    s = _src(
        language="luau",
        text="local x = 1",
        doc_name="combat_book.md",
        metadata={},
    )
    out = format_code_source(s, doc_label="combat_book.md")
    assert 'path="combat_book.md"' in out


def test_format_code_source_escapes_special_chars_in_path():
    s = _src(
        language="html",
        text="<div></div>",
        metadata={"file_path": 'weird"name<>.html'},
    )
    out = format_code_source(s)
    assert '&quot;' in out
    assert '&lt;' in out


def test_format_code_source_caps_metadata_list_sizes():
    """Long symbols / imports lists shouldn't bloat the payload."""
    many = [f"sym_{i}" for i in range(200)]
    s = _src(
        language="python",
        text="...",
        metadata={"symbols_defined": many, "imports": many, "symbols_called": many},
    )
    out = format_code_source(s)
    sym_line = next(l for l in out.splitlines() if l.strip().startswith("<symbols_defined>"))
    assert sym_line.count("sym_") <= 40
    imp_line = next(l for l in out.splitlines() if l.strip().startswith("<imports>"))
    assert imp_line.count("sym_") <= 20


# ─── detect_dominant_code_language ──────────────────────────────────────────

def test_detect_dominant_picks_majority():
    sources = [
        _src(language="python"),
        _src(language="python"),
        _src(language="rust"),
    ]
    assert detect_dominant_code_language(sources) == "python"


def test_detect_dominant_returns_none_for_pure_prose():
    sources = [_src(language=None), _src(language=None)]
    assert detect_dominant_code_language(sources) is None


def test_detect_dominant_is_case_insensitive_on_output():
    sources = [_src(language="Python"), _src(language="PYTHON")]
    assert detect_dominant_code_language(sources) == "python"


# ─── build_auto_code_skill ──────────────────────────────────────────────────

def test_build_auto_code_skill_uses_generic_rules_for_unknown_lang():
    skill = build_auto_code_skill("brainfuck")
    assert skill["name"] == "code-brainfuck"
    assert skill["slash_command"] == "/code-brainfuck"
    assert skill["auto_selected"] is True
    # Generic rules always present.
    assert GENERIC_CODE_RULES.split(".")[0] in skill["instructions"]
    # No override appended for an unknown language.
    for override in LANGUAGE_PROMPT_OVERRIDES.values():
        # Match the first 30 chars of each override — if any appears for
        # brainfuck, the override-lookup is wrong.
        assert override[:30] not in skill["instructions"]


def test_build_auto_code_skill_injects_override_for_luau():
    skill = build_auto_code_skill("luau")
    # The luau override should appear verbatim in the rendered instructions.
    assert LANGUAGE_PROMPT_OVERRIDES["luau"] in skill["instructions"]
    assert "```luau" in skill["instructions"]


def test_build_auto_code_skill_injects_override_for_rust():
    skill = build_auto_code_skill("rust")
    assert LANGUAGE_PROMPT_OVERRIDES["rust"] in skill["instructions"]


# ─── already_has_code_skill ─────────────────────────────────────────────────

def test_already_has_code_skill_detects_manual_code_skill():
    assert already_has_code_skill([{"name": "code", "slash_command": "/code"}]) is True
    assert already_has_code_skill([{"name": "code-python", "slash_command": "/code-python"}]) is True
    assert already_has_code_skill([{"name": "anything", "slash_command": "/python"}]) is True


def test_already_has_code_skill_negative_cases():
    assert already_has_code_skill([]) is False
    assert already_has_code_skill([{"name": "researcher", "slash_command": "/research"}]) is False


# ─── maybe_inject_code_skill ────────────────────────────────────────────────

def test_maybe_inject_appends_when_no_existing_code_skill():
    sources = [_src(language="python"), _src(language="python"), _src(language="rust")]
    out = maybe_inject_code_skill(sources, [{"name": "researcher", "slash_command": "/research"}])
    assert len(out) == 2
    assert out[-1]["name"] == "code-python"
    assert out[-1]["auto_selected"] is True


def test_maybe_inject_no_op_when_no_code_chunks():
    sources = [_src(language=None), _src(language=None)]
    out = maybe_inject_code_skill(sources, [{"name": "researcher", "slash_command": "/research"}])
    assert len(out) == 1
    assert out[0]["name"] == "researcher"


def test_maybe_inject_no_op_when_manual_code_skill_active():
    sources = [_src(language="rust"), _src(language="rust")]
    out = maybe_inject_code_skill(sources, [{"name": "code-pinned", "slash_command": "/code"}])
    assert len(out) == 1
    assert out[0]["name"] == "code-pinned"


def test_maybe_inject_handles_empty_active_skills():
    sources = [_src(language="go")]
    out = maybe_inject_code_skill(sources, [])
    assert len(out) == 1
    assert out[0]["name"] == "code-go"


# ─── End-to-end scale test — every language in the supported set ────────────

def test_formatter_handles_every_supported_language():
    """Smoke test: format_code_source must produce a non-empty, well-formed
    block for every entry in the supported langs set. No branching means
    this should pass trivially — guard against future regression."""
    from config import get_settings
    langs = get_settings().TIER_CHUNKER_CODE_SUPPORTED_LANGS
    for lang in langs:
        s = _src(language=lang, text=f"sample for {lang}", metadata={})
        out = format_code_source(s)
        assert out.startswith("<file ")
        assert out.rstrip().endswith("</file>")
        assert f'language="{lang}"' in out
        assert f"sample for {lang}" in out


def test_auto_skill_builder_handles_every_supported_language():
    from config import get_settings
    langs = get_settings().TIER_CHUNKER_CODE_SUPPORTED_LANGS
    for lang in langs:
        skill = build_auto_code_skill(lang)
        assert skill["name"] == f"code-{lang}"
        assert skill["instructions"]
        assert f"```{lang}" in skill["instructions"]
