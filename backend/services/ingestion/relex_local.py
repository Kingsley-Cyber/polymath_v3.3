"""relex_local — the ONE canonical deterministic extraction engine.

Architecture invariant (owner, 2026-08): Polymath uses one
non-autoregressive extraction pipeline: GLiNER-Relex performs joint entity
and relation scoring in one encoder pass (served by the relex_extract_svc
sidecar), while spaCy and deterministic rules provide structural evidence,
canonicalization, and acceptance control.

Per chunk this lane executes EXACTLY:
  1. ONE Relex encoder pass (sidecar POST /extract, one task row)
  2. ONE spaCy parse of the chunk text (shared Doc)
  3. FrameExtractor + DependencyMatcher frames on that parse
  4. Native SVO on the same parse
  5. Entity consolidation / alias canonicalization
  6. Union evidence through the deterministic corroboration gate

Fail-closed contract:
  * Sidecar unreachable, not ready, or hash-unverified → the whole batch
    raises ``RelexUnavailableError``. There is NO fallback to any other
    extractor and NO partial graph promotion.
  * Per-chunk sidecar failures become ``ExtractionFailureItem`` rows.
  * Only ACCEPT_HIGH / ACCEPT_CORROBORATED / ACCEPT_SYNTAX_HIGH gate
    decisions become RelationItems. REVIEW_*, STORE_*, REJECT_* and
    SHADOW_RELEX_HIGH decisions are recorded in the audit ledger only.

Artifact identity: every ExtractionResult is stamped with
extractor_engine / extractor_release / model_hash / ontology_hash /
acceptance_policy_hash so stored output is never ambiguous about which
extractor produced it. Same (document_id, chunk_id, extractor_release)
re-runs are deterministic and replace the same artifact.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import os
import time
from pathlib import Path

import httpx

from services.extraction.canonical import (
    canonical_entity_type,
    canonicalize_entity_name,
)
from services.extraction.corroboration_gate import evaluate_relation, load_policy
from services.extraction.frame_extractor import FrameExtractor
from services.extraction.relation_evidence import GateStatus
from services.extraction.syntax_lane import (
    build_union_evidence,
    generate_syntax_records,
)
from services.ghost_b import (
    EntityItem,
    ExtractionBatchReport,
    ExtractionFailureItem,
    ExtractionResult,
    RelationItem,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical engine identity
# ---------------------------------------------------------------------------

EXTRACTOR_ENGINE = "relex_local"
EXTRACTOR_RELEASE = "relex_local.v1"
SCHEMA_VERSION = "polymath.extract.relex_local.v1"
SERVICE_SCHEMA_VERSION = "polymath.relex_sidecar.v1"

_REPO_ROOT_CANDIDATES = (3, 2)  # host repo root vs /app container layout


def _config_dir() -> Path:
    """Resolve the repo config/ directory. Raises if not found (fail LOUD).

    Host layout puts the code at <repo>/backend/services/ingestion/, container
    layout at /app/services/ingestion/ — mirror dep_path_extractor's resolver
    so extraction identity never depends on an uncertain path.
    """
    _this = Path(__file__).resolve()
    for parent_idx in _REPO_ROOT_CANDIDATES:
        candidate = _this.parents[parent_idx] / "config"
        if candidate.is_dir():
            return candidate
    raise RuntimeError(
        f"FATAL: config/ directory not found relative to {__file__}. "
        "Cannot hash ontology.yaml or relation_acceptance.yaml. "
        "Refusing to extract with uncertain identity."
    )


_ONTOLOGY_PATH = _config_dir() / "ontology.yaml"
_POLICY_PATH = _config_dir() / "relation_acceptance.yaml"

# Only these gate decisions may ever become RelationItems (positive edges).
# Everything else is audit-only: REVIEW_CONFLICT, STORE_UNMAPPED_SURFACE_
# RELATION, STORE_QUALIFIED_FACT, REJECT_*, SHADOW_RELEX_HIGH etc.
_ACCEPTED_GATES = frozenset({
    GateStatus.ACCEPT_HIGH,
    GateStatus.ACCEPT_CORROBORATED,
    GateStatus.ACCEPT_SYNTAX_HIGH,
})

_DEFAULT_SIDECAR_URL = "http://host.docker.internal:8086"


class RelexUnavailableError(RuntimeError):
    """Raised when the Relex extractor cannot serve this batch.

    Ingestion must stop safely and retry later. This error is the
    fail-closed boundary: nothing downstream may catch it and substitute
    another extractor.
    """


def sidecar_url() -> str:
    return os.getenv("RELEX_LOCAL_URL", _DEFAULT_SIDECAR_URL).rstrip("/")


# ---------------------------------------------------------------------------
# Release-pin hashes (deterministic, cached per process)
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def ontology_hash() -> str:
    # Fail closed: an extraction stamp with an unknown ontology hash is an
    # uncertain identity and must never be produced.
    return hashlib.sha256(_ONTOLOGY_PATH.read_bytes()).hexdigest()


@functools.lru_cache(maxsize=1)
def acceptance_policy_hash() -> str:
    return hashlib.sha256(_POLICY_PATH.read_bytes()).hexdigest()


def reset_hash_caches() -> None:
    ontology_hash.cache_clear()
    acceptance_policy_hash.cache_clear()


# ---------------------------------------------------------------------------
# Sidecar client — fail closed, no fallback
# ---------------------------------------------------------------------------


async def _probe_health(client: httpx.AsyncClient) -> dict:
    """Verify the sidecar is ready and hash-verified before leasing work."""
    try:
        resp = await client.get("/health", timeout=10.0)
    except httpx.HTTPError as exc:
        raise RelexUnavailableError(
            f"relex sidecar unreachable at {sidecar_url()}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if resp.status_code != 200:
        raise RelexUnavailableError(
            f"relex sidecar /health returned HTTP {resp.status_code}"
        )
    health = resp.json()
    if health.get("extractor") != EXTRACTOR_ENGINE:
        raise RelexUnavailableError(
            f"sidecar reports extractor={health.get('extractor')!r}, "
            f"expected {EXTRACTOR_ENGINE!r}"
        )
    if health.get("service_schema_version") != SERVICE_SCHEMA_VERSION:
        raise RelexUnavailableError(
            "sidecar service_schema_version mismatch: "
            f"{health.get('service_schema_version')!r}"
        )
    if not health.get("ready"):
        raise RelexUnavailableError(
            "relex sidecar not ready: "
            f"{health.get('readiness_error') or 'unknown readiness_error'}"
        )
    if not health.get("model_hash_verified"):
        raise RelexUnavailableError(
            "relex sidecar model hash NOT verified — refusing to extract "
            "with unverified weights"
        )
    return health


# Wire ceiling is relex_extract_svc ExtractRequest.tasks max_length (512),
# but each POST stays silent until the WHOLE slice is encoded. Through the
# Docker Desktop host proxy (host.docker.internal) a 512-chunk slice is a
# 10–25 min mute TCP connection and gets severed mid-request (MEASURED
# 2026-08-05: Fundamentals died with "Server disconnected without sending a
# response" while the sidecar stayed healthy). Keep slices small enough that
# every POST answers within a couple of minutes.
RELEX_SIDECAR_TASK_MAX = 32


async def _extract_prediction_slices(
    client: httpx.AsyncClient,
    tasks: list,
):
    """POST /extract slice-by-slice, yielding each slice as it returns.

    The sidecar accepts at most ``RELEX_SIDECAR_TASK_MAX`` tasks per request.
    Callers may pass an entire document's children; this generator slices them
    into wire-legal POSTs and yields ``(slice_tasks, preds, fails)`` per slice
    so CPU-side processing can overlap the next slice's encode wait.

    Raises RelexUnavailableError when the sidecar itself cannot serve.
    """
    if not tasks:
        return

    for offset in range(0, len(tasks), RELEX_SIDECAR_TASK_MAX):
        slice_tasks = tasks[offset : offset + RELEX_SIDECAR_TASK_MAX]
        payload = {
            "tasks": [
                {"chunk_id": t.chunk_id, "text": t.text} for t in slice_tasks
            ],
        }
        # MPS is single-flight inside the sidecar. Under concurrent docs the
        # HTTP call waits in queue; a 30s base timed out 1–2 chunk files while
        # Fundamentals held the lock (MEASURED 2026-08-05: 2-chunk extract
        # under load ≈77s). Budget for queue wait + ~5s/chunk encode.
        timeout = max(300.0, 120.0 + 5.0 * len(slice_tasks))
        resp = None
        for attempt in (1, 2):
            try:
                resp = await client.post(
                    "/extract", json=payload, timeout=timeout,
                )
                break
            except (httpx.RemoteProtocolError, httpx.ConnectError) as exc:
                # A severed keep-alive/proxy connection is transient: the
                # sidecar was healthy moments ago. Retry ONCE on a fresh
                # connection, then fail closed.
                if attempt == 2:
                    raise RelexUnavailableError(
                        f"relex sidecar /extract failed after retry: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                logger.warning(
                    "relex /extract transport drop (%s) — retrying slice "
                    "offset=%d size=%d on fresh connection",
                    type(exc).__name__, offset, len(slice_tasks),
                )
            except httpx.HTTPError as exc:
                raise RelexUnavailableError(
                    f"relex sidecar /extract failed: {type(exc).__name__}: {exc}"
                ) from exc
        if resp.status_code == 503:
            raise RelexUnavailableError(
                "relex sidecar returned 503 blocked_extractor_unavailable"
            )
        if resp.status_code != 200:
            raise RelexUnavailableError(
                f"relex sidecar /extract returned HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        body = resp.json()
        preds: dict[str, dict] = {}
        fails: dict[str, str] = {}
        for row in body.get("results", []):
            cid = str(row.get("chunk_id", ""))
            if cid:
                preds[cid] = row
        for row in body.get("failures", []):
            cid = str(row.get("chunk_id", ""))
            if cid:
                fails[cid] = str(row.get("error") or "sidecar chunk failure")
        yield slice_tasks, preds, fails


async def _extract_predictions(
    client: httpx.AsyncClient,
    tasks: list,
) -> tuple[dict[str, dict], dict[str, str]]:
    """Aggregate every slice into merged prediction/failure maps."""
    preds: dict[str, dict] = {}
    fails: dict[str, str] = {}
    async for _slice_tasks, slice_preds, slice_fails in _extract_prediction_slices(
        client, tasks,
    ):
        preds.update(slice_preds)
        fails.update(slice_fails)
    return preds, fails


# ---------------------------------------------------------------------------
# Per-chunk deterministic pipeline (runs in a worker thread)
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _shared_extractor() -> FrameExtractor:
    return FrameExtractor()


def _prediction_fingerprint(pred_row: dict) -> dict:
    encoded = json.dumps(
        pred_row, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
    ).encode("utf-8")
    return {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "entities": len(pred_row.get("entities", [])),
        "thresholded_relations": len(pred_row.get("relations", [])),
        "raw_pairs": len(pred_row.get("raw_pair_scores", [])),
        "raw_scores_complete": bool(pred_row.get("raw_scores_complete", False)),
    }


def _evidence_phrase(ev, text: str) -> str:
    """Exact substring of the chunk covering both endpoint spans."""
    lo = min(ev.subject_start, ev.object_start)
    hi = max(ev.subject_end, ev.object_end)
    if lo < 0 or hi <= lo or hi > len(text):
        return ""
    return text[lo:hi][:300]


def _consolidate_entities(pred_row: dict) -> list[EntityItem]:
    """Deduplicate Relex entity spans by (canonical_name, type).

    Later surface mentions of the same canonical entity become
    query_aliases; confidence keeps the max observed score.
    """
    merged: dict[tuple[str, str], EntityItem] = {}
    for ent in pred_row.get("entities", []):
        surface = str(ent.get("text") or "").strip()
        if not surface:
            continue
        etype = canonical_entity_type(str(ent.get("type") or ""))
        cname = canonicalize_entity_name(surface)
        score = float(ent.get("score") or 0.0)
        key = (cname, etype)
        existing = merged.get(key)
        if existing is None:
            merged[key] = EntityItem(
                canonical_name=cname,
                surface_form=surface,
                entity_type=etype,
                confidence=score,
                query_aliases=[],
            )
        else:
            existing.confidence = max(existing.confidence, score)
            if surface != existing.surface_form and surface not in (
                existing.query_aliases
            ):
                existing.query_aliases.append(surface)
    return list(merged.values())


def _process_chunk_sync(
    task,
    pred_row: dict,
    policy,
    health: dict,
) -> ExtractionResult:
    """Deterministic per-chunk pipeline. ONE parse, ONE gate pass."""
    text = task.text or ""
    chunk_id = task.chunk_id
    extractor = _shared_extractor()

    # --- ONE spaCy parse (parse-once invariant) ---
    doc = extractor._nlp(text)  # noqa: SLF001 — shared pipeline of the extractor

    # --- Syntax lane: FrameExtractor + SVO on the shared parse ---
    resolved, unmapped = generate_syntax_records(
        text, pred_row.get("entities", []), chunk_id, extractor, doc,
    )

    # --- Union evidence through the deterministic gate ---
    union = build_union_evidence(chunk_id, pred_row, resolved, unmapped, text)

    gate_counts: dict[str, int] = {}
    relations_out: list[RelationItem] = []
    for ev in union:
        decision = evaluate_relation(ev, policy)
        status_value = decision.status.value
        gate_counts[status_value] = gate_counts.get(status_value, 0) + 1
        if decision.status not in _ACCEPTED_GATES:
            continue  # audit-only; never a positive edge

        syntax_conf = max(
            (s.confidence for s in ev.syntax_evidence), default=0.0,
        )
        confidence = float(decision.score or 0.0) or syntax_conf
        se = ev.syntax_evidence[0] if ev.syntax_evidence else None
        relations_out.append(RelationItem(
            subject=canonicalize_entity_name(ev.subject_text),
            predicate=str(decision.predicate or ""),
            object=canonicalize_entity_name(ev.object_text),
            object_kind="entity",
            confidence=confidence,
            evidence_phrase=_evidence_phrase(ev, text),
            relation_cue=(se.surface_predicate if se else "") or "",
            source_predicate=(se.canonical_predicate if se else None)
            or decision.predicate,
            validation_status=status_value,
        ))

    entities_out = _consolidate_entities(pred_row)

    stamp = {
        "contract": "polymath.relex_local.v1",
        "extractor_engine": EXTRACTOR_ENGINE,
        "extractor_release": EXTRACTOR_RELEASE,
        "schema_version": SCHEMA_VERSION,
        "model_id": health.get("model_id", ""),
        "model_hash": health.get("model_hash") or "",
        "model_hash_verified": bool(health.get("model_hash_verified")),
        "entity_schema_hash": health.get("entity_schema_hash", ""),
        "relation_schema_hash": health.get("relation_schema_hash", ""),
        "ontology_hash": ontology_hash(),
        "acceptance_policy_hash": acceptance_policy_hash(),
        "gate_decision_counts": gate_counts,
        "spacy_parses_per_chunk": 1,
        "relex_encodes_per_chunk": 1,
    }

    return ExtractionResult(
        schema_version=SCHEMA_VERSION,
        chunk_id=chunk_id,
        doc_id=task.doc_id,
        corpus_id=task.corpus_id,
        entities=entities_out,
        relations=relations_out,
        facts=[],  # Relex emits no facts — honest empty, never fabricated
        text=text,
        model=str(health.get("model_id") or "gliner-relex-large-v1.0"),
        provider=EXTRACTOR_ENGINE,
        attempts=1,
        schema_mode="none",
        output_mode="deterministic_gate",
        raw_output_fingerprint=_prediction_fingerprint(pred_row),
        local_extraction=stamp,
    )


# ---------------------------------------------------------------------------
# Batch entrypoint (worker dispatch target)
# ---------------------------------------------------------------------------


async def extract_entities(
    tasks: list,
    *,
    return_report: bool = True,
    **_ignored_kwargs,
) -> ExtractionBatchReport | list[ExtractionResult]:
    """Extract entities/relations for a chunk batch via GLiNER-Relex.

    Provider-pool arguments (schema, pool, model, ...) are accepted and
    ignored: the deterministic extractor has NO dependency on the generic
    LLM/local-provider card pool. Unknown or removed engine values never
    reach this function — validation happens before ingestion starts.

    Raises RelexUnavailableError (fail closed) when the sidecar is
    unreachable, not ready, or serving unverified weights.
    """
    if not tasks:
        empty = ExtractionBatchReport(results=[], failures=[], metrics={
            "engine": EXTRACTOR_ENGINE,
            "requested_chunks": 0,
            "extracted_chunks": 0,
            "failed_chunks": 0,
        })
        return empty if return_report else []

    policy = load_policy()
    started = time.monotonic()

    failures: list[ExtractionFailureItem] = []

    # Slice pipelining: while slice N+1 waits on the sidecar's MPS encode,
    # slice N's spaCy/gate work runs in a single worker thread (the shared
    # spaCy pipeline is not thread-safe, so exactly ONE consumer). Chunks are
    # enqueued in task order, so results keep the same deterministic order
    # the sequential implementation produced. MEASURED 2026-08-05
    # (Fundamentals, 1695 chunks): encode wait ≈318s, CPU lane ≈172s run
    # serially after it — overlap hides nearly all of the CPU lane.
    queue: asyncio.Queue = asyncio.Queue()

    async def _consume() -> list[ExtractionResult]:
        out: list[ExtractionResult] = []
        while True:
            item = await queue.get()
            if item is None:
                return out
            task, pred_row, health_row = item
            # CPU-bound deterministic work stays off the event loop.
            out.append(await asyncio.to_thread(
                _process_chunk_sync, task, pred_row, policy, health_row,
            ))

    consumer = asyncio.create_task(_consume())
    try:
        # No keep-alive: uvicorn closes idle persistent connections after ~5s
        # and a reused dead socket surfaces as RemoteProtocolError mid-batch.
        # Fresh connections per request cost microseconds against
        # multi-second encodes.
        async with httpx.AsyncClient(
            base_url=sidecar_url(),
            limits=httpx.Limits(max_keepalive_connections=0),
        ) as client:
            health = await _probe_health(client)
            async for slice_tasks, preds, sidecar_failures in (
                _extract_prediction_slices(client, tasks)
            ):
                for task in slice_tasks:
                    cid = task.chunk_id
                    if cid in sidecar_failures:
                        failures.append(ExtractionFailureItem(
                            chunk_id=cid,
                            doc_id=task.doc_id,
                            corpus_id=task.corpus_id,
                            model=str(health.get("model_id") or ""),
                            lane=0,
                            attempts=1,
                            error_type="relex_chunk_failed",
                            error_message=sidecar_failures[cid],
                            provider=EXTRACTOR_ENGINE,
                        ))
                        continue
                    pred_row = preds.get(cid)
                    if pred_row is None:
                        failures.append(ExtractionFailureItem(
                            chunk_id=cid,
                            doc_id=task.doc_id,
                            corpus_id=task.corpus_id,
                            model=str(health.get("model_id") or ""),
                            lane=0,
                            attempts=1,
                            error_type="relex_chunk_missing",
                            error_message="sidecar returned no prediction for chunk",
                            provider=EXTRACTOR_ENGINE,
                        ))
                        continue
                    queue.put_nowait((task, pred_row, health))
    except BaseException:
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer
        raise

    queue.put_nowait(None)
    results = await consumer

    elapsed = time.monotonic() - started
    gate_totals: dict[str, int] = {}
    for r in results:
        stamp = r.local_extraction or {}
        for status, n in (stamp.get("gate_decision_counts") or {}).items():
            gate_totals[status] = gate_totals.get(status, 0) + n

    metrics = {
        "engine": EXTRACTOR_ENGINE,
        "extractor_release": EXTRACTOR_RELEASE,
        "requested_chunks": len(tasks),
        "extracted_chunks": len(results),
        "failed_chunks": len(failures),
        "gate_decision_counts": gate_totals,
        "accepted_relations": sum(len(r.relations) for r in results),
        "entities": sum(len(r.entities) for r in results),
        "model_hash_verified": bool(health.get("model_hash_verified")),
        "model_hash": health.get("model_hash") or "",
        "device": health.get("device", ""),
        "ontology_hash": ontology_hash(),
        "acceptance_policy_hash": acceptance_policy_hash(),
        "spacy_parses_per_chunk": 1,
        "relex_encodes_per_chunk": 1,
        "latency_s": round(elapsed, 3),
        "provider_pool_required": False,
        "fallback_used": False,
    }
    logger.info(
        "phase=relex_local chunks=%d ok=%d failed=%d relations=%d %.2fs",
        len(tasks), len(results), len(failures),
        metrics["accepted_relations"], elapsed,
    )

    report = ExtractionBatchReport(
        results=results, failures=failures, metrics=metrics,
    )
    return report if return_report else results
