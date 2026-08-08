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


def _post(path: str, payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        sidecar_url() + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, OSError) as exc:
        raise RelexSidecarError(f"relex sidecar unreachable at {sidecar_url()}: {exc}") from exc


def health(timeout: float = 5.0) -> dict:
    try:
        with urllib.request.urlopen(sidecar_url() + "/health", timeout=timeout) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, OSError) as exc:
        raise RelexSidecarError(f"relex sidecar unreachable at {sidecar_url()}: {exc}") from exc


def infer(
    texts: Sequence[str],
    *,
    entity_labels: Sequence[str] | None = None,
    relation_labels: Sequence[str] | None = None,
    entity_threshold: float | None = None,
    relation_threshold: float | None = None,
    timeout: float = 300.0,
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
    body = _post("/infer", payload, timeout)
    if body.get("contract") != RELEX_CONTRACT:
        raise RelexSidecarError(f"contract mismatch: {body.get('contract')!r}")
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
