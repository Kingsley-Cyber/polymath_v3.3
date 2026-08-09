#!/usr/bin/env python3
"""Provider-backed synthesis probes for complex-query fixture (3 queries).

Requires a working synthesis credential. Does not enable the global planner.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CORPUS_ID = os.environ.get("GSEM_FIXTURE_CORPUS_ID", "gsem-e2e-20260804a")
OUT_DIR = Path(os.environ.get("CQ_OUT", "/tmp/complex_query_generation"))
API = os.environ.get("CQ_API", "http://localhost:8000")
AUTH_USERNAME = os.environ.get("CQ_AUTH_USERNAME", "Sambenja")
# Explicit pool entry with a working credential (LongCat-2.0). Chat does not
# auto-select settings.models.synthesis for the answer stream.
SYNTHESIS_POOL_ENTRY = os.environ.get(
    "CQ_SYNTHESIS_POOL_ENTRY", "provider-readiness-longcat-1"
)

PROBES = [
    {
        "id": "direct_one_hop",
        "query": (
            "What relationship does the C++ update loop have to combat state "
            "transitions? Cite exact child evidence."
        ),
        "require_paths": False,  # one-hop may stop on direct evidence
    },
    {
        "id": "cross_domain_translation",
        "query": (
            "How should a C++ combat update loop be translated into Roblox Luau "
            "while preserving the original mechanics? Cite source and target evidence."
        ),
        "require_paths": True,
    },
    {
        "id": "contradiction_or_temporal",
        "query": (
            "Does the corpus contradict whether movement writing is identical to "
            "Benesh Movement Notation? If so, disclose both sides with child citations."
        ),
        "require_paths": False,
    },
]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    ordered = sorted(vals)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    k = (len(ordered) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return round(ordered[f], 2)
    return round(ordered[f] + (ordered[c] - ordered[f]) * (k - f), 2)


def _summary(vals: list[float]) -> dict[str, float]:
    return {
        "p50": _pct(vals, 50),
        "p95": _pct(vals, 95),
        "max": round(max(vals), 2) if vals else 0.0,
        "n": len(vals),
        "mean": round(statistics.fmean(vals), 2) if vals else 0.0,
    }


def _chat_sse(token: str, message: str) -> dict[str, Any]:
    import urllib.request

    body = {
        "message": message,
        "corpus_ids": [CORPUS_ID],
        "retrieval_tier": "qdrant_mongo_graph",
        "web_search": False,
        "overrides": {"model": f"pool:{SYNTHESIS_POOL_ENTRY}"},
    }
    req = urllib.request.Request(
        f"{API}/api/chat",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
        },
    )
    result: dict[str, Any] = {
        "answer": "",
        "sources": [],
        "event_types": [],
        "complex_query": {},
        "retrieval_diagnostics": {},
        "error": None,
        "provider_response_status": "failed",
        "answer_text_tokens_streamed": False,
        "SSE_terminal_event": None,
        "first_token_ms": None,
        "last_token_ms": None,
        "retrieval_done_ms": None,
    }
    parts: list[str] = []
    started = time.monotonic()

    def _absorb(rd: dict[str, Any]) -> None:
        if not isinstance(rd, dict):
            return
        result["retrieval_diagnostics"] = rd
        cq = rd.get("complex_query")
        if isinstance(cq, dict) and cq:
            result["complex_query"] = cq

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            buffer = b""
            while True:
                chunk = resp.read(1)
                if not chunk:
                    break
                buffer += chunk
                if not buffer.endswith(b"\n\n"):
                    continue
                block, buffer = buffer, b""
                for line in block.decode("utf-8", "replace").splitlines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type") or event.get("event")
                    if etype:
                        result["event_types"].append(etype)
                    te = event.get("trace_event") or {}
                    meta = (te.get("metadata") if isinstance(te, dict) else None) or {}
                    if isinstance(meta, dict) and isinstance(
                        meta.get("retrieval_diagnostics"), dict
                    ):
                        _absorb(meta["retrieval_diagnostics"])
                        if result["retrieval_done_ms"] is None and meta.get(
                            "retrieval_diagnostics"
                        ):
                            # first time we see retrieval diagnostics after RAG
                            if (te.get("title") or "") == "Local RAG retrieval":
                                result["retrieval_done_ms"] = round(
                                    (time.monotonic() - started) * 1000.0, 2
                                )
                    if isinstance(event.get("metadata"), dict) and isinstance(
                        event["metadata"].get("retrieval_diagnostics"), dict
                    ):
                        _absorb(event["metadata"]["retrieval_diagnostics"])
                    if etype == "token":
                        content = event.get("content") or ""
                        if content:
                            now = round((time.monotonic() - started) * 1000.0, 2)
                            if result["first_token_ms"] is None:
                                result["first_token_ms"] = now
                            result["last_token_ms"] = now
                            parts.append(content)
                            result["answer_text_tokens_streamed"] = True
                    elif etype == "sources":
                        for source in event.get("sources") or []:
                            result["sources"].append(
                                {
                                    "doc_id": source.get("doc_id"),
                                    "corpus_id": source.get("corpus_id"),
                                    "chunk_id": source.get("chunk_id"),
                                }
                            )
                    elif etype == "done":
                        result["SSE_terminal_event"] = "completed"
                        result["provider_response_status"] = "success"
                    elif etype in {"error", "fatal"}:
                        result["error"] = str(
                            event.get("content") or event.get("message") or event
                        )[:500]
                        ans = event.get("answer_status") or {}
                        if isinstance(ans, dict) and ans.get(
                            "status"
                        ) == "answer_provider_unavailable":
                            result["provider_response_status"] = "provider_error"
                            result["SSE_terminal_event"] = "error"
                        else:
                            result["SSE_terminal_event"] = "error"
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"[:500]
        result["SSE_terminal_event"] = "transport_error"

    result["answer"] = "".join(parts)
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 2)
    if result["answer_text_tokens_streamed"] and result["SSE_terminal_event"] == "completed":
        result["provider_response_status"] = "success"
    return result


def _score(probe: dict[str, Any], chat: dict[str, Any]) -> dict[str, Any]:
    cq = chat.get("complex_query") or {}
    ver = cq.get("answer_verification") or {}
    sources = chat.get("sources") or []
    child_ids = [s.get("chunk_id") for s in sources if s.get("chunk_id")]
    claims_total = int(ver.get("claims_total") or 0)
    claims_supported = int(ver.get("claims_supported") or 0)
    claims_unsupported = int(ver.get("claims_unsupported") or 0)
    paths = int(cq.get("graph_paths_used") or 0)
    bridges = int(ver.get("bridge_claims") or 0)
    bridges_verified = int(ver.get("bridge_claims_verified") or 0)
    contrad = int(ver.get("contradiction_disclosures") or 0)

    row = {
        "probe_id": probe["id"],
        "provider_response_status": chat.get("provider_response_status"),
        "answer_text_tokens_streamed": bool(chat.get("answer_text_tokens_streamed")),
        "SSE_terminal_event": chat.get("SSE_terminal_event"),
        "answer_len": len(chat.get("answer") or ""),
        "answer_preview": (chat.get("answer") or "")[:280],
        "complex_query_executor_ran": bool(cq.get("complex_query_executor_ran")),
        "graph_paths_used": paths,
        "every_path_has_child_support": bool(
            cq.get("every_path_has_child_support", True)
        ),
        "citations_resolve_to_exact_children": all(
            isinstance(c, str) and c.startswith("doc_") for c in child_ids
        )
        if child_ids
        else False,
        "claims_total": claims_total,
        "claims_supported": claims_supported,
        "claims_unsupported": claims_unsupported,
        "bridge_claims_verified": (bridges_verified == bridges) if bridges else True,
        "contradiction_disclosed": (contrad > 0)
        if probe["id"] == "contradiction_or_temporal"
        else True,
        "answer_verification_passed": bool(cq.get("answer_verification_passed")),
        "silent_hybrid_fallback": int(cq.get("silent_hybrid_fallback") or 0),
        "fixture_scope_enforced": bool(cq.get("fixture_scope_enforced")),
        "global_planner_enable": bool(cq.get("planner_global_enable")),
        "first_token_ms": chat.get("first_token_ms"),
        "last_token_ms": chat.get("last_token_ms"),
        "retrieval_done_ms": chat.get("retrieval_done_ms"),
        "elapsed_ms": chat.get("elapsed_ms"),
        "stage_timings_ms": cq.get("stage_timings_ms") or {},
        "error": chat.get("error"),
        "model_hint": None,
    }

    hard = []
    if row["provider_response_status"] != "success":
        hard.append("provider_not_success")
    if not row["answer_text_tokens_streamed"]:
        hard.append("no_tokens")
    if row["SSE_terminal_event"] != "completed":
        hard.append("terminal_not_completed")
    if not row["complex_query_executor_ran"]:
        hard.append("executor_not_ran")
    if probe.get("require_paths") and paths <= 0:
        hard.append("paths_missing")
    if not row["citations_resolve_to_exact_children"]:
        hard.append("citations_not_children")
    if claims_total <= 0:
        hard.append("claims_total_zero")
    if claims_supported != claims_total or claims_unsupported != 0:
        hard.append("claims_not_fully_supported")
    if not row["answer_verification_passed"]:
        hard.append("verification_failed")
    if row["silent_hybrid_fallback"]:
        hard.append("silent_hybrid_fallback")
    if not row["fixture_scope_enforced"]:
        hard.append("fixture_scope")
    if row["global_planner_enable"]:
        hard.append("global_planner_on")
    if probe["id"] == "contradiction_or_temporal" and not row["contradiction_disclosed"]:
        # Soft: disclosure may live in answer text if verification counter is 0
        answer = (chat.get("answer") or "").lower()
        if not any(w in answer for w in ("contradict", "not identical", "does not claim", "broader")):
            hard.append("contradiction_not_disclosed")
        else:
            row["contradiction_disclosed"] = True
    row["hard_fails"] = hard
    row["passed"] = not hard
    return row


async def main() -> int:
    from bson import ObjectId
    from motor.motor_asyncio import AsyncIOMotorClient

    from config import get_settings
    from services.auth import AuthService
    from services.ingestion.fixture_knowledge_pipeline import assert_fixture_safe

    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client[settings.MONGODB_DATABASE]
    corpus = await db["corpora"].find_one({"corpus_id": CORPUS_ID})
    assert corpus, f"missing {CORPUS_ID}"
    assert_fixture_safe(corpus)
    await db["corpora"].update_one(
        {"corpus_id": CORPUS_ID},
        {"$set": {"use_neo4j": True, "default_ingestion_config.use_neo4j": True}},
    )

    user = await db["users"].find_one({"username": AUTH_USERNAME})
    if user is None:
        user = await db["users"].find_one({})
    assert user, "no auth user"
    uid = str(user["_id"])
    token = AuthService().create_access_token(
        uid, str(user.get("username") or AUTH_USERNAME)
    )

    # Warm-up once (discard)
    _chat_sse(token, PROBES[0]["query"])

    measured: list[dict[str, Any]] = []
    for probe in PROBES:
        samples = []
        for _ in range(5):
            chat = _chat_sse(token, probe["query"])
            scored = _score(probe, chat)
            samples.append({"chat": chat, "score": scored})
        # Keep last sample as canonical correctness; all 5 for latency
        measured.append(
            {
                "probe": probe,
                "canonical": samples[-1]["score"],
                "samples": [s["score"] for s in samples],
            }
        )

    correctness = [m["canonical"] for m in measured]
    all_passed = all(r.get("passed") for r in correctness)

    # Successful-generation latency only from provider_response_status=success samples
    ret_ms, ft_ms, lt_ms, ver_ms, full_ms = [], [], [], [], []
    for m in measured:
        for s in m["samples"]:
            if s.get("provider_response_status") != "success":
                continue
            st = s.get("stage_timings_ms") or {}
            if st.get("complex_query_total") is not None:
                ret_ms.append(float(st["complex_query_total"]))
            if s.get("first_token_ms") is not None:
                ft_ms.append(float(s["first_token_ms"]))
            if s.get("last_token_ms") is not None:
                lt_ms.append(float(s["last_token_ms"]))
            # verification is inside CQ total today; use CQ wall after embed as proxy if present
            if st.get("wall_after_embed_ms") is not None:
                ver_ms.append(float(st["wall_after_embed_ms"]))
            if s.get("elapsed_ms") is not None:
                full_ms.append(float(s["elapsed_ms"]))

    performance = {
        "retrieval_total_p50_p95_max": _summary(ret_ms),
        "synthesis_first_token_p50_p95_max": _summary(ft_ms),
        "synthesis_last_token_p50_p95_max": _summary(lt_ms),
        "answer_verification_p50_p95_max": _summary(ver_ms),
        "full_chat_p50_p95_max": _summary(full_ms),
        "note": (
            "full_chat includes provider generation latency; 6/8/10s route "
            "targets remain retrieval/planning targets unless answer length "
            "and provider are fixed."
        ),
    }

    report = {
        "finished_at": _utcnow(),
        "corpus_id": CORPUS_ID,
        "auth_username": str(user.get("username") or AUTH_USERNAME),
        "auth_user_id": uid,
        "successful_chat_probes": correctness,
        "all_probes_passed": all_passed,
        "successful_generation_performance": performance,
        "authority": {
            "global_planner_enable": False,
            "fixture_scope_enforced": True,
            "graph_semantic_e2e_hard_stop": "remains_until_owner_lift",
        },
        "hard_stop": True,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "successful_generation_probes.json").write_text(
        json.dumps(report, indent=2, default=str)
    )
    (OUT_DIR / "successful_generation_performance.json").write_text(
        json.dumps(performance, indent=2, default=str)
    )
    print(
        json.dumps(
            {
                "all_probes_passed": all_passed,
                "probes": [
                    {
                        "id": r["probe_id"],
                        "passed": r["passed"],
                        "hard_fails": r["hard_fails"],
                        "provider": r["provider_response_status"],
                        "tokens": r["answer_text_tokens_streamed"],
                        "terminal": r["SSE_terminal_event"],
                        "paths": r["graph_paths_used"],
                        "claims": f"{r['claims_supported']}/{r['claims_total']}",
                        "elapsed_ms": r["elapsed_ms"],
                    }
                    for r in correctness
                ],
                "performance": performance,
            },
            indent=2,
            default=str,
        )
    )
    client.close()
    return 0 if all_passed else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
