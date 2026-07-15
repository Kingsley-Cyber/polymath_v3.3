#!/usr/bin/env python3
"""Deterministically select the fresh-corpus 15-document E2E set."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any


DEFAULT_SOURCE = Path("/Users/king/Desktop/hermes agent/ECOMMERCE/pdf")
TOKEN_RE = re.compile(r"[a-z][a-z0-9]+")
STOPWORDS = {
    "and",
    "for",
    "from",
    "the",
    "with",
    "using",
    "volume",
    "edition",
    "approach",
    "study",
    "theory",
    "practice",
    "new",
    "book",
    "books",
    "libgen",
    "press",
    "wiley",
    "january",
    "journal",
    "report",
    "status",
    "pdf",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tokens(path: Path) -> list[str]:
    return [
        token
        for token in TOKEN_RE.findall(path.stem.casefold())
        if token not in STOPWORDS and not token.isdigit() and len(token) > 2
    ]


def tfidf_vectors(paths: list[Path]) -> tuple[list[dict[str, float]], Counter[str]]:
    rows = [Counter(tokens(path)) for path in paths]
    df: Counter[str] = Counter()
    for row in rows:
        df.update(row)
    vectors = []
    for row in rows:
        vector = {
            token: count * (math.log((1 + len(rows)) / (1 + df[token])) + 1.0)
            for token, count in row.items()
        }
        norm = math.sqrt(sum(value * value for value in vector.values())) or 1.0
        vectors.append({token: value / norm for token, value in vector.items()})
    return vectors, df


def similarity(left: dict[str, float], right: dict[str, float]) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(token, 0.0) for token, value in left.items())


def cluster(
    paths: list[Path], vectors: list[dict[str, float]], count: int
) -> list[int]:
    if len(paths) % count:
        raise ValueError("balanced topic bands require an even capacity")
    capacity = len(paths) // count
    medoids = [0]
    while len(medoids) < count:
        candidate = max(
            (index for index in range(len(paths)) if index not in medoids),
            key=lambda index: (
                min(
                    1.0 - similarity(vectors[index], vectors[item]) for item in medoids
                ),
                paths[index].name.casefold(),
            ),
        )
        medoids.append(candidate)
    for _ in range(20):
        assignments = [-1] * len(paths)
        remaining = [capacity] * count
        edges = sorted(
            (
                (
                    -similarity(vectors[index], vectors[medoids[cluster_index]]),
                    paths[index].name.casefold(),
                    cluster_index,
                    index,
                )
                for index in range(len(paths))
                for cluster_index in range(count)
            )
        )
        for _negative_similarity, _name, cluster_index, index in edges:
            if assignments[index] < 0 and remaining[cluster_index] > 0:
                assignments[index] = cluster_index
                remaining[cluster_index] -= 1
        if any(value < 0 for value in assignments) or any(remaining):
            raise RuntimeError("balanced topic assignment did not close")
        next_medoids = []
        for cluster_index in range(count):
            members = [
                index
                for index, assigned in enumerate(assignments)
                if assigned == cluster_index
            ]
            if not members:
                raise RuntimeError(
                    "deterministic topic clustering produced an empty band"
                )
            next_medoids.append(
                max(
                    members,
                    key=lambda candidate: (
                        sum(
                            similarity(vectors[candidate], vectors[other])
                            for other in members
                        ),
                        paths[candidate].name.casefold(),
                    ),
                )
            )
        if next_medoids == medoids:
            return assignments
        medoids = next_medoids
    raise RuntimeError("deterministic topic clustering did not converge")


def select(source: Path) -> dict[str, Any]:
    paths = sorted(
        (
            path
            for path in source.iterdir()
            if path.is_file()
            and path.suffix.casefold() == ".md"
            and not path.name.startswith("._")
        ),
        key=lambda path: path.name.casefold(),
    )
    if len(paths) != 75:
        raise RuntimeError(
            f"expected exactly 75 non-AppleDouble markdown files, found {len(paths)}"
        )
    vectors, _df = tfidf_vectors(paths)
    assignments = cluster(paths, vectors, count=5)
    by_cluster: dict[int, list[int]] = defaultdict(list)
    for index, assigned in enumerate(assignments):
        by_cluster[assigned].append(index)
    if any(len(by_cluster[index]) < 3 for index in range(5)):
        raise RuntimeError(
            "every deterministic topic band must contain at least three files"
        )

    selected = []
    topic_bands = []
    for cluster_index in range(5):
        members = by_cluster[cluster_index]
        term_scores: Counter[str] = Counter()
        for index in members:
            term_scores.update(vectors[index])
        top_terms = [
            term
            for term, _score in sorted(
                term_scores.items(), key=lambda item: (-item[1], item[0])
            )[:6]
        ]
        ordered = sorted(
            members,
            key=lambda index: (
                paths[index].stat().st_size,
                paths[index].name.casefold(),
            ),
        )
        positions = [0, (len(ordered) - 1) // 2, len(ordered) - 1]
        topic_bands.append(
            {
                "topic_band": cluster_index,
                "top_filename_terms": top_terms,
                "candidate_count": len(members),
            }
        )
        for size_band, position in zip(("small", "medium", "large"), positions):
            path = paths[ordered[position]]
            selected.append(
                {
                    "selection_rank": len(selected) + 1,
                    "topic_band": cluster_index,
                    "topic_terms": top_terms,
                    "size_band": size_band,
                    "filename": path.name,
                    "byte_size": path.stat().st_size,
                    "sha256": sha256(path),
                }
            )
    if len(selected) != 15 or len({row["filename"] for row in selected}) != 15:
        raise RuntimeError("selection must contain exactly 15 unique files")
    source_manifest = [
        {
            "filename": path.name,
            "byte_size": path.stat().st_size,
            "sha256": sha256(path),
        }
        for path in paths
    ]
    source_manifest_sha256 = hashlib.sha256(
        json.dumps(source_manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema_version": "polymath.runpod_e2e_15doc_selection.v1",
        "preregistered_at_utc": "2026-07-15T11:46:00Z",
        "source_root": str(source),
        "source_count": len(paths),
        "source_manifest_sha256": source_manifest_sha256,
        "selection_count": len(selected),
        "selection_method": {
            "topic_bands": "five deterministic balanced filename-TF-IDF k-medoids clusters of 15",
            "initial_medoids": "lexical first then farthest-first cosine distance",
            "cluster_refinement": "deterministic cosine medoid, maximum 20 iterations",
            "size_bands": "minimum, lower-median, maximum byte size within each topic band",
            "content_or_gold_read": False,
            "appledouble_policy": "exclude filenames beginning ._",
        },
        "topic_bands": topic_bands,
        "selected": selected,
        "run_constraints": {
            "new_corpus_only": True,
            "existing_corpus_writes": 0,
            "corpus_id_discovery": "real create API response",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = select(args.source)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "out": str(args.out),
                "source_count": report["source_count"],
                "selection_count": report["selection_count"],
                "source_manifest_sha256": report["source_manifest_sha256"],
                "selected_filenames": [row["filename"] for row in report["selected"]],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
