#!/usr/bin/env python3
"""Ad-hoc SSE timing probe for /api/chat. Diagnostic only — not committed logic.

Usage: python3 scripts/sse_probe.py <tier> [query]
  tier: qdrant_only | qdrant_mongo | qdrant_mongo_graph
"""
import json
import os
import sys
import time
import urllib.request

TOKEN = os.environ["PROBE_TOKEN"]
CORPUS = os.environ.get("PROBE_CORPUS", "f8a0aa85-6cb4-4f64-a973-f9183f1546bb")
BASE = os.environ.get("PROBE_BASE", "http://localhost:8000")

tier = sys.argv[1] if len(sys.argv) > 1 else "qdrant_mongo"
query = sys.argv[2] if len(sys.argv) > 2 else "what is nlp"

body = json.dumps({
    "message": query,
    "corpus_ids": [CORPUS],
    "retrieval_tier": tier,
}).encode()

req = urllib.request.Request(
    f"{BASE}/api/chat",
    data=body,
    headers={
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    },
    method="POST",
)

t0 = time.perf_counter()
first_token_t = None
counts = {}
trace_titles = []
sources_at = None
answer_chars = 0
thinking_chars = 0
answer_buf = []
thinking_buf = []
first_event_t = None
done_payload = None
last_print = 0.0

print(f"\n===== TIER={tier}  Q={query!r} =====", flush=True)
try:
    with urllib.request.urlopen(req, timeout=600) as resp:
        cur_event = None
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            now = time.perf_counter() - t0
            if first_event_t is None:
                first_event_t = now
                print(f"[{now:7.2f}s] FIRST BYTE", flush=True)
            if not line:
                continue
            if line.startswith("event:"):
                cur_event = line.split(":", 1)[1].strip()
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            try:
                obj = json.loads(data)
            except Exception:
                continue
            etype = obj.get("type") or cur_event or "?"
            counts[etype] = counts.get(etype, 0) + 1
            if etype == "token":
                tok = obj.get("token") or obj.get("content") or obj.get("text") or ""
                if first_token_t is None and tok:
                    first_token_t = now
                    print(f"[{now:7.2f}s] FIRST TOKEN", flush=True)
                answer_chars += len(tok)
                answer_buf.append(tok)
            elif etype == "thinking":
                th = obj.get("thinking") or obj.get("content") or obj.get("text") or ""
                thinking_chars += len(th)
                thinking_buf.append(th)
                if counts[etype] == 1:
                    print(f"[{now:7.2f}s] FIRST THINKING", flush=True)
            elif etype in ("trace_event",) or obj.get("trace_event"):
                te = obj.get("trace_event") or obj
                title = te.get("title") or te.get("lane") or "?"
                trace_titles.append((round(now, 2), title))
                meta = te.get("metadata") or {}
                extra = ""
                if title in ("Chat model route", "Chat model stream") and meta.get("model"):
                    extra = f"  model={meta.get('model')}"
                if title in ("Native tool call", "Native tool result"):
                    extra = f"  content={str(te.get('content'))[:200]}"
                print(f"[{now:7.2f}s] trace: {title}{extra}", flush=True)
            elif etype == "tool_call_start":
                print(f"[{now:7.2f}s] TOOL_CALL_START: {str(obj.get('content'))[:200]}", flush=True)
            elif etype == "sources":
                srcs = obj.get("sources") or obj.get("data") or []
                sources_at = now
                print(f"[{now:7.2f}s] SOURCES n={len(srcs) if hasattr(srcs,'__len__') else '?'}", flush=True)
            elif etype == "done":
                done_payload = obj
                print(f"[{now:7.2f}s] DONE", flush=True)
            elif etype == "error":
                print(f"[{now:7.2f}s] ERROR: {str(obj)[:300]}", flush=True)
except Exception as exc:
    print(f"PROBE EXCEPTION: {exc}", flush=True)

total = time.perf_counter() - t0
print(f"\n--- SUMMARY tier={tier} ---")
print(f"first_byte={first_event_t}")
print(f"first_token={first_token_t}  (gap before tokens: {first_token_t and round(first_token_t,2)})")
print(f"sources_at={sources_at and round(sources_at,2)}")
print(f"answer_chars={answer_chars}  thinking_chars={thinking_chars}")
print(f"event_counts={counts}")
print(f"total={round(total,2)}s")
answer = "".join(answer_buf)
also_count = answer.lower().count("also ")
print(f"ALSO_COUNT={also_count}")
print("===== ANSWER TEXT =====")
print(answer)
if thinking_buf:
    print("===== THINKING TEXT =====")
    print("".join(thinking_buf)[:1500])
