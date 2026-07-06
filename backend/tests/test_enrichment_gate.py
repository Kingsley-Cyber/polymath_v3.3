"""Pure tests for the E1 enrichment gate (§13-H Fast Local Graph + RTX).

Runnable standalone: python3 tests/test_enrichment_gate.py — file-path load,
duck-typed stubs, non-zero exit on failure.
"""
import importlib.util
import os
import sys
from dataclasses import dataclass, field

_spec = importlib.util.spec_from_file_location(
    "enrichment_gate",
    os.path.join(
        os.path.dirname(__file__), "..", "services", "ingestion", "enrichment_gate.py"
    ),
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)
enrichment_verdict = _mod.enrichment_verdict
select_enrichment_tasks = _mod.select_enrichment_tasks


@dataclass
class Task:
    chunk_id: str


@dataclass
class Rel:
    predicate: str


@dataclass
class Result:
    chunk_id: str
    entities: list = field(default_factory=list)
    relations: list = field(default_factory=list)
    facts: list = field(default_factory=list)


def _metrics(requested=10, extracted=10, facts=15, relations=20, related_to=2):
    return {
        "requested_chunks": requested,
        "extracted_chunks": extracted,
        "fact_count": facts,
        "relation_count": relations,
        "related_to_count": related_to,
    }


# ── verdict ────────────────────────────────────────────────────────────────

def test_healthy_local_pass_skips_enrichment():
    v = enrichment_verdict(_metrics())
    assert not v.enrich and v.reasons == ()


def test_low_coverage_triggers():
    v = enrichment_verdict(_metrics(requested=100, extracted=60))
    assert v.enrich and any(r.startswith("coverage") for r in v.reasons)


def test_thin_facts_triggers():
    v = enrichment_verdict(_metrics(facts=3))  # 0.3/chunk < 1.0
    assert v.enrich and any(r.startswith("facts/chunk") for r in v.reasons)


def test_related_to_heavy_triggers():
    v = enrichment_verdict(_metrics(relations=20, related_to=12))  # 60% > 40%
    assert v.enrich and any(r.startswith("related_to") for r in v.reasons)


def test_empty_or_missing_metrics_fail_toward_enrichment_but_only_via_real_signals():
    # No requested chunks at all -> nothing to enrich, no reasons.
    v = enrichment_verdict({})
    assert not v.enrich
    # Requested but nothing extracted -> coverage reason fires.
    v = enrichment_verdict({"requested_chunks": 10})
    assert v.enrich and any(r.startswith("coverage") for r in v.reasons)


def test_thresholds_are_tunable():
    v = enrichment_verdict(_metrics(facts=3), min_facts_per_chunk=0.2)
    assert not v.enrich


# ── selection ──────────────────────────────────────────────────────────────

def _gap_setup():
    tasks = [Task(f"c{i}") for i in range(10)]
    # c0..c6 have results; c7..c9 are gaps. c1 is empty. c2 is generic-heavy.
    results = [
        Result("c0", entities=["e"], relations=[Rel("uses")], facts=["f"]),
        Result("c1"),  # empty
        Result("c2", entities=["e"], relations=[Rel("related_to"), Rel("related_to"), Rel("uses")]),
        Result("c3", entities=["e"], relations=[Rel("part_of")], facts=[]),
        Result("c4", entities=["e"], relations=[Rel("uses")], facts=["f"]),
        Result("c5", entities=["e"], relations=[Rel("uses")], facts=["f"]),
        Result("c6", entities=["e"], relations=[Rel("uses")], facts=["f"]),
    ]
    return tasks, results


def test_gaps_selected_first_in_task_order():
    tasks, results = _gap_setup()
    v = enrichment_verdict(_metrics(requested=10, extracted=7))
    picks = select_enrichment_tasks(tasks, results, [], v)
    assert [t.chunk_id for t in picks[:4]] == ["c7", "c8", "c9", "c1"]


def test_related_to_targeting_only_when_reason_tripped():
    tasks, results = _gap_setup()
    # coverage fine, facts fine, related_to heavy verdict
    v = enrichment_verdict(_metrics(relations=20, related_to=12, facts=15))
    picks = select_enrichment_tasks(tasks, results, [], v)
    ids = [t.chunk_id for t in picks]
    assert "c2" in ids  # 2/3 generic
    assert "c0" not in ids  # healthy chunk untouched


def test_fact_thin_targeting_only_when_reason_tripped():
    tasks, results = _gap_setup()
    v = enrichment_verdict(_metrics(facts=3))  # facts reason only
    # widen the cap so band 4 is fully observable past the gap/empty picks
    picks = select_enrichment_tasks(tasks, results, [], v, max_chunk_ratio=0.8)
    ids = [t.chunk_id for t in picks]
    assert "c3" in ids  # zero facts
    assert "c4" not in ids


def test_cap_bounds_selection():
    tasks = [Task(f"c{i}") for i in range(100)]
    v = enrichment_verdict(_metrics(requested=100, extracted=0))
    picks = select_enrichment_tasks(tasks, [], [], v, max_chunk_ratio=0.25)
    assert len(picks) == 25


def test_no_enrich_verdict_selects_nothing():
    tasks, results = _gap_setup()
    v = enrichment_verdict(_metrics())
    assert select_enrichment_tasks(tasks, results, [], v) == []


def test_selection_deterministic():
    tasks, results = _gap_setup()
    v = enrichment_verdict(_metrics(requested=10, extracted=7, facts=1))
    a = [t.chunk_id for t in select_enrichment_tasks(tasks, results, [], v)]
    b = [t.chunk_id for t in select_enrichment_tasks(tasks, results, [], v)]
    assert a == b


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
