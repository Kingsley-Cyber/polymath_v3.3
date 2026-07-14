#!/usr/bin/env python3
"""Senior oversight sync: one deterministic pass that mimics a senior's
status sweep. Gathers every status surface, rewrites STATUS.md (live document),
and prints ALERT lines to stdout ONLY when senior attention is warranted.

Run once per cycle (a persistent monitor loops it). No LLM, no writes to any
store — reads + STATUS.md only. State between runs: /tmp/senior_oversight_state.json.

Alert lines (stdout, one per event):
  ALERT NEW_EXECUTOR_ENTRY :: <type> <first line>
  ALERT PENDING_FOR_SENIOR :: <QUESTION|BLOCKER unanswered>
  ALERT COVERAGE_FAIL :: buildline coverage checker non-zero
  ALERT BATCH_TERMINAL :: <corpus> <status>
  ALERT GATE_LOG :: <logname> EXIT=<code>
  ALERT EXECUTOR_SILENT :: no entry/commit for >45m while CP active
  ALERT NOW_POINTER_CHANGED :: <new NOW line>
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STATE = Path("/tmp/senior_oversight_state.json")
STATUS = REPO / "STATUS.md"
COORD = REPO / "COORDINATION.md"
BUILD = REPO / "BUILDLINE.md"
CHECK = REPO / "docs" / "RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md"


def sh(cmd: str, timeout: int = 30) -> str:
    try:
        return subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        ).stdout.strip()
    except Exception as exc:  # noqa: BLE001 — status sweep must never crash
        return f"UNKNOWN ({type(exc).__name__})"


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def coordination_entries() -> list[dict]:
    if not COORD.exists():
        return []
    entries = []
    for m in re.finditer(
        r"^## \[(?P<ts>[^\]]+)\] (?P<who>EXECUTOR|SENIOR|OWNER)[^:]*:: (?P<typ>\w+)\s*\n(?P<body>.*?)(?=^## \[|\Z)",
        COORD.read_text(), re.M | re.S,
    ):
        entries.append({
            "ts": m.group("ts"), "who": m.group("who"), "typ": m.group("typ"),
            "head": (m.group("body").strip().splitlines() or [""])[0][:110],
        })
    return entries


def checklist_census() -> tuple[int, int]:
    open_n = done_n = 0
    for line in CHECK.read_text().splitlines():
        s = line.lstrip()
        if s.startswith("- [ ]"):
            open_n += 1
        elif s.startswith("- [x]"):
            done_n += 1
    return open_n, done_n


def now_pointer() -> str:
    for line in BUILD.read_text().splitlines():
        if line.startswith("**NOW"):
            return line.strip("* ").strip()
    return "UNKNOWN"


def gate_logs() -> list[tuple[str, str]]:
    out = []
    for p in sorted(Path("/tmp").glob("rebatch_*.log")):
        try:
            tail = p.read_text()[-400:]
            m = re.findall(r"EXIT=(\d+)", tail)
            if m:
                out.append((p.name, m[-1]))
        except Exception:  # noqa: BLE001
            continue
    return out


def live_batches() -> str:
    code = (
        "import asyncio\n"
        "from motor.motor_asyncio import AsyncIOMotorClient\n"
        "from config import get_settings\n"
        "async def m():\n"
        "    s = get_settings(); db = AsyncIOMotorClient(s.MONGODB_URI)[s.MONGODB_DATABASE]\n"
        "    async for b in db.ingest_batches.find({'status': {'$in': ['queued','running']}}, {'corpus_id':1,'status':1,'counts':1}).limit(5):\n"
        "        print(b.get('corpus_id','?')[:8], b.get('status'), b.get('counts'))\n"
        "asyncio.run(m())\n"
    )
    r = sh(
        "docker exec -e PYTHONPATH=/app -w /app polymath_v33-backend-1 "
        f"python -W ignore -c {json.dumps(code)} 2>/dev/null"
    )
    return r or "none"


def main() -> int:
    state = load_state()
    alerts: list[str] = []
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    entries = coordination_entries()
    exec_entries = [e for e in entries if e["who"] == "EXECUTOR"]
    n_exec = len(exec_entries)
    if n_exec > state.get("exec_entries", 0):
        for e in exec_entries[state.get("exec_entries", 0):]:
            alerts.append(f"ALERT NEW_EXECUTOR_ENTRY :: {e['typ']} {e['head']}")

    # unanswered QUESTION/BLOCKER: an executor Q/B with no later senior entry
    if entries:
        last_senior_idx = max((i for i, e in enumerate(entries) if e["who"] in ("SENIOR", "OWNER")), default=-1)
        for i, e in enumerate(entries):
            if e["who"] == "EXECUTOR" and e["typ"] in ("QUESTION", "BLOCKER") and i > last_senior_idx:
                alerts.append(f"ALERT PENDING_FOR_SENIOR :: {e['typ']} {e['head']}")

    cov = subprocess.run(
        ["python3", str(REPO / "scripts" / "check_buildline_coverage.py")],
        capture_output=True, text=True,
    )
    if cov.returncode != 0:
        alerts.append("ALERT COVERAGE_FAIL :: buildline coverage checker non-zero")

    ptr = now_pointer()
    if state.get("now_pointer") and ptr != state["now_pointer"]:
        alerts.append(f"ALERT NOW_POINTER_CHANGED :: {ptr[:110]}")

    logs = gate_logs()
    prev_logs = dict(state.get("gate_logs", []))
    for name, exit_code in logs:
        if prev_logs.get(name) != exit_code:
            alerts.append(f"ALERT GATE_LOG :: {name} EXIT={exit_code}")

    head = sh(f"git -C {REPO} log --oneline -1")
    batches = live_batches()
    for line in batches.splitlines():
        parts = line.split()
        if parts and parts[0] != "none" and len(parts) >= 2:
            pass  # running batches are shown in STATUS, terminal detection below
    prev_batches = state.get("batches", "")
    if prev_batches and prev_batches != "none" and batches == "none":
        alerts.append("ALERT BATCH_TERMINAL :: previously-running batch reached a terminal state")

    last_exec_ts = state.get("last_activity_ts", time.time())
    if n_exec > state.get("exec_entries", 0) or head != state.get("git_head"):
        last_exec_ts = time.time()
    silent_min = int((time.time() - last_exec_ts) / 60)
    if silent_min > 45 and "ACTIVE" in ptr.upper():
        alerts.append(f"ALERT EXECUTOR_SILENT :: {silent_min}m without executor entry or commit")
        last_exec_ts = time.time()  # re-arm, don't spam every cycle

    open_n, done_n = checklist_census()
    pend = [a for a in alerts if "PENDING_FOR_SENIOR" in a]
    STATUS.write_text(f"""# STATUS — live oversight snapshot (generated; do not edit)

Updated: {now} · Generator: scripts/senior_status_sync.py

## Now
{ptr}

## Executor channel
Entries: {len(entries)} total ({n_exec} executor). Last executor entry:
{(exec_entries[-1]['ts'] + ' :: ' + exec_entries[-1]['typ'] + ' — ' + exec_entries[-1]['head']) if exec_entries else 'none yet'}
Pending for senior: {len(pend)}

## Repo
HEAD: {head}
Checklist census: {open_n} open / {done_n} done boxes
BUILDLINE coverage: {'OK' if cov.returncode == 0 else 'FAILING'}

## Live ingest batches
{batches}

## Gate logs (latest EXIT per /tmp/rebatch_*.log)
{chr(10).join(f'- {n}: EXIT={c}' for n, c in logs) or '- none'}

## Alerts this cycle
{chr(10).join('- ' + a for a in alerts) or '- none'}
""")

    STATE.write_text(json.dumps({
        "exec_entries": n_exec,
        "now_pointer": ptr,
        "gate_logs": logs,
        "git_head": head,
        "batches": batches,
        "last_activity_ts": last_exec_ts,
    }))

    for a in alerts:
        print(a, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
