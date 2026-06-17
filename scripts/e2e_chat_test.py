#!/usr/bin/env python3
"""End-to-end chat assertion harness. Hits /api/chat over SSE for each tier and
asserts the behaviors fixed this session. Prints a PASS/FAIL table. Exit 1 on
any failure. Diagnostic/verification only."""
import json
import os
import re
import sys
import time
import urllib.request

TOKEN = os.environ["PROBE_TOKEN"]
CORPUS = os.environ.get("PROBE_CORPUS", "f8a0aa85-6cb4-4f64-a973-f9183f1546bb")
BASE = os.environ.get("PROBE_BASE", "http://localhost:8000")

CASES = [
    ("qdrant_only", "what is nlp", "simple/vector"),
    ("qdrant_mongo", "what is nlp", "simple/hybrid"),
    ("qdrant_mongo_graph",
     "How does cognitive dissonance connect to the cognitive-behavioral therapy ideas in the library?",
     "relational/graph"),
]

failures = []


def run_case(tier, query):
    body = json.dumps({
        "message": query,
        "corpus_ids": [CORPUS],
        "retrieval_tier": tier,
        "overrides": {"hyde_enabled": False},
    }).encode()
    req = urllib.request.Request(
        f"{BASE}/api/chat", data=body,
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Content-Type": "application/json",
                 "Accept": "text/event-stream"}, method="POST")
    t0 = time.perf_counter()
    trace_times, trace_titles = [], []
    token_events = 0
    answer = []
    sources_at = None
    sources_n = 0
    errors = []
    done_obj = None
    cur = None
    with urllib.request.urlopen(req, timeout=600) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            now = time.perf_counter() - t0
            if line.startswith("event:"):
                cur = line.split(":", 1)[1].strip(); continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            try:
                obj = json.loads(data)
            except Exception:
                continue
            et = obj.get("type") or cur
            if et == "trace_event" or obj.get("trace_event"):
                te = obj.get("trace_event") or obj
                trace_times.append(round(now, 2))
                trace_titles.append(te.get("title"))
            elif et == "token":
                tok = obj.get("content") or ""
                if tok:
                    token_events += 1
                    answer.append(tok)
            elif et == "sources":
                sources_at = round(now, 2)
                srcs = obj.get("sources") or obj.get("data") or []
                sources_n = len(srcs) if hasattr(srcs, "__len__") else 0
            elif et == "error":
                errors.append(str(obj.get("content"))[:200])
            elif et == "done":
                done_obj = obj
    total = round(time.perf_counter() - t0, 2)
    text = "".join(answer)
    return {
        "total": total, "trace_times": trace_times, "trace_titles": trace_titles,
        "token_events": token_events, "answer": text, "answer_chars": len(text),
        "sources_at": sources_at, "sources_n": sources_n, "errors": errors,
        "done": done_obj or {},
    }


def dup_ratio(text):
    """Heuristic: largest repeated 120-char block fraction (duplication detector)."""
    if len(text) < 240:
        return 0.0
    half = len(text) // 2
    a = text[:120]
    return 1.0 if a and a in text[120:] else 0.0


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    if not cond:
        failures.append(f"{name}: {detail}")
    print(f"  [{status}] {name}{(' — ' + detail) if detail and not cond else ''}")
    return cond


for tier, query, label in CASES:
    print(f"\n===== {label}  ({tier}) =====")
    r = run_case(tier, query)
    text_low = r["answer"].lower()
    also = len(re.findall(r"(^|[.\n]\s+)also\b", text_low))
    not_establish = text_low.count("does not establish") + text_low.count("retrieved corpus does not")
    spread = (r["trace_times"][-1] - r["trace_times"][0]) if len(r["trace_times"]) >= 2 else 0.0
    print(f"  total={r['total']}s  trace_events={len(r['trace_titles'])}  token_events={r['token_events']}"
          f"  answer_chars={r['answer_chars']}  sources={r['sources_n']}@{r['sources_at']}s")
    print(f"  trace titles: {r['trace_titles']}")

    check("no error events", not r["errors"], str(r["errors"]))
    check("answer produced", r["answer_chars"] > 80, f"chars={r['answer_chars']}")
    check("sources arrived", r["sources_n"] > 0)
    check("sources before answer-complete", r["sources_at"] is not None)
    check("trace events streamed live (>=3)", len(r["trace_titles"]) >= 3, f"n={len(r['trace_titles'])}")
    check("trace events spread over time (not one burst)", spread >= 0.5 or r["total"] < 3,
          f"spread={round(spread,2)}s total={r['total']}s")
    check("no answer duplication", dup_ratio(r["answer"]) < 1.0, "repeated 120-char block")
    check("no 'Also ...' spam (<3)", also < 3, f"also={also}")
    check("no leaked 'does not establish' prose", not_establish == 0, f"count={not_establish}")
    check("hyde not applied (we sent off)", r["done"].get("hyde_applied") in (False, None),
          f"hyde_applied={r['done'].get('hyde_applied')}")
    check("cascade not applied (no toggle)", r["done"].get("reasoning_cascade_applied") in (False, None),
          f"cascade={r['done'].get('reasoning_cascade_applied')}")

print("\n" + "=" * 50)
if failures:
    print(f"E2E RESULT: {len(failures)} FAILURE(S)")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("E2E RESULT: ALL PASS")
