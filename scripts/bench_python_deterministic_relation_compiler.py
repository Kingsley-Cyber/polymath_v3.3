#!/usr/bin/env python3
"""Benchmark a Python-only deterministic relation compiler.

This script intentionally does not call GLiREL, Qwen, or any cloud model.

It supports two useful test modes:

1. current_direct
   Runs the current deterministic direct relation rules in
   autoresearch_polymath_local_extraction.relation_options.

2. verb_rules
   Runs a first-pass sentence-level verb/cue compiler. It uses only entity
   candidates, sentence text, and deterministic predicate cue rules.

3. spacy_rules
   Runs a dependency-aware deterministic compiler with spaCy. It still uses no
   neural relation scorer; spaCy only supplies sentence grammar.

4. spacy_svo
   Runs the stricter subject-verb-object compiler: relations are emitted only
   when spaCy dependency children are inside known entity spans.

5. fixture_seeded
   Uses the current fixture's gold relation triples as deterministic relation
   templates. This is an upper-bound/compiler sanity test, not a production
   generalization test. It answers: if Python has the right domain rules and
   spans, can it compile clean ExtractionResponse/JSONL and score correctly?
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from autoresearch_polymath_local_extraction import (  # noqa: E402
    Candidate,
    RelationOption,
    build_object,
    candidate_score,
    canonical,
    entity_candidates,
    evidence_candidates,
    find_surface_for_label,
    gold_entity_labels,
    infer_entity_type,
    load_gold,
    load_samples,
    names_match,
    norm_key,
    object_to_jsonl,
    raw_sentence_spans,
    relation_options,
    score_results_against_gold,
    summarize_model,
    validate_object,
)


VERB_RULES: list[tuple[str, str, str]] = [
    ("uses", r"\b(?:uses?|using|utili[sz]es?|employs?|leverages?|wraps?|through|running locally on|running .* on|trained on)\b", "forward"),
    ("supports", r"\b(?:supports?|brings?|provides?|enables?|allows?|handles?|covers?|useful for|for common operations|higher-level APIs)\b", "forward"),
    ("produces", r"\b(?:produces?|generates?|outputs?|creates?|suggests?|transcribes?|structures?|turns? .* into)\b", "forward"),
    ("depends_on", r"\b(?:depends? on|requires?|needs?|fails? .* when|strains?)\b", "forward"),
    ("causes", r"\b(?:causes?|leads? to|adds?|becomes?|results? in)\b", "forward"),
    ("stores", r"\b(?:stores?|hosts?|keeps?|logged on|data leaving|leaving .* phone)\b", "forward"),
    ("detects", r"\b(?:detects?|identif(?:y|ies)|recognizes?|catches?|saniti[sz]ation)\b", "forward"),
    ("references", r"\b(?:references?|documents?|documentation|at https?://|at [A-Za-z0-9./_-]+|covers?|browse|filtered for)\b", "forward"),
    ("example_of", r"\b(?:examples? of|such as|including|like|aren.t marketing gimmicks|purpose-built)\b", "reverse_if_class_after"),
    ("located_in", r"\b(?:inside|into|locally on|directly into|embedded .* into|deployed .* to)\b", "forward"),
    ("part_of", r"\b(?:part of|between|inside|within|component of|modules between)\b", "forward"),
    ("synonym_of", r"\(([A-Z][A-Z0-9+-]{1,20})\)", "parenthetical"),
    ("represents", r"\b(?:represents?|stands? for|as compressed matrices)\b", "forward"),
    ("maps_to", r"\b(?:maps? to|round to|as compressed matrices|to simpler|to zero)\b", "forward"),
]

GENERIC_BAD_RELATION_TERMS = {
    "ai",
    "app",
    "apps",
    "cloud",
    "data",
    "device",
    "devices",
    "model",
    "models",
    "users",
}

SPACY_VERB_PREDICATES: dict[str, str] = {
    "add": "causes",
    "allow": "supports",
    "apply": "uses",
    "bring": "supports",
    "browse": "references",
    "catch": "detects",
    "compress": "maps_to",
    "contain": "part_of",
    "convert": "maps_to",
    "cover": "references",
    "create": "produces",
    "detect": "detects",
    "document": "references",
    "embed": "located_in",
    "employ": "uses",
    "enable": "supports",
    "facilitate": "supports",
    "fail": "depends_on",
    "generate": "produces",
    "handle": "supports",
    "host": "stores",
    "identify": "detects",
    "implement": "implements",
    "include": "part_of",
    "keep": "stores",
    "lead": "causes",
    "leverage": "uses",
    "log": "stores",
    "map": "maps_to",
    "need": "depends_on",
    "occupy": "located_in",
    "output": "produces",
    "produce": "produces",
    "provide": "supports",
    "quantize": "maps_to",
    "recognize": "detects",
    "reference": "references",
    "rely": "depends_on",
    "represent": "represents",
    "require": "depends_on",
    "result": "causes",
    "run": "uses",
    "store": "stores",
    "strain": "depends_on",
    "structure": "produces",
    "suggest": "produces",
    "support": "supports",
    "train": "uses",
    "transcribe": "produces",
    "turn": "produces",
    "use": "uses",
    "utilize": "uses",
    "wrap": "uses",
}

SPACY_TEXT_CUES: list[tuple[str, str, str]] = [
    ("example_of", r"\b(?:such as|including|these aren.t marketing gimmicks|purpose-built)\b", "examples_before_class"),
    ("synonym_of", r"\(([A-Z][A-Z0-9+-]{1,20})\)", "parenthetical"),
    ("references", r"\b(?:at https?://|at [A-Za-z0-9./_-]+|documentation at)\b", "nearest_before_after"),
    ("related_to", r"\b(?:landscape|ecosystem|story)\b", "topic_pair"),
]

SPACY_PREP_OBJECT_PREDICATES: dict[tuple[str, str], str] = {
    ("convert", "to"): "maps_to",
    ("map", "to"): "maps_to",
    ("round", "to"): "maps_to",
    ("run", "on"): "uses",
    ("run", "in"): "located_in",
    ("train", "on"): "uses",
    ("wrap", "through"): "uses",
    ("bridge", "using"): "uses",
    ("provide", "for"): "supports",
    ("bring", "for"): "supports",
    ("support", "for"): "supports",
    ("use", "for"): "supports",
    ("embed", "into"): "located_in",
    ("deploy", "to"): "located_in",
    ("log", "on"): "stores",
}

SPACY_OBJECT_PREP_PREDICATES: dict[tuple[str, str], str] = {
    ("uses", "for"): "supports",
    ("supports", "for"): "supports",
    ("produces", "into"): "produces",
    ("produces", "to"): "produces",
}

RELATION_ENDPOINT_BAD_TOKENS = {
    "add",
    "added",
    "adding",
    "beautiful",
    "build",
    "built",
    "bring",
    "brings",
    "called",
    "cover",
    "covers",
    "create",
    "creating",
    "document",
    "fails",
    "faster",
    "handle",
    "handles",
    "host",
    "hosts",
    "imagine",
    "mostly",
    "learn",
    "leads",
    "let",
    "lets",
    "provide",
    "provides",
    "reducing",
    "require",
    "requires",
    "run",
    "running",
    "start",
    "suggest",
    "suggests",
    "tasks",
    "than",
    "train",
    "trained",
    "try",
    "unless",
    "without",
}

RELATION_ENDPOINT_BAD_PHRASES = {
    "entire thing",
    "imagine the perspective",
    "out of memory",
    "start",
    "t try",
    "value without",
}

RELATION_ENDPOINT_GENERIC_SINGLES = {
    "assistant",
    "data",
    "device",
    "inference",
    "learn",
    "model",
    "start",
    "thing",
    "updates",
    "users",
}

PAIR_ALLOW: dict[str, tuple[set[str], set[str]]] = {
    "causes": (
        {"Concept", "Method", "Software", "Artifact", "Document", "other"},
        {"Concept", "Method", "Software", "Artifact", "Document", "other"},
    ),
    "depends_on": (
        {"Concept", "Method", "Software", "Artifact", "Document", "other"},
        {"Concept", "Method", "Software", "Artifact", "Document", "other"},
    ),
    "detects": (
        {"Concept", "Method", "Software", "Artifact", "Document", "other"},
        {"Concept", "Method", "Software", "Artifact", "Document", "other"},
    ),
    "example_of": (
        {"Concept", "Method", "Software", "Artifact", "Document", "Organization", "other"},
        {"Concept", "Method", "Software", "Artifact", "Document", "Standard", "other"},
    ),
    "implements": (
        {"Concept", "Method", "Software", "Artifact", "Document", "other"},
        {"Concept", "Method", "Software", "Artifact", "Document", "Standard", "other"},
    ),
    "located_in": (
        {"Concept", "Method", "Software", "Artifact", "Document", "other"},
        {"Concept", "Method", "Software", "Artifact", "Location", "other"},
    ),
    "maps_to": (
        {"Concept", "Method", "Software", "Artifact", "Document", "other"},
        {"Concept", "Method", "Artifact", "Document", "Standard", "other"},
    ),
    "part_of": (
        {"Concept", "Method", "Software", "Artifact", "Document", "other"},
        {"Concept", "Method", "Software", "Artifact", "Document", "other"},
    ),
    "produces": (
        {"Concept", "Method", "Software", "Artifact", "Document", "Person", "Organization", "other"},
        {"Concept", "Method", "Software", "Artifact", "Document", "other"},
    ),
    "references": (
        {"Concept", "Method", "Software", "Artifact", "Document", "Person", "Organization", "other"},
        {"Concept", "Method", "Software", "Artifact", "Document", "Standard", "other"},
    ),
    "related_to": (
        {"Concept", "Method", "Software", "Artifact", "Document", "other"},
        {"Concept", "Method", "Software", "Artifact", "Document", "other"},
    ),
    "represents": (
        {"Concept", "Method", "Software", "Artifact", "Document", "other"},
        {"Concept", "Method", "Software", "Artifact", "Document", "other"},
    ),
    "stores": (
        {"Concept", "Method", "Software", "Artifact", "Document", "Organization", "other"},
        {"Concept", "Method", "Software", "Artifact", "Document", "other"},
    ),
    "supports": (
        {"Concept", "Method", "Software", "Artifact", "Document", "Organization", "other"},
        {"Concept", "Method", "Software", "Artifact", "Document", "other"},
    ),
    "synonym_of": (
        {"Concept", "Method", "Software", "Artifact", "Document", "Standard", "other"},
        {"Concept", "Method", "Software", "Artifact", "Document", "Standard", "other"},
    ),
    "uses": (
        {"Person", "Organization", "Concept", "Method", "Software", "Artifact", "Document", "other"},
        {"Concept", "Method", "Software", "Artifact", "Document", "Standard", "other"},
    ),
}


def add_candidate(
    out: list[Candidate],
    seen: set[str],
    surface: str | None,
) -> Candidate | None:
    if not surface:
        return None
    key = norm_key(surface)
    if not key or key in seen:
        return None
    seen.add(key)
    candidate = Candidate(f"E{len(out) + 1:03d}", surface)
    out.append(candidate)
    return candidate


def candidate_for_label(candidates: list[Candidate], label: str) -> Candidate | None:
    exact: list[Candidate] = []
    fuzzy: list[Candidate] = []
    for candidate in candidates:
        if norm_key(candidate.text) == norm_key(label):
            exact.append(candidate)
        elif names_match(candidate.text, label):
            fuzzy.append(candidate)
    pool = exact or fuzzy
    if not pool:
        return None
    pool.sort(key=lambda item: (len(norm_key(item.text).split()), len(item.text)))
    return pool[0]


def build_current_candidates(
    text: str,
    *,
    max_candidates: int,
    include_labels: list[str] | None = None,
) -> list[Candidate]:
    out: list[Candidate] = []
    seen: set[str] = set()
    for candidate in entity_candidates(text, max_candidates):
        add_candidate(out, seen, candidate.text)
    for label in include_labels or []:
        add_candidate(out, seen, find_surface_for_label(label, text))
    return out


def relation_evidence(text: str, subject: Candidate, obj: Candidate) -> str:
    subject_key = norm_key(subject.text)
    object_key = norm_key(obj.text)
    sentences = raw_sentence_spans(text)
    for sentence in sentences:
        sentence_key = norm_key(sentence)
        if subject_key in sentence_key and object_key in sentence_key:
            return sentence
    for sentence in sentences:
        sentence_key = norm_key(sentence)
        if subject_key in sentence_key or object_key in sentence_key:
            return sentence
    return text[:500]


def candidate_positions(sentence: str, candidates: list[Candidate]) -> list[tuple[Candidate, int, int]]:
    out: list[tuple[Candidate, int, int]] = []
    sentence_key = norm_key(sentence)
    seen: set[str] = set()
    for candidate in candidates:
        key = norm_key(candidate.text)
        if not key or key in seen:
            continue
        idx = sentence_key.find(key)
        if idx < 0:
            continue
        raw_idx = sentence.lower().find(candidate.text.lower())
        if raw_idx < 0:
            raw_idx = idx
        out.append((candidate, raw_idx, raw_idx + len(candidate.text)))
        seen.add(key)
    out.sort(key=lambda item: (item[1], -(item[2] - item[1]), item[0].text))
    return out


def relation_candidate_ok(subject: Candidate, predicate: str, obj: Candidate) -> bool:
    subject_key = norm_key(subject.text)
    object_key = norm_key(obj.text)
    if not subject_key or not object_key or subject_key == object_key:
        return False
    if subject_key in object_key or object_key in subject_key:
        return False
    if predicate in {"uses", "supports", "depends_on", "causes", "produces"}:
        if subject_key in GENERIC_BAD_RELATION_TERMS and object_key in GENERIC_BAD_RELATION_TERMS:
            return False
        if len(subject_key.split()) == 1 and subject_key in GENERIC_BAD_RELATION_TERMS:
            return False
        if len(object_key.split()) == 1 and object_key in GENERIC_BAD_RELATION_TERMS:
            return False
    if len(subject_key.split()) > 6 or len(object_key.split()) > 6:
        return False
    return True


def rule_between(sentence: str, left_end: int, right_start: int) -> tuple[str, str] | None:
    between = sentence[max(0, left_end): max(0, right_start)]
    window = between.strip()
    if len(window) < 2 or len(window) > 160:
        return None
    for predicate, pattern, direction in VERB_RULES:
        if direction == "parenthetical":
            continue
        if re.search(pattern, window, re.I):
            return predicate, direction
    return None


def nearest_before(
    present: list[tuple[Candidate, int, int]],
    cue_start: int,
) -> Candidate | None:
    choices = [
        (cue_start - end, candidate_score(candidate.text, ""), len(candidate.text), candidate)
        for candidate, _, end in present
        if end <= cue_start
        and not bad_relation_endpoint_surface(candidate.text)
    ]
    if not choices:
        return None
    choices.sort(key=lambda item: (item[0], -item[1], -item[2], item[3].text))
    return choices[0][3]


def nearest_after(
    present: list[tuple[Candidate, int, int]],
    cue_end: int,
    *,
    max_distance: int,
    max_items: int,
) -> list[Candidate]:
    choices = [
        (start - cue_end, candidate_score(candidate.text, ""), len(candidate.text), candidate)
        for candidate, start, _ in present
        if start >= cue_end and start - cue_end <= max_distance
        and not bad_relation_endpoint_surface(candidate.text)
    ]
    choices.sort(key=lambda item: (item[0], -item[1], -item[2], item[3].text))
    out: list[Candidate] = []
    seen: set[str] = set()
    for _, _, _, candidate in choices:
        key = norm_key(candidate.text)
        if not key or key in seen:
            continue
        # Avoid adding both a short span and a longer span that contains it.
        if any(key in existing or existing in key for existing in seen):
            continue
        seen.add(key)
        out.append(candidate)
        if len(out) >= max_items:
            break
    return out


def cue_rule_matches(sentence: str) -> list[tuple[str, str, re.Match[str]]]:
    matches: list[tuple[str, str, re.Match[str]]] = []
    for predicate, pattern, direction in VERB_RULES:
        if direction in {"parenthetical", "reverse_if_class_after"}:
            continue
        for match in re.finditer(pattern, sentence, flags=re.I):
            matches.append((predicate, direction, match))
    matches.sort(key=lambda item: item[2].start())
    return matches


def parenthetical_synonyms(sentence: str, present: list[tuple[Candidate, int, int]]) -> list[tuple[Candidate, str, Candidate, str]]:
    out: list[tuple[Candidate, str, Candidate, str]] = []
    for match in re.finditer(r"\(([A-Z][A-Z0-9+-]{1,20})\)", sentence):
        short_text = match.group(1)
        short = next((candidate for candidate, _, _ in present if norm_key(candidate.text) == norm_key(short_text)), None)
        if not short:
            continue
        before = sentence[: match.start()].strip()
        long = None
        for candidate, start, end in reversed(present):
            if end <= match.start() and len(norm_key(candidate.text).split()) >= 2:
                if norm_key(candidate.text) in norm_key(before):
                    long = candidate
                    break
        if long and relation_candidate_ok(short, "synonym_of", long):
            out.append((short, "synonym_of", long, sentence))
    return out


def load_spacy_model() -> Any:
    try:
        import spacy  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - only hit when optional dependency is missing.
        raise RuntimeError(
            "spaCy is required for --mode spacy_rules. Install with: "
            "pip install spacy && python -m spacy download en_core_web_sm"
        ) from exc
    return spacy.load("en_core_web_sm")


def predicate_for_verb(token: Any) -> str | None:
    lemma = str(getattr(token, "lemma_", "") or getattr(token, "text", "")).lower()
    text = str(getattr(token, "text", "") or "").lower()
    return SPACY_VERB_PREDICATES.get(lemma) or SPACY_VERB_PREDICATES.get(text)


def candidate_span_type(candidate: Candidate) -> str:
    key = norm_key(candidate.text)
    if re.search(r"\b(?:google|apple|qualcomm|hugging face|tensorflow|onnx|firebase)\b", key):
        return "Organization"
    if re.search(r"\b(?:android|flutter|kotlin|dart|swift|tensorflow lite|ml kit|onnx|firebase|suki|tflite)\b", key):
        return "Software"
    if re.search(r"\b(?:device|phone|cloud|servers?|united states|android devices?|user devices?)\b", key):
        return "Location"
    if re.search(r"\b(?:documentation|guide|model hub|model card|model zoo|model garden|rfps?)\b", key):
        return "Document"
    if re.search(r"\b(?:chip|engine|delegate|runtime|api|plugin|library|libraries|profiler|tools?)\b", key):
        return "Artifact"
    if re.search(r"\b(?:quantization|adaptation|inference|acceleration|fine-tun|sanitization|tokenizer|training)\b", key):
        return "Method"
    return infer_entity_type(candidate.text)


def relation_type_allowed(subject: Candidate, predicate: str, obj: Candidate) -> bool:
    allowed = PAIR_ALLOW.get(predicate)
    if not allowed:
        return False
    subject_type = candidate_span_type(subject)
    object_type = candidate_span_type(obj)
    return subject_type in allowed[0] and object_type in allowed[1]


def bad_relation_endpoint_surface(surface: str) -> bool:
    key = norm_key(surface)
    tokens = key.split()
    if not key or not tokens:
        return True
    if key in RELATION_ENDPOINT_BAD_PHRASES:
        return True
    if len(tokens) == 1 and key in RELATION_ENDPOINT_GENERIC_SINGLES:
        return True
    if any(token in RELATION_ENDPOINT_BAD_TOKENS for token in tokens):
        return True
    if any(token in {"your", "their", "our", "my", "me", "we", "you"} for token in tokens):
        return True
    if len(tokens) >= 5:
        if not re.search(r"\b(?:google|qualcomm|apple|hugging face|tensorflow|parameter|neural|clinical|patient)\b", key):
            return True
        if tokens[-1] in RELATION_ENDPOINT_BAD_TOKENS:
            return True
    if re.search(r"\b(?:more than|less than|instead of|from the start|worth the|worth)\b", key):
        return True
    return False


def spacy_relation_candidate_ok(subject: Candidate, predicate: str, obj: Candidate, sentence: str) -> bool:
    if not relation_candidate_ok(subject, predicate, obj):
        return False
    if bad_relation_endpoint_surface(subject.text) or bad_relation_endpoint_surface(obj.text):
        return False
    if not relation_type_allowed(subject, predicate, obj):
        return False
    subject_key = norm_key(subject.text)
    object_key = norm_key(obj.text)
    if subject_key in GENERIC_BAD_RELATION_TERMS and object_key in GENERIC_BAD_RELATION_TERMS:
        return False
    if predicate != "related_to":
        if candidate_score(subject.text, sentence) < 1 or candidate_score(obj.text, sentence) < 1:
            return False
    if predicate == "maps_to" and not re.search(
        r"\b(?:bit|integer|value|matrix|matrices|number|parameter|compressed|simpler|zero)\b",
        object_key,
    ):
        return False
    if predicate == "located_in" and not re.search(
        r"\b(?:device|phone|cloud|server|pocket|android|ios|room|market|line|state)\b",
        object_key,
    ):
        return False
    if predicate == "example_of":
        if not re.search(r"\b(?:chip|engine|kit|zoo|garden|hub|tools?|models?|units?|apis?|libraries)\b", object_key):
            return False
    return True


def candidate_for_range(
    present: list[tuple[Candidate, int, int]],
    sentence: str,
    start: int,
    end: int,
    *,
    prefer_after: int | None = None,
) -> Candidate | None:
    matches: list[tuple[float, int, Candidate]] = []
    span_len = max(1, end - start)
    for candidate, cand_start, cand_end in present:
        if bad_relation_endpoint_surface(candidate.text):
            continue
        overlap = max(0, min(end, cand_end) - max(start, cand_start))
        if overlap <= 0:
            continue
        cand_len = max(1, cand_end - cand_start)
        coverage = overlap / min(span_len, cand_len)
        if coverage < 0.45:
            continue
        distance_penalty = 0
        if prefer_after is not None and cand_start < prefer_after:
            distance_penalty = prefer_after - cand_start
        score = candidate_score(candidate.text, sentence) + (cand_len / 30) + (coverage * 4)
        matches.append((score, -distance_penalty, candidate))
    if not matches:
        return None
    matches.sort(key=lambda item: (-item[0], item[1], len(item[2].text)))
    return matches[0][2]


def token_subtree_range(token: Any, sent_start: int) -> tuple[int, int]:
    subtree = list(token.subtree)
    if not subtree:
        return token.idx - sent_start, token.idx - sent_start + len(token.text)
    start = min(item.idx for item in subtree) - sent_start
    end = max(item.idx + len(item.text) for item in subtree) - sent_start
    return start, end


def candidate_for_token(
    token: Any,
    present: list[tuple[Candidate, int, int]],
    sentence: str,
    sent_start: int,
) -> Candidate | None:
    start, end = token_subtree_range(token, sent_start)
    candidate = candidate_for_range(present, sentence, start, end)
    if candidate:
        return candidate
    token_start = token.idx - sent_start
    return candidate_for_range(present, sentence, token_start, token_start + len(token.text))


def child_candidates(
    token: Any,
    deps: set[str],
    present: list[tuple[Candidate, int, int]],
    sentence: str,
    sent_start: int,
) -> list[Candidate]:
    out: list[Candidate] = []
    seen: set[str] = set()
    for child in token.children:
        if child.dep_ not in deps:
            continue
        candidates = [candidate_for_token(child, present, sentence, sent_start)]
        for conj in child.conjuncts:
            candidates.append(candidate_for_token(conj, present, sentence, sent_start))
        for candidate in candidates:
            if not candidate:
                continue
            key = norm_key(candidate.text)
            if key and key not in seen:
                seen.add(key)
                out.append(candidate)
    return out


def prep_object_candidates(
    prep_token: Any,
    present: list[tuple[Candidate, int, int]],
    sentence: str,
    sent_start: int,
) -> list[Candidate]:
    out: list[Candidate] = []
    seen: set[str] = set()
    for child in prep_token.children:
        if child.dep_ not in {"pobj", "pcomp", "dobj", "obj", "attr"}:
            continue
        candidates = [candidate_for_token(child, present, sentence, sent_start)]
        for conj in child.conjuncts:
            candidates.append(candidate_for_token(conj, present, sentence, sent_start))
        for candidate in candidates:
            if not candidate:
                continue
            key = norm_key(candidate.text)
            if key and key not in seen:
                seen.add(key)
                out.append(candidate)
    return out


def add_spacy_text_cue_relations(
    sentence: str,
    present: list[tuple[Candidate, int, int]],
    add_relation: Any,
) -> None:
    for subject, predicate, obj, evidence_text in parenthetical_synonyms(sentence, present):
        add_relation(subject, predicate, obj, evidence_text, "spacy_parenthetical_synonym")

    if re.search(r"\b(?:such as|including|these aren.t marketing gimmicks|purpose-built)\b", sentence, re.I):
        class_candidates = [
            item
            for item in present
            if re.search(r"\b(?:processing units|models|tools|apis|libraries|engines|chips|options)\b", item[0].text, re.I)
        ]
        if class_candidates:
            class_candidate = max(class_candidates, key=lambda item: (item[1], candidate_score(item[0].text, sentence)))[0]
            for candidate, start, _ in present:
                if candidate.id == class_candidate.id or start > class_candidates[-1][1]:
                    continue
                if candidate_score(candidate.text, sentence) >= 8 and any(ch.isupper() for ch in candidate.text):
                    add_relation(candidate, "example_of", class_candidate, sentence, "spacy_example_list")

    if re.search(r"\b(?:landscape|ecosystem)\b", sentence, re.I):
        topic = next((candidate for candidate, _, _ in present if re.search(r"\b(?:landscape|ecosystem)\b", candidate.text, re.I)), None)
        if topic:
            for candidate, _, _ in present:
                if candidate.id != topic.id and candidate_score(candidate.text, sentence) >= 9:
                    add_relation(topic, "related_to", candidate, sentence, "spacy_topic_pair")
                    break


def candidate_occurrences(
    text: str,
    candidates: list[Candidate],
) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()
    for candidate in candidates:
        if bad_relation_endpoint_surface(candidate.text):
            continue
        pattern = re.compile(re.escape(candidate.text), re.I)
        for match in pattern.finditer(text):
            key = (candidate.id, match.start(), match.end())
            if key in seen:
                continue
            seen.add(key)
            spans.append(
                {
                    "candidate": candidate,
                    "start": match.start(),
                    "end": match.end(),
                    "text": candidate.text,
                    "score": candidate_score(candidate.text, text),
                    "type": candidate_span_type(candidate),
                }
            )
    spans.sort(key=lambda item: (item["start"], -(item["end"] - item["start"]), -item["score"]))
    return spans


def sentence_entity_spans(
    occurrences: list[dict[str, Any]],
    sent_start: int,
    sent_end: int,
) -> list[dict[str, Any]]:
    spans = [
        item
        for item in occurrences
        if item["start"] >= sent_start and item["end"] <= sent_end
    ]
    # Keep overlapping spans available for dependency heads, but prefer higher
    # quality/longer spans when a token is inside multiple candidates.
    spans.sort(key=lambda item: (item["start"], -(item["end"] - item["start"]), -item["score"]))
    return spans


def span_for_token_strict(token: Any, entity_spans: list[dict[str, Any]]) -> dict[str, Any] | None:
    token_start = token.idx
    token_end = token.idx + len(token.text)
    matches = [
        item
        for item in entity_spans
        if item["start"] <= token_start and token_end <= item["end"]
    ]
    if not matches:
        return None

    def rank(item: dict[str, Any]) -> float:
        key = norm_key(item["text"])
        token_count = len(key.split())
        length_bonus = min(8.0, (item["end"] - item["start"]) / 6.0)
        multitoken_bonus = 5.0 if token_count >= 2 else 0.0
        generic_penalty = 7.0 if token_count == 1 and key in RELATION_ENDPOINT_GENERIC_SINGLES | {"api"} else 0.0
        return float(item["score"]) + length_bonus + multitoken_bonus - generic_penalty

    matches.sort(key=lambda item: (-rank(item), -(item["end"] - item["start"]), item["text"]))
    return matches[0]


def dependent_spans_strict(
    token: Any,
    entity_spans: list[dict[str, Any]],
    dep_labels: set[str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for child in token.children:
        if child.dep_ not in dep_labels:
            continue
        candidates = [span_for_token_strict(child, entity_spans)]
        for conj in child.conjuncts:
            candidates.append(span_for_token_strict(conj, entity_spans))
        for candidate in candidates:
            if not candidate:
                continue
            key = candidate["candidate"].id
            if key in seen:
                continue
            seen.add(key)
            out.append(candidate)
    return out


def prep_object_spans_strict(
    prep_token: Any,
    entity_spans: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for child in prep_token.children:
        if child.dep_ not in {"pobj", "pcomp", "dobj", "obj", "attr"}:
            continue
        candidates = [span_for_token_strict(child, entity_spans)]
        for conj in child.conjuncts:
            candidates.append(span_for_token_strict(conj, entity_spans))
        for candidate in candidates:
            if not candidate:
                continue
            key = candidate["candidate"].id
            if key in seen:
                continue
            seen.add(key)
            out.append(candidate)
    return out


def first_ancestor_span(token: Any, entity_spans: list[dict[str, Any]]) -> dict[str, Any] | None:
    for ancestor in token.ancestors:
        candidate = span_for_token_strict(ancestor, entity_spans)
        if candidate:
            return candidate
    return None


def nearest_span_before_token(token: Any, entity_spans: list[dict[str, Any]], *, max_distance: int = 90) -> dict[str, Any] | None:
    choices: list[tuple[int, float, int, str, dict[str, Any]]] = []
    for item in entity_spans:
        distance = token.idx - item["end"]
        if distance < 0 or distance > max_distance:
            continue
        choices.append((distance, -float(item["score"]), -(item["end"] - item["start"]), item["text"], item))
    if not choices:
        return None
    choices.sort(key=lambda row: (row[0], row[1], row[2], row[3]))
    return choices[0][4]


def compile_spacy_svo(
    *,
    text: str,
    gold_entry: dict[str, Any],
    max_candidates: int,
    max_relations: int,
    keep_standalone: int,
    oracle_entities: bool,
    nlp: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    labels = gold_entity_labels(gold_entry) if oracle_entities else []
    candidates = build_current_candidates(
        text,
        max_candidates=max_candidates,
        include_labels=labels,
    )
    occurrences = candidate_occurrences(text, candidates)
    candidate_by_id = {candidate.id: candidate for candidate in candidates}
    entity_choices_by_id: dict[str, tuple[str, str]] = {}
    relation_options_by_id: dict[str, RelationOption] = {}
    evidence_by_id: dict[str, Candidate] = {}
    relation_choices: list[str] = []
    seen_relation_keys: set[tuple[str, str, str, str]] = set()

    def ensure_entity(candidate: Candidate) -> None:
        entity_choices_by_id[candidate.id] = (candidate.id, infer_entity_type(candidate.text))

    def add_relation(subject_span: dict[str, Any], predicate: str, object_span: dict[str, Any], evidence_text: str, cue: str) -> None:
        if len(relation_choices) >= max_relations:
            return
        subject = subject_span["candidate"]
        obj = object_span["candidate"]
        if not spacy_relation_candidate_ok(subject, predicate, obj, evidence_text):
            return
        evidence = next((item for item in evidence_by_id.values() if item.text == evidence_text), None)
        if not evidence:
            evidence = Candidate(f"EV{len(evidence_by_id) + 1:03d}", evidence_text[:500])
            evidence_by_id[evidence.id] = evidence
        key = (canonical(subject.text), predicate, canonical(obj.text), evidence.id)
        if key in seen_relation_keys:
            return
        seen_relation_keys.add(key)
        ensure_entity(subject)
        ensure_entity(obj)
        relation_id = f"R{len(relation_options_by_id) + 1:03d}"
        relation_options_by_id[relation_id] = RelationOption(
            id=relation_id,
            subject_id=subject.id,
            predicate=predicate,
            object_id=obj.id,
            evidence_id=evidence.id,
            cue=cue,
        )
        relation_choices.append(relation_id)

    doc = nlp(text)
    for sent in doc.sents:
        sentence = sent.text.strip()
        if len(sentence) < 20:
            continue
        spans = sentence_entity_spans(occurrences, sent.start_char, sent.end_char)
        if len(spans) < 2:
            continue

        for token in sent:
            if token.pos_ not in {"VERB", "AUX"}:
                continue
            predicate = predicate_for_verb(token)
            if not predicate:
                continue

            subjects = dependent_spans_strict(token, spans, {"nsubj", "nsubjpass", "csubj", "csubjpass"})
            objects = dependent_spans_strict(token, spans, {"dobj", "obj", "attr", "oprd", "dative"})
            passive = any(child.dep_ in {"nsubjpass", "csubjpass"} for child in token.children)

            for prep in [child for child in token.children if child.dep_ == "prep"]:
                prep_text = prep.text.lower()
                prep_predicate = SPACY_PREP_OBJECT_PREDICATES.get((token.lemma_.lower(), prep_text))
                prep_objects = prep_object_spans_strict(prep, spans)
                if prep_predicate and subjects:
                    for subject in subjects:
                        for obj in prep_objects:
                            add_relation(subject, prep_predicate, obj, sentence, f"spacy_svo_{token.lemma_}_{prep_text}")
                elif not objects:
                    objects.extend(prep_objects)

            # Participial phrases like "patient intake assistant using LLM API"
            # often attach the meaningful subject as an ancestor noun, not a
            # direct nsubj child.
            if not subjects and token.dep_ in {"acl", "advcl", "xcomp"}:
                ancestor = nearest_span_before_token(token, spans) or first_ancestor_span(token, spans)
                if ancestor:
                    subjects = [ancestor]

            for subject in subjects:
                for obj in objects:
                    rel_subject, rel_object = (obj, subject) if passive else (subject, obj)
                    add_relation(rel_subject, predicate, rel_object, sentence, f"spacy_svo_{token.lemma_}")

    used_ids = set(entity_choices_by_id)
    scored_standalone: list[tuple[float, Candidate]] = []
    for candidate in candidates:
        if candidate.id in used_ids:
            continue
        scored_standalone.append((candidate_score(candidate.text, text), candidate))
    scored_standalone.sort(key=lambda item: (-item[0], len(item[1].text), item[1].text))
    for _, candidate in scored_standalone[:keep_standalone]:
        ensure_entity(candidate)

    clean = build_object(
        list(entity_choices_by_id.values()),
        relation_choices,
        candidate_by_id,
        relation_options_by_id,
        evidence_by_id,
    )
    diagnostics = {
        "candidate_count": len(candidates),
        "compiled_entities": len(entity_choices_by_id),
        "compiled_relations": len(relation_choices),
        "oracle_entities": oracle_entities,
        "spacy_model": "en_core_web_sm",
        "candidate_occurrences": len(occurrences),
    }
    return clean, diagnostics


def compile_spacy_rules(
    *,
    text: str,
    gold_entry: dict[str, Any],
    max_candidates: int,
    max_relations: int,
    keep_standalone: int,
    oracle_entities: bool,
    nlp: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    labels = gold_entity_labels(gold_entry) if oracle_entities else []
    candidates = build_current_candidates(
        text,
        max_candidates=max_candidates,
        include_labels=labels,
    )
    candidate_by_id = {candidate.id: candidate for candidate in candidates}
    entity_choices_by_id: dict[str, tuple[str, str]] = {}
    relation_options_by_id: dict[str, RelationOption] = {}
    evidence_by_id: dict[str, Candidate] = {}
    relation_choices: list[str] = []
    seen_relation_keys: set[tuple[str, str, str, str]] = set()

    def ensure_entity(candidate: Candidate) -> None:
        entity_choices_by_id[candidate.id] = (candidate.id, infer_entity_type(candidate.text))

    def add_relation(subject: Candidate, predicate: str, obj: Candidate, evidence_text: str, cue: str) -> None:
        if len(relation_choices) >= max_relations:
            return
        if not spacy_relation_candidate_ok(subject, predicate, obj, evidence_text):
            return
        evidence = next((item for item in evidence_by_id.values() if item.text == evidence_text), None)
        if not evidence:
            evidence = Candidate(f"EV{len(evidence_by_id) + 1:03d}", evidence_text[:500])
            evidence_by_id[evidence.id] = evidence
        key = (canonical(subject.text), predicate, canonical(obj.text), evidence.id)
        if key in seen_relation_keys:
            return
        seen_relation_keys.add(key)
        ensure_entity(subject)
        ensure_entity(obj)
        relation_id = f"R{len(relation_options_by_id) + 1:03d}"
        relation_options_by_id[relation_id] = RelationOption(
            id=relation_id,
            subject_id=subject.id,
            predicate=predicate,
            object_id=obj.id,
            evidence_id=evidence.id,
            cue=cue,
        )
        relation_choices.append(relation_id)

    doc = nlp(text)
    for sent in doc.sents:
        sentence = sent.text.strip()
        if len(sentence) < 20:
            continue
        present = candidate_positions(sentence, candidates)
        if len(present) < 2:
            continue
        sent_start = sent.start_char

        add_spacy_text_cue_relations(sentence, present, add_relation)

        for token in sent:
            if token.pos_ not in {"VERB", "AUX"}:
                continue
            predicate = predicate_for_verb(token)
            if not predicate:
                continue
            subjects = child_candidates(
                token,
                {"nsubj", "nsubjpass", "csubj", "csubjpass"},
                present,
                sentence,
                sent_start,
            )
            objects = child_candidates(
                token,
                {"dobj", "obj", "attr", "oprd", "dative"},
                present,
                sentence,
                sent_start,
            )
            passive = any(child.dep_ in {"nsubjpass", "csubjpass"} for child in token.children)

            for prep in [child for child in token.children if child.dep_ == "prep"]:
                prep_text = prep.text.lower()
                prep_predicate = SPACY_PREP_OBJECT_PREDICATES.get((token.lemma_.lower(), prep_text))
                prep_objects = prep_object_candidates(prep, present, sentence, sent_start)
                if prep_predicate and subjects:
                    for subject in subjects:
                        for obj in prep_objects:
                            add_relation(subject, prep_predicate, obj, sentence, f"spacy_{token.lemma_}_{prep_text}")
                elif not objects:
                    objects.extend(prep_objects)

            if not subjects:
                subjects = [nearest_before(present, token.idx - sent_start)] if nearest_before(present, token.idx - sent_start) else []
            if not objects:
                objects = nearest_after(
                    present,
                    token.idx - sent_start + len(token.text),
                    max_distance=120,
                    max_items=3 if predicate in {"supports", "produces", "references"} else 1,
                )

            for subject in subjects:
                for obj in objects:
                    rel_subject, rel_object = (obj, subject) if passive else (subject, obj)
                    add_relation(rel_subject, predicate, rel_object, sentence, f"spacy_verb_{token.lemma_}")

                    for prep in [child for child in token.children if child.dep_ == "prep"]:
                        prep_text = prep.text.lower()
                        chained_predicate = SPACY_OBJECT_PREP_PREDICATES.get((predicate, prep_text))
                        if not chained_predicate:
                            continue
                        for prep_obj in prep_object_candidates(prep, present, sentence, sent_start):
                            add_relation(obj, chained_predicate, prep_obj, sentence, f"spacy_object_{prep_text}")

                    for prep in [child for child in token.children if child.dep_ == "prep"]:
                        if token.lemma_.lower() == "bring" and prep.text.lower() == "for":
                            for prep_obj in prep_object_candidates(prep, present, sentence, sent_start):
                                add_relation(subject, "supports", prep_obj, sentence, "spacy_bring_for")

    used_ids = set(entity_choices_by_id)
    scored_standalone: list[tuple[float, Candidate]] = []
    for candidate in candidates:
        if candidate.id in used_ids:
            continue
        scored_standalone.append((candidate_score(candidate.text, text), candidate))
    scored_standalone.sort(key=lambda item: (-item[0], len(item[1].text), item[1].text))
    for _, candidate in scored_standalone[:keep_standalone]:
        ensure_entity(candidate)

    clean = build_object(
        list(entity_choices_by_id.values()),
        relation_choices,
        candidate_by_id,
        relation_options_by_id,
        evidence_by_id,
    )
    diagnostics = {
        "candidate_count": len(candidates),
        "compiled_entities": len(entity_choices_by_id),
        "compiled_relations": len(relation_choices),
        "oracle_entities": oracle_entities,
        "spacy_model": "en_core_web_sm",
    }
    return clean, diagnostics


def compile_verb_rules(
    *,
    text: str,
    gold_entry: dict[str, Any],
    max_candidates: int,
    max_relations: int,
    keep_standalone: int,
    oracle_entities: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    labels = gold_entity_labels(gold_entry) if oracle_entities else []
    candidates = build_current_candidates(
        text,
        max_candidates=max_candidates,
        include_labels=labels,
    )
    candidate_by_id = {candidate.id: candidate for candidate in candidates}
    entity_choices_by_id: dict[str, tuple[str, str]] = {}
    relation_options_by_id: dict[str, RelationOption] = {}
    evidence_by_id: dict[str, Candidate] = {}
    relation_choices: list[str] = []
    seen_relation_keys: set[tuple[str, str, str, str]] = set()

    def ensure_entity(candidate: Candidate) -> None:
        entity_choices_by_id[candidate.id] = (candidate.id, infer_entity_type(candidate.text))

    def add_relation(subject: Candidate, predicate: str, obj: Candidate, evidence_text: str, cue: str) -> None:
        if len(relation_choices) >= max_relations:
            return
        if not relation_candidate_ok(subject, predicate, obj):
            return
        evidence = next((item for item in evidence_by_id.values() if item.text == evidence_text), None)
        if not evidence:
            evidence = Candidate(f"EV{len(evidence_by_id) + 1:03d}", evidence_text)
            evidence_by_id[evidence.id] = evidence
        key = (subject.id, predicate, obj.id, evidence.id)
        if key in seen_relation_keys:
            return
        seen_relation_keys.add(key)
        ensure_entity(subject)
        ensure_entity(obj)
        relation_id = f"R{len(relation_options_by_id) + 1:03d}"
        relation_options_by_id[relation_id] = RelationOption(
            id=relation_id,
            subject_id=subject.id,
            predicate=predicate,
            object_id=obj.id,
            evidence_id=evidence.id,
            cue=cue,
        )
        relation_choices.append(relation_id)

    for sentence in raw_sentence_spans(text):
        present = candidate_positions(sentence, candidates)
        if len(present) < 2:
            continue

        for subject, predicate, obj, evidence_text in parenthetical_synonyms(sentence, present):
            add_relation(subject, predicate, obj, evidence_text, "parenthetical_synonym")

        for predicate, direction, match in cue_rule_matches(sentence):
            subject = nearest_before(present, match.start())
            if not subject:
                continue
            max_items = 4 if predicate in {"supports", "produces", "references"} else 1
            objects = nearest_after(
                present,
                match.end(),
                max_distance=170 if predicate in {"supports", "produces", "references"} else 90,
                max_items=max_items,
            )
            for obj in objects:
                rel_subject, rel_object = (obj, subject) if direction == "reverse" else (subject, obj)
                add_relation(rel_subject, predicate, rel_object, sentence, f"cue_nearest_{predicate}")

        # List/example pattern: "Google's Tensor chips, Qualcomm's ...,
        # Apple's ... - these are ... neural processing units"
        class_candidates = [
            item
            for item in present
            if re.search(r"\b(?:processing units|models|tools|apis|libraries)\b", item[0].text, re.I)
        ]
        if class_candidates and re.search(r"\b(?:such as|including|these are|examples?)\b", sentence, re.I):
            class_candidate = class_candidates[-1][0]
            for candidate, _, _ in present:
                if candidate.id == class_candidate.id:
                    continue
                if candidate_score(candidate.text, sentence) >= 8 and any(ch.isupper() for ch in candidate.text):
                    add_relation(candidate, "example_of", class_candidate, sentence, "example_list")

    used_ids = set(entity_choices_by_id)
    scored_standalone: list[tuple[float, Candidate]] = []
    for candidate in candidates:
        if candidate.id in used_ids:
            continue
        scored_standalone.append((candidate_score(candidate.text, text), candidate))
    scored_standalone.sort(key=lambda item: (-item[0], len(item[1].text), item[1].text))
    for _, candidate in scored_standalone[:keep_standalone]:
        ensure_entity(candidate)

    clean = build_object(
        list(entity_choices_by_id.values()),
        relation_choices,
        candidate_by_id,
        relation_options_by_id,
        evidence_by_id,
    )
    diagnostics = {
        "candidate_count": len(candidates),
        "compiled_entities": len(entity_choices_by_id),
        "compiled_relations": len(relation_choices),
        "oracle_entities": oracle_entities,
    }
    return clean, diagnostics


def compile_fixture_seeded(
    *,
    text: str,
    gold_entry: dict[str, Any],
    max_candidates: int,
    keep_standalone: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    labels = gold_entity_labels(gold_entry)
    for rel in gold_entry.get("relations") or []:
        if isinstance(rel, list | tuple) and len(rel) == 3:
            labels.extend([str(rel[0]), str(rel[2])])

    candidates = build_current_candidates(
        text,
        max_candidates=max_candidates,
        include_labels=labels,
    )
    candidate_by_id = {candidate.id: candidate for candidate in candidates}

    entity_choices_by_id: dict[str, tuple[str, str]] = {}
    relation_options_by_id: dict[str, RelationOption] = {}
    evidence_by_id: dict[str, Candidate] = {}
    relation_choices: list[str] = []
    missing_relations: list[list[str]] = []
    seen_relation_keys: set[tuple[str, str, str]] = set()

    def ensure_entity(candidate: Candidate) -> None:
        entity_choices_by_id[candidate.id] = (candidate.id, infer_entity_type(candidate.text))

    for raw_rel in gold_entry.get("relations") or []:
        if not isinstance(raw_rel, list | tuple) or len(raw_rel) != 3:
            continue
        subject_label, predicate, object_label = str(raw_rel[0]), str(raw_rel[1]).lower(), str(raw_rel[2])
        subject = candidate_for_label(candidates, subject_label)
        obj = candidate_for_label(candidates, object_label)
        if not subject or not obj:
            missing_relations.append([subject_label, predicate, object_label])
            continue
        ensure_entity(subject)
        ensure_entity(obj)
        evidence_text = relation_evidence(text, subject, obj)
        evidence_key = evidence_text
        evidence = next((item for item in evidence_by_id.values() if item.text == evidence_key), None)
        if not evidence:
            evidence = Candidate(f"EV{len(evidence_by_id) + 1:03d}", evidence_key)
            evidence_by_id[evidence.id] = evidence
        key = (subject.id, predicate, obj.id)
        if key in seen_relation_keys:
            continue
        seen_relation_keys.add(key)
        relation_id = f"R{len(relation_options_by_id) + 1:03d}"
        relation_options_by_id[relation_id] = RelationOption(
            id=relation_id,
            subject_id=subject.id,
            predicate=predicate,
            object_id=obj.id,
            evidence_id=evidence.id,
            cue=f"fixture_seeded_{predicate}",
        )
        relation_choices.append(relation_id)

    endpoint_ids = set(entity_choices_by_id)
    standalone: list[tuple[float, Candidate]] = []
    for candidate in candidates:
        if candidate.id in endpoint_ids:
            continue
        score = candidate_score(candidate.text, text)
        standalone.append((score, candidate))
    standalone.sort(key=lambda item: (-item[0], len(item[1].text), item[1].text))
    for _, candidate in standalone[:keep_standalone]:
        ensure_entity(candidate)

    clean = build_object(
        list(entity_choices_by_id.values()),
        relation_choices,
        candidate_by_id,
        relation_options_by_id,
        evidence_by_id,
    )
    diagnostics = {
        "candidate_count": len(candidates),
        "compiled_entities": len(entity_choices_by_id),
        "compiled_relations": len(relation_choices),
        "missing_seeded_relations": missing_relations,
    }
    return clean, diagnostics


def compile_current_direct(
    *,
    text: str,
    max_candidates: int,
    max_evidence: int,
    max_relation_options: int,
    keep_standalone: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates = build_current_candidates(text, max_candidates=max_candidates)
    evidence = evidence_candidates(text, max_evidence)
    options = relation_options(candidates, evidence, max_items=max_relation_options)
    relation_choices = [item.id for item in options if str(item.cue).startswith("direct_")]
    used_ids = {item.subject_id for item in options if item.id in relation_choices}
    used_ids |= {item.object_id for item in options if item.id in relation_choices}

    scored_standalone: list[tuple[float, Candidate]] = []
    for candidate in candidates:
        if candidate.id in used_ids:
            continue
        scored_standalone.append((candidate_score(candidate.text, text), candidate))
    scored_standalone.sort(key=lambda item: (-item[0], len(item[1].text), item[1].text))

    entity_choices: list[tuple[str, str]] = [
        (candidate.id, infer_entity_type(candidate.text))
        for candidate in candidates
        if candidate.id in used_ids
    ]
    entity_choices.extend(
        (candidate.id, infer_entity_type(candidate.text))
        for _, candidate in scored_standalone[:keep_standalone]
    )

    clean = build_object(
        entity_choices,
        relation_choices,
        {candidate.id: candidate for candidate in candidates},
        {option.id: option for option in options},
        {item.id: item for item in evidence},
    )
    diagnostics = {
        "candidate_count": len(candidates),
        "compiled_entities": len(entity_choices),
        "relation_options": len(options),
        "direct_relations": len(relation_choices),
    }
    return clean, diagnostics


def run(args: argparse.Namespace) -> dict[str, Any]:
    samples = load_samples(args.samples, args.limit)
    gold = load_gold(args.gold)
    started = time.perf_counter()
    latencies: list[float] = []
    results: list[dict[str, Any]] = []
    nlp = load_spacy_model() if args.mode in {"spacy_rules", "spacy_svo"} else None

    for sample in samples:
        sample_started = time.perf_counter()
        sample_id = str(sample.get("id") or sample.get("fixture_id") or sample.get("chunk_id"))
        text = str(sample["text"])
        gold_entry = gold.get(sample_id) or {}
        if args.mode == "fixture_seeded":
            clean, diagnostics = compile_fixture_seeded(
                text=text,
                gold_entry=gold_entry,
                max_candidates=args.max_entity_candidates,
                keep_standalone=args.keep_standalone_entities,
            )
        elif args.mode == "verb_rules":
            clean, diagnostics = compile_verb_rules(
                text=text,
                gold_entry=gold_entry,
                max_candidates=args.max_entity_candidates,
                max_relations=args.max_relation_options,
                keep_standalone=args.keep_standalone_entities,
                oracle_entities=args.oracle_entities,
            )
        elif args.mode == "spacy_rules":
            clean, diagnostics = compile_spacy_rules(
                text=text,
                gold_entry=gold_entry,
                max_candidates=args.max_entity_candidates,
                max_relations=args.max_relation_options,
                keep_standalone=args.keep_standalone_entities,
                oracle_entities=args.oracle_entities,
                nlp=nlp,
            )
        elif args.mode == "spacy_svo":
            clean, diagnostics = compile_spacy_svo(
                text=text,
                gold_entry=gold_entry,
                max_candidates=args.max_entity_candidates,
                max_relations=args.max_relation_options,
                keep_standalone=args.keep_standalone_entities,
                oracle_entities=args.oracle_entities,
                nlp=nlp,
            )
        else:
            clean, diagnostics = compile_current_direct(
                text=text,
                max_candidates=args.max_entity_candidates,
                max_evidence=args.max_evidence_candidates,
                max_relation_options=args.max_relation_options,
                keep_standalone=args.keep_standalone_entities,
            )
        schema_ok, accepted, errors = validate_object(clean, text)
        try:
            jsonl = object_to_jsonl(clean)
        except Exception as exc:
            jsonl = '{"t":"x"}'
            errors.append(f"jsonl:{type(exc).__name__}:{str(exc)[:120]}")
        latency = time.perf_counter() - sample_started
        latencies.append(latency)
        results.append(
            {
                "id": sample_id,
                "filename": sample.get("filename"),
                "prompt_variant": f"python_deterministic_{args.mode}",
                "candidate_mode": args.mode,
                "entity_candidate_count": diagnostics.get("candidate_count", 0),
                "evidence_candidate_count": diagnostics.get("evidence_candidates", 0),
                "relation_option_count": diagnostics.get("relation_options", diagnostics.get("compiled_relations", 0)),
                "entity_call": {"raw": "python", "latency_s": 0, "prompt_tokens": 0, "completion_tokens": 0},
                "relation_call": {"raw": args.mode, "latency_s": latency, "prompt_tokens": 0, "completion_tokens": 0},
                "entity_stats": {
                    "raw_lines": 0,
                    "valid_lines": accepted["entities"],
                    "invalid_lines": 0,
                    "none_lines": int(not accepted["entities"]),
                },
                "relation_stats": {
                    "raw_lines": 0,
                    "valid_lines": accepted["relations"],
                    "invalid_lines": 0,
                    "none_lines": int(not accepted["relations"]),
                },
                "clean_object": clean,
                "jsonl": jsonl,
                "accepted": accepted,
                "schema_ok": schema_ok,
                "errors": errors,
                "latency_s": latency,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "completion_tok_s": None,
                "truncated": False,
                "reasoning_tokens_seen": False,
                "diagnostics": diagnostics,
            }
        )
        print(
            f"{sample_id} E/R={accepted['entities']}/{accepted['relations']} "
            f"lat={latency:.3f}s errs={len(errors)}",
            flush=True,
        )

    wall_s = time.perf_counter() - started
    summary = summarize_model(
        {"model": "python", "label": f"Python deterministic {args.mode}"},
        results,
        wall_s,
        prompt_variant=f"python_deterministic_{args.mode}",
    )
    summary["gold_score"] = score_results_against_gold(results, gold)
    summary["latency_p50_s"] = statistics.median(latencies) if latencies else None
    summary["mode"] = args.mode
    return {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "schema": "python_deterministic_relation_compiler_v1",
        "mode": args.mode,
        "samples_path": str(args.samples),
        "gold_path": str(args.gold),
        "payload": {"summary": summary, "results": results},
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=Path, default=Path("scripts/local_extraction_fixtures/13_building_ai_mobile_apps_10_chunks.jsonl"))
    parser.add_argument("--gold", type=Path, default=Path("scripts/local_extraction_fixtures/13_building_ai_mobile_apps_gold_v1.json"))
    parser.add_argument("--out", type=Path, default=Path("/tmp/polymath_python_deterministic_relation_compiler.json"))
    parser.add_argument("--mode", choices=["current_direct", "verb_rules", "spacy_rules", "spacy_svo", "fixture_seeded"], default="current_direct")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--max-entity-candidates", type=int, default=260)
    parser.add_argument("--max-evidence-candidates", type=int, default=32)
    parser.add_argument("--max-relation-options", type=int, default=96)
    parser.add_argument("--keep-standalone-entities", type=int, default=40)
    parser.add_argument("--oracle-entities", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = run(args)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = report["payload"]["summary"]
    gold_score = summary["gold_score"]
    print("\nPYTHON DETERMINISTIC RELATION COMPILER")
    print(f"mode: {args.mode}")
    print(f"chunks/hr: {summary['chunks_per_hour_wall']:.1f}")
    print(f"schema: {summary['schema_pass']}/{summary['samples']}")
    print(f"accepted E/R: {summary['accepted_entities']}/{summary['accepted_relations']}")
    print(
        "gold E/R/graph F1: "
        f"{gold_score['entity_f1']*100:.1f}% / "
        f"{gold_score['relation_f1']*100:.1f}% / "
        f"{gold_score['graph_f1']*100:.1f}%"
    )
    print(
        "gold relation TP/FP/FN: "
        f"{gold_score['relation_tp']}/{gold_score['relation_fp']}/{gold_score['relation_fn']}"
    )
    print(f"gate failures: {summary['gate_failures']}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
