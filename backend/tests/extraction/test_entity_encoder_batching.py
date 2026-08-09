"""Relex transport batching: predict_joint must bound each /infer call.

A whole-book census (hundreds of windows) can never ride ONE sidecar call —
the call would exceed any sane HTTP timeout. Batching is transport-level
only: the sidecar scores each text independently, so outputs must be
byte-identical to the single-call ordering.
"""

from __future__ import annotations

from services.extraction import entity_encoder
from services.extraction.relex_sidecar_client import RelexResult


def _fake_infer_factory(calls):
    def fake_infer(texts, *, entity_labels=None, relation_labels=None, **kw):
        calls.append(list(texts))
        out = []
        for text in texts:
            out.append(RelexResult(
                entities=({"text": text[:4], "label": (entity_labels or ["concept"])[0],
                           "start": 0, "end": 4, "score": 0.9},),
                relations=(),
            ))
        return out
    return fake_infer


def test_predict_joint_bounds_each_infer_call(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "services.extraction.relex_sidecar_client.infer", _fake_infer_factory(calls),
    )
    monkeypatch.setenv("RELEX_INFER_BATCH", "4")
    provider = entity_encoder.RelexSidecarEntityProvider()
    texts = [f"text{i:04d} content here" for i in range(11)]
    entity_rows, relation_rows = provider.predict_joint(texts)
    assert len(entity_rows) == 11 and len(relation_rows) == 11
    assert [len(c) for c in calls] == [4, 4, 3]
    # order preserved: row i derives from text i
    for i, row in enumerate(entity_rows):
        assert row and row[0].text == texts[i][:4]


def test_predict_joint_single_call_when_under_batch(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "services.extraction.relex_sidecar_client.infer", _fake_infer_factory(calls),
    )
    monkeypatch.delenv("RELEX_INFER_BATCH", raising=False)
    provider = entity_encoder.RelexSidecarEntityProvider()
    entity_rows, _ = provider.predict_joint(["alpha text", "beta text"])
    assert len(calls) == 1 and len(entity_rows) == 2
