"""Probe reranker score behavior against positive, negative, and code cases.

Usage:
  python scripts/probe_reranker_calibration.py --url http://localhost:8081

The script accepts both supported sidecar response shapes:
  - {"results": [{"index": int, "score": float, ...}]}
  - {"scores": [float, ...]}
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class ProbeCase:
    name: str
    query: str
    documents: list[str]
    expected_top: int


CASES = [
    ProbeCase(
        name="plain_relevance",
        query="What is the way ahead for small language models on mobile RAG?",
        documents=[
            "On-device RAG for mobile apps combines a small language model with local retrieval over compact documents.",
            "Small is an adjective meaning little in size.",
            "Running shoes from On are designed for athletic performance.",
            "A PDF compression service can reduce file size online.",
        ],
        expected_top=0,
    ),
    ProbeCase(
        name="code_relevance",
        query="How does AbstractMapper map rows to domain objects?",
        documents=[
            "AbstractMapper loads database rows and maps them into domain objects through find and insert methods.",
            "A recipe for sourdough bread requires flour, water, and salt.",
            "TeamMapper extends AbstractMapper to hydrate Team entities from tabular data.",
        ],
        expected_top=0,
    ),
]


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _scores_from_response(data: dict[str, Any], count: int) -> list[float]:
    if isinstance(data.get("scores"), list):
        return [float(v) for v in data["scores"][:count]]
    if isinstance(data.get("results"), list):
        scores = [float("-inf")] * count
        for item in data["results"]:
            scores[int(item["index"])] = float(item["score"])
        return scores
    raise ValueError(f"Unsupported response shape: {data.keys()}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8081")
    args = parser.parse_args()

    failures = 0
    for case in CASES:
        data = _post_json(
            f"{args.url.rstrip('/')}/rerank",
            {"query": case.query, "documents": case.documents},
        )
        scores = _scores_from_response(data, len(case.documents))
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        print(f"\n[{case.name}] query={case.query!r}")
        for index in ranked:
            marker = " expected" if index == case.expected_top else ""
            print(f"  #{index} score={scores[index]:.4f}{marker} :: {case.documents[index][:92]}")
        if ranked[0] != case.expected_top:
            failures += 1
            print(f"  FAIL: expected document {case.expected_top} on top")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
