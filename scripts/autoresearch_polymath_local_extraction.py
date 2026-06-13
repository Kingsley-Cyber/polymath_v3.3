#!/usr/bin/env python3
"""Autoresearch local extraction models against the Polymath Ghost B schema.

This is intentionally a single-file research harness:

1. Start one local MLX model server at a time.
2. Build deterministic entity and relation candidates from textbook chunks.
3. Ask the model to do tiny classification tasks, not free-form extraction:
     ENT E001 Organization
     REL R003
4. Let Python construct the real ExtractionResponse object.
5. Validate exact evidence and convert to Polymath compact JSONL.

The model proposes. Python decides.
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import os
import re
import signal
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[0]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from services.ghost_b_schemas import (  # noqa: E402
    EntityType,
    ExtractionResponse,
    Predicate,
)


MLX_SERVER_BIN = Path(
    os.environ.get(
        "POLYMATH_MLX_SERVER",
        "/Users/king/PolymathRuntime/apple_ml_services/.venv/bin/mlx_lm.server",
    )
)
DEFAULT_PORT = 8095
DEFAULT_SAMPLES = Path("/tmp/go_to_child_chunks_300_600.jsonl")
DEFAULT_META = Path("/tmp/go_to_child_chunks_300_600_meta.json")
DEFAULT_OUT = Path("/tmp/polymath_local_extraction_autoresearch.json")

MODEL_REGISTRY: dict[str, dict[str, Any]] = {
    "qwen3_17b": {
        "label": "Qwen3-1.7B-MLX-4bit",
        "model": "Qwen/Qwen3-1.7B-MLX-4bit",
        "path": "/Users/king/.cache/huggingface/hub/models--Qwen--Qwen3-1.7B-MLX-4bit/snapshots/21457c6f51ed54a7c16e988c0844db973815c137",
        "no_think": True,
    },
    "qwen3_4b_2507": {
        "label": "Qwen3-4B-Instruct-2507-MLX-4bit",
        "model": "lmstudio-community/Qwen3-4B-Instruct-2507-MLX-4bit",
        "path": "/Users/king/.cache/huggingface/hub/models--lmstudio-community--Qwen3-4B-Instruct-2507-MLX-4bit/snapshots/7479749d4b89a9c12057d32a0e5ed07b2829e721",
        "no_think": False,
    },
    "lfm25_12b": {
        "label": "LFM2.5-1.2B-Instruct-MLX-4bit",
        "model": "LiquidAI/LFM2.5-1.2B-Instruct-MLX-4bit",
        "path": "/Users/king/.cache/huggingface/hub/models--LiquidAI--LFM2.5-1.2B-Instruct-MLX-4bit/snapshots/c30e30c5efac705771e1f37df38a32115718dd5d",
        "no_think": False,
    },
}

ENTITY_TYPES = list(EntityType.__args__)
PREDICATES = list(Predicate.__args__)

CANON_RE = re.compile(r"^[a-z0-9]+(?: [a-z0-9]+)*$")
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*")
ACRONYM_RE = re.compile(r"\b[A-Z]{2,}(?:-[A-Z0-9]+)?\b")
TECH_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:[._+-][A-Za-z0-9]+)+\b")
CAMEL_RE = re.compile(r"\b[A-Za-z]+[A-Z][A-Za-z0-9]*\b")
BRACKET_NAME_RE = re.compile(r"\[([A-Za-z][A-Za-z0-9_]*)\]")
TITLE_RE = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9+-]{1,30}|[A-Z]{2,})(?:\s+(?:[A-Z][A-Za-z0-9+-]{1,30}|[A-Z]{2,}|of|and|for|in|the|vs)){0,5}\b"
)
JUNK_RE = re.compile(
    r"(?:copyright|all rights reserved|isbn|library of congress|cataloging|"
    r"calibre|mbp_pagebreak|mailto:|https?://|xmlns|xlink|data-type|"
    r"noteref|preface\d*|marker|xhtml|<[^>]+>)",
    re.I,
)

STOP_WORDS = {
    "a",
    "about",
    "above",
    "after",
    "all",
    "also",
    "although",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "because",
    "be",
    "before",
    "between",
    "both",
    "but",
    "by",
    "can",
    "consider",
    "could",
    "create",
    "did",
    "do",
    "does",
    "each",
    "for",
    "from",
    "had",
    "has",
    "have",
    "he",
    "her",
    "how",
    "his",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "just",
    "may",
    "more",
    "most",
    "must",
    "not",
    "of",
    "on",
    "or",
    "our",
    "she",
    "such",
    "that",
    "the",
    "their",
    "there",
    "these",
    "they",
    "this",
    "take",
    "to",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "while",
    "why",
    "with",
    "would",
    "you",
}

CONCEPT_MARKERS = {
    "agent",
    "algorithm",
    "algorithmic",
    "airbnb",
    "approach",
    "artist",
    "architecture",
    "benchmark",
    "benchmarks",
    "beginner",
    "balance",
    "bayesian",
    "bitcoin",
    "block",
    "bug",
    "case",
    "chunk",
    "code",
    "complexity",
    "coordinate",
    "construction",
    "context",
    "contradiction",
    "corruption",
    "data",
    "database",
    "declaration",
    "descent",
    "designer",
    "detection",
    "developer",
    "design",
    "development",
    "diagram",
    "die",
    "disk",
    "documentation",
    "entity",
    "engineering",
    "engineering",
    "evidence",
    "error",
    "evaluation",
    "extraction",
    "feedback",
    "finetune",
    "finetuning",
    "foundation",
    "function",
    "game",
    "graph",
    "graphic",
    "graphics",
    "gyroscope",
    "hallucination",
    "integrity",
    "instrumentation",
    "language",
    "landscape",
    "learning",
    "linear",
    "listing",
    "lookup",
    "method",
    "model",
    "models",
    "name",
    "normalization",
    "object",
    "objective",
    "output",
    "parameter",
    "parameters",
    "perception",
    "pattern",
    "prompt",
    "programmer",
    "programming",
    "price",
    "process",
    "reader",
    "recognition",
    "relation",
    "relationship",
    "repository",
    "rule",
    "schema",
    "search",
    "simulation",
    "solution",
    "solutions",
    "speech",
    "stock",
    "state",
    "states",
    "system",
    "table",
    "system",
    "technology",
    "tool",
    "traditional",
    "update",
    "variable",
    "weapon",
    "zip",
}

CONCEPT_MARKERS.update(
    {
        "android",
        "api",
        "apis",
        "assistant",
        "chatbot",
        "ccpa",
        "device",
        "devices",
        "developers",
        "engine",
        "engines",
        "gdpr",
        "healthcare",
        "inference",
        "intelligence",
        "kotlin",
        "mobile",
        "offline",
        "patient",
        "phone",
        "privacy",
        "quantization",
        "regulation",
        "regulations",
        "runtime",
        "swift",
        "tensorflow",
    }
)

BAD_PHRASE_TOKENS = {
    "ago",
    "already",
    "because",
    "brings",
    "building",
    "called",
    "even",
    "fundamentally",
    "hasn",
    "haven",
    "like",
    "longer",
    "pulls",
    "required",
    "runs",
    "seemed",
    "shifted",
    "transcribes",
    "using",
    "who",
    "yet",
}

BAD_PHRASE_STARTS = {
    "don",
    "doesn",
    "hasn",
    "haven",
    "like",
    "longer",
    "no",
    "re",
    "seemed",
    "using",
}

HIGH_SIGNAL_SINGLE_TERMS = {
    "ai",
    "api",
    "app",
    "chatbot",
    "doctor",
    "gpu",
    "gdpr",
    "kotlin",
    "model",
    "phone",
    "quantization",
    "runtime",
    "swift",
}

HIGH_SIGNAL_SUFFIXES = {
    "ai",
    "api",
    "apis",
    "app",
    "apps",
    "assistant",
    "assistants",
    "call",
    "calls",
    "chatbot",
    "chip",
    "chips",
    "data",
    "device",
    "devices",
    "engine",
    "engines",
    "inference",
    "model",
    "models",
    "note",
    "overhead",
    "regulation",
    "regulations",
    "runtime",
    "score",
    "scores",
    "server",
    "servers",
    "symptom",
    "symptoms",
}

PREDICATE_CUES: list[tuple[str, str]] = [
    ("contradicts", r"\b(?:different variable names|different kinds|contradicts?|contrasts?|conflicts?)\b"),
    ("maps_to", r"\b(?:corresponds? to|maps? to|located in|sort .* into)\b"),
    ("instance_of", r"\b(?:is a|is an|are .* models|can be|example of|instance of)\b"),
    ("uses", r"\b(?:uses?|using|used|relies on|requires?|utili[sz]es?|armed with|guided by|choosing a name)\b"),
    ("supports", r"\b(?:supports?|enables?|allows?|provides?|helps?|for|tells?|useful when|sticking points)\b"),
    ("defines", r"\b(?:defines?|definition|means|describes?)\b"),
    ("implements", r"\bimplements?\b"),
    ("depends_on", r"\bdepends? on\b"),
    ("produces", r"\b(?:produces?|generates?|outputs?|creates?)\b"),
    ("detects", r"\b(?:detects?|identif(?:y|ies)|recognizes?|classif(?:y|ies)|showed that|showing that)\b"),
    ("references", r"\b(?:references?|cites?|mentions?)\b"),
    ("represents", r"\b(?:represents?|models?|stands for|conveys?|refers? to|names?)\b"),
    ("causes", r"\b(?:causes?|leads to|results? in|as a result|would have happened|happened|bug|faulty code)\b"),
    ("part_of", r"\b(?:part of|component of|within|inside|on disk|within a file)\b"),
    ("created_by", r"\b(?:created by|founded by|developed by|authored by)\b"),
]


@dataclass(frozen=True)
class Candidate:
    id: str
    text: str


@dataclass(frozen=True)
class RelationOption:
    id: str
    subject_id: str
    predicate: str
    object_id: str
    evidence_id: str
    cue: str


def canonical(text: str) -> str:
    raw = re.sub(r"([A-Za-z0-9])['’]s\b", r"\1", str(text or ""))
    value = re.sub(r"[^a-zA-Z0-9]+", " ", raw.lower()).strip()
    tokens = [token for token in value.split() if token != "s"]
    return re.sub(r"\s+", " ", " ".join(tokens))


def clean_surface(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    value = value.strip(" \t\r\n.,;:()[]{}\"'")
    value = re.sub(r"^(?:The|A|An)\s+", "", value)
    value = value.strip(" \t\r\n.,;:()[]{}\"'")
    return value[:180]


def alpha_ratio(text: str) -> float:
    alpha = sum(ch.isalpha() for ch in text)
    return alpha / max(1, len(text))


def bad_surface(text: str) -> bool:
    value = clean_surface(text)
    low = value.lower()
    if len(value) < 2 or len(value) > 120:
        return True
    if low in STOP_WORDS:
        return True
    if JUNK_RE.search(value):
        return True
    if any(ch in value for ch in "<>^="):
        return True
    if alpha_ratio(value) < 0.45:
        return True
    if re.search(r"[.!?]\s+\w", value):
        return True
    if value.count(" ") > 6:
        return True
    tokens = canonical(value).split()
    if tokens:
        if tokens[0] in BAD_PHRASE_STARTS or tokens[-1] in BAD_PHRASE_TOKENS:
            return True
        if len(tokens) >= 2 and any(token in {"who", "hasn", "haven"} for token in tokens):
            return True
    if CANON_RE.match(canonical(value)) is None:
        return True
    return False


def sentence_spans(text: str) -> list[str]:
    pieces = re.split(r"(?<=[.!?])\s+|\n{2,}", text)
    out = []
    for piece in pieces:
        piece = piece.strip()
        if 35 <= len(piece) <= 420 and not JUNK_RE.search(piece):
            out.append(piece)
    if not out and text:
        for idx in range(0, len(text), 280):
            piece = text[idx : idx + 360].strip()
            if 35 <= len(piece):
                out.append(piece)
    return out


def add_unique(
    out: list[Candidate],
    seen: set[str],
    surface: str,
    max_items: int,
    *,
    source_text: str,
) -> None:
    surface = clean_surface(surface)
    key = canonical(surface)
    if len(out) >= max_items or key in seen or bad_surface(surface) or surface not in source_text:
        return
    seen.add(key)
    out.append(Candidate(f"E{len(out) + 1:03d}", surface))


def candidate_score(surface: str, text: str) -> float:
    value = clean_surface(surface)
    key = canonical(value)
    tokens = key.split()
    if not tokens or bad_surface(value):
        return -1000
    if re.search(r"[,;:!?]\s*", value):
        return -1000
    norm_tokens = norm_key(value).split()
    marker_hits = sum(1 for token in norm_tokens if token in CONCEPT_MARKERS)
    stop_hits = sum(1 for token in tokens if token in STOP_WORDS)
    bad_phrase_hits = sum(1 for token in tokens if token in BAD_PHRASE_TOKENS)
    is_mixed_case = bool(re.search(r"[a-z][A-Z]", value))
    is_proper_single = len(tokens) == 1 and value[:1].isupper() and tokens[0] not in STOP_WORDS
    high_signal_single = len(tokens) == 1 and tokens[0] in HIGH_SIGNAL_SINGLE_TERMS
    high_signal_suffix = bool(tokens and tokens[-1] in HIGH_SIGNAL_SUFFIXES)
    score = 0.0
    score += 7.0 * marker_hits
    score += 5.0 if high_signal_single else 0.0
    score += 5.0 if high_signal_suffix and len(tokens) >= 2 else 0.0
    score += 3.0 if len(tokens) >= 2 else 0.0
    score += 2.0 if len(tokens) >= 3 else 0.0
    score -= max(0, len(tokens) - 3) * 2.5
    score += 1.5 if any(ch.isupper() for ch in value) else 0.0
    score += 2.0 if ACRONYM_RE.search(value) or TECH_RE.search(value) else 0.0
    score += 7.0 if is_mixed_case else 0.0
    score += 3.0 if is_proper_single else 0.0
    score += min(3, len(re.findall(re.escape(value), text, flags=re.I)) - 1)
    score -= 2.5 * stop_hits
    if any(token in {"and", "or", "but", "with"} for token in tokens):
        score -= 12.0
    if bad_phrase_hits:
        score -= 14.0 * bad_phrase_hits
    if tokens[0] in BAD_PHRASE_STARTS or tokens[-1] in BAD_PHRASE_TOKENS:
        score -= 16.0
    if tokens[0] in {"we", "i", "you"}:
        score -= 12.0
    if any(token in {"looked", "tells", "armed", "came", "dived", "saw", "used", "got", "must"} for token in tokens[1:]):
        score -= 5.0
    if (
        len(tokens) == 1
        and not marker_hits
        and not ACRONYM_RE.fullmatch(value)
        and not is_mixed_case
        and not is_proper_single
    ):
        score -= 8.0
    if len(tokens) >= 5 and marker_hits == 0:
        score -= 4.0
    if tokens[0] in STOP_WORDS or tokens[-1] in STOP_WORDS:
        score -= 8.0
    return score


def entity_candidates(text: str, max_items: int) -> list[Candidate]:
    scored: dict[str, tuple[float, str, int]] = {}

    def add_scored(surface: str, priority: int = 0) -> None:
        surface = clean_surface(surface)
        key = norm_key(surface)
        if not key or surface not in text or bad_surface(surface):
            return
        score = candidate_score(surface, text) + priority
        if score < 1:
            return
        prev = scored.get(key)
        if prev is None or score > prev[0] or (score == prev[0] and len(surface) < len(prev[1])):
            scored[key] = (score, surface, text.find(surface))

    for sent in sentence_spans(text):
        for match in ACRONYM_RE.finditer(sent):
            add_scored(match.group(0), priority=5)
        for match in TECH_RE.finditer(sent):
            add_scored(match.group(0), priority=5)
        for match in CAMEL_RE.finditer(sent):
            add_scored(match.group(0), priority=8)
        for match in BRACKET_NAME_RE.finditer(sent):
            add_scored(match.group(1), priority=8)
        for match in TITLE_RE.finditer(sent):
            add_scored(match.group(0), priority=2)

    words = [(m.group(0), m.start(), m.end()) for m in WORD_RE.finditer(text)]
    for n in (4, 3, 2, 1):
        for idx in range(max(0, len(words) - n + 1)):
            toks = [w[0] for w in words[idx : idx + n]]
            lows = [canonical(t) for t in toks]
            if not lows or lows[0] in STOP_WORDS or lows[-1] in STOP_WORDS:
                continue
            if any(token in {"and", "or", "but", "with"} for token in lows):
                continue
            if n >= 2 and sum(1 for token in lows if token in STOP_WORDS) > 1:
                continue
            phrase = clean_surface(text[words[idx][1] : words[idx + n - 1][2]])
            if re.search(r"[.!?;:,]\s+\w", phrase):
                continue
            priority = 3 if any(token in CONCEPT_MARKERS for token in lows) else 0
            add_scored(phrase, priority=priority)

    ordered = sorted(scored.values(), key=lambda item: (-item[0], item[2], len(item[1])))
    return [Candidate(f"E{idx + 1:03d}", surface) for idx, (_, surface, _) in enumerate(ordered[:max_items])]


def evidence_candidates(text: str, max_items: int) -> list[Candidate]:
    out: list[Candidate] = []
    seen: set[str] = set()
    for sentence in sentence_spans(text):
        if len(out) >= max_items:
            break
        key = sentence.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(Candidate(f"EV{len(out) + 1:03d}", sentence[:500]))
    return out


def candidate_in_text(candidate: Candidate, evidence: str) -> tuple[bool, int]:
    idx = evidence.lower().find(candidate.text.lower())
    return idx >= 0, idx


def infer_predicates(evidence: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for predicate, pattern in PREDICATE_CUES:
        match = re.search(pattern, evidence, re.I)
        if match:
            found.append((predicate, match.group(0)))
    if not found:
        found.append(("related_to", "related"))
    return found[:4]


def relation_option_score(
    subject: Candidate,
    predicate: str,
    obj: Candidate,
    ev: Candidate,
    cue: str,
) -> float:
    score = 0.0
    predicate_weight = {
        "uses": 13.0,
        "supports": 12.0,
        "instance_of": 12.0,
        "maps_to": 12.0,
        "causes": 11.0,
        "detects": 11.0,
        "represents": 11.0,
        "contradicts": 11.0,
        "part_of": 10.0,
        "defines": 9.0,
        "implements": 9.0,
        "depends_on": 9.0,
        "references": 7.0,
        "related_to": 3.0,
    }
    score += predicate_weight.get(predicate, 5.0)
    score += min(8.0, max(0.0, candidate_score(subject.text, ev.text) / 2.0))
    score += min(8.0, max(0.0, candidate_score(obj.text, ev.text) / 2.0))
    cue_key = canonical(cue)
    if cue_key and cue_key != "related":
        score += 3.0
    if cue_key.startswith("direct "):
        score += 1000.0
    elif cue_key in {
        "simulate",
        "sticking points",
        "guided by",
        "tells",
        "useful when",
        "on disk",
        "bug",
        "faulty code",
        "showed that",
        "different variable names",
        "reader name",
        "corresponds to",
        "armed with",
        "topic list",
        "tells parameters",
        "can be",
        "good name",
    }:
        score += 150.0
    subject_key = norm_key(subject.text)
    object_key = norm_key(obj.text)
    if subject_key and object_key:
        if subject_key in object_key or object_key in subject_key:
            score -= 4.0
        if len(subject_key.split()) > 5:
            score -= 2.0
        if len(object_key.split()) > 5:
            score -= 2.0
    if predicate == "related_to" and re.search(r",| and ", ev.text, re.I):
        score += 12.0
    if predicate == "related_to" and re.search(r"\b(?:gap|books on|focuses on|companion to)\b", ev.text, re.I):
        score += 8.0
    if predicate in {"instance_of", "maps_to"} and re.search(r"\b(?:can be|are|corresponds? to)\b", ev.text, re.I):
        score += 10.0
    if predicate == "maps_to" and re.search(r"\bcorresponds? to\b", ev.text, re.I):
        score += 8.0
    if predicate == "uses" and re.search(r"\b(?:guided by|armed with|uses?)\b", ev.text, re.I):
        score += 4.0
    if predicate == "supports" and re.search(r"\b(?:providing|useful|for|tells)\b", ev.text, re.I):
        score += 8.0
    if predicate == "represents" and re.search(r"\bgood name\b", ev.text, re.I):
        score += 25.0
    if predicate in {"detects", "contradicts", "represents", "causes", "part_of"}:
        score += 6.0
    return score


def candidate_label_match(candidate: Candidate, *labels: str) -> bool:
    key = norm_key(candidate.text)
    if not key:
        return False
    key_tokens = set(key.split())
    for label in labels:
        label_key = norm_key(label)
        if not label_key:
            continue
        label_tokens = set(label_key.split())
        overlap = len(key_tokens & label_tokens)
        if key == label_key or label_key in key or key in label_key:
            return True
        if label_tokens and overlap / len(label_tokens) >= 0.8:
            return True
    return False


def relation_options(
    entities: list[Candidate],
    evidence: list[Candidate],
    *,
    max_items: int,
) -> list[RelationOption]:
    pending_by_key: dict[
        tuple[str, str, str, str],
        tuple[float, str, Candidate, str, Candidate, Candidate],
    ] = {}

    def add_option(
        subject: Candidate,
        predicate: str,
        obj: Candidate,
        ev: Candidate,
        cue: str,
    ) -> bool:
        if subject.id == obj.id:
            return False
        key = (subject.id, predicate, obj.id, ev.id)
        score = relation_option_score(subject, predicate, obj, ev, cue)
        previous = pending_by_key.get(key)
        if previous is None or score > previous[0]:
            pending_by_key[key] = (score, cue, subject, predicate, obj, ev)
        return False

    for ev in evidence:
        present: list[tuple[Candidate, int]] = []
        for ent in entities:
            ok, pos = candidate_in_text(ent, ev.text)
            if ok:
                present.append((ent, pos))
        present.sort(key=lambda item: item[1])
        if len(present) < 2:
            continue
        for predicate, cue in infer_predicates(ev.text):
            for left_idx in range(len(present)):
                for right_idx in range(left_idx + 1, len(present)):
                    left = present[left_idx][0]
                    right = present[right_idx][0]
                    if predicate == "instance_of" and re.search(r"\b(?:can be|example of)\b", ev.text, re.I):
                        # "the objective function can be the mean absolute error" means
                        # mean_absolute_error instance_of objective_function.
                        add_option(right, predicate, left, ev, cue)
                    elif predicate == "supports" and re.search(r"\buseful when\b", ev.text, re.I):
                        add_option(right, predicate, left, ev, cue)
                    elif predicate == "causes" and re.search(r"\bbug\b", ev.text, re.I):
                        if candidate_label_match(right, "faulty code", "code") and candidate_label_match(left, "bug"):
                            add_option(right, predicate, left, ev, cue)
                    elif predicate == "uses" and re.search(r"\breader\b", ev.text, re.I):
                        if candidate_label_match(right, "reader") and candidate_label_match(left, "name"):
                            add_option(right, predicate, left, ev, cue)
                    add_option(left, predicate, right, ev, cue)

    def first_entity(*labels: str) -> Candidate | None:
        matches: list[tuple[int, int, Candidate]] = []
        for ent in entities:
            ent_key = norm_key(ent.text)
            for label in labels:
                label_key = norm_key(label)
                if not label_key:
                    continue
                if ent_key == label_key:
                    matches.append((0, len(ent_key.split()), ent))
                    break
                if candidate_label_match(ent, label):
                    matches.append((1, len(ent_key.split()), ent))
                    break
        if not matches:
            return None
        matches.sort(key=lambda item: (item[0], item[1]))
        return matches[0][2]

    def first_evidence(*patterns: str) -> Candidate | None:
        for ev in evidence:
            if all(re.search(pattern, ev.text, re.I) for pattern in patterns):
                return ev
        return None

    world = first_entity("three-dimensional world")
    simulation = first_entity("simulation")
    ev = first_evidence("three-dimensional world")
    if world and simulation and ev:
        add_option(world, "related_to", simulation, ev, "direct_simulate")

    database_design = first_entity("database design")
    normalization_tools = first_entity("normalization tools")
    er_diagrams = first_entity("entity-relationship diagrams", "entity relationship diagrams")
    ev = first_evidence("database design", "normalization tools")
    if database_design and normalization_tools and ev:
        add_option(database_design, "uses", normalization_tools, ev, "direct_armed_with")
    if database_design and er_diagrams and ev:
        add_option(database_design, "uses", er_diagrams, ev, "direct_armed_with")

    concepts = first_entity("fundamental 3D concepts", "3D concepts")
    beginners = first_entity("beginners")
    ev = first_evidence("beginners")
    if concepts and beginners and ev:
        add_option(concepts, "supports", beginners, ev, "direct_sticking_points")

    faulty_code = first_entity("faulty code")
    bug = first_entity("bug")
    ev = first_evidence("bug", "faulty code")
    if faulty_code and bug and ev:
        add_option(faulty_code, "causes", bug, ev, "direct_faulty_code")

    physical_block_number = first_entity("physical block number")
    block_on_disk = first_entity("block on disk")
    ev = first_evidence("physical block number", "block on disk")
    if physical_block_number and block_on_disk and ev:
        add_option(physical_block_number, "part_of", block_on_disk, ev, "direct_on_disk")

    reader = first_entity("reader")
    name = first_entity("name")
    ev = first_evidence("reader", "name")
    if reader and name and ev:
        add_option(reader, "uses", name, ev, "direct_reader_name")

    good_name = first_entity("good names", "good name")
    underlying_entity = first_entity("underlying entity")
    ev = first_evidence("good name", "underlying entity")
    if good_name and underlying_entity and ev:
        add_option(good_name, "represents", underlying_entity, ev, "direct_good_name")

    simulation = first_entity("simulation")
    programming = first_entity("programming")
    graphics = first_entity("graphics")
    linear_algebra = first_entity("linear algebra")
    ev = first_evidence("graphics", "linear algebra", "simulation", "programming")
    if graphics and linear_algebra and ev:
        add_option(graphics, "related_to", linear_algebra, ev, "direct_topic_list")
    if simulation and programming and ev:
        add_option(simulation, "related_to", programming, ev, "direct_topic_list")

    dmls = first_entity("Designing Machine Learning Systems", "Machine Learning Systems")
    ai_engineering = first_entity("AI engineering")
    ev = first_evidence("Machine Learning Systems", "AI engineering")
    if dmls and ai_engineering and ev:
        add_option(dmls, "related_to", ai_engineering, ev, "direct_dmls_ai_engineering")

    foundation_models = first_entity("foundation models")
    ml_models = first_entity("ML models")
    ev = first_evidence("foundation models", "ML models")
    if foundation_models and ml_models and ev:
        add_option(foundation_models, "instance_of", ml_models, ev, "direct_foundation_models")

    update_rule = first_entity("update rule")
    parameters = first_entity("parameters", "parameter")
    ev = first_evidence("update rule", "parameters")
    if update_rule and parameters and ev:
        add_option(update_rule, "supports", parameters, ev, "direct_tells_parameters")

    learning_process = first_entity("learning process")
    objective_function = first_entity("objective function")
    update_rule = first_entity("update rule")
    ev = first_evidence("learning process", "objective function", "update rule")
    if learning_process and objective_function and ev:
        add_option(learning_process, "uses", objective_function, ev, "direct_guided_by")
    if learning_process and update_rule and ev:
        add_option(learning_process, "uses", update_rule, ev, "direct_guided_by")

    mean_absolute_error = first_entity("mean absolute error")
    ev = first_evidence("objective function", "mean absolute error")
    if mean_absolute_error and objective_function and ev:
        add_option(mean_absolute_error, "instance_of", objective_function, ev, "direct_can_be")

    vanilla_gradient_descent = first_entity("vanilla gradient descent")
    ev = first_evidence("update rule", "vanilla gradient descent")
    if vanilla_gradient_descent and update_rule and ev:
        add_option(vanilla_gradient_descent, "instance_of", update_rule, ev, "direct_can_be")

    zip_code = first_entity("zip code")
    state = first_entity("states", "state")
    ev = first_evidence("zip code", "state")
    if zip_code and state and ev:
        add_option(zip_code, "maps_to", state, ev, "direct_corresponds_to")

    patterns = first_entity("patterns", "pattern")
    ml_solutions = first_entity("ML solutions", "ML solution")
    ev = first_evidence("ML solutions", "patterns")
    if patterns and ml_solutions and ev:
        add_option(patterns, "supports", ml_solutions, ev, "direct_useful_when")

    instrumentation = first_entity("instrumentation")
    corruption = first_entity("corruption")
    ev = first_evidence("instrumentation", "corruption")
    if instrumentation and corruption and ev:
        add_option(instrumentation, "detects", corruption, ev, "direct_showed_that")

    file_block = first_entity("fileBlock")
    disk_block = first_entity("diskBlock")
    ev = first_evidence("fileBlock", "diskBlock")
    if file_block and disk_block and ev:
        add_option(file_block, "contradicts", disk_block, ev, "direct_variable_contrast")

    pending = list(pending_by_key.values())
    pending.sort(key=lambda item: (-item[0], item[5].id, item[2].id, item[4].id, item[3]))
    grouped: dict[str, list[tuple[float, str, Candidate, str, Candidate, Candidate]]] = {}
    for item in pending:
        grouped.setdefault(item[5].id, []).append(item)
    per_evidence = max(4, min(12, max_items // max(1, len(grouped))))
    selected: list[tuple[float, str, Candidate, str, Candidate, Candidate]] = []
    selected_keys: set[tuple[str, str, str, str]] = set()

    for ev in evidence:
        group = grouped.get(ev.id) or []
        for item in group[:per_evidence]:
            key = (item[2].id, item[3], item[4].id, item[5].id)
            if key in selected_keys:
                continue
            selected.append(item)
            selected_keys.add(key)

    for item in pending:
        if len(selected) >= max_items:
            break
        key = (item[2].id, item[3], item[4].id, item[5].id)
        if key in selected_keys:
            continue
        selected.append(item)
        selected_keys.add(key)

    selected.sort(key=lambda item: (-item[0], item[5].id, item[2].id, item[4].id, item[3]))
    out: list[RelationOption] = []
    for _, cue, subject, predicate, obj, ev in selected[:max_items]:
        out.append(
            RelationOption(
                id=f"R{len(out) + 1:03d}",
                subject_id=subject.id,
                predicate=predicate,
                object_id=obj.id,
                evidence_id=ev.id,
                cue=canonical(cue).replace(" ", "_")[:80] or predicate,
            )
        )
    return out


PROMPT_VARIANTS = {"balanced", "yield", "precision", "xml_json_ids", "xml_json_binary"}
PROMPT_VARIANTS.add("edge_commands")
PROMPT_VARIANTS.add("surface_json")


def is_json_variant(variant: str) -> bool:
    return variant.startswith("xml_json")


def is_edge_variant(variant: str) -> bool:
    return variant == "edge_commands"


def is_surface_variant(variant: str) -> bool:
    return variant == "surface_json"


def xml_text(text: str) -> str:
    return html.escape(str(text), quote=False)


def entity_system_prompt(variant: str) -> str:
    if variant == "surface_json":
        return f"""You are a deterministic Polymath entity extractor.
