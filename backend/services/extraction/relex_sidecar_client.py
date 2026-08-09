"""Client for the host-MPS GLiNER-Relex sidecar (contract relex-infer-v1).

The sidecar owns model compatibility; this client owns nothing but
transport and the contract shape. Endpoint resolution:

    RELEX_SIDECAR_URL           explicit override
    in-container default        http://host.docker.internal:8737
    host default                http://127.0.0.1:8737

Stdlib-only (urllib) so it imports in every runtime, container or host.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Sequence

RELEX_CONTRACT = "relex-infer-v1"


def sidecar_url() -> str:
    # Runtime routing (Mongo control doc, TTL-cached) outranks the env
    # chain so an MCP agent can flip the engine without restarts. Any
    # routing failure falls back to env/defaults (the MPS host sidecar).
    try:
        from services.extraction.engine_routing import routed_sidecar_url

        routed = routed_sidecar_url()
        if routed:
            return routed
    except Exception:  # noqa: BLE001 — routing is strictly optional
        pass
    explicit = os.environ.get("RELEX_SIDECAR_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    if os.path.exists("/.dockerenv"):
        return "http://host.docker.internal:8737"
    return "http://127.0.0.1:8737"


@dataclass(frozen=True)
class RelexRelation:
    head_start: int
    head_end: int
    head_text: str
    tail_start: int
    tail_end: int
    tail_text: str
    label: str
    score: float


@dataclass(frozen=True)
class RelexResult:
    entities: tuple[dict[str, Any], ...]   # EntitySpan-shaped: start,end,text,label,score
    relations: tuple[RelexRelation, ...]


class RelexSidecarError(RuntimeError):
    pass


def sidecar_pool() -> tuple[str, ...]:
    """All identically-pinned replica URLs (2026-08-10 scale-out).

    Priority: routing doc ``sidecar_pool`` → env RELEX_SIDECAR_POOL
    (comma-separated) → the single sidecar_url(). Every replica serves the
    same sha-verified weights; the per-response release/contract handshake
    still verifies each call, so a mis-provisioned replica fails closed.
    """
    try:
        from services.extraction.engine_routing import routed_sidecar_pool

        pool = routed_sidecar_pool()
        if len(pool) > 1:
            return pool
    except Exception:  # noqa: BLE001 — routing is strictly optional
        pass
    raw = os.environ.get("RELEX_SIDECAR_POOL", "").strip()
    if raw:
        urls = tuple(u.strip().rstrip("/") for u in raw.split(",") if u.strip())
        if urls:
            return urls
    return (sidecar_url(),)


def _post(path: str, payload: dict, timeout: float, base_url: str | None = None) -> dict:
    base = (base_url or sidecar_url()).rstrip("/")
    request = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, OSError) as exc:
        raise RelexSidecarError(f"relex sidecar unreachable at {base}: {exc}") from exc


def health(timeout: float = 5.0) -> dict:
    try:
        with urllib.request.urlopen(sidecar_url() + "/health", timeout=timeout) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, OSError) as exc:
        raise RelexSidecarError(f"relex sidecar unreachable at {sidecar_url()}: {exc}") from exc


def _infer_timeout() -> float:
    """Per-call ceiling. The sidecar serves one MPS inference at a time, so a
    caller's wall time = its own batch + everything queued ahead of it; under
    concurrent extraction lanes the queue wait dominates (O5 soak finding).
    Bounded batches keep per-call work small — the timeout must tolerate the
    queue, and the failed_recoverable retry lane remains the backstop."""
    raw = os.environ.get("RELEX_INFER_TIMEOUT_SECONDS", "").strip()
    try:
        return max(60.0, float(raw)) if raw else 900.0
    except ValueError:
        return 900.0


def infer(
    texts: Sequence[str],
    *,
    entity_labels: Sequence[str] | None = None,
    relation_labels: Sequence[str] | None = None,
    entity_threshold: float | None = None,
    relation_threshold: float | None = None,
    timeout: float | None = None,
    base_url: str | None = None,
) -> list[RelexResult]:
    payload: dict[str, Any] = {"texts": list(texts)}
    if entity_labels is not None:
        payload["entity_labels"] = list(entity_labels)
    if relation_labels is not None:
        payload["relation_labels"] = list(relation_labels)
    if entity_threshold is not None:
        payload["entity_threshold"] = entity_threshold
    if relation_threshold is not None:
        payload["relation_threshold"] = relation_threshold
    body = _post(
        "/infer", payload, _infer_timeout() if timeout is None else timeout,
        base_url=base_url,
    )
    if body.get("contract") != RELEX_CONTRACT:
        raise RelexSidecarError(f"contract mismatch: {body.get('contract')!r}")
    expected_release = os.environ.get("RELEX_EXPECT_RELEASE", "").strip()
    if not expected_release:
        try:
            from services.extraction.engine_routing import routed_expected_release

            expected_release = routed_expected_release() or ""
        except Exception:  # noqa: BLE001
            expected_release = ""
    if expected_release and body.get("release") != expected_release:
        raise RelexSidecarError(
            f"release pin mismatch: sidecar={body.get('release')!r} "
            f"expected={expected_release!r} — refusing unpinned inference"
        )
    results = []
    for row in body["results"]:
        results.append(RelexResult(
            entities=tuple(row["entities"]),
            relations=tuple(
                RelexRelation(
                    head_start=r["head"]["start"], head_end=r["head"]["end"],
                    head_text=r["head"]["text"], tail_start=r["tail"]["start"],
                    tail_end=r["tail"]["end"], tail_text=r["tail"]["text"],
                    label=r["label"], score=r["score"],
                )
                for r in row["relations"]
            ),
        ))
    return results


def infer_sharded(
    texts: Sequence[str],
    *,
    batch_size: int,
    entity_labels: Sequence[str] | None = None,
    relation_labels: Sequence[str] | None = None,
    entity_threshold: float | None = None,
    relation_threshold: float | None = None,
) -> list[RelexResult]:
    """Fan window batches across the replica pool, order-preserving.

    The sidecar scores each window independently (transport batching is
    semantics-free), so sharding across identically-pinned replicas changes
    nothing about outputs — only wall clock. Concurrency equals the pool
    size (each replica is a serial engine; more in-flight calls per replica
    would just queue there). A failed shard gets ONE failover attempt on
    the next replica; a second failure raises, keeping the recoverable-lane
    retry semantics unchanged. With a single-URL pool this degrades to the
    exact serial behavior predict_joint always had.
    """
    text_list = list(texts)
    if not text_list:
        return []
    pool = sidecar_pool()
    shards = [
        (index, text_list[start:start + batch_size])
        for index, start in enumerate(range(0, len(text_list), batch_size))
    ]
    if len(pool) <= 1:
        results: list[RelexResult] = []
        for _idx, shard in shards:
            results.extend(infer(
                shard, entity_labels=entity_labels,
                relation_labels=relation_labels,
                entity_threshold=entity_threshold,
                relation_threshold=relation_threshold,
            ))
        return results

    from concurrent.futures import ThreadPoolExecutor

    def _run(shard_index: int, shard_texts: list[str]) -> tuple[int, list[RelexResult]]:
        primary = pool[shard_index % len(pool)]
        try:
            rows = infer(
                shard_texts, entity_labels=entity_labels,
                relation_labels=relation_labels,
                entity_threshold=entity_threshold,
                relation_threshold=relation_threshold,
                base_url=primary,
            )
        except RelexSidecarError:
            fallback = pool[(shard_index + 1) % len(pool)]
            rows = infer(
                shard_texts, entity_labels=entity_labels,
                relation_labels=relation_labels,
                entity_threshold=entity_threshold,
                relation_threshold=relation_threshold,
                base_url=fallback,
            )
        return shard_index, rows

    ordered: dict[int, list[RelexResult]] = {}
    with ThreadPoolExecutor(max_workers=len(pool)) as executor:
        for shard_index, rows in executor.map(lambda s: _run(*s), shards):
            ordered[shard_index] = rows
    results = []
    for shard_index in range(len(shards)):
        results.extend(ordered[shard_index])
    return results
