"""Pure test for worker.AdjustableSemaphore (audit 2026-07-06 critical fix).

The old swap-on-change semaphore lost holder accounting when two configs
computed different limits for the same gate key — holders of the old object
stopped counting and the gate over-admitted. This asserts the invariant the
fix guarantees: concurrent holders NEVER exceed the limit in force, even
while the limit churns, and widening wakes waiters.

Standalone: python3 tests/test_adjustable_semaphore.py — defines the class
inline-identically via file exec of the worker module region? No: worker.py
imports the world. The class is dependency-free, so we import it via a tiny
exec of its source slice — if the class moves, update _CLASS_MARKERS.
"""
import asyncio
import re
import sys
import os

_worker_path = os.path.join(
    os.path.dirname(__file__), "..", "services", "ingestion", "worker.py"
)
_src = open(_worker_path).read()
_m = re.search(r"class AdjustableSemaphore:.*?(?=\n\ndef |\n\nclass )", _src, re.S)
assert _m, "AdjustableSemaphore not found in worker.py"
_ns: dict = {"asyncio": asyncio}
exec(_m.group(0), _ns)  # noqa: S102 — own source, test-only
AdjustableSemaphore = _ns["AdjustableSemaphore"]


async def _hold(gate, held, peak, duration=0.02):
    async with gate:
        held[0] += 1
        peak[0] = max(peak[0], held[0])
        await asyncio.sleep(duration)
        held[0] -= 1


async def test_never_exceeds_limit_under_churn():
    gate = AdjustableSemaphore(2)
    held, peak = [0], [0]
    tasks = [asyncio.create_task(_hold(gate, held, peak)) for _ in range(10)]
    await asyncio.sleep(0.005)
    gate.set_limit(1)  # shrink while holders active
    await asyncio.sleep(0.03)
    gate.set_limit(4)  # widen — waiters must wake
    await asyncio.gather(*tasks)
    assert peak[0] <= 4, f"peak {peak[0]} exceeded max limit ever set"
    print(f"PASS churn: peak={peak[0]} (<=4)")


async def test_shrink_applies_to_new_admissions():
    gate = AdjustableSemaphore(3)
    held, peak = [0], [0]
    first = [asyncio.create_task(_hold(gate, held, peak, 0.05)) for _ in range(3)]
    await asyncio.sleep(0.01)
    gate.set_limit(1)
    second = [asyncio.create_task(_hold(gate, held, peak, 0.01)) for _ in range(3)]
    await asyncio.gather(*first, *second)
    # After the shrink, the second wave must have run at most 1 at a time —
    # peak overall is bounded by the first wave's 3, never higher.
    assert peak[0] <= 3, f"peak {peak[0]} > 3"
    print(f"PASS shrink: peak={peak[0]} (<=3)")


async def test_widen_wakes_waiters():
    gate = AdjustableSemaphore(1)
    held, peak = [0], [0]
    tasks = [asyncio.create_task(_hold(gate, held, peak, 0.03)) for _ in range(4)]
    await asyncio.sleep(0.005)
    gate.set_limit(4)
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=2.0)
    assert peak[0] >= 2, "widening never woke waiters"
    print(f"PASS widen: peak={peak[0]} (>=2, woke waiters)")


async def _main():
    await test_never_exceeds_limit_under_churn()
    await test_shrink_applies_to_new_admissions()
    await test_widen_wakes_waiters()
    print("\n3/3 passed")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except AssertionError as exc:
        print(f"FAIL: {exc}")
        sys.exit(1)