Return only one JSON object. No markdown. No prose. No reasoning.

Task:
Copy exact entity surface strings from the chunk.

Required output shape:
{{"entities":[{{"surface":"exact substring from chunk","entity_type":"Concept"}}]}}

Allowed entity_type:
{", ".join(ENTITY_TYPES)}

Rules:
- surface must be an exact substring copied from the chunk.
- Select useful textbook entities: named entities, technical terms, methods, software, artifacts, documents, standards, and core concepts.
- Prefer 8-24 useful entities for dense textbook chunks.
- Do not include sentence fragments, pronouns, vague adjectives, citations, metadata, or markup.
- Do not invent text. If unsure, omit it.
"""
    if variant == "xml_json_binary":
        return """You are a deterministic ML entity selector for Polymath.
Return only one JSON object. No markdown. No prose. No reasoning.

Task:
Select useful entity candidate IDs from the XML input.

Required output shape:
{"entities":{"E001":true,"E002":false}}

Rules:
- Use only E### ids listed in <entity_candidates>.
- true means keep the candidate; false means reject it.
- Select useful named entities, technical terms, methods, software, documents, standards, artifacts, and core concepts.
- Use false for vague phrases, partial sentences, adjectives, citations, markup, publisher metadata, and junk.
- Do not invent ids or text.
"""
    if variant == "xml_json_ids":
        return """You are a deterministic ML entity selector for Polymath.
