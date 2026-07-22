#!/usr/bin/env bash
set -euo pipefail

container="${BACKEND_CONTAINER:-polymath_v33-backend-1}"

docker exec -i "$container" python - <<'PY'
import hashlib
import json
import os
import sys
import time
from urllib.parse import quote

import httpx
from jose import jwt
from pymongo import MongoClient

BASE = os.environ.get("POLYMATH_API_BASE", "http://127.0.0.1:8000")
SAFE_TIMEOUT = int(os.environ.get("RESEARCH_E2E_TIMEOUT_SECONDS", "180"))


def print_json(payload):
    print(json.dumps(payload, sort_keys=True))


def mint_token_from_first_user():
    uri = os.environ.get("MONGODB_URI")
    secret = os.environ.get("AUTH_SECRET_KEY")
    if not uri or not secret:
        return None
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    db = client.get_default_database()
    user = db["users"].find_one({}, sort=[("created_at", 1)])
    if not user:
        return None
    return jwt.encode(
        {"sub": str(user["_id"]), "username": user.get("username", "user")},
        secret,
        algorithm=os.environ.get("AUTH_ALGORITHM", "HS256"),
    )


def login_or_mint_token(client):
    username = os.environ.get("DEFAULT_ADMIN_USERNAME", "admin")
    password = os.environ.get("DEFAULT_ADMIN_PASSWORD")
    if password:
        resp = client.post(
            f"{BASE}/api/auth/login",
            json={"username": username, "password": password},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()["access_token"], "login"
    token = mint_token_from_first_user()
    if token:
        return token, "minted_existing_user_jwt"
    raise RuntimeError("Could not authenticate for research E2E smoke")


with httpx.Client(timeout=30.0) as client:
    token, auth_method = login_or_mint_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    me = client.get(f"{BASE}/api/auth/me", headers=headers)
    me.raise_for_status()

    corpora_resp = client.get(f"{BASE}/api/corpora", headers=headers)
    corpus_ids = []
    if corpora_resp.status_code == 200:
        corpora = corpora_resp.json()
        if isinstance(corpora, list):
            corpus_ids = [
                str(item.get("corpus_id") or item.get("id"))
                for item in corpora
                if item.get("corpus_id") or item.get("id")
            ]
        elif isinstance(corpora, dict):
            items = corpora.get("items") or corpora.get("corpora") or []
            corpus_ids = [
                str(item.get("corpus_id") or item.get("id"))
                for item in items
                if item.get("corpus_id") or item.get("id")
            ]
    db_client = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=5000)
    db = db_client.get_default_database()
    counted_corpora = [
        {
            "corpus_id": corpus_id,
            "chunks": db["chunks"].count_documents({"corpus_id": corpus_id}),
        }
        for corpus_id in corpus_ids
    ]
    non_empty_corpora = [item for item in counted_corpora if item["chunks"] > 0]
    selected = [
        (os.environ.get("RESEARCH_E2E_CORPUS_ID") or "").strip()
        or min(non_empty_corpora, key=lambda item: item["chunks"])["corpus_id"]
    ] if non_empty_corpora else corpus_ids[:1]

    created = client.post(
        f"{BASE}/api/research/jobs?run=true",
        headers=headers,
        json={
            "question": os.environ.get(
                "RESEARCH_E2E_QUESTION",
                "Produce a short cited research artifact describing the main systems, topics, or claims in this corpus.",
            ),
            "corpus_ids": selected,
            "mode": "quick",
            "budgets": {
                "max_subquestions": 1,
                "max_tool_calls": 2,
                "max_graph_hops": 1,
                "max_evidence_items": 4,
                "max_output_tokens": 1024,
            },
            "metadata": {"source": "codex_live_e2e_smoke"},
        },
        timeout=30,
    )
    if created.status_code >= 400:
        print_json({"ok": False, "stage": "create", "status": created.status_code, "body": created.text[:500]})
        sys.exit(1)

    job = created.json()
    job_id = job["job_id"]
    deadline = time.time() + SAFE_TIMEOUT
    statuses = [job.get("status")]
    final = job
    while time.time() < deadline:
        time.sleep(2)
        current = client.get(f"{BASE}/api/research/jobs/{quote(job_id)}", headers=headers, timeout=15)
        current.raise_for_status()
        final = current.json()
        state = final.get("status")
        if state != statuses[-1]:
            statuses.append(state)
        if state in {"done", "failed", "cancelled"}:
            break
    else:
        print_json({"ok": False, "stage": "poll", "job_id": job_id, "statuses": statuses, "final_status": final.get("status")})
        sys.exit(1)

    events_resp = client.get(f"{BASE}/api/research/jobs/{quote(job_id)}/events?limit=500", headers=headers, timeout=20)
    events_resp.raise_for_status()
    events = events_resp.json().get("items", [])
    artifacts_resp = client.get(f"{BASE}/api/research/jobs/{quote(job_id)}/artifacts", headers=headers, timeout=20)
    artifacts_resp.raise_for_status()
    artifacts = artifacts_resp.json().get("items", [])

    downloads = []
    for artifact in artifacts:
        artifact_id = artifact["artifact_id"]
        download = client.get(
            f"{BASE}/api/research/artifacts/{quote(artifact_id)}/download",
            headers=headers,
            timeout=30,
        )
        downloads.append({
            "artifact_id": artifact_id,
            "format": artifact.get("format"),
            "status": download.status_code,
            "bytes": len(download.content),
            "sha256": hashlib.sha256(download.content).hexdigest(),
            "content_type": download.headers.get("content-type"),
        })

    required_formats = {"markdown", "html", "json"}
    present_formats = {str(item.get("format")) for item in artifacts}
    event_stages = [str(event.get("stage")) for event in events]
    ok = (
        final.get("status") == "done"
        and required_formats.issubset(present_formats)
        and all(item["status"] == 200 and item["bytes"] > 0 for item in downloads)
        and "retrieval" in event_stages
        and "context" in event_stages
    )
    print_json({
        "ok": ok,
        "auth_method": auth_method,
        "selected_corpus_count": len(selected),
        "selected_corpus_id": selected[0] if selected else None,
        "available_corpus_count": len(corpus_ids),
        "job_id": job_id,
        "statuses": statuses,
        "final_status": final.get("status"),
        "artifact_formats": sorted(present_formats),
        "artifact_count": len(artifacts),
        "download_receipts": downloads,
        "event_count": len(events),
        "event_stages": event_stages,
        "has_context_receipt": any(event.get("stage") == "context" and event.get("status") == "done" for event in events),
        "has_graph_event": any(event.get("stage") == "graph" for event in events),
        "has_retrieval_event": any(event.get("stage") == "retrieval" for event in events),
    })
    if not ok:
        sys.exit(1)
PY
