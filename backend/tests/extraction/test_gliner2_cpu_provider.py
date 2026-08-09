from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.extraction import gliner2_cpu_provider as provider_module


class FakeTensor:
    def __init__(self, device: str = "cpu") -> None:
        self.device = SimpleNamespace(type=device)


class FakeSchema:
    def __init__(self) -> None:
        self.entity_types = None

    def entities(self, entity_types):
        self.entity_types = entity_types
        return self


class FakeModel:
    def __init__(self, device: str = "cpu") -> None:
        self.tensor = FakeTensor(device)
        self.schemas: list[FakeSchema] = []

    def named_parameters(self):
        return [("weight", self.tensor)]

    def named_buffers(self):
        return [("position", self.tensor)]

    def create_schema(self):
        schema = FakeSchema()
        self.schemas.append(schema)
        return schema

    def batch_extract(self, texts, schema, **kwargs):
        assert schema.entity_types == provider_module.ENTITY_DESCRIPTIONS
        return [
            {"entities": {"Software": [{
                "text": text[:8], "start": 0, "end": 8, "confidence": 0.99,
            }]}}
            for text in texts
        ]


@pytest.fixture(autouse=True)
def reset_provider():
    provider_module._reset_provider_for_tests()
    yield
    provider_module._reset_provider_for_tests()


def test_process_wide_model_loads_once_and_provider_preserves_order() -> None:
    loads = []

    def loader():
        loads.append(1)
        return FakeModel()

    first = provider_module.GLiNER2CPUProvider(loader=loader)
    second = provider_module.GLiNER2CPUProvider(loader=loader)
    rows = first.predict_entities(["Polymath one", "Graphify two"])
    second.predict_entities(["Polymath three"])
    second.health()
    assert len(loads) == 1
    assert len(first._model().schemas) == 1
    assert first.load_count == second.load_count == 1
    assert [row[0].text for row in rows] == ["Polymath", "Graphify"]
    assert all(row[0].entity_type == "software" for row in rows)


@pytest.mark.parametrize("device", ["cuda", "mps", "mlx", "gpu"])
def test_non_cpu_parameter_fails_closed(device: str) -> None:
    provider = provider_module.GLiNER2CPUProvider(loader=lambda: FakeModel(device))
    with pytest.raises(RuntimeError, match="non-CPU"):
        provider.health()


def test_span_mismatch_reanchors_or_passes_through() -> None:
    # Contract revision (owner doctrine, 2026-08-07): a misaligned model span
    # re-anchors on a unique exact occurrence; otherwise the emission passes
    # through VERBATIM and the census persists it as an ALIGNMENT_FAILURE
    # mention — an incoherent emission never crashes a corpus run and never
    # silently disappears.
    model = FakeModel()

    def invalid_batch(texts, schema, **kwargs):
        return [{"entities": {"Software": [
            {"text": "text", "start": 0, "end": 4, "confidence": 1.0},
            {"text": "wrong", "start": 0, "end": 5, "confidence": 1.0},
        ]}}]

    model.batch_extract = invalid_batch
    provider = provider_module.GLiNER2CPUProvider(loader=lambda: model)
    rows = provider.predict_entities(["right text"])
    by_surface = {item.text: item for item in rows[0]}
    assert (by_surface["text"].start, by_surface["text"].end) == (6, 10)  # re-anchored
    assert (by_surface["wrong"].start, by_surface["wrong"].end) == (0, 5)  # verbatim


def test_release_identity_is_pinned_and_hashed() -> None:
    assert provider_module.MODEL_ID == "fastino/gliner2-base-v1"
    assert len(provider_module.MODEL_REVISION) == 40
    assert len(provider_module.MODEL_CHECKPOINT_SHA256) == 64
    assert len(provider_module.description_hash()) == 64
    assert len(provider_module.provider_release_hash()) == 64
    assert provider_module.DEFAULT_BATCH_SIZE == 4
