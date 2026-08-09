#!/usr/bin/env python3
"""Bounded dark-canary canary run — shadow metrics via /api/chat.

Never promotes ranking. Collects DarkCanaryComparisonV1 + acceptance, then STOP.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CORPUS_ID = os.environ.get("GSEM_FIXTURE_CORPUS_ID", "gsem-e2e-20260804a")
OUT_DIR = Path(os.environ.get("CQ_OUT", "/tmp/complex_query_dark_canary"))
API = os.environ.get("CQ_API", "http://localhost:8000")
AUTH_USERNAME = os.environ.get("CQ_AUTH_USERNAME", "Sambenja")
SYNTHESIS_POOL_ENTRY = os.environ.get(
    "CQ_SYNTHESIS_POOL_ENTRY", "provider-readiness-longcat-1"
)
# Dark synthesis optional — canary comparison is retrieval-side; keep provider
# for full chat path so SSE completes like production.
INCLUDE_SYNTHESIS = os.environ.get("CQ_DARK_SYNTHESIS", "1") != "0"

# Relationship-heavy first; stay well under measured_queries_max=100.
CANARY_QUERIES = [
    {
        "id": "dependency_analysis",
        "query_class": "dependency_analysis",
        "query": (
            "What does the C++ combat update loop depend on before state "
            "transitions can fire? Cite child evidence."
        ),
    },
    {
        "id": "cross_domain_translation",
        "query_class": "cross_domain_translation",
        "query": (
            "How should a C++ combat update loop be translated into Roblox Luau "
            "while preserving the original mechanics? Cite source and target evidence."
        ),
    },
    {
        "id": "explicit_relationship_path",
        "query_class": "multi_hop_relationship",
        "query": (
            "Trace the explicit relationship path from the C++ update loop to "
            "combat state transitions. List intermediate nodes with child citations."
        ),
    },
    {
        "id": "contradiction_analysis",
        "query_class": "contradiction_analysis",
        "query": (
            "Does the corpus contradict whether movement writing is identical to "
            "Benesh Movement Notation? If so, disclose both sides with child citations."
        ),
    },
    {
        "id": "temporal_latest_state",
        "query_class": "temporal_latest_state",
        "query": (
            "What is the latest stated relationship between combat state and the "
            "update loop? Prefer the most recent assertion with child evidence."
        ),
    },
]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _chat_sse(token: str, message: str) -> dict[str, Any]:
    import urllib.request

    overrides: dict[str, Any] = {}
    if INCLUDE_SYNTHESIS:
        overrides["model"] = f"pool:{SYNTHESIS_POOL_ENTRY}"
    body = {
        "message": message,
        "corpus_ids": [CORPUS_ID],
        "retrieval_tier": "qdrant_mongo_graph",
        "web_search": False,
        "overrides": overrides,
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
        "event_types": [],
        "dark_canary": {},
        "complex_query": {},
        "retrieval_diagnostics": {},
        "error": None,
        "SSE_terminal_event": None,
        "wall_ms": None,
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
        dc = rd.get("dark_canary")
        if isinstance(dc, dict) and dc:
            result["dark_canary"] = dc

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
                    if isinstance(event.get("metadata"), dict) and isinstance(
                        event["metadata"].get("retrieval_diagnostics"), dict
                    ):
                        _absorb(event["metadata"]["retrieval_diagnostics"])
                    if etype == "token":
                        content = event.get("content") or ""
                        if content:
                            parts.append(content)
                    elif etype == "done":
                        result["SSE_terminal_event"] = "completed"
                    elif etype in {"error", "fatal"}:
                        result["error"] = str(
                            event.get("content") or event.get("message") or event
                        )[:500]
                        result["SSE_terminal_event"] = "error"
        result["answer"] = "".join(parts)
        result["wall_ms"] = round((time.monotonic() - started) * 1000.0, 2)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"[:400]
        result["wall_ms"] = round((time.monotonic() - started) * 1000.0, 2)
    return result


async def _amain() -> int:
    from motor.motor_asyncio import AsyncIOMotorClient

    from config import get_settings
    from services.auth import AuthService
    from services.ingestion.fixture_knowledge_pipeline import assert_fixture_safe
    from services.retriever.complex_query_dark_canary import acceptance_snapshot

    OUT_DIR.mkdir(parents=True, exist_ok=True)
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
    print(f"[dark-canary] user={AUTH_USERNAME} uid={uid}", flush=True)
    token = AuthService().create_access_token(
        uid, str(user.get("username") or AUTH_USERNAME)
    )

    rows: list[dict[str, Any]] = []
    probes: list[dict[str, Any]] = []

    for item in CANARY_QUERIES:
        print(f"[dark-canary] {item['id']} …", flush=True)
        r = _chat_sse(token, item["query"])
        dc = r.get("dark_canary") or {}
        comparison = dc.get("comparison") if isinstance(dc, dict) else None
        probe = {
            "id": item["id"],
            "query_class": item["query_class"],
            "query": item["query"],
            "wall_ms": r.get("wall_ms"),
            "error": r.get("error"),
            "SSE_terminal_event": r.get("SSE_terminal_event"),
            "user_id": uid,
            "dark_canary_gate": {
                "enabled": dc.get("enabled") if isinstance(dc, dict) else None,
                "gate_reason": dc.get("gate_reason") if isinstance(dc, dict) else None,
            },
            "complex_query_ran": bool(
                (r.get("complex_query") or {}).get("complex_query_executor_ran")
            ),
            "ranking_mutated": bool(
                (r.get("complex_query") or {}).get("ranking_mutated")
            ),
            "answer_chars": len(r.get("answer") or ""),
            "comparison": comparison,
        }
        probes.append(probe)
        if isinstance(comparison, dict):
            rows.append(comparison)
        time.sleep(0.2)

    # Prefer container ledger if mounted
    ledger_path = Path("/data/ingest-files/complex-query-dark-canary/comparisons.jsonl")
    if ledger_path.is_file():
        try:
            ledger_rows = [
                json.loads(line)
                for line in ledger_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if ledger_rows:
                rows = ledger_rows[-len(CANARY_QUERIES) :]
        except Exception:  # noqa: BLE001
            pass

    acceptance = acceptance_snapshot(rows)
    report = {
        "schema_version": "dark_canary_run.v1",
        "generated_at": _utcnow(),
        "corpus_id": CORPUS_ID,
        "auth_username": AUTH_USERNAME,
        "user_id": uid,
        "n_probes": len(probes),
        "n_comparisons": len(rows),
        "acceptance": acceptance,
        "probes": probes,
        "contract": {
            "user_visible_answer_mutation": False,
            "production_ranking_mutation": False,
            "baseline_retrieval_remains_authoritative": True,
            "global_planner_enable": False,
            "production_ontology_activation": False,
        },
        "hard_stop": {
            "user_visible_canary_activation": "NOT_AUTHORIZED",
            "global_activation": "NOT_AUTHORIZED",
            "next": "owner decision on user-visible canary — do not auto-promote",
        },
    }
    (OUT_DIR / "dark_canary_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    (OUT_DIR / "comparisons.jsonl").write_text(
        "\n".join(json.dumps(r, default=str) for r in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    print(json.dumps({"acceptance": acceptance, "out": str(OUT_DIR)}, indent=2))
    if not rows:
        print("ERROR: zero comparisons recorded", file=sys.stderr)
        return 2
    return 0


def main() -> int:
    import asyncio

    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
