#!/usr/bin/env python3
"""Per-stage latency breakdown for one query+model. Diagnostic only.
Usage: PROBE_TOKEN=... python3 scripts/latency_probe.py <tier> <model> "<query>"
"""
import json, os, sys, time, urllib.request

TOKEN = os.environ["PROBE_TOKEN"]
CORPUS = os.environ.get("PROBE_CORPUS", "f8a0aa85-6cb4-4f64-a973-f9183f1546bb")
BASE = os.environ.get("PROBE_BASE", "http://localhost:8000")
tier = sys.argv[1] if len(sys.argv) > 1 else "qdrant_mongo"
model = sys.argv[2] if len(sys.argv) > 2 else ""
query = sys.argv[3] if len(sys.argv) > 3 else "what is nlp"

body = {"message": query, "corpus_ids": [CORPUS], "retrieval_tier": tier}
if model:
    body["overrides"] = {"model": model}
req = urllib.request.Request(
    f"{BASE}/api/chat", data=json.dumps(body).encode(),
    headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json",
             "Accept": "text/event-stream"}, method="POST")

t0 = time.perf_counter()
marks = {}
def mark(name):
    if name not in marks:
        marks[name] = time.perf_counter() - t0
think_chars = ans_chars = 0
think_events = ans_events = 0
last_trace = None

with urllib.request.urlopen(req, timeout=600) as resp:
    cur = None
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\n")
        now = time.perf_counter() - t0
        if line.startswith("event:"):
            cur = line.split(":", 1)[1].strip(); continue
        if not line.startswith("data:"): continue
        data = line[5:].strip()
        if not data: continue
        try: obj = json.loads(data)
        except Exception: continue
        et = obj.get("type") or cur
        if obj.get("trace_event"):
            te = obj["trace_event"]; last_trace = (round(now,2), te.get("title"))
        if et == "sources":
            mark("retrieval_done (sources)")
        elif et == "thinking":
            mark("first_thinking"); think_events += 1
            think_chars += len(obj.get("thinking") or "")
            marks["last_thinking"] = now
        elif et == "token":
            tok = obj.get("content") or ""
            if tok:
                mark("first_answer_token"); ans_events += 1; ans_chars += len(tok)
        elif et == "done":
            mark("done")

total = time.perf_counter() - t0
print(f"\n===== LATENCY BREAKDOWN  tier={tier}  model={model or '(default)'} =====")
order = ["retrieval_done (sources)", "first_thinking", "last_thinking",
         "first_answer_token", "done"]
prev = 0.0
for k in order:
    if k in marks:
        dt = marks[k] - prev
        print(f"  {marks[k]:7.2f}s   (+{dt:6.2f}s)  {k}")
        prev = marks[k]
print(f"\n  thinking: {think_chars} chars in {think_events} events")
print(f"  answer:   {ans_chars} chars in {ans_events} events")
print(f"  TOTAL:    {total:.2f}s")
# derived stages
r = marks.get("retrieval_done (sources)")
ft = marks.get("first_thinking"); fa = marks.get("first_answer_token"); dn = marks.get("done")
if r is not None:
    print(f"\n  STAGE 1 retrieval+prework : {r:.2f}s")
    gen_start = r
    if ft is not None and (fa is None or ft < fa):
        think_end = marks.get("last_thinking", fa or dn)
        print(f"  STAGE 2 model thinking    : {(think_end - gen_start):.2f}s  ({think_chars} chars)")
        if fa is not None:
            print(f"  STAGE 3 answer streaming  : {(dn - fa):.2f}s  ({ans_chars} chars)")
    elif fa is not None:
        print(f"  STAGE 2 model TTFT (no separated thinking): {(fa - gen_start):.2f}s")
        print(f"  STAGE 3 answer streaming  : {(dn - fa):.2f}s  ({ans_chars} chars)")