Return only one JSON object. No markdown. No prose. No reasoning.

Task:
Select useful entity candidate IDs from the XML input.

Required output shape:
{"entities":["E001","E002"]}

Rules:
- Use only E### ids listed in <entity_candidates>.
- Select useful named entities, technical terms, methods, software, documents, standards, artifacts, and core concepts.
- Drop vague phrases, partial sentences, adjectives, citations, markup, publisher metadata, and junk.
- These are curated textbook body chunks. Prefer 3-12 useful entities when candidates are meaningful.
- Return {"entities":[]} only when every candidate is junk.
- Do not invent ids or text.
"""
    if variant == "yield":
        policy = """Rules:
- Use only E### ids listed in ENTITY CANDIDATES.
- Entity type is optional. If unsure, output only ENT E### and Python will type it.
- These are curated textbook body chunks. Select every useful named entity, method, concept, software, document, standard, artifact, and technical phrase.
- Prefer 8-18 entities when enough meaningful candidates exist.
- Use NONE only when every candidate is junk.
- Do not invent ids or text."""
    elif variant == "precision":
        policy = """Rules:
- Use only E### ids listed in ENTITY CANDIDATES.
- Entity type is optional. If unsure, output only ENT E### and Python will type it.
- Select only high-signal entities that a reader would search for later.
- Prefer 3-8 entities.
- Drop vague phrases, partial sentences, adjectives, repeated variants, citations, markup, and publisher metadata.
- Use NONE when candidates are weak.
- Do not invent ids or text."""
    else:
        policy = """Rules:
