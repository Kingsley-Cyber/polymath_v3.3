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


def test_infer_sharded_orders_and_distributes_across_pool(monkeypatch):
    from services.extraction import relex_sidecar_client as client

    calls = []

    def fake_infer(texts, *, base_url=None, **_kwargs):
        calls.append((base_url, tuple(texts)))
        return [f"r:{t}" for t in texts]

    monkeypatch.setattr(client, "infer", fake_infer)
    monkeypatch.setattr(
        client, "sidecar_pool", lambda: ("http://a:8737", "http://b:8738")
    )

    out = client.infer_sharded([f"t{i}" for i in range(7)], batch_size=2)

    assert out == [f"r:t{i}" for i in range(7)]  # order preserved exactly
    assert {base for base, _ in calls} == {"http://a:8737", "http://b:8738"}


def test_infer_sharded_single_pool_degrades_to_serial(monkeypatch):
    from services.extraction import relex_sidecar_client as client

    bases = []

    def fake_infer(texts, *, base_url=None, **_kwargs):
        bases.append(base_url)
        return [f"r:{t}" for t in texts]

    monkeypatch.setattr(client, "infer", fake_infer)
    monkeypatch.setattr(client, "sidecar_pool", lambda: ("http://a:8737",))

    out = client.infer_sharded(["x", "y", "z"], batch_size=2)

    assert out == ["r:x", "r:y", "r:z"]
    assert bases == [None, None]  # serial path, default url resolution


def test_infer_sharded_fails_over_once(monkeypatch):
    from services.extraction import relex_sidecar_client as client

    attempts = []

    def fake_infer(texts, *, base_url=None, **_kwargs):
        attempts.append(base_url)
        if base_url == "http://a:8737":
            raise client.RelexSidecarError("replica down")
        return [f"r:{t}" for t in texts]

    monkeypatch.setattr(client, "infer", fake_infer)
    monkeypatch.setattr(
        client, "sidecar_pool", lambda: ("http://a:8737", "http://b:8738")
    )

    out = client.infer_sharded(["x"], batch_size=2)

    assert out == ["r:x"]
    assert attempts == ["http://a:8737", "http://b:8738"]
