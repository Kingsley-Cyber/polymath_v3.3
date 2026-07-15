from __future__ import annotations

import app


def test_custom_image_handler_preserves_flash_payload_envelope(monkeypatch):
    observed = []

    def fake_extract(payload):
        observed.append(payload)
        return {"contract_version": "polymath.runpod_local_extraction.v1"}

    monkeypatch.setattr(app, "extract_batch", fake_extract)
    result = app.handle_serverless_job({"input": {"payload": {"batch_id": "b1"}}})
    assert result == {"contract_version": "polymath.runpod_local_extraction.v1"}
    assert observed == [{"batch_id": "b1"}]


def test_custom_image_handler_rejects_malformed_envelopes(monkeypatch):
    monkeypatch.setattr(
        app,
        "extract_batch",
        lambda _payload: (_ for _ in ()).throw(AssertionError("must not execute")),
    )
    cases = [
        None,
        {},
        {"input": []},
        {"input": {}},
        {"input": {"payload": []}},
        {"input": {"payload": {}, "extra": True}},
    ]
    for job in cases:
        result = app.handle_serverless_job(job)
        assert result["success"] is False
        assert result["error_code"].startswith("invalid_")


def test_custom_image_handler_surfaces_contract_rejection(monkeypatch):
    def reject(_payload):
        raise ValueError("unsupported extraction contract")

    monkeypatch.setattr(app, "extract_batch", reject)
    result = app.handle_serverless_job({"input": {"payload": {}}})
    assert result == {
        "success": False,
        "error_code": "extraction_contract_rejected",
        "error": "ValueError: unsupported extraction contract",
    }
