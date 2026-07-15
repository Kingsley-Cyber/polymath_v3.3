"""Deterministic GLiNER-to-LocalExtractionV1 mention selection.

This is the product form of the production-shaped C2 selection boundary.
Inference may propose spans, but Python owns controlled labels, exact offsets,
deduplication, overlap resolution, canonical names, and stable mention IDs.
"""

from __future__ import annotations

from collections import Counter
import re
import unicodedata
from typing import Any, Iterable

from models.hash_taxonomy import namespace_hash
from models.local_extraction import EntityMention


def normalize_mention_name(value: str) -> str:
    """Return the extraction contract's deterministic canonical label."""

    normalized = unicodedata.normalize("NFKC", str(value or "")).lower()
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())[:200]


def _overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def select_gliner_mentions(
    *,
    document_id: str,
    child_id: str,
    text: str,
    raw_entities: Iterable[dict[str, Any]],
    controlled_types: list[str],
) -> tuple[list[EntityMention], Counter[str]]:
    """Select one controlled, exact, non-overlapping entity per source span."""

    type_order = {label: index for index, label in enumerate(controlled_types)}
    by_span: dict[tuple[int, int], dict[str, Any]] = {}
    counts: Counter[str] = Counter()
    for row in raw_entities:
        counts["raw"] += 1
        try:
            start = int(row["start"])
            end = int(row["end"])
            surface = str(row["text"])
            label = str(row["label"])
            score = float(row["score"])
        except (KeyError, TypeError, ValueError):
            counts["malformed"] += 1
            continue
        if label not in type_order:
            counts["label_violations"] += 1
            continue
        if not 0.0 <= score <= 1.0:
            counts["score_violations"] += 1
            continue
        if start < 0 or end <= start or text[start:end] != surface:
            counts["offset_violations"] += 1
            continue
        candidate = {
            "start": start,
            "end": end,
            "text": surface,
            "label": label,
            "score": score,
        }
        previous = by_span.get((start, end))
        if previous is None or (
            -score,
            type_order[label],
        ) < (
            -float(previous["score"]),
            type_order[str(previous["label"])],
        ):
            if previous is not None:
                counts["same_span_dropped"] += 1
            by_span[(start, end)] = candidate
        else:
            counts["same_span_dropped"] += 1

    accepted: list[dict[str, Any]] = []
    for candidate in sorted(
        by_span.values(),
        key=lambda item: (
            -float(item["score"]),
            -(int(item["end"]) - int(item["start"])),
            int(item["start"]),
            int(item["end"]),
            type_order[str(item["label"])],
        ),
    ):
        coordinate = (int(candidate["start"]), int(candidate["end"]))
        if any(
            _overlap(
                coordinate,
                (int(existing["start"]), int(existing["end"])),
            )
            for existing in accepted
        ):
            counts["overlap_dropped"] += 1
            continue
        accepted.append(candidate)

    mentions = [
        EntityMention(
            mention_id="mention:"
            + namespace_hash(
                "logical-artifact",
                {
                    "kind": "local-extraction-entity-mention",
                    "document_id": document_id,
                    "child_id": child_id,
                    "start": item["start"],
                    "end": item["end"],
                    "entity_type": item["label"],
                    "surface": item["text"],
                },
            ).split(":", 1)[1],
            text=str(item["text"]),
            entity_type=str(item["label"]),
            start_char=int(item["start"]),
            end_char=int(item["end"]),
            canonical_label=normalize_mention_name(str(item["text"])),
            confidence=float(item["score"]),
        )
        for item in sorted(
            accepted,
            key=lambda item: (
                int(item["start"]),
                int(item["end"]),
                str(item["label"]),
            ),
        )
    ]
    counts["selected"] = len(mentions)
    return mentions, counts
