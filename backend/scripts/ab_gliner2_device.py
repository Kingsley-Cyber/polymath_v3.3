"""GLiNER2 execution-placement A/B (owner-ratified 2026-08-08).

Same checkpoint, same labels, same thresholds, same windows, same ordering —
only the execution device changes:

    A: CPU (what the Docker Linux VM runs)         [container measured: 496.4s]
    B: host CPU (isolates VM overhead)
    C: host MPS / Metal (the candidate boundary)

Acceptance is SEMANTIC-DECISION conservation, not float identity: exact
entity span sets, types, per-chunk counts, plus flip counts (boundary flips,
type flips, device-only entities). Inputs are the factory-E2E corpus's exact
census windows, rebuilt deterministically from its frozen normalized
documents + surveys.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from dotenv import dotenv_values  # noqa: E402
from pymongo import MongoClient  # noqa: E402

from services.extraction.gliner2_cpu_provider import (  # noqa: E402
    DEFAULT_BATCH_SIZE,
    DEFAULT_THRESHOLD,
    GLiNER2CPUProvider,
    _default_loader,
)
from services.extraction.graphify_census import (  # noqa: E402
    _bucket_key,
    build_census_windows,
    select_schema_adapters,
)
from services.extraction.graphify_survey import DocumentSurveyV1  # noqa: E402
from models.graphify_contracts import NormalizedDocumentV1  # noqa: E402

ENV = dotenv_values(os.path.join(_BACKEND, "..", ".env"))


class DeviceProvider(GLiNER2CPUProvider):
    """Experiment-only: same parsing/schema path, device-parameterized model.

    Bypasses the production CPU contract deliberately and locally — the
    production provider stays CPU-pinned until this experiment ratifies a
    new permanent boundary.
    """

    def __init__(self, device: str) -> None:
        super().__init__()
        self._device = device
        self._instance = None

    def _model(self):
        if self._instance is None:
            model = _default_loader()
            if self._device != "cpu":
                model = model.to(self._device)
            self._instance = model
        return self._instance


def _corpus_documents(corpus_id: str):
    uri = re.sub(r"@mongodb:", "@localhost:", ENV.get("MONGODB_URI") or ENV.get("MONGO_URI") or "")
    db = MongoClient(uri, serverSelectionTimeoutMS=8000)[ENV.get("MONGODB_DB", "polymath")]
    rows = list(db.graphify_stage_artifacts.find(
        {"corpus_id": corpus_id, "stage": {"$in": ["NORMALIZED", "SURVEY_COMPLETE", "ENTITY_CENSUS_COMPLETE"]}},
    ))
    by_doc: dict[str, dict] = {}
    from services.storage.graphify_artifact_codec import decode_stage_payload
    for row in rows:
        if "payload" in row:
            payload = row["payload"]
        else:
            parts = list(db.graphify_stage_artifact_parts.find(
                {"artifact_id": row["artifact_id"]}).sort("part", 1))
            payload = decode_stage_payload(row, lambda a, n: [p["blob"] for p in parts])
        by_doc.setdefault(row["doc_id"], {})[row["stage"]] = payload
    documents = []
    for doc_id, stages in sorted(by_doc.items()):
        if "NORMALIZED" not in stages or "SURVEY_COMPLETE" not in stages:
            continue
        documents.append((
            NormalizedDocumentV1.model_validate(stages["NORMALIZED"]["document"]),
            DocumentSurveyV1.model_validate(stages["SURVEY_COMPLETE"]["survey"]),
            stages.get("ENTITY_CENSUS_COMPLETE", {}).get("mentions", []),
        ))
    return documents


def _predict_all(provider, jobs):
    """jobs: list of (window, adapters). Grouped exactly like the census."""
    results: dict[str, list] = {}
    groups: dict[tuple, list] = {}
    for window, adapters in jobs:
        groups.setdefault(adapters, []).append(window)
    for adapters, windows in sorted(groups.items()):
        for start in range(0, len(windows), DEFAULT_BATCH_SIZE):
            batch = windows[start:start + DEFAULT_BATCH_SIZE]
            rows = provider.predict_entities(
                [w.text for w in batch], batch_size=DEFAULT_BATCH_SIZE,
                threshold=DEFAULT_THRESHOLD, adapters=adapters,
            )
            for window, row in zip(batch, rows):
                results[window.window_id] = row
    return results


def _decision_set(results):
    decisions = set()
    for window_id, row in results.items():
        for p in row:
            decisions.add((window_id, p.start, p.end, p.text, p.entity_type))
    return decisions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--devices", nargs="+", default=["cpu", "mps"])
    parser.add_argument("--json-out", default="/Users/king/polymath_v3.3/data_eval/gliner2_device_ab.json")
    args = parser.parse_args()

    documents = _corpus_documents(args.corpus)
    jobs = []
    container_mentions = set()
    for document, survey, mentions in documents:
        adapters = select_schema_adapters(document, survey)
        for window in sorted(build_census_windows(document, survey), key=_bucket_key):
            jobs.append((window, adapters))
        for m in mentions:
            if "identifier-miner" in str(m.get("provider_release", "")):
                continue
            container_mentions.add(
                (m["window_id"], m["local_start"], m["local_end"], m["surface"], m["entity_type"])
            )
    total_tokens = sum(w.token_count for w, _ in jobs)
    print(f"{len(documents)} docs, {len(jobs)} windows, {total_tokens} tokens")

    report = {"corpus": args.corpus, "windows": len(jobs), "tokens": total_tokens,
              "container_reference_mentions": len(container_mentions), "devices": {}}
    sets = {}
    for device in args.devices:
        provider = DeviceProvider(device)
        provider._model()  # warm load outside the timed section
        started = time.perf_counter()
        results = _predict_all(provider, jobs)
        elapsed = time.perf_counter() - started
        decisions = _decision_set(results)
        sets[device] = decisions
        report["devices"][device] = {
            "inference_seconds": round(elapsed, 1),
            "entities": len(decisions),
            "tokens_per_second": round(total_tokens / elapsed, 1),
        }
        print(f"{device}: {elapsed:.1f}s, {len(decisions)} entity decisions, "
              f"{total_tokens/elapsed:.0f} tok/s")

    if len(args.devices) == 2:
        a, b = args.devices
        only_a = sets[a] - sets[b]
        only_b = sets[b] - sets[a]
        type_flips = 0
        boundary_flips = 0
        span_index_a = {(d[0], d[1], d[2], d[3]) for d in sets[a]}
        for d in only_b:
            if (d[0], d[1], d[2], d[3]) in span_index_a:
                type_flips += 1
            elif any(x[0] == d[0] and x[3] == d[3] for x in only_a):
                boundary_flips += 1
        report["conservation"] = {
            f"{a}_only": len(only_a),
            f"{b}_only": len(only_b),
            "type_flips": type_flips,
            "boundary_flips": boundary_flips,
            "identical_decision_sets": sets[a] == sets[b],
            "jaccard": round(
                len(sets[a] & sets[b]) / len(sets[a] | sets[b]), 6,
            ) if sets[a] | sets[b] else 1.0,
        }
        print("conservation:", json.dumps(report["conservation"]))
        for label, decisions in (("host_" + a, sets[a]), ("host_" + b, sets[b])):
            inter = len(decisions & container_mentions)
            report["devices"].setdefault(label.split("_", 1)[1], {})["vs_container_jaccard"] = round(
                inter / len(decisions | container_mentions), 6,
            ) if decisions | container_mentions else 1.0

    with open(args.json_out, "w") as handle:
        json.dump(report, handle, indent=1)
    print("report:", args.json_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