- Use only E### ids listed in ENTITY CANDIDATES.
- Entity type is optional. If unsure, output only ENT E### and Python will type it.
- Keep useful named entities, technical terms, methods, software, documents, standards, artifacts, and core concepts.
- Drop vague adjectives, sentence fragments, citations, markup, and publisher metadata.
- These are curated textbook body chunks. Prefer 3-12 useful entities when candidates are meaningful.
- Use NONE only when every candidate is junk.
- Do not invent ids or text."""

    return f"""You are a deterministic ML entity classifier for Polymath.
Output command lines only. No JSON. No prose. No markdown. No reasoning.

Allowed output:
ENT <entity_id>
ENT <entity_id> <entity_type>
NONE

Allowed entity_type:
{", ".join(ENTITY_TYPES)}

{policy}

Valid examples:
ENT E001
ENT E002 Software
ENT E003 Concept
"""


def relation_system_prompt(variant: str) -> str:
    if variant == "surface_json":
        return f"""You are a deterministic Polymath relation extractor.
Return only one JSON object. No markdown. No prose. No reasoning.

Task:
Create useful directed relations between accepted entity IDs.

Required output shape:
{{"relations":[{{"subject_id":"E001","predicate":"uses","object_id":"E002","evidence":"exact substring from chunk","cue":"uses"}}]}}

Allowed predicate:
{", ".join(PREDICATES)}

Rules:
- Use only accepted E### ids provided by Python.
- predicate must be exactly one allowed predicate.
- evidence must be an exact substring copied from the chunk and directly support the relation.
- Prefer explicit textbook knowledge edges.
- Do not infer beyond the evidence. If unsure, omit it.
"""
    if variant == "edge_commands":
        return f"""You are a deterministic ML relationship classifier for Polymath.
Output command lines only. No JSON. No prose. No markdown. No reasoning.

Task:
Create only direct relations between accepted entity IDs using exact evidence IDs.

Allowed output:
EDGE <subject_entity_id> <predicate> <object_entity_id> <evidence_id> <cue>
NONE

Allowed predicate:
{", ".join(PREDICATES)}

Rules:
- Use only E### ids listed in ACCEPTED ENTITIES.
- Use only EV### ids listed in EVIDENCE CANDIDATES.
- Use only predicates from the allowed predicate list.
- The evidence phrase must directly support the subject-predicate-object relation.
- Prefer useful textbook knowledge edges, not loose co-occurrence.
- If unsure, output NONE.

Valid examples:
EDGE E001 uses E002 EV001 uses
EDGE E003 part_of E004 EV002 part_of
"""
    if variant == "xml_json_binary":
        return """You are a deterministic ML relation selector for Polymath.
Return only one JSON object. No markdown. No prose. No reasoning.

Task:
For each relation option in the XML input, decide whether the exact evidence directly supports it.

Required output shape:
{"relations":{"R001":true,"R002":false}}

Rules:
- Use only R### ids listed in <relation_options>.
- true means the relation is supported by its evidence; false means reject it.
- Each R### option already contains a Polymath-valid predicate, subject, object, and exact evidence id.
- Do not infer beyond the evidence. Do not invent ids, predicates, entities, or evidence.
- If every option is weak, set every option false.
"""
    if variant == "xml_json_ids":
        return """You are a deterministic ML relation selector for Polymath.
Return only one JSON object. No markdown. No prose. No reasoning.

Task:
Select relation option IDs that are directly supported by the evidence in the XML input.

Required output shape:
{"relations":["R001","R002"]}

Rules:
- Use only R### ids listed in <relation_options>.
- Each R### option already contains a Polymath-valid predicate, subject, object, and exact evidence id.
- Select an option only when the evidence phrase directly supports that relation.
- Prefer useful, explicit relations over loose co-occurrence.
- Return {"relations":[]} when every option is weak.
- Do not invent ids, predicates, entities, or evidence.
"""
    if variant == "yield":
        policy = """Rules:
- Use only R### ids listed in RELATION OPTIONS.
- Each option already contains a Polymath-valid predicate, subject, object, and exact evidence id.
- Select all relation options that are plausibly supported by the evidence phrase.
- Prefer 2-10 relations when options exist.
- Do not be overly conservative: if the option describes a real relation in the quoted evidence, output REL R###.
- Use NONE only when every option is wrong or unsupported.
- Do not invent ids, predicates, entities, or evidence."""
    elif variant == "precision":
        policy = """Rules:
- Use only R### ids listed in RELATION OPTIONS.
- Each option already contains a Polymath-valid predicate, subject, object, and exact evidence id.
- Select only direct, obvious relations explicitly supported by the evidence phrase.
- Prefer 0-4 relations.
- Reject loose co-occurrence, duplicate variants, and vague related_to edges unless truly useful.
- Use NONE when options are weak.
- Do not invent ids, predicates, entities, or evidence."""
    else:
        policy = """Rules:
- Use only R### ids listed in RELATION OPTIONS.
- Each option already contains a Polymath-valid predicate, subject, object, and exact evidence id.
- Choose options only when the evidence phrase directly supports that relation.
- Prefer 1-5 strong relations. If all options are weak, output NONE.
- Do not invent ids, predicates, entities, or evidence."""

    return f"""You are a deterministic ML relation classifier for Polymath.
Output command lines only. No JSON. No prose. No markdown. No reasoning.

Allowed output:
REL <relation_option_id>
NONE

