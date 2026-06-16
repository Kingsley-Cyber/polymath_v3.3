#!/usr/bin/env python3
"""Dump the `sources` SSE payload to inspect hydration: distinct parents,
text sizes, provenance/graph presence. Diagnostic only."""
import json
import os
import sys
import urllib.request

TOKEN = os.environ["PROBE_TOKEN"]
CORPUS = os.environ.get("PROBE_CORPUS", "f8a0aa85-6cb4-4f64-a973-f9183f1546bb")
BASE = os.environ.get("PROBE_BASE", "http://localhost:8000")
tier = sys.argv[1] if len(sys.argv) > 1 else "qdrant_mongo"
query = sys.argv[2] if len(sys.argv) > 2 else "what is nlp"

body = json.dumps({"message": query, "corpus_ids": [CORPUS], "retrieval_tier": tier}).encode()
req = urllib.request.Request(
    f"{BASE}/api/chat", data=body,
    headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json",
             "Accept": "text/event-stream"}, method="POST")

sources = None
facts_seen = 0
with urllib.request.urlopen(req, timeout=600) as resp:
    cur = None
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\n")
        if line.startswith("event:"):
            cur = line.split(":", 1)[1].strip(); continue
        if not line.startswith("data:"):
            continue
        try:
            obj = json.loads(line[5:].strip())
        except Exception:
            continue
        if (obj.get("type") or cur) == "sources":
            sources = obj.get("sources") or obj.get("data") or []
            break

print(f"\n===== TIER={tier}  sources n={len(sources) if sources else 0} =====")
if not sources:
    print("no sources"); raise SystemExit

parents, docs, total_chars = {}, {}, 0
prov_count = 0
for i, s in enumerate(sources):
    pid = s.get("parent_id") or "<none>"
    doc = s.get("doc_name") or s.get("doc_id") or "?"
    txt = s.get("text") or ""
    total_chars += len(txt)
    parents[pid] = parents.get(pid, 0) + 1
    docs[doc] = docs.get(doc, 0) + 1
    prov = s.get("provenance") or []
    if prov:
        prov_count += 1
    st = s.get("source_tier") or s.get("chunk_kind") or "?"
    pv = f" prov={len(prov)}" if prov else ""
    print(f"[{i}] doc={doc[:42]!r:44} parent={str(pid)[:14]:16} tier={st:18} len(text)={len(txt):5}{pv}")

print(f"\n--- HYDRATION SUMMARY ({tier}) ---")
print(f"sources={len(sources)}  distinct_parents={len(parents)}  distinct_docs={len(docs)}")
dup_parents = {k: v for k, v in parents.items() if v > 1 and k != '<none>'}
print(f"parents appearing >1x (redundant hydration): {dup_parents or 'none'}")
print(f"total hydrated text chars={total_chars}  (~{total_chars//4} tokens)  avg/chunk={total_chars//max(1,len(sources))}")
print(f"sources carrying provenance={prov_count}/{len(sources)}")
