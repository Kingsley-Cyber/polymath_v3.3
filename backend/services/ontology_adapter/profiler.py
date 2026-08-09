"""DocumentProfiler — document-blind mechanism, corpus-specific output.

Reads ONLY the document text (plus optional survey structure). It NEVER
reads gold labels, answer keys, or benchmark artifacts — the compiler's
inputs are the profile and the declarative packs, nothing else.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]+")
_CAP_TERM_RE = re.compile(r"\b(?:[A-Z][a-zA-Z0-9]+(?:[ -][A-Z][a-zA-Z0-9]+)+|[A-Z]{2,})\b")
_STRUCT_RES = {
    "code_fences": re.compile(r"^```", re.M),
    "tables": re.compile(r"^\|.+\|\s*$", re.M),
    "kv_lines": re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_ -]{0,40}:\s+\S", re.M),
    "headings": re.compile(r"^#{1,6}\s", re.M),
    "speaker_turns": re.compile(r"^[A-Z][A-Za-z .]{1,30}:\s", re.M),
    "citations": re.compile(r"\[\d{1,3}\]|\bet al\.\b"),
    "json_blocks": re.compile(r"^\s*[{\[]", re.M),
}


@dataclass(frozen=True)
class DocumentProfile:
    token_count: int
    term_frequencies: dict          # lowercase word -> count
    capitalized_terms: tuple        # recurring multiword/acronym names
    structure: dict                 # structural inventory counts

    def cue_hits(self, cues: list[str]) -> int:
        total = 0
        for cue in cues:
            cue_l = cue.lower()
            if " " in cue_l:
                total += 1 if cue_l in self._joined else 0
            else:
                total += min(self.term_frequencies.get(cue_l, 0), 5)
        return total

    @property
    def _joined(self) -> str:
        return getattr(self, "_joined_cache", "")


def profile_document(text: str) -> DocumentProfile:
    words = [w.lower() for w in _WORD_RE.findall(text)]
    frequencies = Counter(words)
    capitalized = Counter(m.group() for m in _CAP_TERM_RE.finditer(text))
    structure = {name: len(rx.findall(text)) for name, rx in _STRUCT_RES.items()}
    profile = DocumentProfile(
        token_count=len(words),
        term_frequencies=dict(frequencies),
        capitalized_terms=tuple(t for t, c in capitalized.most_common(50) if c >= 2),
        structure=structure,
    )
    object.__setattr__(profile, "_joined_cache", " ".join(words))
    return profile