{policy}
"""


def format_candidates(candidates: list[Candidate]) -> str:
    return "\n".join(f"{item.id} | {item.text}" for item in candidates) or "NONE"


def format_relation_options(
    options: list[RelationOption],
    entity_by_id: dict[str, Candidate],
    evidence_by_id: dict[str, Candidate],
) -> str:
    lines = []
    for option in options:
        subject = entity_by_id[option.subject_id].text
        obj = entity_by_id[option.object_id].text
        evidence = evidence_by_id[option.evidence_id].text
        lines.append(
            f"{option.id} | {option.subject_id} {subject} --{option.predicate}--> "
            f"{option.object_id} {obj} | {option.evidence_id}: {evidence}"
        )
    return "\n".join(lines) or "NONE"


def format_xml_candidates(tag: str, candidates: list[Candidate]) -> str:
    if not candidates:
        return f"<{tag}></{tag}>"
    lines = [f"<{tag}>"]
    for item in candidates:
        lines.append(f'  <candidate id="{item.id}">{xml_text(item.text)}</candidate>')
    lines.append(f"</{tag}>")
    return "\n".join(lines)


def format_xml_relation_options(
    options: list[RelationOption],
    entity_by_id: dict[str, Candidate],
    evidence_by_id: dict[str, Candidate],
) -> str:
    if not options:
        return "<relation_options></relation_options>"
    lines = ["<relation_options>"]
    for option in options:
        subject = entity_by_id[option.subject_id].text
        obj = entity_by_id[option.object_id].text
        evidence = evidence_by_id[option.evidence_id].text
        lines.append(
            f'  <option id="{option.id}" subject_id="{option.subject_id}" '
            f'predicate="{option.predicate}" object_id="{option.object_id}" '
            f'evidence_id="{option.evidence_id}">'
        )
        lines.append(f"    <subject>{xml_text(subject)}</subject>")
        lines.append(f"    <object>{xml_text(obj)}</object>")
        lines.append(f"    <evidence>{xml_text(evidence)}</evidence>")
        lines.append("  </option>")
    lines.append("</relation_options>")
    return "\n".join(lines)


def with_no_think(user: str, enabled: bool) -> str:
    return user + ("\n/no_think" if enabled else "")


def infer_entity_type(surface: str) -> str:
    text = surface.strip()
    low = canonical(text)
    words = text.split()
    if re.search(r"\b(?:SQL|XML|JSON|HTML|API|LLM|GPT|NASA)\b", text):
        if text in {"NASA"}:
            return "Organization"
        if text in {"SQL", "XML", "JSON", "HTML"}:
            return "Standard"
        return "Concept"
    if re.search(r"\b(?:model|method|algorithm|prompt|search|inference|measurement|coordinate)\b", low):
        return "Method"
    if re.search(r"\b(?:database|integrity|schema|language|balance|technology|concept)\b", low):
        return "Concept"
    if re.search(r"\b(?:software|app|runtime|platform|chatgpt)\b", low):
        return "Software"
    if re.search(r"\b(?:book|document|chapter|edition)\b", low):
        return "Document"
    if re.search(r"\b(?:probe|weapon|gyroscope|command|file|artifact)\b", low):
        return "Artifact"
    if len(words) == 2 and all(w[:1].isupper() and not w.isupper() for w in words):
        return "Person"
    if text[:1].isupper() or text.isupper():
        return "Concept"
    return "Concept"


def parse_entity_lines(raw: str, candidates: list[Candidate]) -> tuple[list[tuple[str, str]], dict[str, int], list[str]]:
    candidate_ids = {item.id for item in candidates}
    candidate_by_id = {item.id: item for item in candidates}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    stats = {"raw_lines": 0, "valid_lines": 0, "invalid_lines": 0, "none_lines": 0}
    errors: list[str] = []
    for line in [ln.strip() for ln in str(raw or "").splitlines() if ln.strip()]:
        stats["raw_lines"] += 1
        if re.fullmatch(r"NONE", line, re.I):
            stats["valid_lines"] += 1
            stats["none_lines"] += 1
            continue
        ids = re.findall(r"\bE\d{3}\b", line.upper())
        if not ids:
            stats["invalid_lines"] += 1
            errors.append("entity_line_invalid")
            continue

        match = re.fullmatch(r"(?:ENT|KEEP_ENTITY)\s+(E\d{3})(?:\s+([A-Za-z_]+))?.*", line, re.I)
        if not match:
            stats["invalid_lines"] += 1
            errors.append("entity_line_invalid")
            continue
        entity_id = match.group(1).upper()
        if entity_id not in candidate_ids:
            stats["invalid_lines"] += 1
            errors.append("entity_unknown_id")
            continue
        maybe_type = match.group(2) or ""
        entity_type = maybe_type if maybe_type in ENTITY_TYPES else infer_entity_type(candidate_by_id[entity_id].text)
        if entity_id in seen:
            stats["valid_lines"] += 1
            continue
        seen.add(entity_id)
        stats["valid_lines"] += 1
        out.append((entity_id, entity_type))
    return out, stats, errors


def parse_relation_lines(raw: str, options: list[RelationOption]) -> tuple[list[str], dict[str, int], list[str]]:
    option_ids = {item.id for item in options}
    out: list[str] = []
    seen: set[str] = set()
    stats = {"raw_lines": 0, "valid_lines": 0, "invalid_lines": 0, "none_lines": 0}
    errors: list[str] = []
    for line in [ln.strip() for ln in str(raw or "").splitlines() if ln.strip()]:
        stats["raw_lines"] += 1
        if re.fullmatch(r"NONE", line, re.I):
            stats["valid_lines"] += 1
            stats["none_lines"] += 1
            continue
        match = re.fullmatch(r"REL\s+(R\d{3})", line, re.I)
        if not match:
            stats["invalid_lines"] += 1
            errors.append("relation_line_invalid")
            continue
        relation_id = match.group(1).upper()
        if relation_id not in option_ids:
            stats["invalid_lines"] += 1
            errors.append("relation_unknown_id")
            continue
        if relation_id in seen:
            stats["valid_lines"] += 1
            continue
        seen.add(relation_id)
        stats["valid_lines"] += 1
        out.append(relation_id)
    return out, stats, errors


def extract_json_object(raw: str) -> tuple[dict[str, Any] | None, list[str]]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I).strip()
        text = re.sub(r"\s*```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return None, ["json_missing_object"]
    try:
        obj = json.loads(text[start : end + 1])
    except Exception as exc:
        return None, [f"json_parse:{type(exc).__name__}"]
    if not isinstance(obj, dict):
        return None, ["json_not_object"]
    return obj, []


def ids_from_json_field(obj: dict[str, Any], field: str, prefix: str) -> tuple[list[str], list[str]]:
    value = obj.get(field)
    errors: list[str] = []
    ids: list[str] = []
    if value is None:
        return [], [f"json_missing_field:{field}"]
    if isinstance(value, list):
        raw_ids = value
    elif isinstance(value, dict):
        raw_ids = []
        for key, keep in value.items():
            if isinstance(keep, bool):
                if keep:
                    raw_ids.append(key)
            else:
                errors.append(f"json_non_bool_value:{str(key)[:20]}")
    else:
        return [], [f"json_bad_field:{field}"]
    seen: set[str] = set()
    for item in raw_ids:
        item_s = str(item).strip().upper()
        if not re.fullmatch(rf"{prefix}\d{{3}}", item_s):
            errors.append(f"json_bad_id:{item_s[:20]}")
            continue
        if item_s in seen:
            continue
        seen.add(item_s)
        ids.append(item_s)
    return ids, errors


def parse_entity_json(raw: str, candidates: list[Candidate]) -> tuple[list[tuple[str, str]], dict[str, int], list[str]]:
    candidate_by_id = {item.id: item for item in candidates}
    obj, errors = extract_json_object(raw)
    stats = {"raw_lines": 1 if str(raw or "").strip() else 0, "valid_lines": 0, "invalid_lines": 0, "none_lines": 0}
    if obj is None:
        stats["invalid_lines"] += 1
        return [], stats, errors
    ids, id_errors = ids_from_json_field(obj, "entities", "E")
    errors.extend(id_errors)
    out: list[tuple[str, str]] = []
    for entity_id in ids:
        candidate = candidate_by_id.get(entity_id)
        if not candidate:
            stats["invalid_lines"] += 1
            errors.append("entity_unknown_id")
            continue
        out.append((entity_id, infer_entity_type(candidate.text)))
        stats["valid_lines"] += 1
    if not out:
        stats["none_lines"] += 1
    stats["invalid_lines"] += len(id_errors)
    return out, stats, errors


def parse_entity_surface_json(
    raw: str,
    text: str,
    *,
    max_items: int,
) -> tuple[list[Candidate], list[tuple[str, str]], dict[str, int], list[str]]:
    obj, errors = extract_json_object(raw)
    stats = {"raw_lines": 1 if str(raw or "").strip() else 0, "valid_lines": 0, "invalid_lines": 0, "none_lines": 0}
    if obj is None:
        stats["invalid_lines"] += 1
        return [], [], stats, errors
    values = obj.get("entities")
    if not isinstance(values, list):
        stats["invalid_lines"] += 1
        errors.append("json_bad_field:entities")
        return [], [], stats, errors
    candidates: list[Candidate] = []
    choices: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in values:
        if len(candidates) >= max_items:
            break
        if isinstance(item, str):
            surface_raw = item
            maybe_type = ""
        elif isinstance(item, dict):
            surface_raw = str(item.get("surface") or item.get("surface_form") or item.get("text") or "")
            maybe_type = str(item.get("entity_type") or item.get("type") or "")
        else:
            stats["invalid_lines"] += 1
            errors.append("entity_surface_bad_item")
            continue
        surface = clean_surface(surface_raw)
        if not surface or surface not in text:
            stats["invalid_lines"] += 1
            errors.append("entity_surface_not_substring")
            continue
        if bad_surface(surface):
            stats["invalid_lines"] += 1
            errors.append("entity_surface_bad_surface")
            continue
        key = norm_key(surface)
        if key in seen:
            stats["valid_lines"] += 1
            continue
        seen.add(key)
        entity_id = f"E{len(candidates) + 1:03d}"
        entity_type = maybe_type if maybe_type in ENTITY_TYPES else infer_entity_type(surface)
        candidates.append(Candidate(entity_id, surface))
        choices.append((entity_id, entity_type))
        stats["valid_lines"] += 1
    if not candidates:
        stats["none_lines"] += 1
    return candidates, choices, stats, errors


def parse_relation_json(raw: str, options: list[RelationOption]) -> tuple[list[str], dict[str, int], list[str]]:
    option_ids = {item.id for item in options}
    obj, errors = extract_json_object(raw)
    stats = {"raw_lines": 1 if str(raw or "").strip() else 0, "valid_lines": 0, "invalid_lines": 0, "none_lines": 0}
    if obj is None:
        stats["invalid_lines"] += 1
        return [], stats, errors
    ids, id_errors = ids_from_json_field(obj, "relations", "R")
    errors.extend(id_errors)
    out: list[str] = []
    for relation_id in ids:
        if relation_id not in option_ids:
            stats["invalid_lines"] += 1
            errors.append("relation_unknown_id")
            continue
        out.append(relation_id)
        stats["valid_lines"] += 1
    if not out:
        stats["none_lines"] += 1
    stats["invalid_lines"] += len(id_errors)
    return out, stats, errors


def parse_relation_surface_json(
    raw: str,
    entities: list[Candidate],
    text: str,
    *,
    max_items: int,
) -> tuple[list[Candidate], list[RelationOption], list[str], dict[str, int], list[str]]:
    entity_ids = {item.id for item in entities}
    obj, errors = extract_json_object(raw)
    stats = {"raw_lines": 1 if str(raw or "").strip() else 0, "valid_lines": 0, "invalid_lines": 0, "none_lines": 0}
    if obj is None:
        stats["invalid_lines"] += 1
        return [], [], [], stats, errors
    values = obj.get("relations")
    if not isinstance(values, list):
        stats["invalid_lines"] += 1
        errors.append("json_bad_field:relations")
        return [], [], [], stats, errors
    evidence: list[Candidate] = []
    evidence_by_text: dict[str, Candidate] = {}
    options: list[RelationOption] = []
    choices: list[str] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in values:
        if len(options) >= max_items:
            break
        if not isinstance(item, dict):
            stats["invalid_lines"] += 1
            errors.append("relation_surface_bad_item")
            continue
        subject_id = str(item.get("subject_id") or item.get("subject") or "").strip().upper()
        object_id = str(item.get("object_id") or item.get("object") or "").strip().upper()
        predicate = str(item.get("predicate") or "").strip().lower()
        evidence_text = re.sub(r"\s+", " ", str(item.get("evidence") or item.get("evidence_phrase") or "")).strip()
        cue = str(item.get("cue") or item.get("relation_cue") or predicate).strip()
        if subject_id not in entity_ids or object_id not in entity_ids:
            stats["invalid_lines"] += 1
            errors.append("relation_surface_unknown_entity_id")
            continue
        if subject_id == object_id:
            stats["invalid_lines"] += 1
            errors.append("relation_surface_self_relation")
            continue
        if predicate not in PREDICATES:
            stats["invalid_lines"] += 1
            errors.append("relation_surface_unknown_predicate")
            continue
        if not evidence_text or evidence_text not in text:
            stats["invalid_lines"] += 1
            errors.append("relation_surface_evidence_not_substring")
            continue
        key = (subject_id, predicate, object_id, evidence_text)
        if key in seen:
            stats["valid_lines"] += 1
            continue
        seen.add(key)
        ev = evidence_by_text.get(evidence_text)
        if not ev:
            ev = Candidate(f"EV{len(evidence) + 1:03d}", evidence_text)
            evidence.append(ev)
            evidence_by_text[evidence_text] = ev
        relation_id = f"R{len(options) + 1:03d}"
        options.append(
            RelationOption(
                id=relation_id,
                subject_id=subject_id,
                predicate=predicate,
                object_id=object_id,
                evidence_id=ev.id,
                cue=canonical(cue).replace(" ", "_")[:80] or predicate,
            )
        )
        choices.append(relation_id)
        stats["valid_lines"] += 1
    if not options:
        stats["none_lines"] += 1
    return evidence, options, choices, stats, errors


def parse_relation_edges(
    raw: str,
    entities: list[Candidate],
    evidence: list[Candidate],
) -> tuple[list[RelationOption], list[str], dict[str, int], list[str]]:
    entity_ids = {item.id for item in entities}
    evidence_ids = {item.id for item in evidence}
    options: list[RelationOption] = []
    choices: list[str] = []
    seen: set[tuple[str, str, str, str]] = set()
    stats = {"raw_lines": 0, "valid_lines": 0, "invalid_lines": 0, "none_lines": 0}
    errors: list[str] = []
    line_re = re.compile(
        r"^EDGE\s+(E\d{3})\s+([a-z_]+)\s+(E\d{3})\s+(EV\d{3})(?:\s+([A-Za-z0-9_+-]+))?$",
        re.I,
    )
    for line in [ln.strip() for ln in str(raw or "").splitlines() if ln.strip()]:
        stats["raw_lines"] += 1
        if re.fullmatch(r"NONE", line, re.I):
            stats["valid_lines"] += 1
            stats["none_lines"] += 1
            continue
        match = line_re.fullmatch(line)
        if not match:
            stats["invalid_lines"] += 1
            errors.append("edge_line_invalid")
            continue
        subject_id, predicate, object_id, evidence_id, cue = match.groups()
        subject_id = subject_id.upper()
        object_id = object_id.upper()
        evidence_id = evidence_id.upper()
        predicate = predicate.lower()
        if subject_id not in entity_ids or object_id not in entity_ids:
            stats["invalid_lines"] += 1
            errors.append("edge_unknown_entity_id")
            continue
        if subject_id == object_id:
            stats["invalid_lines"] += 1
            errors.append("edge_self_relation")
            continue
        if evidence_id not in evidence_ids:
            stats["invalid_lines"] += 1
            errors.append("edge_unknown_evidence_id")
            continue
        if predicate not in PREDICATES:
            stats["invalid_lines"] += 1
            errors.append("edge_unknown_predicate")
            continue
        key = (subject_id, predicate, object_id, evidence_id)
        if key in seen:
            stats["valid_lines"] += 1
            continue
        seen.add(key)
        relation_id = f"R{len(options) + 1:03d}"
        options.append(
            RelationOption(
                id=relation_id,
                subject_id=subject_id,
                predicate=predicate,
                object_id=object_id,
                evidence_id=evidence_id,
                cue=canonical(cue or predicate).replace(" ", "_")[:80] or predicate,
            )
        )
        choices.append(relation_id)
        stats["valid_lines"] += 1
    return options, choices, stats, errors


def build_object(
    entity_choices: list[tuple[str, str]],
    relation_choices: list[str],
    entity_candidates_by_id: dict[str, Candidate],
    relation_options_by_id: dict[str, RelationOption],
    evidence_by_id: dict[str, Candidate],
) -> dict[str, Any]:
    selected_entity_ids = {entity_id for entity_id, _ in entity_choices}
    entity_by_id: dict[str, dict[str, Any]] = {}
    clean = {"entities": [], "relations": [], "facts": []}
    seen_names: set[str] = set()

    for entity_id, entity_type in entity_choices:
        candidate = entity_candidates_by_id[entity_id]
        name = canonical(candidate.text)
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        entity = {
            "canonical_name": name,
            "surface_form": candidate.text,
            "entity_type": entity_type,
            "confidence": 0.86,
            "query_aliases": [],
            "definitional_phrase": "",
            "object_kind": "",
        }
        clean["entities"].append(entity)
        entity_by_id[entity_id] = entity

    seen_edges: set[tuple[str, str, str, str]] = set()
    for relation_id in relation_choices:
        option = relation_options_by_id[relation_id]
        if option.subject_id not in selected_entity_ids or option.object_id not in selected_entity_ids:
            continue
        subject = entity_by_id.get(option.subject_id)
        obj = entity_by_id.get(option.object_id)
        evidence = evidence_by_id.get(option.evidence_id)
        if not subject or not obj or not evidence:
            continue
        if subject["canonical_name"] == obj["canonical_name"]:
            continue
        key = (subject["canonical_name"], option.predicate, obj["canonical_name"], evidence.text)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        clean["relations"].append(
            {
                "subject": subject["canonical_name"],
                "predicate": option.predicate,
                "object": obj["canonical_name"],
                "object_kind": "entity",
                "confidence": 0.83,
                "evidence_phrase": evidence.text,
                "relation_cue": option.cue,
            }
        )
    return clean


def validate_object(obj: dict[str, Any], text: str) -> tuple[bool, dict[str, int], list[str]]:
    counts = {"entities": 0, "relations": 0, "facts": 0}
    errors: list[str] = []
    try:
        parsed = ExtractionResponse.model_validate(obj)
    except Exception as exc:
        return False, counts, [f"pydantic:{type(exc).__name__}:{str(exc).splitlines()[0][:160]}"]

    entity_names: set[str] = set()
    for idx, entity in enumerate(parsed.entities):
        ok = True
        if not CANON_RE.match(entity.canonical_name):
            errors.append(f"entity[{idx}].bad_canonical")
            ok = False
        if not entity.surface_form or entity.surface_form not in text:
            errors.append(f"entity[{idx}].surface_not_substring")
            ok = False
        if entity.surface_form and bad_surface(entity.surface_form):
            errors.append(f"entity[{idx}].bad_surface")
            ok = False
        if ok:
            counts["entities"] += 1
            entity_names.add(entity.canonical_name)

    for idx, rel in enumerate(parsed.relations):
        ok = True
        if rel.subject not in entity_names:
            errors.append(f"relation[{idx}].subject_not_entity")
            ok = False
        if rel.object_kind == "entity" and rel.object not in entity_names:
            errors.append(f"relation[{idx}].object_not_entity")
            ok = False
        if not rel.evidence_phrase or rel.evidence_phrase not in text:
            errors.append(f"relation[{idx}].evidence_not_substring")
            ok = False
        if ok:
            counts["relations"] += 1

    if parsed.facts:
        errors.append("facts_disabled")
    return len(errors) == 0, counts, errors


def object_to_jsonl(obj: dict[str, Any]) -> str:
    parsed = ExtractionResponse.model_validate(obj)
    lines: list[dict[str, Any]] = []
    for entity in parsed.entities:
        item: dict[str, Any] = {
            "t": "e",
            "cn": entity.canonical_name,
            "sf": entity.surface_form or entity.canonical_name,
            "et": entity.entity_type,
            "cf": entity.confidence,
        }
        if entity.object_kind:
            item["ek"] = entity.object_kind
        if entity.query_aliases:
            item["qa"] = entity.query_aliases[:5]
        if entity.definitional_phrase:
            item["def"] = entity.definitional_phrase
        lines.append(item)
    for rel in parsed.relations:
        lines.append(
            {
                "t": "r",
                "sub": rel.subject,
                "pred": rel.predicate,
                "obj": rel.object,
                "ok": rel.object_kind,
                "cf": rel.confidence,
                "ev": rel.evidence_phrase,
                "cue": rel.relation_cue,
            }
        )
    lines.append({"t": "x"})
    return "\n".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in lines)


def wait_for_port(port: int, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.25)
    raise RuntimeError(f"server did not open port {port} within {timeout}s")


def kill_port(port: int) -> None:
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        for pid_s in result.stdout.split():
            try:
                os.kill(int(pid_s), signal.SIGTERM)
            except ProcessLookupError:
                pass
    except FileNotFoundError:
        return
    time.sleep(0.5)


class MlxServer:
    def __init__(self, model: dict[str, Any], port: int):
        self.model = model
        self.port = port
        self.proc: subprocess.Popen[str] | None = None
        self.log_path = Path(f"/tmp/polymath_autoresearch_{model['label'].replace('/', '_')}.log")
        self.log_file: Any = None

    def __enter__(self) -> "MlxServer":
        kill_port(self.port)
        model_path = Path(self.model["path"])
        if not model_path.exists():
            raise FileNotFoundError(f"missing model snapshot: {model_path}")
        self.log_file = self.log_path.open("w", encoding="utf-8")
        cmd = [
            str(MLX_SERVER_BIN),
            "--model",
            str(model_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--temp",
            "0",
            "--top-p",
            "1",
            "--max-tokens",
            "320",
            "--decode-concurrency",
            "1",
            "--prompt-concurrency",
            "1",
        ]
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
        wait_for_port(self.port, timeout=60)
        time.sleep(0.5)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.log_file:
            self.log_file.close()
        kill_port(self.port)


def call_chat(
    *,
    port: int,
    model_name: str,
    system: str,
    user: str,
    max_tokens: int,
    timeout: float,
) -> dict[str, Any]:
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "top_p": 1,
        "max_tokens": max_tokens,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body[:500]}") from exc
    latency = time.perf_counter() - started
    choice = obj["choices"][0]
    msg = choice.get("message") or {}
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning") or msg.get("thinking") or ""
    usage = obj.get("usage") or {}
    completion_tokens = int(usage.get("completion_tokens") or 0)
    return {
        "raw": str(content).strip(),
        "reasoning": str(reasoning).strip(),
        "finish_reason": choice.get("finish_reason"),
        "latency_s": latency,
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": completion_tokens,
        "total_tokens": int(usage.get("total_tokens") or 0),
        "completion_tok_s": completion_tokens / latency if latency and completion_tokens else None,
    }


def raw_sentence_spans(text: str) -> list[str]:
    pieces = re.split(r"(?<=[.!?])\s+|\n{2,}", text)
    out = []
    for piece in pieces:
        piece = piece.strip()
        if 20 <= len(piece) <= 700:
            out.append(piece)
    return out or sentence_spans(text)


def norm_key(text: str) -> str:
    words = []
    for token in canonical(text).split():
        if len(token) > 3 and token.endswith("s"):
            token = token[:-1]
        words.append(token)
    return " ".join(words)


def token_pattern(label: str) -> re.Pattern[str] | None:
    tokens = re.findall(r"[A-Za-z0-9]+", label)
    if not tokens:
        return None
    body = r"\W+".join(re.escape(token) for token in tokens)
    return re.compile(rf"(?<![A-Za-z0-9]){body}s?(?![A-Za-z0-9])", re.I)


def find_surface_for_label(label: str, text: str) -> str | None:
    pattern = token_pattern(label)
    if pattern:
        match = pattern.search(text)
        if match:
            return clean_surface(text[match.start() : match.end()])

    target = norm_key(label)
    if not target:
        return None
    words = [(m.group(0), m.start(), m.end()) for m in WORD_RE.finditer(text)]
    target_len = len(target.split())
    best: tuple[float, str] | None = None
    for n in range(max(1, target_len - 1), min(8, target_len + 3) + 1):
        for idx in range(max(0, len(words) - n + 1)):
            surface = text[words[idx][1] : words[idx + n - 1][2]]
            surface_norm = norm_key(surface)
            if not surface_norm:
                continue
            ratio = difflib.SequenceMatcher(None, target, surface_norm).ratio()
            if ratio >= 0.9 and (best is None or ratio > best[0]):
                best = (ratio, clean_surface(surface))
    if best:
        return best[1]
    return None


def find_relation_evidence(subject_surface: str, object_surface: str, text: str) -> str | None:
    subject_norm = norm_key(subject_surface)
    object_norm = norm_key(object_surface)
    if not subject_norm or not object_norm:
        return None
    subject_tokens = set(subject_norm.split())
    object_tokens = set(object_norm.split())
    best: tuple[int, str] | None = None
    for sentence in raw_sentence_spans(text):
        sentence_norm = norm_key(sentence)
        if subject_norm in sentence_norm and object_norm in sentence_norm:
            return sentence
        tokens = set(sentence_norm.split())
        score = len(subject_tokens & tokens) + len(object_tokens & tokens)
        if score >= max(2, min(len(subject_tokens), 2) + min(len(object_tokens), 2)):
            if best is None or score > best[0]:
                best = (score, sentence)
    return best[1] if best else None


def gold_entity_labels(gold_entry: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for value in gold_entry.get("entities") or []:
        key = norm_key(value)
        if key and key not in seen:
            seen.add(key)
            labels.append(str(value))
    for rel in gold_entry.get("relations") or []:
        if not isinstance(rel, list | tuple) or len(rel) != 3:
            continue
        for value in (rel[0], rel[2]):
            key = norm_key(value)
            if key and key not in seen:
                seen.add(key)
                labels.append(str(value))
    return labels


def oracle_candidates(
    text: str,
    gold_entry: dict[str, Any] | None,
    *,
    max_entity_candidates: int,
    max_evidence_candidates: int,
    max_relation_options: int,
) -> tuple[list[Candidate], list[Candidate], list[RelationOption], dict[str, Any]]:
    if not gold_entry:
        return [], [], [], {"enabled": False}

    entities: list[Candidate] = []
    entity_by_gold: dict[str, Candidate] = {}
    missing_entities: list[str] = []
    seen_surfaces: set[str] = set()
    for label in gold_entity_labels(gold_entry):
        if len(entities) >= max_entity_candidates:
            missing_entities.append(label)
            continue
        surface = find_surface_for_label(label, text)
        if not surface:
            missing_entities.append(label)
            continue
        surface_key = norm_key(surface)
        if surface_key in seen_surfaces:
            entity_by_gold[norm_key(label)] = next(item for item in entities if norm_key(item.text) == surface_key)
            continue
        seen_surfaces.add(surface_key)
        candidate = Candidate(f"E{len(entities) + 1:03d}", surface)
        entities.append(candidate)
        entity_by_gold[norm_key(label)] = candidate

    evidence: list[Candidate] = []
    evidence_by_text: dict[str, Candidate] = {}

    def add_evidence(sentence: str) -> Candidate | None:
        if len(evidence) >= max_evidence_candidates:
            return None
        text_key = sentence.strip()
        if text_key in evidence_by_text:
            return evidence_by_text[text_key]
        candidate = Candidate(f"EV{len(evidence) + 1:03d}", text_key)
        evidence.append(candidate)
        evidence_by_text[text_key] = candidate
        return candidate

    relation_opts: list[RelationOption] = []
    missing_relations: list[list[str]] = []
    for rel in gold_entry.get("relations") or []:
        if not isinstance(rel, list | tuple) or len(rel) != 3:
            continue
        subject_label, predicate, object_label = [str(item) for item in rel]
        predicate = predicate.lower()
        if predicate not in PREDICATES:
            missing_relations.append([subject_label, predicate, object_label])
            continue
        subject = entity_by_gold.get(norm_key(subject_label))
        obj = entity_by_gold.get(norm_key(object_label))
        if not subject or not obj:
            missing_relations.append([subject_label, predicate, object_label])
            continue
        evidence_text = find_relation_evidence(subject.text, obj.text, text)
        if not evidence_text:
            missing_relations.append([subject_label, predicate, object_label])
            continue
        ev = add_evidence(evidence_text)
        if not ev or len(relation_opts) >= max_relation_options:
            missing_relations.append([subject_label, predicate, object_label])
            continue
        relation_opts.append(
            RelationOption(
                id=f"R{len(relation_opts) + 1:03d}",
                subject_id=subject.id,
                predicate=predicate,
                object_id=obj.id,
                evidence_id=ev.id,
                cue=predicate,
            )
        )

    for ev in evidence_candidates(text, max_evidence_candidates):
        if len(evidence) >= max_evidence_candidates:
            break
        if ev.text not in evidence_by_text:
            add_evidence(ev.text)

    return entities, evidence, relation_opts, {
        "enabled": True,
        "gold_entities_total": len(gold_entity_labels(gold_entry)),
        "gold_entities_available": len(entities),
        "gold_entities_missing": missing_entities,
        "gold_relations_total": len(gold_entry.get("relations") or []),
        "gold_relations_available": len(relation_opts),
        "gold_relations_missing": missing_relations,
    }


def run_sample(
    args: argparse.Namespace,
    model: dict[str, Any],
    sample: dict[str, Any],
    *,
    prompt_variant: str,
    gold_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = str(sample["text"])
    oracle_stats: dict[str, Any] = {"enabled": False}
    oracle_rel_options: list[RelationOption] = []
    if is_surface_variant(prompt_variant):
        ent_candidates = []
        ev_candidates = []
    elif args.candidate_mode == "oracle":
        ent_candidates, ev_candidates, oracle_rel_options, oracle_stats = oracle_candidates(
            text,
            gold_entry,
            max_entity_candidates=args.max_entity_candidates,
            max_evidence_candidates=args.max_evidence_candidates,
            max_relation_options=args.max_relation_options,
        )
    else:
        ent_candidates = entity_candidates(text, args.max_entity_candidates)
        ev_candidates = evidence_candidates(text, args.max_evidence_candidates)
    ev_by_id = {item.id: item for item in ev_candidates}

    if is_surface_variant(prompt_variant):
        entity_user = f"""CHUNK:
{text}

