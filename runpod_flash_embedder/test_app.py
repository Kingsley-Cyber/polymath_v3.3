from __future__ import annotations

import math

import pytest

import app


class _FakeModel:
    """Duck-typed sentence-transformers model (no torch in test envs)."""

    def __init__(self, dim: int = 4, rows: list[list[float]] | None = None):
        self.dim = dim
        self.rows = rows
        self.calls: list[tuple[list[str], dict]] = []

    def encode(self, texts, **kwargs):
        self.calls.append((list(texts), dict(kwargs)))
        if self.rows is not None:
            return self.rows
        # Unit-norm rows: what normalize_embeddings=True guarantees live.
        value = 1.0 / math.sqrt(self.dim)
        return [[value] * self.dim for _ in texts]

    def get_sentence_embedding_dimension(self) -> int:
        return self.dim


@pytest.fixture(autouse=True)
def _reset_model_cache(monkeypatch):
    monkeypatch.setattr(app, "_MODEL", None)
    yield


def _request(texts: list[str]) -> dict:
    return {"contract_version": app._CONTRACT_VERSION, "texts": texts}


def test_flash_endpoint_contract_is_burst_safe() -> None:
    remote = getattr(app.embed_texts, "__remote_config__", None)
    if remote is not None:  # real runpod_flash SDK installed
        config = remote["resource_config"].model_dump(mode="json")
        assert config["name"] == "polymath-embed-qwen3"
        assert config["workersMin"] == 0
        assert config["workersMax"] == 8
        assert config["scalerType"] == "REQUEST_COUNT"
        assert config["scalerValue"] == 1
        assert config["executionTimeoutMs"] == 600_000
        gpus = config["gpus"]
    else:  # test-stub path (backend container has no runpod_flash)
        config = app.embed_texts.__stub_endpoint_config__
        assert config["name"] == "polymath-embed-qwen3"
        assert config["workers"] == (0, 8)
        assert config["max_concurrency"] == 1
        assert config["scaler_type"] == app.ServerlessScalerType.REQUEST_COUNT
        assert config["scaler_value"] == 1
        assert config["execution_timeout_ms"] == 600_000
        gpus = [gpu.value for gpu in config["gpu"]]
    # GPU preference identical to runpod_flash_extractor/app.py.
    assert gpus == [
        "NVIDIA L4",
        "NVIDIA RTX A5000",
        "NVIDIA GeForce RTX 4090",
    ]


def test_contract_version_constant() -> None:
    assert app._CONTRACT_VERSION == "polymath.runpod_embed.v1"
    assert app._ACCEPTED_CONTRACT_VERSIONS == frozenset(
        {"polymath.runpod_embed.v1"}
    )
    assert app._MAX_TEXTS_PER_REQUEST == 256


def test_unsupported_contract_version_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(app, "_MODEL", _FakeModel())
    with pytest.raises(ValueError, match="unsupported embed contract"):
        app._handle_embed_request(
            {"contract_version": "polymath.runpod_embed.v999", "texts": ["x"]}
        )
    with pytest.raises(ValueError, match="unsupported embed contract"):
        app._handle_embed_request({"texts": ["x"]})


def test_missing_empty_or_non_string_texts_are_rejected(monkeypatch) -> None:
    monkeypatch.setattr(app, "_MODEL", _FakeModel())
    for bad_texts in (None, [], "not a list", ["ok", 7]):
        with pytest.raises(ValueError, match="non-empty list of strings"):
            app._handle_embed_request(
                {"contract_version": app._CONTRACT_VERSION, "texts": bad_texts}
            )


def test_over_cap_request_is_rejected_with_a_clear_error(monkeypatch) -> None:
    fake = _FakeModel()
    monkeypatch.setattr(app, "_MODEL", fake)
    with pytest.raises(ValueError, match=r"capped at 256; got 257"):
        app._handle_embed_request(_request(["t"] * 257))
    # Rejected BEFORE any GPU work.
    assert fake.calls == []
    # Exactly at the cap is accepted.
    result = app._handle_embed_request(_request(["t"] * 256))
    assert len(result["vectors"]) == 256


def test_encode_uses_the_reference_normalization_contract(monkeypatch) -> None:
    """The compatibility invariant: the exact encode call the local sidecar
    (embedder/main.py) and modal_embedder.py use — normalize_embeddings=True,
    no prompt/instruction kwargs."""
    fake = _FakeModel(dim=4)
    monkeypatch.setattr(app, "_MODEL", fake)

    result = app._handle_embed_request(_request(["alpha", "beta"]))

    assert len(fake.calls) == 1
    texts, kwargs = fake.calls[0]
    assert texts == ["alpha", "beta"]
    assert kwargs == {
        "batch_size": app._ENCODE_BATCH_SIZE,
        "normalize_embeddings": True,
        "show_progress_bar": False,
    }
    # No prompt_name / instruction prefixes may ever sneak in.
    assert "prompt_name" not in kwargs and "prompt" not in kwargs
    # Rows come back unit-normalized (what the flag guarantees live).
    for row in result["vectors"]:
        assert math.isclose(math.fsum(x * x for x in row), 1.0, rel_tol=1e-9)


def test_response_shape_and_contract_fields(monkeypatch) -> None:
    monkeypatch.setattr(app, "_MODEL", _FakeModel(dim=4))

    result = app._handle_embed_request(_request(["one", "two", "three"]))

    assert result["contract_version"] == "polymath.runpod_embed.v1"
    assert result["model"] == app._MODEL_ID
    assert result["dim"] == 4
    assert len(result["vectors"]) == 3
    for row in result["vectors"]:
        assert len(row) == 4
        assert all(isinstance(x, float) for x in row)
    assert result["metrics"]["texts"] == 3


def test_row_count_contract_violation_raises(monkeypatch) -> None:
    fake = _FakeModel(dim=4, rows=[[0.5, 0.5, 0.5, 0.5]])  # 1 row for 2 texts
    monkeypatch.setattr(app, "_MODEL", fake)
    with pytest.raises(ValueError, match="1 vectors for 2 texts"):
        app._handle_embed_request(_request(["one", "two"]))


def test_dimension_contract_violation_raises(monkeypatch) -> None:
    fake = _FakeModel(dim=4, rows=[[0.5, 0.5]])  # 2-dim row, model says 4
    monkeypatch.setattr(app, "_MODEL", fake)
    with pytest.raises(ValueError, match="2-dim vector; expected 4"):
        app._handle_embed_request(_request(["one"]))


def test_model_is_loaded_once_and_kept_as_a_module_global(monkeypatch) -> None:
    loads: list[int] = []

    def fake_load():
        loads.append(1)
        return _FakeModel(dim=4)

    monkeypatch.setattr(app, "_load_model", fake_load)

    app._handle_embed_request(_request(["one"]))
    app._handle_embed_request(_request(["two"]))

    assert len(loads) == 1
