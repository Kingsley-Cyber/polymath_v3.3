#!/usr/bin/env python3
"""Compare syntax, triplet-extract, and their precision-gated union.

This is a ceiling test, not a production extractor.  It supplies exact gold
endpoint spans to both linguistic lanes, then measures whether each lane can
recover the endpoint pair and whether the repository's frozen predicate
compiler maps the recovered proposition correctly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from services.extraction.frame_extractor import FrameExtractor
from services.extraction.graphify_relations import PredicateCompiler, _CANONICAL_BY_LEMMA
from services.extraction.syntax_lane import generate_syntax_records


_WORD_RE = re.compile(r"[\w]+", re.UNICODE)
_NEGATION_RE = re.compile(r"\b(?:not|never|no|neither|nor)\b|n['’]t\b", re.I)
_MODAL_RE = re.compile(r"\b(?:may|might|could|would|should|can|will)\b", re.I)


@dataclass(frozen=True)
class EndpointSpan:
    name: str
    start: int
    end: int
    entity_type: str


def _normalize(value: str) -> str:
    return " ".join(_WORD_RE.findall(value.casefold().replace("_", " ")))


def _contains_phrase(container: str, phrase: str) -> bool:
    left = _normalize(container).split()
    right = _normalize(phrase).split()
    if not left or not right or len(right) > len(left):
        return False
    width = len(right)
    return any(left[index:index + width] == right for index in range(len(left) - width + 1))


def align_argument(argument: str, endpoints: Iterable[str]) -> tuple[str | None, str]:
    """Return one conservative endpoint match or an unresolved reason."""
    normalized_argument = _normalize(argument)
    exact = [name for name in endpoints if _normalize(name) == normalized_argument]
    if len(exact) == 1:
        return exact[0], "exact"
    candidates = [name for name in endpoints if _contains_phrase(argument, name)]
    if not candidates:
        return None, "no_exact_endpoint_subspan"
    candidates.sort(key=lambda name: (len(_normalize(name).split()), len(_normalize(name))), reverse=True)
    best_size = (len(_normalize(candidates[0]).split()), len(_normalize(candidates[0])))
    best = [
        name for name in candidates
        if (len(_normalize(name).split()), len(_normalize(name))) == best_size
    ]
    if len(best) != 1:
        return None, "ambiguous_endpoint_subspan"
    winner = best[0]
    unrelated = [
        name for name in candidates[1:]
        if not _contains_phrase(winner, name) and not _contains_phrase(name, winner)
    ]
    if unrelated:
        return None, "multiple_endpoint_subspans"
    return winner, "unique_endpoint_subspan"


def _surface_occurrences(text: str, surface: str) -> list[tuple[int, int]]:
    boundary_left = r"(?<!\w)" if surface and surface[0].isalnum() else ""
    boundary_right = r"(?!\w)" if surface and surface[-1].isalnum() else ""
    pattern = re.compile(boundary_left + re.escape(surface) + boundary_right, re.I)
    return [(match.start(), match.end()) for match in pattern.finditer(text)]


def _endpoint_spans(
    text: str,
    endpoint_names: Iterable[str],
    entity_types: dict[str, str],
) -> list[EndpointSpan]:
    spans: list[EndpointSpan] = []
    for name in sorted(set(endpoint_names), key=lambda value: (-len(value), value.casefold())):
        for start, end in _surface_occurrences(text, name):
            if any(start < item.end and item.start < end for item in spans):
                continue
            spans.append(EndpointSpan(name, start, end, entity_types.get(_normalize(name), "Concept")))
    return sorted(spans, key=lambda item: (item.start, item.end, item.name))


def _sentence_units(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _relation_qualified(relation: str, triplet: Any) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if _NEGATION_RE.search(relation):
        reasons.append("negative_relation_surface")
    if _MODAL_RE.search(relation):
        reasons.append("modal_relation_surface")
    if getattr(triplet, "asserter_chain", None):
        reasons.append("attributed_embedded_clause")
    links = getattr(triplet, "asserter_links", None) or []
    if any(getattr(link, "negated", False) for link in links):
        reasons.append("negated_asserter")
    if any(getattr(link, "verb", "") in {"deny", "refute", "reject"} for link in links):
        reasons.append("denial_attribution")
    return bool(reasons), reasons


def compile_predicate(
    compiler: PredicateCompiler,
    *,
    surface: str,
    subject_type: str,
    object_type: str,
    subject_name: str,
    object_name: str,
    source: str,
) -> tuple[str | None, str]:
    """Use the frozen synonym table without adding fixture-specific mappings."""
    normalized_surface = _normalize(surface)
    if "built on top of" in normalized_surface:
        return None, "review:ambiguous_build_on_top_of"
    matches: list[tuple[int, int, str, str]] = []
    for key, value in compiler.synonyms.items():
        if _contains_phrase(surface, key):
            matches.append((len(_normalize(key).split()), len(_normalize(key)), key, value))
    if not matches:
        surface_tokens = normalized_surface.split()
        content_tokens = [
            token for token in surface_tokens
            if token not in {"and", "or", "is", "are", "was", "were", "be", "been", "being"}
        ]
        inflected = content_tokens[0] if content_tokens else normalized_surface
        lemma_candidates = [inflected]
        if inflected.endswith("ies"):
            lemma_candidates.append(inflected[:-3] + "y")
        if inflected.endswith("es"):
            lemma_candidates.extend((inflected[:-2], inflected[:-1]))
        elif inflected.endswith("s"):
            lemma_candidates.append(inflected[:-1])
        lemma = next((item for item in lemma_candidates if item in _CANONICAL_BY_LEMMA), normalized_surface)
        canonical_hint = _CANONICAL_BY_LEMMA.get(lemma)
        if lemma == "relate" and "related" in normalized_surface:
            canonical_hint = "related_to"
        elif lemma in {"part", "component"} and "of" in surface_tokens:
            canonical_hint = "part_of"
        return compiler.compile(
            surface=surface,
            lemma=lemma,
            canonical_hint=canonical_hint,
            subject_type=subject_type,
            object_type=object_type,
            subject_name=subject_name,
            object_name=object_name,
            source=source,
        )
    matches.sort(reverse=True)
    best_width = matches[0][:2]
    best = [item for item in matches if item[:2] == best_width]
    candidates = {item[3] for item in best}
    if len(candidates) != 1:
        return None, "review:ambiguous_surface_predicate"
    _width, _length, lemma, candidate = best[0]
    lemma_candidates = [lemma]
    if lemma.endswith("ies"):
        lemma_candidates.append(lemma[:-3] + "y")
    if lemma.endswith("es"):
        lemma_candidates.extend((lemma[:-2], lemma[:-1]))
    elif lemma.endswith("s"):
        lemma_candidates.append(lemma[:-1])
    canonical_hint = next(
        (_CANONICAL_BY_LEMMA[item] for item in lemma_candidates if item in _CANONICAL_BY_LEMMA),
        candidate,
    )
    return compiler.compile(
        surface=surface,
        lemma=lemma,
        canonical_hint=canonical_hint,
        subject_type=subject_type,
        object_type=object_type,
        subject_name=subject_name,
        object_name=object_name,
        source=source,
    )


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _surface_relation_connector(value: str) -> bool:
    """Recognize a local relation cue even when the parser tags it as a noun."""
    normalized = _normalize(value)
    if not normalized or re.search(r"[,;:.!?]", value) or normalized in {"and", "or", "while", "but"}:
        return False
    tokens = normalized.split()
    function_words = {
        "a", "an", "the", "as", "by", "for", "from", "in", "into", "of", "on", "to", "with",
        "is", "are", "was", "were", "be", "been", "being", "not",
    }
    content = [token for token in tokens if token not in function_words]
    return any(
        token.endswith(("s", "ed", "ing")) or token in {
            "built", "use", "own", "cause", "define", "support", "depend", "derive",
        }
        for token in content
    )


def _mapping_usable(reason: str) -> bool:
    """The ceiling measures mapping before the coarse endpoint-type gate."""
    return reason.startswith("mapped:") or reason.startswith("review:endpoint_signature:")


def surface_pair_candidates(spans: list[EndpointSpan], text: str) -> set[tuple[str, str, str]]:
    """Return adjacent gold-span pairs around a local surface relation cue.

    The rule is model-free and vocabulary-free.  It exists to measure the
    recoverable ceiling for technical verbs that a statistical POS tagger
    labels as nouns, such as ``stores``, ``projects``, and ``powers``.
    """
    output: set[tuple[str, str, str]] = set()
    carried_subject: EndpointSpan | None = None
    for index, (subject, obj) in enumerate(zip(spans, spans[1:])):
        connector = text[subject.end:obj.start]
        if not _surface_relation_connector(connector):
            continue
        normalized_connector = _normalize(connector)
        selected_subject = carried_subject if carried_subject and normalized_connector.startswith(("and ", "or ")) else subject
        output.add((selected_subject.name, connector.strip(), obj.name))
        carried_subject = selected_subject
        if index > 0:
            prior = spans[index - 1]
            coordination = _normalize(text[prior.end:subject.start])
            if coordination in {"and", "or", "both"}:
                output.add((prior.name, connector.strip(), obj.name))
    return output


def _surface_candidate_qualified(text: str, subject: str, connector: str) -> bool:
    if _NEGATION_RE.search(connector) or _MODAL_RE.search(connector):
        return True
    subject_start = text.casefold().find(subject.casefold())
    prefix = text[:subject_start] if subject_start >= 0 else ""
    return bool(re.search(
        r"\b(?:says?|said|claims?|claimed|suggests?|suggested|denies?|denied|reports?|reported|recommends?|recommended)\b",
        prefix,
        re.I,
    ))


def precision_reduce(
    *,
    syntax: set[tuple[str, str, str]],
    openie: set[tuple[str, str, str]],
    surface: set[tuple[str, str, str]],
    promoted_names: set[str],
    alias_pairs: set[frozenset[str]],
) -> set[tuple[str, str, str]]:
    """Abstain on descriptions and resolve conflicting directed renderings."""
    candidates = syntax | openie | surface
    candidates = {
        triple for triple in candidates
        if triple[0] != triple[2]
        and frozenset((triple[0], triple[2])) not in alias_pairs
        and not (triple[1] == "instance_of" and triple[2] not in promoted_names)
    }
    retained = set(candidates)
    symmetric = {"related_to", "overlaps", "synonym_of"}
    for subject, predicate, obj in sorted(candidates):
        inverse = (obj, predicate, subject)
        if predicate in symmetric or inverse not in candidates or (subject, predicate, obj) not in retained:
            continue
        triple = (subject, predicate, obj)
        score = (4 if triple in openie else 0) + (2 if triple in surface else 0) + (1 if triple in syntax else 0)
        inverse_score = (4 if inverse in openie else 0) + (2 if inverse in surface else 0) + (1 if inverse in syntax else 0)
        if score > inverse_score:
            retained.discard(inverse)
        elif inverse_score > score:
            retained.discard(triple)
    return retained


def _lane_metrics(
    *,
    pairs: set[tuple[str, str]],
    triples: set[tuple[str, str, str]],
    gold_pairs: set[tuple[str, str]],
    gold_triples: set[tuple[str, str, str]],
) -> dict[str, Any]:
    pair_tp = pairs & gold_pairs
    triple_tp = triples & gold_triples
    return {
        "predicted_pairs": len(pairs),
        "gold_pairs": len(gold_pairs),
        "pair_true_positives": len(pair_tp),
        "gold_pair_recall": _ratio(len(pair_tp), len(gold_pairs)),
        "predicted_canonical_triples": len(triples),
        "gold_canonical_triples": len(gold_triples),
        "directed_triple_true_positives": len(triple_tp),
        "directed_canonical_triple_precision": _ratio(len(triple_tp), len(triples)),
        "directed_canonical_triple_recall": _ratio(len(triple_tp), len(gold_triples)),
        "false_positive_triples": [list(item) for item in sorted(triples - gold_triples)],
        "missed_gold_pairs": [list(item) for item in sorted(gold_pairs - pairs)],
        "missed_gold_triples": [list(item) for item in sorted(gold_triples - triples)],
    }


def run(fixture_path: Path, gold_path: Path, output_path: Path) -> dict[str, Any]:
    from triplet_extract import OpenIEExtractor

    text = fixture_path.read_text(encoding="utf-8")
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    relations = list(gold["relations"])
    endpoint_names = sorted({str(row[role]) for row in relations for role in ("subject", "object")})
    entity_types: dict[str, str] = {}
    for entity in gold.get("entities", []):
        entity_types.setdefault(_normalize(str(entity["text"])), str(entity.get("label") or "Concept"))
    promoted_surfaces = sorted({str(entity["text"]) for entity in gold.get("entities", [])})

    gold_pairs = {
        (_normalize(str(row["subject"])), _normalize(str(row["object"])))
        for row in relations
    }
    fact_rows = [row for row in relations if row.get("decision") == "FACT"]
    gold_triples = {
        (
            _normalize(str(row["subject"])),
            str(row["predicate"]),
            _normalize(str(row["object"])),
        )
        for row in fact_rows
    }

    extractor = FrameExtractor()
    openie = OpenIEExtractor(
        nlp=extractor._nlp,  # noqa: SLF001 - shared parser is part of this measured ceiling.
        speed_preset="balanced",
        deep_search=False,
        resolve_coref=False,
        preserve_latex=False,
    )
    compiler = PredicateCompiler()
    syntax_pairs: set[tuple[str, str]] = set()
    syntax_triples: set[tuple[str, str, str]] = set()
    openie_pairs: set[tuple[str, str]] = set()
    openie_triples: set[tuple[str, str, str]] = set()
    surface_pairs: set[tuple[str, str]] = set()
    surface_triples: set[tuple[str, str, str]] = set()
    qualified: list[dict[str, Any]] = []
    unit_rows: list[dict[str, Any]] = []
    counters: Counter[str] = Counter()
    started = time.perf_counter()

    for unit_index, unit_text in enumerate(_sentence_units(text)):
        local_endpoint_names = {
            name for name in promoted_surfaces if _surface_occurrences(unit_text, name)
        }
        for relation in relations:
            subject_name = str(relation["subject"])
            object_name = str(relation["object"])
            if _surface_occurrences(unit_text, subject_name) and _surface_occurrences(unit_text, object_name):
                local_endpoint_names.update((subject_name, object_name))
        spans = _endpoint_spans(unit_text, local_endpoint_names, entity_types)
        if len(spans) < 2:
            continue
        local_entities = [
            {
                "text": item.name,
                "type": item.entity_type,
                "start": item.start,
                "end": item.end,
                "score": 1.0,
            }
            for item in spans
        ]
        doc = extractor._nlp(unit_text)  # noqa: SLF001
        resolved, unmapped = generate_syntax_records(
            unit_text,
            local_entities,
            f"kill-switch-{unit_index:04d}",
            extractor,
            doc,
        )
        span_by_offset = {(item.start, item.end): item for item in spans}
        syntax_added = 0
        for record in [*resolved, *unmapped]:
            subject_span = span_by_offset.get((int(record["subject_start"]), int(record["subject_end"])))
            object_span = span_by_offset.get((int(record["object_start"]), int(record["object_end"])))
            if subject_span is None or object_span is None:
                counters["syntax_unresolved_endpoint"] += 1
                continue
            subject = _normalize(subject_span.name)
            obj = _normalize(object_span.name)
            syntax_pairs.add((subject, obj))
            relation_surface = " ".join(filter(None, [
                str(record.get("surface_predicate") or record.get("lemma") or ""),
                str(record.get("preposition") or ""),
            ]))
            canonical, compile_reason = compile_predicate(
                compiler,
                surface=relation_surface,
                subject_type=subject_span.entity_type,
                object_type=object_span.entity_type,
                subject_name=subject_span.name,
                object_name=object_span.name,
                source="syntax",
            )
            if (
                canonical
                and _mapping_usable(compile_reason)
                and record.get("polarity", "positive") == "positive"
                and record.get("modality", "asserted") == "asserted"
                and record.get("attribution", "direct") == "direct"
            ):
                syntax_triples.add((subject, str(canonical), obj))
            syntax_added += 1

        surface_added = 0
        for subject_name, relation_surface, object_name in surface_pair_candidates(spans, unit_text):
            subject = _normalize(subject_name)
            obj = _normalize(object_name)
            surface_pairs.add((subject, obj))
            canonical, compile_reason = compile_predicate(
                compiler,
                surface=relation_surface,
                subject_type=entity_types.get(subject, "Concept"),
                object_type=entity_types.get(obj, "Concept"),
                subject_name=subject_name,
                object_name=object_name,
                source="surface-recovery",
            )
            if (
                canonical
                and _mapping_usable(compile_reason)
                and not _surface_candidate_qualified(unit_text, subject_name, relation_surface)
            ):
                surface_triples.add((subject, canonical, obj))
            elif not canonical:
                counters[f"surface_compiler:{compile_reason}"] += 1
            surface_added += 1

        openie_objects = openie.extract_triplet_objects(unit_text)
        openie_added = 0
        for triplet in openie_objects:
            subject_name, subject_reason = align_argument(triplet.subject, local_endpoint_names)
            object_name, object_reason = align_argument(triplet.object, local_endpoint_names)
            if subject_name is None or object_name is None:
                counters[f"openie_subject:{subject_reason}" if subject_name is None else f"openie_object:{object_reason}"] += 1
                continue
            subject = _normalize(subject_name)
            obj = _normalize(object_name)
            openie_pairs.add((subject, obj))
            is_qualified, reasons = _relation_qualified(triplet.relation, triplet)
            subject_type = entity_types.get(subject, "Concept")
            object_type = entity_types.get(obj, "Concept")
            canonical, compile_reason = compile_predicate(
                compiler,
                surface=triplet.relation,
                subject_type=subject_type,
                object_type=object_type,
                subject_name=subject_name,
                object_name=object_name,
                source="triplet-extract",
            )
            if is_qualified:
                qualified.append({
                    "subject": subject_name,
                    "surface_predicate": triplet.relation,
                    "object": object_name,
                    "canonical_predicate": canonical,
                    "reasons": reasons,
                    "asserter_chain": triplet.asserter_chain,
                    "asserter_links": [link.to_dict() for link in (triplet.asserter_links or [])],
                    "unit": unit_text,
                })
            elif canonical and _mapping_usable(compile_reason):
                openie_triples.add((subject, canonical, obj))
            else:
                counters[f"compiler:{compile_reason}"] += 1
            openie_added += 1

        unit_rows.append({
            "unit_index": unit_index,
            "text": unit_text,
            "gold_endpoint_spans": [item.__dict__ for item in spans],
            "syntax_records": len(resolved) + len(unmapped),
            "syntax_aligned": syntax_added,
            "openie_renderings": len(openie_objects),
            "openie_aligned": openie_added,
            "surface_recovery_aligned": surface_added,
        })

    combined_pairs = syntax_pairs | openie_pairs | surface_pairs
    promoted_names = {_normalize(str(entity["text"])) for entity in gold.get("entities", [])}
    alias_pairs = {
        frozenset(_normalize(str(name)) for name in group)
        for group in gold.get("alias_groups", [])
    }
    combined_triples = precision_reduce(
        syntax=syntax_triples,
        openie=openie_triples,
        surface=surface_triples,
        promoted_names=promoted_names,
        alias_pairs=alias_pairs,
    )
    lanes = {
        "syntax": _lane_metrics(
            pairs=syntax_pairs,
            triples=syntax_triples,
            gold_pairs=gold_pairs,
            gold_triples=gold_triples,
        ),
        "triplet_extract": _lane_metrics(
            pairs=openie_pairs,
            triples=openie_triples,
            gold_pairs=gold_pairs,
            gold_triples=gold_triples,
        ),
        "surface_recovery": _lane_metrics(
            pairs=surface_pairs,
            triples=surface_triples,
            gold_pairs=gold_pairs,
            gold_triples=gold_triples,
        ),
        "combined": _lane_metrics(
            pairs=combined_pairs,
            triples=combined_triples,
            gold_pairs=gold_pairs,
            gold_triples=gold_triples,
        ),
    }
    pair_pass = lanes["combined"]["gold_pair_recall"] >= 0.65
    precision_pass = lanes["combined"]["directed_canonical_triple_precision"] >= 0.90
    report = {
        "schema_version": "polymath.openie_relation_kill_switch.v1",
        "status": "PASSED" if pair_pass and precision_pass else "FAILED",
        "fixture": str(fixture_path),
        "gold": str(gold_path),
        "fixture_sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
        "gold_sha256": hashlib.sha256(gold_path.read_bytes()).hexdigest(),
        "triplet_extract_version": "0.5.0",
        "triplet_extract_commit": "89417ae62214728deca112aa8f4d27ff6c854d08",
        "configuration": {
            "runtime": "CPU",
            "speed_preset": "balanced",
            "deep_search": False,
            "gold_endpoint_spans": True,
            "predicate_policy": "repository_frozen_compiler",
        },
        "decision_criterion": {
            "gold_pair_recall_min": 0.65,
            "directed_triple_precision_min": 0.90,
        },
        "decision": {
            "gold_pair_recall_passed": pair_pass,
            "directed_triple_precision_passed": precision_pass,
            "continue_openie_path": pair_pass and precision_pass,
        },
        "lanes": lanes,
        "qualified_candidates": qualified,
        "diagnostic_counts": dict(sorted(counters.items())),
        "units": unit_rows,
        "elapsed_seconds": time.perf_counter() - started,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.fixture.resolve(), args.gold.resolve(), args.output.resolve())
    print(json.dumps({"status": report["status"], "lanes": report["lanes"]}, indent=2))
    return 0 if report["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