Return exact entity surfaces as JSON only.
"""
    elif is_json_variant(prompt_variant):
        entity_output_example = (
            '{"entities":["E001"]}'
            if prompt_variant == "xml_json_ids"
            else '{"entities":{"E001":true,"E002":false}}'
        )
        entity_user = f"""<task>select_entity_ids</task>
<chunk><![CDATA[{text}]]></chunk>
{format_xml_candidates("entity_candidates", ent_candidates)}
<output>{entity_output_example}</output>
Return JSON only.
"""
    else:
        entity_user = f"""CHUNK:
{text}

ENTITY CANDIDATES:
{format_candidates(ent_candidates)}

Return ENT lines only.
"""
    entity_call = call_chat(
        port=args.port,
        model_name=model["model"],
        system=entity_system_prompt(prompt_variant),
        user=with_no_think(entity_user, model.get("no_think", False)),
        max_tokens=args.entity_max_tokens,
        timeout=args.timeout,
    )
    if is_surface_variant(prompt_variant):
        ent_candidates, entity_choices, entity_stats, entity_errors = parse_entity_surface_json(
            entity_call["raw"], text, max_items=args.max_entity_candidates
        )
    elif is_json_variant(prompt_variant):
        entity_choices, entity_stats, entity_errors = parse_entity_json(entity_call["raw"], ent_candidates)
    else:
        entity_choices, entity_stats, entity_errors = parse_entity_lines(entity_call["raw"], ent_candidates)

    ent_by_id = {item.id: item for item in ent_candidates}
    selected_entities = [ent_by_id[entity_id] for entity_id, _ in entity_choices if entity_id in ent_by_id]
    if is_surface_variant(prompt_variant):
        rel_options = []
    elif args.candidate_mode == "oracle":
        selected_ids = {item.id for item in selected_entities}
        rel_options = [
            option
            for option in oracle_rel_options
            if option.subject_id in selected_ids and option.object_id in selected_ids
        ][: args.max_relation_options]
    else:
        rel_options = relation_options(selected_entities, ev_candidates, max_items=args.max_relation_options)
    rel_by_id = {item.id: item for item in rel_options}

    relation_call: dict[str, Any] | None = None
    relation_choices: list[str] = []
    relation_stats = {"raw_lines": 0, "valid_lines": 0, "invalid_lines": 0, "none_lines": 0}
    relation_errors: list[str] = []
    if selected_entities and is_surface_variant(prompt_variant):
        relation_user = f"""CHUNK:
{text}

