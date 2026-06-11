"""Tests for extraction endpoint deploy-readiness validation + lister guard."""

from __future__ import annotations

import asyncio
from pathlib import Path

from services.extraction_validation import _evaluate, validate_endpoints
from services.ingestion.batches import discover_local_files

LOCAL_V = "v1.2026.06e"


def _health(**over) -> dict:
    base = {
        "status": "ok",
        "pipeline_version": LOCAL_V,
        "warm": True,
        "device": "cuda (NVIDIA RTX PRO 6000)",
        "gliner": {
            "backend": "onnx",
            "model": "E:\\models\\gliner_onnx",
            "loaded": True,
            "device": "cuda",
            "providers": ["CUDAExecutionProvider", "CPUExecutionProvider"],
        },
    }
    gliner_over = over.pop("gliner", {})
    base.update(over)
    base["gliner"] = {**base["gliner"], **gliner_over}
    return base


def test_evaluate_ready_onnx_cuda():
    r = _evaluate(_health(), LOCAL_V)
    assert r["state"] == "ready"
    assert r["checks"]["gpu_active"] is True
    assert r["checks"]["version_match"] is True


def test_evaluate_flags_onnx_cpu_fallback():
    # The documented Blackwell trap: ONNX lane up, but CUDA EP silently absent.
    r = _evaluate(_health(gliner={"providers": ["CPUExecutionProvider"], "device": "cpu"}),
                  LOCAL_V)
    assert r["state"] == "warning"
    assert r["checks"]["gpu_active"] is False
    assert "GPU" in r["detail"]


def test_evaluate_torch_cpu_is_warning_not_fail():
    r = _evaluate(_health(gliner={"backend": "torch", "providers": [], "device": "cpu"}),
                  LOCAL_V)
    assert r["state"] == "warning"
    assert r["checks"]["gpu_active"] is False


def test_evaluate_version_mismatch_is_warning():
    r = _evaluate(_health(pipeline_version="v1.2025.01a"), LOCAL_V)
    assert r["state"] == "warning"
    assert r["checks"]["version_match"] is False
    assert "v1.2025.01a" in r["detail"]


def test_evaluate_cold_endpoint_is_warning():
    r = _evaluate(_health(warm=False), LOCAL_V)
    assert r["state"] == "warning"
    assert r["checks"]["warm"] is False


def test_evaluate_unhealthy_is_fail():
    r = _evaluate(_health(status="error"), LOCAL_V)
    assert r["state"] == "fail"


def test_validate_endpoints_unreachable_and_verdict():
    report = asyncio.run(validate_endpoints([
        {"label": "dead box", "url": "http://127.0.0.1:1", "enabled": True},
        {"label": "no url", "url": "", "enabled": False},
    ]))
    by_label = {e["label"]: e for e in report["endpoints"]}
    assert by_label["dead box"]["state"] == "fail"
    assert by_label["dead box"]["checks"]["reachable"] is False
    assert by_label["no url"]["state"] == "fail"
    assert report["deploy_ready"] is False
    assert report["enabled_total"] == 1
    assert report["enabled_ready"] == 0


def test_discover_local_files_skips_dotfiles(tmp_path: Path):
    (tmp_path / "real.md").write_text("# doc")
    (tmp_path / "._real.md").write_text("\x00applefork")  # AppleDouble junk
    (tmp_path / ".hidden.md").write_text("# hidden")
    root, files = discover_local_files(str(tmp_path), recursive=False,
                                       extensions=[".md"])
    assert [f.name for f in files] == ["real.md"]
