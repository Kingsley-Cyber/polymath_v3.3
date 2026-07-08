"""Canary + report-card decision logic (pure): validates the two burn modes."""
import __future__, ast, os, sys
SRC = os.path.join(os.path.dirname(__file__), "..", "services", "ingestion", "batches.py")
src = open(SRC).read()

def test_canary_covers_the_two_burn_modes():
    assert "returned EMPTY content" in src            # thinking-mode empties
    assert "output unparseable" in src                # junk output
    assert "no summary model configured" in src       # missing config
    assert 'setdefault("thinking", {"type": "disabled"})' in src  # v4 injection

def test_report_card_accounts_fallbacks():
    assert "summary_fallback_rate" in src and "structure_rate" in src
    assert "alert" in src and "slow-motion data loss" in src

def test_canary_gates_before_workers():
    i_canary = src.find("_preflight_summary_canary(db, batch)")
    i_workers = src.find("await asyncio.gather", i_canary)
    assert 0 < i_canary < i_workers

def _run_all():
    fails = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            try: v(); print(f"PASS {k}")
            except Exception as e: fails += 1; print(f"FAIL {k}: {e!r}")
    print(f"\n{3-fails}/3 passed"); return fails

if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