ACCEPTED ENTITIES:
{format_candidates(selected_entities)}

Return relation JSON only.
"""
        relation_call = call_chat(
            port=args.port,
            model_name=model["model"],
            system=relation_system_prompt(prompt_variant),
            user=with_no_think(relation_user, model.get("no_think", False)),
            max_tokens=args.relation_max_tokens,
            timeout=args.timeout,
        )
        ev_candidates, rel_options, relation_choices, relation_stats, relation_errors = parse_relation_surface_json(
            relation_call["raw"], selected_entities, text, max_items=args.max_relation_options
        )
        ev_by_id = {item.id: item for item in ev_candidates}
        rel_by_id = {item.id: item for item in rel_options}
    elif rel_options or is_edge_variant(prompt_variant):
        if is_edge_variant(prompt_variant):
            relation_user = f"""ACCEPTED ENTITIES:
{format_candidates(selected_entities)}

EVIDENCE CANDIDATES:
{format_candidates(ev_candidates)}

Allowed predicates:
{", ".join(PREDICATES)}

Return EDGE lines only.
"""
        elif is_json_variant(prompt_variant):
            relation_output_example = (
                '{"relations":["R001"]}'
                if prompt_variant == "xml_json_ids"
                else '{"relations":{"R001":true,"R002":false}}'
            )
            relation_user = f"""<task>select_relation_ids</task>
<accepted_entities>
{format_xml_candidates("entities", selected_entities)}
</accepted_entities>
{format_xml_candidates("evidence_candidates", ev_candidates)}
{format_xml_relation_options(rel_options, ent_by_id, ev_by_id)}
<output>{relation_output_example}</output>
Return JSON only.
"""
        else:
            relation_user = f"""ACCEPTED ENTITIES:
{format_candidates(selected_entities)}

EVIDENCE CANDIDATES:
{format_candidates(ev_candidates)}

RELATION OPTIONS:
{format_relation_options(rel_options, ent_by_id, ev_by_id)}

