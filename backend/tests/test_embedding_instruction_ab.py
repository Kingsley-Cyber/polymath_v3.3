import json

from scripts.compare_embedding_instruction_ab import compare_pair


def _row(qid, shape, *, recall=None, hit=None, answerability_ok=True):
    return {
        "id": qid,
        "shape": shape,
        "doc_recall": recall,
        "doc_hit": hit,
        "answerability_ok": answerability_ok,
        "error": None,
    }


def _artifact(naive_recall, cross_recall, *, latency=10.0):
    results = [
        _row("naive", "naive", recall=naive_recall, hit=True),
        _row("cross", "cross_corpus", recall=cross_recall, hit=True),
        _row("direct", "direct", recall=0.5, hit=True),
    ]
    results.extend(
        _row(f"neg-{index}", "negative_control", hit=None, answerability_ok=True)
        for index in range(5)
    )
    return {
        "summary": {"tier": "qdrant_only", "latency_mean_s": latency},
        "results": results,
    }


def _write(path, payload):
    path.write_text(json.dumps(payload))
    return path


def test_compare_embedding_instruction_ab_accepts_strict_primary_lift(tmp_path):
    baseline = _write(tmp_path / "baseline.json", _artifact(0.50, 0.20))
    candidate = _write(tmp_path / "candidate.json", _artifact(0.60, 0.30, latency=11.0))

    result = compare_pair(baseline, candidate)

    assert result["passed"] is True
    assert all(check["passed"] for check in result["checks"])


def test_compare_embedding_instruction_ab_rejects_flat_cross_corpus(tmp_path):
    baseline = _write(tmp_path / "baseline.json", _artifact(0.50, 0.20))
    candidate = _write(tmp_path / "candidate.json", _artifact(0.60, 0.20))

    result = compare_pair(baseline, candidate)

    assert result["passed"] is False
    failed = {check["name"] for check in result["checks"] if not check["passed"]}
    assert "primary_cross_corpus_strict_recall_lift" in failed
