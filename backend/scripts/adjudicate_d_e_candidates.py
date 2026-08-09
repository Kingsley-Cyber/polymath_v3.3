#!/usr/bin/env python3
"""Adjudicate every production-eligible candidate introduced by D and E.

Runs the frame extractor + credit patterns on a diverse adjudication corpus
under three profiles:
  C = baseline (no credit patterns, no p2_verb_prep)
  D = credit patterns enabled, p2_verb_prep disabled
  E = credit patterns enabled, p2_verb_prep enabled (production)

Reports:
  D−C delta: candidates introduced by credit patterns
  E−D delta: candidates introduced by verb-preposition feature

Each candidate is classified as:
  CORRECT_CANONICAL    — correct pair + correct canonical predicate
  CORRECT_SURFACE      — correct pair + surface relation, wrong canonical mapping
  WRONG_PAIR           — predicate fires but on wrong entity pair
  WRONG_DIRECTION      — correct pair but subject/object swapped
  NOT_ASSERTED         — text implies relation but system didn't fire
  SUPPRESSED_REVIEWED  — system correctly suppressed/reviewed a risky candidate

Usage:
  .venv-relex/bin/python backend/scripts/adjudicate_d_e_candidates.py
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.extraction.dep_path_extractor import EntitySpan
from services.extraction.frame_extractor import FrameExtractor
from services.extraction.credit_patterns import extract_credit_patterns


# ---------------------------------------------------------------------------
# Adjudication corpus: diverse naturalistic text covering D and E patterns
# ---------------------------------------------------------------------------
# Each entry: (text, entities, expected_verdict)
# expected_verdict is the human-adjudicated ground truth for the INTRODUCED
# candidate. "none" means no candidate should be introduced.

ADJUDICATION_CORPUS: list[dict] = [
    # === D: Credit-pattern candidates ===
    # Positive: metadata lines
    {
        "id": "adj_d_001",
        "feature": "D",
        "text": "A CHRISTMAS STORY\n\n(screenplay by Jean Shepherd & Leigh Brown & Bob Clark, 1983)",
        "entities": [
            {"surface": "A CHRISTMAS STORY", "start": 0, "end": 17, "type": "Concept"},
            {"surface": "Jean Shepherd", "start": 35, "end": 48, "type": "Person"},
            {"surface": "Leigh Brown", "start": 51, "end": 62, "type": "Person"},
            {"surface": "Bob Clark", "start": 65, "end": 74, "type": "Person"},
        ],
        "verdict": "CORRECT_CANONICAL",
        "expected_predicate": "created_by",
    },
    {
        "id": "adj_d_002",
        "feature": "D",
        "text": "THE REPORT\n\nWritten by Alice Brown and Robert Green",
        "entities": [
            {"surface": "THE REPORT", "start": 0, "end": 10, "type": "Document"},
            {"surface": "Alice Brown", "start": 23, "end": 34, "type": "Person"},
            {"surface": "Robert Green", "start": 39, "end": 51, "type": "Person"},
        ],
        "verdict": "CORRECT_CANONICAL",
        "expected_predicate": "created_by",
    },
    {
        "id": "adj_d_003",
        "feature": "D",
        "text": "STAR WARS\n\nDirected by George Lucas",
        "entities": [
            {"surface": "STAR WARS", "start": 0, "end": 9, "type": "Concept"},
            {"surface": "George Lucas", "start": 23, "end": 35, "type": "Person"},
        ],
        "verdict": "CORRECT_CANONICAL",
        "expected_predicate": "created_by",
    },
    {
        "id": "adj_d_004",
        "feature": "D",
        "text": "THE ALGORITHM\n\nStory by Alan Turing",
        "entities": [
            {"surface": "THE ALGORITHM", "start": 0, "end": 13, "type": "Concept"},
            {"surface": "Alan Turing", "start": 25, "end": 36, "type": "Person"},
        ],
        "verdict": "CORRECT_CANONICAL",
        "expected_predicate": "created_by",
    },
    # Negative: prose contexts (should NOT introduce candidates)
    {
        "id": "adj_d_005",
        "feature": "D",
        "text": "FILM A and FILM B were both discussed.\n\nThe screenplay by Jane Smith was praised.",
        "entities": [
            {"surface": "FILM A", "start": 0, "end": 6, "type": "Concept"},
            {"surface": "FILM B", "start": 11, "end": 17, "type": "Concept"},
            {"surface": "Jane Smith", "start": 59, "end": 69, "type": "Person"},
        ],
        "verdict": "SUPPRESSED_REVIEWED",
        "expected_predicate": None,
    },
    {
        "id": "adj_d_006",
        "feature": "D",
        "text": 'THE FILM\n\nThe film was inspired by a screenplay by Jean Shepherd.',
        "entities": [
            {"surface": "THE FILM", "start": 0, "end": 8, "type": "Concept"},
            {"surface": "Jean Shepherd", "start": 51, "end": 64, "type": "Person"},
        ],
        "verdict": "SUPPRESSED_REVIEWED",
        "expected_predicate": None,
    },
    {
        "id": "adj_d_007",
        "feature": "D",
        "text": 'THE BOOK\n\n"The screenplay by John Smith was terrible," said the reviewer.',
        "entities": [
            {"surface": "THE BOOK", "start": 0, "end": 8, "type": "Document"},
            {"surface": "John Smith", "start": 30, "end": 40, "type": "Person"},
        ],
        "verdict": "SUPPRESSED_REVIEWED",
        "expected_predicate": None,
    },
    {
        "id": "adj_d_008",
        "feature": "D",
        "text": "THE DOCUMENT\n\nThis report was not written by any single person.",
        "entities": [
            {"surface": "THE DOCUMENT", "start": 0, "end": 12, "type": "Document"},
        ],
        "verdict": "SUPPRESSED_REVIEWED",
        "expected_predicate": None,
    },
    # === E: Verb-preposition candidates ===
    # works_for positives
    {
        "id": "adj_e_001",
        "feature": "E",
        "text": "Alice works for Microsoft.",
        "entities": [
            {"surface": "Alice", "start": 0, "end": 5, "type": "Person"},
            {"surface": "Microsoft", "start": 16, "end": 25, "type": "Organization"},
        ],
        "verdict": "CORRECT_CANONICAL",
        "expected_predicate": "works_for",
    },
    {
        "id": "adj_e_002",
        "feature": "E",
        "text": "Bob worked for the United Nations.",
        "entities": [
            {"surface": "Bob", "start": 0, "end": 3, "type": "Person"},
            {"surface": "United Nations", "start": 19, "end": 33, "type": "Organization"},
        ],
        "verdict": "CORRECT_CANONICAL",
        "expected_predicate": "works_for",
    },
    {
        "id": "adj_e_003",
        "feature": "E",
        "text": "Carol works for NASA.",
        "entities": [
            {"surface": "Carol", "start": 0, "end": 5, "type": "Person"},
            {"surface": "NASA", "start": 16, "end": 20, "type": "Organization"},
        ],
        "verdict": "CORRECT_CANONICAL",
        "expected_predicate": "works_for",
    },
    # works_for negatives
    {
        "id": "adj_e_004",
        "feature": "E",
        "text": "Alice works for a living.",
        "entities": [
            {"surface": "Alice", "start": 0, "end": 5, "type": "Person"},
            {"surface": "living", "start": 18, "end": 24, "type": "Concept"},
        ],
        "verdict": "SUPPRESSED_REVIEWED",
        "expected_predicate": None,
    },
    {
        "id": "adj_e_005",
        "feature": "E",
        "text": "The machine works for hours without stopping.",
        "entities": [
            {"surface": "machine", "start": 4, "end": 11, "type": "Software"},
            {"surface": "hours", "start": 22, "end": 27, "type": "TimeReference"},
        ],
        "verdict": "SUPPRESSED_REVIEWED",
        "expected_predicate": None,
    },
    # member_of positives
    {
        "id": "adj_e_006",
        "feature": "E",
        "text": "Alice is a member of the committee.",
        "entities": [
            {"surface": "Alice", "start": 0, "end": 5, "type": "Person"},
            {"surface": "committee", "start": 25, "end": 34, "type": "Organization"},
        ],
        "verdict": "CORRECT_CANONICAL",
        "expected_predicate": "member_of",
    },
    {
        "id": "adj_e_007",
        "feature": "E",
        "text": "David served in the Senate.",
        "entities": [
            {"surface": "David", "start": 0, "end": 5, "type": "Person"},
            {"surface": "Senate", "start": 20, "end": 26, "type": "Organization"},
        ],
        "verdict": "CORRECT_CANONICAL",
        "expected_predicate": "member_of",
    },
    # member_of negatives
    {
        "id": "adj_e_008",
        "feature": "E",
        "text": "Alice served in France during the war.",
        "entities": [
            {"surface": "Alice", "start": 0, "end": 5, "type": "Person"},
            {"surface": "France", "start": 16, "end": 22, "type": "Location"},
        ],
        "verdict": "SUPPRESSED_REVIEWED",
        "expected_predicate": None,
    },
    # located_in positives
    {
        "id": "adj_e_009",
        "feature": "E",
        "text": "The company is based in Texas.",
        "entities": [
            {"surface": "company", "start": 4, "end": 11, "type": "Organization"},
            {"surface": "Texas", "start": 24, "end": 29, "type": "Location"},
        ],
        "verdict": "CORRECT_CANONICAL",
        "expected_predicate": "located_in",
    },
    {
        "id": "adj_e_010",
        "feature": "E",
        "text": "The hotel is located in Quebec.",
        "entities": [
            {"surface": "hotel", "start": 4, "end": 9, "type": "Organization"},
            {"surface": "Quebec", "start": 24, "end": 30, "type": "Location"},
        ],
        "verdict": "CORRECT_CANONICAL",
        "expected_predicate": "located_in",
    },
    # located_in negatives
    {
        "id": "adj_e_011",
        "feature": "E",
        "text": "Alice works in Texas.",
        "entities": [
            {"surface": "Alice", "start": 0, "end": 5, "type": "Person"},
            {"surface": "Texas", "start": 15, "end": 20, "type": "Location"},
        ],
        "verdict": "SUPPRESSED_REVIEWED",
        "expected_predicate": None,
    },
    {
        "id": "adj_e_012",
        "feature": "E",
        "text": "The company operates in the market.",
        "entities": [
            {"surface": "company", "start": 4, "end": 11, "type": "Organization"},
            {"surface": "market", "start": 28, "end": 34, "type": "Concept"},
        ],
        "verdict": "SUPPRESSED_REVIEWED",
        "expected_predicate": None,
    },
    # part_of positives
    {
        "id": "adj_e_013",
        "feature": "E",
        "text": "The imprint is part of Penguin Random House.",
        "entities": [
            {"surface": "imprint", "start": 4, "end": 11, "type": "Organization"},
            {"surface": "Penguin Random House", "start": 23, "end": 43, "type": "Organization"},
        ],
        "verdict": "CORRECT_CANONICAL",
        "expected_predicate": "part_of",
    },
    {
        "id": "adj_e_014",
        "feature": "E",
        "text": "The division is part of General Motors.",
        "entities": [
            {"surface": "division", "start": 4, "end": 12, "type": "Organization"},
            {"surface": "General Motors", "start": 24, "end": 38, "type": "Organization"},
        ],
        "verdict": "CORRECT_CANONICAL",
        "expected_predicate": "part_of",
    },
    # part_of negatives
    {
        "id": "adj_e_015",
        "feature": "E",
        "text": "The author took part in the event.",
        "entities": [
            {"surface": "author", "start": 4, "end": 10, "type": "Person"},
            {"surface": "event", "start": 28, "end": 33, "type": "Event"},
        ],
        "verdict": "SUPPRESSED_REVIEWED",
        "expected_predicate": None,
    },
    {
        "id": "adj_e_016",
        "feature": "E",
        "text": "The system plays a part in detection.",
        "entities": [
            {"surface": "system", "start": 4, "end": 10, "type": "Software"},
            {"surface": "detection", "start": 27, "end": 36, "type": "Concept"},
        ],
        "verdict": "SUPPRESSED_REVIEWED",
        "expected_predicate": None,
    },
    # === Adversarial: naturalistic prose that might trigger false positives ===
    {
        "id": "adj_adv_001",
        "feature": "E",
        "text": "The framework works for most use cases but fails under heavy load.",
        "entities": [
            {"surface": "framework", "start": 4, "end": 13, "type": "Software"},
            {"surface": "use cases", "start": 29, "end": 38, "type": "Concept"},
        ],
        "verdict": "SUPPRESSED_REVIEWED",
        "expected_predicate": None,
    },
    {
        "id": "adj_adv_002",
        "feature": "E",
        "text": "This approach is part of a broader research program.",
        "entities": [
            {"surface": "approach", "start": 5, "end": 13, "type": "Method"},
            {"surface": "research program", "start": 35, "end": 51, "type": "Concept"},
        ],
        "verdict": "CORRECT_CANONICAL",
        "expected_predicate": "part_of",
    },
    {
        "id": "adj_adv_003",
        "feature": "E",
        "text": "The professor served in the department for twenty years.",
        "entities": [
            {"surface": "professor", "start": 4, "end": 13, "type": "Person"},
            {"surface": "department", "start": 28, "end": 38, "type": "Organization"},
        ],
        "verdict": "CORRECT_CANONICAL",
        "expected_predicate": "member_of",
    },
    {
        "id": "adj_adv_004",
        "feature": "E",
        "text": "The laboratory is based in Geneva.",
        "entities": [
            {"surface": "laboratory", "start": 4, "end": 14, "type": "Organization"},
            {"surface": "Geneva", "start": 27, "end": 33, "type": "Location"},
        ],
        "verdict": "CORRECT_CANONICAL",
        "expected_predicate": "located_in",
    },
    {
        "id": "adj_adv_005",
        "feature": "E",
        "text": "The theory is based in flawed assumptions.",
        "entities": [
            {"surface": "theory", "start": 4, "end": 10, "type": "Concept"},
            {"surface": "assumptions", "start": 30, "end": 41, "type": "Concept"},
        ],
        "verdict": "SUPPRESSED_REVIEWED",
        "expected_predicate": None,
    },
    {
        "id": "adj_adv_006",
        "feature": "D",
        "text": "INCEPTION\n\nDirected by Christopher Nolan\nProduced by Emma Thomas",
        "entities": [
            {"surface": "INCEPTION", "start": 0, "end": 9, "type": "Concept"},
            {"surface": "Christopher Nolan", "start": 23, "end": 40, "type": "Person"},
            {"surface": "Emma Thomas", "start": 53, "end": 64, "type": "Person"},
        ],
        "verdict": "CORRECT_CANONICAL",
        "expected_predicate": "created_by",
    },
    {
        "id": "adj_adv_007",
        "feature": "E",
        "text": "She served in the army before joining the company.",
        "entities": [
            {"surface": "army", "start": 18, "end": 22, "type": "Organization"},
            {"surface": "company", "start": 44, "end": 51, "type": "Organization"},
        ],
        "verdict": "SUPPRESSED_REVIEWED",
        "expected_predicate": None,
        "note": "Pronoun subject suppressed",
    },
    {
        "id": "adj_adv_008",
        "feature": "E",
        "text": "The startup is based in San Francisco.",
        "entities": [
            {"surface": "startup", "start": 4, "end": 11, "type": "Organization"},
            {"surface": "San Francisco", "start": 24, "end": 37, "type": "Location"},
        ],
        "verdict": "CORRECT_CANONICAL",
        "expected_predicate": "located_in",
    },
]


# ---------------------------------------------------------------------------
# Extraction runner
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    """One extracted relation candidate."""
    source_id: str
    subject: str
    predicate: str | None
    object: str
    source_family: str
    surface_predicate: str = ""
    confidence: float = 1.0


def run_frame_extraction(
    text: str,
    entities: list[dict],
    *,
    disabled_feature_groups: frozenset[str] = frozenset(),
) -> list[Candidate]:
    """Run the frame extractor and return all candidates."""
    ext = FrameExtractor()
    spans = [
        EntitySpan(
            surface=e["surface"],
            start_char=e["start"],
            end_char=e["end"],
            entity_type=e["type"],
        )
        for e in entities
    ]
    triples = ext.extract(
        text, spans, chunk_id="adjudication",
        disabled_feature_groups=disabled_feature_groups,
    )
    return [
        Candidate(
            source_id="frame_extractor",
            subject=t.subject_surface,
            predicate=t.predicate,
            object=t.object_surface,
            source_family="DEPENDENCY_FRAME",
            confidence=t.confidence,
        )
        for t in triples
    ]


def run_credit_extraction(
    text: str,
    entities: list[dict],
) -> list[Candidate]:
    """Run credit patterns and return all candidates."""
    # credit_patterns expects keys: start, end, text, type
    credit_entities = [
        {"start": e["start"], "end": e["end"], "text": e["surface"], "type": e["type"]}
        for e in entities
    ]
    records = extract_credit_patterns(text, credit_entities, "adjudication")
    return [
        Candidate(
            source_id="credit_patterns",
            subject=r["subject_text"],
            predicate=r["canonical_predicate"],
            object=r["object_text"],
            source_family="CREDIT_PATTERN",
            surface_predicate=r["surface_predicate"],
            confidence=r["confidence"],
        )
        for r in records
    ]


def run_profile_c(text: str, entities: list[dict]) -> list[Candidate]:
    """Profile C: baseline (no credit patterns, p2_verb_prep disabled)."""
    return run_frame_extraction(
        text, entities,
        disabled_feature_groups=frozenset({"p2_verb_prep"}),
    )


def run_profile_d(text: str, entities: list[dict]) -> list[Candidate]:
    """Profile D: credit patterns enabled, p2_verb_prep disabled."""
    frames = run_frame_extraction(
        text, entities,
        disabled_feature_groups=frozenset({"p2_verb_prep"}),
    )
    credits = run_credit_extraction(text, entities)
    return frames + credits


def run_profile_e(text: str, entities: list[dict]) -> list[Candidate]:
    """Profile E: production (credit patterns + p2_verb_prep enabled)."""
    frames = run_frame_extraction(text, entities)
    credits = run_credit_extraction(text, entities)
    return frames + credits


def candidate_key(c: Candidate) -> tuple:
    """Dedup key for a candidate."""
    return (c.subject, c.predicate, c.object)


def compute_delta(
    baseline: list[Candidate],
    treatment: list[Candidate],
) -> list[Candidate]:
    """Candidates in treatment but not in baseline."""
    baseline_keys = {candidate_key(c) for c in baseline}
    return [c for c in treatment if candidate_key(c) not in baseline_keys]


# ---------------------------------------------------------------------------
# Main adjudication
# ---------------------------------------------------------------------------

def main():
    print("=" * 72)
    print("ADJUDICATION: Production candidates introduced by D and E")
    print("=" * 72)
    print()

    # Counters
    d_introduced = 0
    e_introduced = 0
    # Candidate-level counts (individual relation instances)
    total_introduced_candidates = 0
    correct_introduced_candidates = 0
    # Fixture-level counts (sentences)
    positive_fixtures = 0
    positive_fixtures_recovered = 0
    negative_fixtures = 0
    negative_fixtures_contained = 0
    mismatches: list[dict] = []
    latency_total_ms = 0.0
    sentence_count = 0

    for entry in ADJUDICATION_CORPUS:
        eid = entry["id"]
        text = entry["text"]
        entities = entry["entities"]
        feature = entry["feature"]
        expected_verdict = entry["verdict"]
        expected_predicate = entry.get("expected_predicate")

        # Run all three profiles
        t0 = time.perf_counter()
        c_candidates = run_profile_c(text, entities)
        d_candidates = run_profile_d(text, entities)
        e_candidates = run_profile_e(text, entities)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        latency_total_ms += elapsed_ms
        sentence_count += 1

        # Compute deltas
        dc_delta = compute_delta(c_candidates, d_candidates)
        ed_delta = compute_delta(d_candidates, e_candidates)

        # Determine which delta this entry tests
        if feature == "D":
            introduced = dc_delta
            d_introduced += len(dc_delta)
        else:
            introduced = ed_delta
            e_introduced += len(ed_delta)

        # Classify
        if expected_verdict == "SUPPRESSED_REVIEWED":
            # No candidate should be introduced
            negative_fixtures += 1
            if len(introduced) == 0:
                negative_fixtures_contained += 1
                status = "PASS"
            else:
                # False positive!
                status = "FAIL"
                mismatches.append({
                    "id": eid,
                    "expected": "no candidate",
                    "got": [(c.subject, c.predicate, c.object) for c in introduced],
                })
        elif expected_verdict == "CORRECT_CANONICAL":
            # Should have at least one candidate with correct predicate
            positive_fixtures += 1
            total_introduced_candidates += len(introduced)
            matching = [
                c for c in introduced
                if c.predicate == expected_predicate
            ]
            correct_introduced_candidates += len(matching)
            if matching:
                positive_fixtures_recovered += 1
                status = "PASS"
            elif introduced:
                # Candidate introduced but wrong predicate
                status = "WARN"
                mismatches.append({
                    "id": eid,
                    "expected": expected_predicate,
                    "got": [(c.subject, c.predicate, c.object) for c in introduced],
                })
            else:
                # Expected candidate not introduced
                status = "FAIL"
                mismatches.append({
                    "id": eid,
                    "expected": expected_predicate,
                    "got": "no candidate introduced",
                })
        else:
            status = "INFO"

        # Print per-entry detail
        intro_str = (
            [(c.subject, c.predicate, c.object) for c in introduced]
            if introduced else "none"
        )
        print(f"  [{status:4s}] {eid:12s} feature={feature} "
              f"introduced={intro_str}")

    # Summary — separate metrics with correct denominators
    print()
    print("-" * 72)
    print("ADJUDICATION SUMMARY")
    print("-" * 72)
    print(f"  Total adjudication sentences:    {sentence_count}")
    print(f"  D−C introduced candidates:       {d_introduced}")
    print(f"  E−D introduced candidates:       {e_introduced}")
    print(f"  Total introduced candidates:     {total_introduced_candidates}")
    print()

    # Candidate precision: correct introduced / all introduced
    candidate_precision = (
        correct_introduced_candidates / total_introduced_candidates
        if total_introduced_candidates else 0.0
    )
    # Positive fixture recall: fixtures with ≥1 correct candidate / all positive fixtures
    positive_fixture_recall = (
        positive_fixtures_recovered / positive_fixtures
        if positive_fixtures else 0.0
    )
    # Negative containment rate: negatives with 0 candidates / all negatives
    negative_containment_rate = (
        negative_fixtures_contained / negative_fixtures
        if negative_fixtures else 0.0
    )
    # Fixture decision accuracy: correctly handled / all fixtures
    correctly_handled = positive_fixtures_recovered + negative_fixtures_contained
    fixture_decision_accuracy = (
        correctly_handled / sentence_count
        if sentence_count else 0.0
    )

    print("  Candidate-level metrics:")
    print(f"    introduced_candidates:         {total_introduced_candidates}")
    print(f"    correct_introduced_candidates: {correct_introduced_candidates}")
    print(f"    candidate_precision:           {candidate_precision:.4f}")
    print()
    print("  Fixture-level metrics:")
    print(f"    positive_fixtures:             {positive_fixtures}")
    print(f"    positive_fixtures_recovered:   {positive_fixtures_recovered}")
    print(f"    positive_fixture_recall:       {positive_fixture_recall:.4f}")
    print()
    print(f"    negative_fixtures:             {negative_fixtures}")
    print(f"    negative_fixtures_contained:   {negative_fixtures_contained}")
    print(f"    negative_containment_rate:     {negative_containment_rate:.4f}")
    print()
    print(f"    fixture_decision_accuracy:     {fixture_decision_accuracy:.4f}")
    print(f"    mean_latency_per_sentence:     {latency_total_ms / sentence_count:.1f} ms")
    print()

    # JSON report
    report = {
        "introduced_candidates": total_introduced_candidates,
        "correct_introduced_candidates": correct_introduced_candidates,
        "candidate_precision": candidate_precision,
        "positive_fixtures": positive_fixtures,
        "positive_fixtures_recovered": positive_fixtures_recovered,
        "positive_fixture_recall": positive_fixture_recall,
        "negative_fixtures": negative_fixtures,
        "negative_fixtures_contained": negative_fixtures_contained,
        "negative_containment_rate": negative_containment_rate,
        "fixture_decision_accuracy": fixture_decision_accuracy,
        "known_coverage_gaps": [
            "produced_by not in credit markers (KNOWN_COVERAGE_GAP)",
        ],
    }
    print("  JSON report:")
    print(f"    {json.dumps(report, indent=2)}")
    print()

    if mismatches:
        print("  MISMATCHES:")
        for m in mismatches:
            print(f"    {m['id']}: expected={m['expected']}, got={m['got']}")
        print()

    # Exit code
    if mismatches:
        print("ADJUDICATION RESULT: FAIL (mismatches found)")
        return 1
    else:
        print("ADJUDICATION RESULT: PASS (all candidates correctly classified)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