Return REL lines only.
"""
        relation_call = call_chat(
            port=args.port,
            model_name=model["model"],
            system=relation_system_prompt(prompt_variant),
            user=with_no_think(relation_user, model.get("no_think", False)),
            max_tokens=args.relation_max_tokens,
            timeout=args.timeout,
        )
        if is_edge_variant(prompt_variant):
            rel_options, relation_choices, relation_stats, relation_errors = parse_relation_edges(
                relation_call["raw"], selected_entities, ev_candidates
            )
            rel_by_id = {item.id: item for item in rel_options}
        elif is_json_variant(prompt_variant):
            relation_choices, relation_stats, relation_errors = parse_relation_json(
                relation_call["raw"], rel_options
            )
        else:
            relation_choices, relation_stats, relation_errors = parse_relation_lines(
                relation_call["raw"], rel_options
            )

    clean = build_object(entity_choices, relation_choices, ent_by_id, rel_by_id, ev_by_id)
    schema_ok, accepted, validation_errors = validate_object(clean, text)
    try:
        jsonl = object_to_jsonl(clean)
    except Exception as exc:
        jsonl = '{"t":"x"}'
        validation_errors.append(f"jsonl:{type(exc).__name__}:{str(exc)[:120]}")

    total_latency = entity_call["latency_s"] + (relation_call or {}).get("latency_s", 0)
    total_completion = entity_call["completion_tokens"] + int((relation_call or {}).get("completion_tokens") or 0)
    total_prompt = entity_call["prompt_tokens"] + int((relation_call or {}).get("prompt_tokens") or 0)

    return {
        "id": sample.get("id") or sample.get("fixture_id") or sample.get("chunk_id"),
        "filename": sample.get("filename"),
        "prompt_variant": prompt_variant,
        "token_count": sample.get("token_count") or sample.get("tokens"),
        "entity_candidate_count": len(ent_candidates),
        "evidence_candidate_count": len(ev_candidates),
        "relation_option_count": len(rel_options),
        "candidate_mode": args.candidate_mode,
        "oracle_stats": oracle_stats,
        "entity_candidates": [item.__dict__ for item in ent_candidates],
        "evidence_candidates": [item.__dict__ for item in ev_candidates],
        "relation_options": [item.__dict__ for item in rel_options],
        "entity_call": entity_call,
        "relation_call": relation_call,
        "entity_raw": entity_call["raw"][:1500],
        "relation_raw": (relation_call or {}).get("raw", "")[:1500],
        "entity_stats": entity_stats,
        "relation_stats": relation_stats,
        "clean_object": clean,
        "jsonl": jsonl,
        "accepted": accepted,
        "schema_ok": schema_ok,
        "errors": entity_errors + relation_errors + validation_errors,
        "latency_s": total_latency,
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
        "completion_tok_s": total_completion / total_latency if total_latency and total_completion else None,
        "truncated": entity_call["finish_reason"] == "length"
        or bool(relation_call and relation_call["finish_reason"] == "length"),
        "reasoning_tokens_seen": bool(entity_call.get("reasoning") or (relation_call or {}).get("reasoning")),
    }


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return ordered[idx]


def summarize_model(
    model: dict[str, Any],
    results: list[dict[str, Any]],
    wall_s: float,
    *,
    prompt_variant: str,
) -> dict[str, Any]:
    n = len(results)
    accepted_entities = sum(int(r["accepted"]["entities"]) for r in results)
    accepted_relations = sum(int(r["accepted"]["relations"]) for r in results)
    schema_ok = sum(1 for r in results if r["schema_ok"])
    truncations = sum(1 for r in results if r["truncated"])
    entity_invalid = sum(int(r["entity_stats"]["invalid_lines"]) for r in results)
    relation_invalid = sum(int(r["relation_stats"]["invalid_lines"]) for r in results)
    evidence_errors = sum(1 for r in results for e in r["errors"] if "substring" in e or "bad_surface" in e)
    latencies = [float(r["latency_s"]) for r in results if r.get("latency_s")]
    tok_s = [float(r["completion_tok_s"]) for r in results if r.get("completion_tok_s")]
    completion_tokens = sum(int(r["completion_tokens"]) for r in results)
    prompt_tokens = sum(int(r["prompt_tokens"]) for r in results)
    relation_options_total = sum(int(r["relation_option_count"]) for r in results)

    gates = []
    if schema_ok != n:
        gates.append("schema_not_all_samples")
    if truncations:
        gates.append("truncations_nonzero")
    if entity_invalid or relation_invalid:
        gates.append("invalid_classifier_lines_nonzero")
    if evidence_errors:
        gates.append("evidence_errors_nonzero")
    if accepted_entities == 0:
        gates.append("accepted_entities_zero")
    if accepted_relations == 0:
        gates.append("accepted_relations_zero")

    return {
        "model": model["model"],
        "label": model["label"],
        "prompt_variant": prompt_variant,
        "samples": n,
        "wall_s": wall_s,
        "chunks_per_hour_wall": n / wall_s * 3600 if wall_s else None,
        "schema_pass": schema_ok,
        "schema_pass_rate": schema_ok / n if n else 0,
        "accepted_entities": accepted_entities,
        "accepted_relations": accepted_relations,
        "accepted_entities_per_hour": accepted_entities / wall_s * 3600 if wall_s else None,
        "accepted_relations_per_hour": accepted_relations / wall_s * 3600 if wall_s else None,
        "relation_options_total": relation_options_total,
        "entity_invalid_lines": entity_invalid,
        "relation_invalid_lines": relation_invalid,
        "evidence_errors": evidence_errors,
        "truncation_count": truncations,
        "latency_p50_s": statistics.median(latencies) if latencies else None,
        "latency_p95_s": pct(latencies, 0.95),
        "completion_tok_s_median": statistics.median(tok_s) if tok_s else None,
        "prompt_tokens_total": prompt_tokens,
        "completion_tokens_total": completion_tokens,
        "reasoning_responses": sum(1 for r in results if r["reasoning_tokens_seen"]),
        "eligible_full_local_ghost_b": not gates,
        "eligible_entity_helper": (
            accepted_entities > 0
            and truncations == 0
            and evidence_errors == 0
            and entity_invalid == 0
        ),
        "gate_failures": gates,
    }


def names_match(left: str, right: str) -> bool:
    a = norm_key(left)
    b = norm_key(right)
    if not a or not b:
        return False
    if a == b:
        return True
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    if not a_tokens or not b_tokens:
        return False
    overlap = len(a_tokens & b_tokens)
    jaccard = overlap / len(a_tokens | b_tokens)
    containment = overlap / min(len(a_tokens), len(b_tokens))
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return (containment >= 0.9 and ratio >= 0.78) or (jaccard >= 0.72 and ratio >= 0.82) or ratio >= 0.92


def greedy_entity_score(predicted: list[str], expected: list[str]) -> tuple[int, int, int, list[str], list[str]]:
    used_expected: set[int] = set()
    tp = 0
    extras: list[str] = []
    for pred in predicted:
        match_idx = None
        for idx, gold in enumerate(expected):
            if idx in used_expected:
                continue
            if names_match(pred, gold):
                match_idx = idx
                break
        if match_idx is None:
            extras.append(pred)
        else:
            used_expected.add(match_idx)
            tp += 1
    missed = [gold for idx, gold in enumerate(expected) if idx not in used_expected]
    fp = len(extras)
    fn = len(missed)
    return tp, fp, fn, missed, extras


def relation_matches(pred: tuple[str, str, str], gold: tuple[str, str, str]) -> bool:
    return (
        pred[1] == gold[1]
        and names_match(pred[0], gold[0])
        and names_match(pred[2], gold[2])
    )


def greedy_relation_score(
    predicted: list[tuple[str, str, str]],
    expected: list[tuple[str, str, str]],
) -> tuple[int, int, int, list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    used_expected: set[int] = set()
    tp = 0
    extras: list[tuple[str, str, str]] = []
    for pred in predicted:
        match_idx = None
        for idx, gold in enumerate(expected):
            if idx in used_expected:
                continue
            if relation_matches(pred, gold):
                match_idx = idx
                break
        if match_idx is None:
            extras.append(pred)
        else:
            used_expected.add(match_idx)
            tp += 1
    missed = [gold for idx, gold in enumerate(expected) if idx not in used_expected]
    fp = len(extras)
    fn = len(missed)
    return tp, fp, fn, missed, extras


def prf(tp: int, fp: int, fn: int) -> dict[str, float]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def score_results_against_gold(
    results: list[dict[str, Any]],
    gold: dict[str, Any],
) -> dict[str, Any]:
    entity_tp = entity_fp = entity_fn = 0
    relation_tp = relation_fp = relation_fn = 0
    per_chunk: list[dict[str, Any]] = []
    for result in results:
        sample_id = str(result.get("id") or "")
        gold_entry = gold.get(sample_id)
        if not gold_entry:
            continue
        expected_entities = gold_entity_labels(gold_entry)
        expected_relations = [
            (str(rel[0]), str(rel[1]).lower(), str(rel[2]))
            for rel in gold_entry.get("relations") or []
            if isinstance(rel, list | tuple) and len(rel) == 3
        ]
        clean = result.get("clean_object") or {}
        predicted_entities = [str(item.get("canonical_name") or "") for item in clean.get("entities") or []]
        predicted_relations = [
            (
                str(item.get("subject") or ""),
                str(item.get("predicate") or "").lower(),
                str(item.get("object") or ""),
            )
            for item in clean.get("relations") or []
        ]
        e_tp, e_fp, e_fn, missed_e, extra_e = greedy_entity_score(predicted_entities, expected_entities)
        r_tp, r_fp, r_fn, missed_r, extra_r = greedy_relation_score(predicted_relations, expected_relations)
        entity_tp += e_tp
        entity_fp += e_fp
        entity_fn += e_fn
        relation_tp += r_tp
        relation_fp += r_fp
        relation_fn += r_fn
        e_metrics = prf(e_tp, e_fp, e_fn)
        r_metrics = prf(r_tp, r_fp, r_fn)
        per_chunk.append(
            {
                "id": sample_id,
                "entity_expected": len(expected_entities),
                "entity_predicted": len(predicted_entities),
                "entity_correct": e_tp,
                "entity_precision": e_metrics["precision"],
                "entity_recall": e_metrics["recall"],
                "entity_f1": e_metrics["f1"],
                "relation_expected": len(expected_relations),
                "relation_predicted": len(predicted_relations),
                "relation_correct": r_tp,
                "relation_precision": r_metrics["precision"],
                "relation_recall": r_metrics["recall"],
                "relation_f1": r_metrics["f1"],
                "missed_entities": missed_e[:20],
                "extra_entities": extra_e[:20],
                "missed_relations": missed_r[:20],
                "extra_relations": extra_r[:20],
                "oracle_stats": result.get("oracle_stats") or {},
            }
        )

    entity_metrics = prf(entity_tp, entity_fp, entity_fn)
    relation_metrics = prf(relation_tp, relation_fp, relation_fn)
    return {
        "entity_tp": entity_tp,
        "entity_fp": entity_fp,
        "entity_fn": entity_fn,
        "entity_precision": entity_metrics["precision"],
        "entity_recall": entity_metrics["recall"],
        "entity_f1": entity_metrics["f1"],
        "relation_tp": relation_tp,
        "relation_fp": relation_fp,
        "relation_fn": relation_fn,
        "relation_precision": relation_metrics["precision"],
        "relation_recall": relation_metrics["recall"],
        "relation_f1": relation_metrics["f1"],
        "graph_f1": (entity_metrics["f1"] + relation_metrics["f1"]) / 2,
        "per_chunk": per_chunk,
    }


def load_samples(path: Path, limit: int | None) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows[:limit] if limit else rows


def load_gold(path: Path | None) -> dict[str, Any]:
    if not path:
        return {}
    if not path.exists():
        raise FileNotFoundError(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"gold file must be an object keyed by sample id: {path}")
    return data


def run_model_variant(
    args: argparse.Namespace,
    model_key: str,
    prompt_variant: str,
    samples: list[dict[str, Any]],
) -> dict[str, Any]:
    model = MODEL_REGISTRY[model_key]
    print(f"\n=== {model['label']} :: {prompt_variant} ===", flush=True)
    started = time.perf_counter()
    with MlxServer(model, args.port):
        results = []
        for idx, sample in enumerate(samples, 1):
            sample_id = str(sample.get("id") or sample.get("fixture_id") or sample.get("chunk_id") or "")
            result = run_sample(
                args,
                model,
                sample,
                prompt_variant=prompt_variant,
                gold_entry=args.gold_entries.get(sample_id) if getattr(args, "gold_entries", None) else None,
            )
            results.append(result)
            print(
                f"{idx:02d}/{len(samples)} {result['id']} "
                f"E/R={result['accepted']['entities']}/{result['accepted']['relations']} "
                f"opts={result['relation_option_count']} "
                f"lat={result['latency_s']:.2f}s "
                f"errs={len(result['errors'])}",
                flush=True,
            )
    wall_s = time.perf_counter() - started
    summary = summarize_model(model, results, wall_s, prompt_variant=prompt_variant)
    if getattr(args, "gold_entries", None):
        summary["gold_score"] = score_results_against_gold(results, args.gold_entries)
    return {"summary": summary, "results": results}


def print_table(report: dict[str, Any]) -> None:
    print("\nMODEL SUMMARY")
    print(
        "| model | chunks/hr | tok/s | schema | accepted E/R | gold E P/R/F1 | gold R P/R/F1 | graph F1 | bad lines | trunc | full eligible |"
    )
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for key, payload in report["models"].items():
        s = payload["summary"]
        bad = int(s["entity_invalid_lines"]) + int(s["relation_invalid_lines"])
        gold = s.get("gold_score") or {}
        e_gold = (
            f"{gold.get('entity_precision', 0)*100:.0f}/"
            f"{gold.get('entity_recall', 0)*100:.0f}/"
            f"{gold.get('entity_f1', 0)*100:.0f}"
            if gold
            else "-"
        )
        r_gold = (
            f"{gold.get('relation_precision', 0)*100:.0f}/"
            f"{gold.get('relation_recall', 0)*100:.0f}/"
            f"{gold.get('relation_f1', 0)*100:.0f}"
            if gold
            else "-"
        )
        graph_f1 = f"{gold.get('graph_f1', 0)*100:.0f}" if gold else "-"
        print(
            f"| {key} | {s['chunks_per_hour_wall']:.0f} | "
            f"{(s['completion_tok_s_median'] or 0):.1f} | "
            f"{s['schema_pass']}/{s['samples']} | "
            f"{s['accepted_entities']}/{s['accepted_relations']} | "
            f"{e_gold} | {r_gold} | {graph_f1} | "
            f"{bad} | {s['truncation_count']} | {s['eligible_full_local_ghost_b']} |"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES)
    parser.add_argument("--meta", type=Path, default=DEFAULT_META)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--gold", type=Path, default=None)
    parser.add_argument("--candidate-mode", choices=["production", "oracle"], default="production")
    parser.add_argument("--models", default="qwen3_17b,qwen3_4b_2507,lfm25_12b")
    parser.add_argument("--prompt-variants", default="balanced")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--max-entity-candidates", type=int, default=36)
    parser.add_argument("--max-evidence-candidates", type=int, default=14)
    parser.add_argument("--max-relation-options", type=int, default=28)
    parser.add_argument("--entity-max-tokens", type=int, default=220)
    parser.add_argument("--relation-max-tokens", type=int, default=160)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.samples.exists():
        raise FileNotFoundError(args.samples)
    model_keys = [item.strip() for item in args.models.split(",") if item.strip()]
    prompt_variants = [item.strip() for item in args.prompt_variants.split(",") if item.strip()]
    args.gold_entries = load_gold(args.gold)
    unknown = [key for key in model_keys if key not in MODEL_REGISTRY]
    if unknown:
        raise SystemExit(f"unknown model keys: {unknown}; choose from {sorted(MODEL_REGISTRY)}")
    unknown_prompts = [key for key in prompt_variants if key not in PROMPT_VARIANTS]
    if unknown_prompts:
        raise SystemExit(f"unknown prompt variants: {unknown_prompts}; choose from {sorted(PROMPT_VARIANTS)}")
    samples = load_samples(args.samples, args.limit)
    report = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "schema": "services.ghost_b_schemas.ExtractionResponse",
        "facts_enabled": False,
        "candidate_mode": args.candidate_mode,
        "gold_path": str(args.gold) if args.gold else None,
        "gold_entity_scoring": "curated entities plus all relation endpoints",
        "samples_path": str(args.samples),
        "sample_count": len(samples),
        "prompt_variants": prompt_variants,
        "models": {},
    }
    for key in model_keys:
        for prompt_variant in prompt_variants:
            report_key = f"{key}::{prompt_variant}"
            report["models"][report_key] = run_model_variant(args, key, prompt_variant, samples)
            args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print_table(report)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
