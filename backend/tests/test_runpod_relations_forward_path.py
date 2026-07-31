"""The RunPod lane must not emit `relations=[]` any more.

MEASURED 2026-07-30: 357,846 of 362,759 chunks (98.6% of the corpus) held ZERO
relations, and every one came from this lane, because `_compile_result`
hardcoded `relations=[]`. The backfill repaired history; this guards the FORWARD
path so newly ingested chunks are not empty again.

The fix needs no wire-contract change and no new pod image: relations are a pure
function of the chunk text plus the span-validated entities the pod already
returns, and _compile_result already runs backend-side with a version-locked
en_core_web_sm. Computing there also guarantees the forward path and the
backfill run identical code, so a chunk cannot get different relations depending
on when it happened to be ingested.

Host venue (needs config/*.yaml): local_ghost_b/.venv.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from services.runpod_local_extraction import _compile_relations


@dataclass
class _Ent:
    """Minimal stand-in for a LocalExtractionV1 entity item."""

    text: str
    start_char: int
    end_char: int
    entity_type: str
    canonical_label: str = ""


def test_forward_path_emits_relations_not_an_empty_list():
    """The regression that cost 98.6% of the corpus its relations."""
    text = "Microsoft acquired GitHub in 2018."
    ents = [
        _Ent("Microsoft", 0, 9, "ORGANIZATION"),
        _Ent("GitHub", 19, 25, "SOFTWARE"),
    ]
    out = _compile_relations(text, ents, "chunk-1")
    assert out, "forward path emitted no relations — the hardcode is back"
    triples = [(r.subject, r.predicate, r.object) for r in out]
    assert ("Microsoft", "owns", "GitHub") in triples


def test_uppercase_entity_types_are_normalized_not_gated_out():
    """The pod stores UPPERCASE types; ontology allowed_pairs is Title Case.

    Without normalization at the adapter boundary these would fail every typed
    constraint. This asserts the forward path goes through that boundary.
    """
    text = "GitHub was acquired by Microsoft."
    ents = [
        _Ent("GitHub", 0, 6, "SOFTWARE"),
        _Ent("Microsoft", 23, 32, "ORGANIZATION"),
    ]
    out = _compile_relations(text, ents, "chunk-2")
    assert [(r.subject, r.predicate, r.object) for r in out] == [
        ("Microsoft", "owns", "GitHub")
    ]


def test_relations_carry_evidence_and_confidence():
    text = "Microsoft acquired GitHub in 2018."
    ents = [
        _Ent("Microsoft", 0, 9, "ORGANIZATION"),
        _Ent("GitHub", 19, 25, "SOFTWARE"),
    ]
    r = _compile_relations(text, ents, "chunk-3")[0]
    assert r.object_kind == "entity"
    assert r.confidence > 0
    assert "Microsoft" in r.evidence_phrase


@pytest.mark.parametrize("text,ents", [
    ("", [_Ent("A", 0, 1, "CONCEPT"), _Ent("B", 2, 3, "CONCEPT")]),
    ("   ", [_Ent("A", 0, 1, "CONCEPT"), _Ent("B", 2, 3, "CONCEPT")]),
    ("Some text here.", [_Ent("A", 0, 1, "CONCEPT")]),   # <2 entities
    ("Some text here.", []),
])
def test_degenerate_inputs_return_empty_without_raising(text, ents):
    """A chunk that cannot produce relations must not fail the ingest."""
    assert _compile_relations(text, ents, "chunk-x") == []


def test_extraction_fault_is_fail_soft_but_logged(caplog):
    """A relation fault must not fail an otherwise good ingest — but must be
    LOUD, or it silently reinstates the empty-relations bug it replaced."""
    class Exploding:
        text = "x"
        start_char = 0
        end_char = 1
        entity_type = "CONCEPT"

        @property
        def canonical_label(self):  # noqa: D401
            raise RuntimeError("boom")

    with caplog.at_level("ERROR"):
        out = _compile_relations("Some sentence here.", [Exploding(), Exploding()],
                                 "chunk-boom")
    assert out == []
    assert any("relation compilation FAILED" in r.message for r in caplog.records), (
        "a relation fault was swallowed silently"
    )


def test_no_hardcoded_empty_relations_remains():
    """Source-level lock on the exact regression."""
    from pathlib import Path
    src = (
        Path(__file__).resolve().parents[1]
        / "services" / "runpod_local_extraction.py"
    ).read_text()
    code = "\n".join(line.split("#")[0] for line in src.splitlines())
    assert "relations=[]," not in code, (
        "runpod_local_extraction is hardcoding relations=[] again"
    )
    assert "relations=relations," in code


# ---------------------------------------------------------------------------
# facts=[] was the SAME bug as relations=[] — Stage D never ran on this lane.
# MEASURED 2026-07-31: all six RunPod corpora held 0 facts across 362,142
# chunks, while the one locally-extracted corpus held 1.0065 facts/chunk.
# ---------------------------------------------------------------------------


def test_forward_path_emits_facts_not_an_empty_list():
    from services.ghost_b import EntityItem
    from services.runpod_local_extraction import _compile_facts

    text = "The server has 64 GB of RAM and costs $1,200 per month."
    ents = [EntityItem(canonical_name="server", surface_form="server",
                       entity_type="Software", confidence=0.9)]
    out = _compile_facts(text, ents, "chunk-f1")
    assert out, "Stage D emitted nothing — the facts=[] hardcode is back"
    assert all(f.subject for f in out)
    assert all(f.evidence_phrase for f in out)


def test_fact_confidence_sentinels_match_ghost_b_local():
    """1.0 deterministic, 0.9 qualitative — must not drift between lanes."""
    from services.ghost_b import EntityItem
    from services.runpod_local_extraction import _compile_facts

    text = "The server has 64 GB of RAM. Operators must restart the server nightly."
    ents = [EntityItem(canonical_name="server", surface_form="server",
                       entity_type="Software", confidence=0.9)]
    out = _compile_facts(text, ents, "chunk-f2")
    assert out
    assert all(f.confidence in (1.0, 0.9) for f in out), (
        f"unexpected confidence values: {sorted({f.confidence for f in out})}"
    )


def test_fact_degenerate_inputs_do_not_raise():
    from services.runpod_local_extraction import _compile_facts
    assert _compile_facts("", [], "c") == []
    assert _compile_facts("   ", [], "c") == []


def test_no_hardcoded_empty_facts_remains():
    from pathlib import Path
    src = (
        Path(__file__).resolve().parents[1]
        / "services" / "runpod_local_extraction.py"
    ).read_text()
    code = "\n".join(line.split("#")[0] for line in src.splitlines())
    assert "facts=[]," not in code, "runpod lane is hardcoding facts=[] again"
    assert "facts=facts," in code
