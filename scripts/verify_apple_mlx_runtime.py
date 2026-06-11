"""Smoke-test the host-native Apple MLX sidecars.

This script uses only the Python standard library so it can run both from the
repo checkout and from the uv venv created by install_apple_mlx_runtime.sh.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class Endpoint:
    name: str
    base_url: str


class CheckError(RuntimeError):
    pass


def _json_request(method: str, url: str, payload: dict | None = None, timeout: float = 10.0) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body else {}


def _get_json(url: str, timeout: float = 10.0) -> Any:
    return _json_request("GET", url, timeout=timeout)


def _post_json(url: str, payload: dict, timeout: float = 30.0) -> Any:
    return _json_request("POST", url, payload=payload, timeout=timeout)


def _wait_for(url: str, wait_seconds: int) -> None:
    deadline = time.monotonic() + wait_seconds
    last_error = ""
    while True:
        try:
            _get_json(url, timeout=5.0)
            return
        except Exception as exc:
            last_error = str(exc)
            if time.monotonic() >= deadline:
                raise CheckError(f"{url} did not become ready: {last_error}") from exc
            time.sleep(2)


def _check_embedder(endpoint: Endpoint, wait_seconds: int, expected_dim: int) -> None:
    _wait_for(f"{endpoint.base_url}/health", wait_seconds)
    info = _get_json(f"{endpoint.base_url}/info")
    if not info.get("ready", True):
        raise CheckError(f"{endpoint.name} info says not ready: {info}")
    if int(info.get("dimension") or 0) != expected_dim:
        raise CheckError(
            f"{endpoint.name} dimension mismatch: expected {expected_dim}, got {info.get('dimension')}"
        )

    response = _post_json(
        f"{endpoint.base_url}/embeddings",
        {"input": ["hello polymath"], "model": "embed"},
        timeout=60.0,
    )
    rows = response.get("data") or []
    if len(rows) != 1:
        raise CheckError(f"{endpoint.name} returned {len(rows)} embedding rows")
    vector = rows[0].get("embedding") or []
    if len(vector) != expected_dim:
        raise CheckError(
            f"{endpoint.name} embedding vector mismatch: expected {expected_dim}, got {len(vector)}"
        )
    print(f"[ OK ] embedder ready: model={info.get('model')} dim={len(vector)}")


def _check_reranker(endpoint: Endpoint, wait_seconds: int) -> None:
    _wait_for(f"{endpoint.base_url}/health", wait_seconds)
    info = _get_json(f"{endpoint.base_url}/info")
    if not info.get("ready", True):
        raise CheckError(f"{endpoint.name} info says not ready: {info}")
    if info.get("score_scale") != "cosine":
        raise CheckError(f"{endpoint.name} score_scale must be cosine: {info}")

    response = _post_json(
        f"{endpoint.base_url}/rerank",
        {
            "query": "object-oriented design pattern",
            "documents": [
                "The decorator pattern adds responsibilities to objects dynamically.",
                "Lemonade recipe: lemons, water, sugar, ice.",
                "Composite pattern lets clients treat individual objects and compositions uniformly.",
            ],
        },
        timeout=60.0,
    )
    scores = [float(score) for score in response.get("scores", [])]
    if len(scores) != 3:
        raise CheckError(f"{endpoint.name} returned {len(scores)} scores: {response}")
    if all(abs(score) < 1e-8 for score in scores):
        raise CheckError(f"{endpoint.name} returned all-zero scores; model is not deployed")
    if not (scores[0] > scores[1] and scores[2] > scores[1]):
        raise CheckError(f"{endpoint.name} failed relevance ordering check: scores={scores}")
    print(f"[ OK ] reranker ready: model={info.get('model')} scores={scores}")


def _check_docling(endpoint: Endpoint, wait_seconds: int) -> None:
    _wait_for(f"{endpoint.base_url}/health", wait_seconds)
    health = _get_json(f"{endpoint.base_url}/health")
    if health.get("status") != "ok":
        raise CheckError(f"{endpoint.name} health is not ok: {health}")
    print("[ OK ] docling ready")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify Apple MLX sidecars.")
    parser.add_argument("--embedder-url", default="http://localhost:8082")
    parser.add_argument("--reranker-url", default="http://localhost:8081")
    parser.add_argument("--docling-url", default="http://localhost:8500")
    parser.add_argument("--expected-dim", type=int, default=1024)
    parser.add_argument("--wait", type=int, default=1, help="Seconds to wait for each endpoint.")
    parser.add_argument("--skip-embedder", action="store_true", help="Do not check the embedder sidecar.")
    parser.add_argument("--skip-reranker", action="store_true", help="Do not check the reranker sidecar.")
    parser.add_argument("--skip-docling", action="store_true", help="Do not check the docling sidecar.")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        if args.skip_embedder:
            print("[SKIP] embedder disabled")
        else:
            _check_embedder(Endpoint("embedder", args.embedder_url.rstrip("/")), args.wait, args.expected_dim)

        if args.skip_reranker:
            print("[SKIP] reranker disabled")
        else:
            _check_reranker(Endpoint("reranker", args.reranker_url.rstrip("/")), args.wait)

        if args.skip_docling:
            print("[SKIP] docling disabled")
        else:
            _check_docling(Endpoint("docling", args.docling_url.rstrip("/")), args.wait)
    except (CheckError, urllib.error.URLError, TimeoutError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1

    print("[ OK ] Apple MLX runtime smoke passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
