"""Deterministic, zero-model survey for Graphify source documents."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Literal

from pydantic import Field

from models.graphify_contracts import NormalizedDocumentV1, StrictFrozenModel, stable_digest

SURVEY_RELEASE = "graphify-survey-v1"

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+.#/-]*")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_ALIAS_RE = re.compile(r"\b([A-Z][A-Za-z0-9-]*(?:[ \t]+[A-Z][A-Za-z0-9-]*){1,8})[ \t]*\(([A-Z][A-Z0-9-]{1,12})\)")
_ALSO_WRITTEN_RE = re.compile(r"\b([A-Z][A-Za-z0-9 ]{1,40}?),[ \t]+also written[ \t]+([A-Z][A-Za-z0-9_.+-]{1,40}),")
_DEFINITION_RE = re.compile(r"\b([A-Z][A-Za-z0-9_.-]*(?:[ \t]+[A-Z][A-Za-z0-9_.-]*){0,6})[ \t]+(?:is|means|refers to|denotes)[ \t]+([^.!?\n]{3,160})")
_TERM_SENTENCE_BREAK_RE = re.compile(r"[.!?]+[\"')\]]*\s+")
# Mirrors graphify_relations._ABBREVIATION_TOKENS (kept local: survey has no
# dependency on the relation lane).
_ABBREVIATION_TOKENS = frozenset({
    "dr", "mr", "mrs", "ms", "prof", "sr", "jr", "st", "vs", "etc", "cf", "al",
    "inc", "ltd", "corp", "co", "dept", "fig", "eq", "no", "approx", "est",
})


def _trim_term_to_final_sentence(raw: str) -> str:
    """A definition term is anchored to the copula that follows it, so text
    before an internal sentence boundary belongs to the previous sentence
    ("... the Falcon Cache. QRL is ..." → term "QRL"). Abbreviation periods and
    single-letter initials do not count as boundaries.
    """
    term = raw.strip()
    last_end = None
    for candidate in _TERM_SENTENCE_BREAK_RE.finditer(term):
        head = term[: candidate.start()]
        token = re.search(r"[A-Za-z][A-Za-z.]*$", head)
        if token:
            word = token.group(0).casefold()
            segment = word.rsplit(".", 1)[-1]
            if word in _ABBREVIATION_TOKENS or (len(segment) == 1 and segment.isalpha()):
                continue
        last_end = candidate.end()
    if last_end is None:
        return term
    return term[last_end:].strip() or term
_NAMED_DEFINITION_RE = re.compile(r"(?m)^(?:The[ \t]+)?([A-Za-z][A-Za-z0-9_.+-]*(?:[ \t]+[A-Za-z][A-Za-z0-9_.+-]*){0,5})[ \t]+is[ \t]+a[ \t]+named[ \t]+([^.!?\n]{3,160})")
_CAPITALIZED_RE = re.compile(r"\b(?:[A-Z][A-Za-z0-9_.+#-]*)(?:[ \t]+[A-Z][A-Za-z0-9_.+#-]*){0,5}\b")
_IDENTIFIER_RE = re.compile(r"\b(?:[A-Za-z]+\d+[A-Za-z0-9._-]*|[A-Za-z]+_[A-Za-z0-9_]+|[A-Za-z]+\.[A-Za-z0-9_.]+)\b")
_FURNITURE_MARKERS = ("table of contents", "contents", "references", "bibliography", "index", "copyright")


class SurveyHeadingV1(StrictFrozenModel):
    level: int = Field(ge=1, le=6)
    text: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    path: tuple[str, ...]


class SurveyAliasV1(StrictFrozenModel):
    canonical: str
    alias: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class SurveyDefinitionV1(StrictFrozenModel):
    term: str
    definition: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class SurveyBlockV1(StrictFrozenModel):
    block_id: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str
    structural_position: Literal["front", "body", "back"]
    repeat_count: int = Field(ge=1)
    furniture_signals: tuple[str, ...]
    furniture_candidate: bool


class GazetteerCandidateV1(StrictFrozenModel):
    surface: str
    sources: tuple[str, ...]
    count: int = Field(ge=1)
    strength: Literal["weak"] = "weak"


class DocumentSurveyV1(StrictFrozenModel):
    schema_version: Literal["polymath.document_survey.v1"] = "polymath.document_survey.v1"
    document_id: str
    survey_release: str = SURVEY_RELEASE
    normalized_sha256: str
    headings: tuple[SurveyHeadingV1, ...]
    aliases: tuple[SurveyAliasV1, ...]
    definitions: tuple[SurveyDefinitionV1, ...]
    blocks: tuple[SurveyBlockV1, ...]
    shape_counts: dict[str, int]
    token_frequency: dict[str, int]
    gazetteer_candidates: tuple[GazetteerCandidateV1, ...]
    survey_hash: str


def _position(start: int, length: int) -> Literal["front", "body", "back"]:
    ratio = start / max(length, 1)
    if ratio <= 0.15:
        return "front"
    if ratio >= 0.85:
        return "back"
    return "body"


def survey_document(document: NormalizedDocumentV1) -> DocumentSurveyV1:
    text = document.normalized_text
    headings: list[SurveyHeadingV1] = []
    heading_stack: list[str] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        match = _HEADING_RE.match(line.rstrip("\n"))
        if match:
            level = len(match.group(1))
            value = match.group(2).strip()
            heading_stack[level - 1:] = [value]
            headings.append(SurveyHeadingV1(
                level=level,
                text=value,
                start=offset,
                end=offset + len(line.rstrip("\n")),
                path=tuple(heading_stack),
            ))
        offset += len(line)

    aliases_list = [SurveyAliasV1(
        canonical=match.group(1).strip(), alias=match.group(2),
        start=match.start(), end=match.end(),
    ) for match in _ALIAS_RE.finditer(text)]
    aliases_list.extend(SurveyAliasV1(
        canonical=match.group(2).strip(), alias=match.group(1).strip(),
        start=match.start(), end=match.end(),
    ) for match in _ALSO_WRITTEN_RE.finditer(text))
    aliases = tuple(sorted(aliases_list, key=lambda item: (item.start, item.end, item.canonical, item.alias)))
    definitions_list = [SurveyDefinitionV1(
        term=_trim_term_to_final_sentence(match.group(1)), definition=match.group(2).strip(),
        start=match.start(), end=match.end(),
    ) for match in _DEFINITION_RE.finditer(text)]
    definitions_list.extend(SurveyDefinitionV1(
        term=_trim_term_to_final_sentence(match.group(1)), definition=("named " + match.group(2).strip()),
        start=match.start(), end=match.end(),
    ) for match in _NAMED_DEFINITION_RE.finditer(text))
    definitions = tuple(sorted(
        {(item.term, item.start, item.end): item for item in definitions_list}.values(),
        key=lambda item: (item.start, item.end, item.term),
    ))

    raw_blocks: list[tuple[int, int, str]] = []
    block_start: int | None = None
    block_parts: list[str] = []
    cursor = 0
    for line in text.splitlines(keepends=True):
        if line.strip():
            if block_start is None:
                block_start = cursor
            block_parts.append(line)
        elif block_start is not None:
            raw_blocks.append((block_start, cursor, "".join(block_parts)))
            block_start = None
            block_parts = []
        cursor += len(line)
    if block_start is not None:
        raw_blocks.append((block_start, len(text), "".join(block_parts)))
    canonical_blocks = [re.sub(r"\s+", " ", block.strip()).casefold() for _s, _e, block in raw_blocks]
    repeats = Counter(canonical_blocks)
    blocks: list[SurveyBlockV1] = []
    for (start, end, block), canonical in zip(raw_blocks, canonical_blocks):
        signals: list[str] = []
        lowered = canonical
        position = _position(start, len(text))
        if repeats[canonical] >= 2:
            signals.append("repeated_block")
        if position in {"front", "back"}:
            signals.append(f"{position}_matter")
        if any(marker in lowered for marker in _FURNITURE_MARKERS):
            signals.append("structural_marker")
        if len(block.strip()) <= 120:
            signals.append("short_block")
        furniture = len(set(signals) & {"repeated_block", "front_matter", "back_matter", "structural_marker"}) >= 2
        blocks.append(SurveyBlockV1(
            block_id=f"block:{hashlib.sha256(f'{start}:{end}:{canonical}'.encode()).hexdigest()[:20]}",
            start=start, end=end, text=block, structural_position=position,
            repeat_count=repeats[canonical], furniture_signals=tuple(signals),
            furniture_candidate=furniture,
        ))

    tokens = [match.group(0) for match in _WORD_RE.finditer(text)]
    frequencies = Counter(token.casefold() for token in tokens)
    capitalized = list(_CAPITALIZED_RE.finditer(text))
    identifiers = list(_IDENTIFIER_RE.finditer(text))
    all_caps = [token for token in tokens if len(token) > 1 and token.isupper()]
    shape_counts = {
        "capitalized_phrases": len(capitalized),
        "all_caps_tokens": len(all_caps),
        "identifier_tokens": len(identifiers),
        "numeric_tokens": sum(any(char.isdigit() for char in token) for token in tokens),
    }

    candidate_sources: dict[str, set[str]] = {}
    candidate_counts: Counter[str] = Counter()
    for heading in headings:
        candidate_sources.setdefault(heading.text, set()).add("heading")
        candidate_counts[heading.text] += 1
    for alias in aliases:
        for surface, source in ((alias.canonical, "alias_long_form"), (alias.alias, "acronym")):
            candidate_sources.setdefault(surface, set()).add(source)
            candidate_counts[surface] += 1
    for definition in definitions:
        candidate_sources.setdefault(definition.term, set()).add("definition")
        candidate_counts[definition.term] += 1
    for match in capitalized:
        surface = match.group(0).strip()
        candidate_sources.setdefault(surface, set()).add("capitalization_shape")
        candidate_counts[surface] += 1
    for match in identifiers:
        surface = match.group(0)
        candidate_sources.setdefault(surface, set()).add("identifier_shape")
        candidate_counts[surface] += 1
    gazetteer = tuple(
        GazetteerCandidateV1(surface=surface, sources=tuple(sorted(sources)), count=candidate_counts[surface])
        for surface, sources in sorted(candidate_sources.items(), key=lambda item: (item[0].casefold(), item[0]))
        if surface
    )
    payload = {
        "release": SURVEY_RELEASE,
        "document": document.normalized_sha256,
        "headings": [item.model_dump() for item in headings],
        "aliases": [item.model_dump() for item in aliases],
        "definitions": [item.model_dump() for item in definitions],
        "blocks": [item.model_dump() for item in blocks],
        "shapes": shape_counts,
        "frequency": dict(sorted(frequencies.items())),
        "gazetteer": [item.model_dump() for item in gazetteer],
    }
    return DocumentSurveyV1(
        document_id=document.document_id,
        normalized_sha256=document.normalized_sha256,
        headings=tuple(headings), aliases=aliases, definitions=definitions,
        blocks=tuple(blocks), shape_counts=shape_counts,
        token_frequency=dict(sorted(frequencies.items())),
        gazetteer_candidates=gazetteer, survey_hash=stable_digest(payload),
    )
