"""Entity-encoder bake-off at the sidecar boundary (owner, 2026-08-08).

Same 385 windows, same 35-label oracle vocabulary, host MPS. Arms:
GLiNER2 (incumbent) and challenger encoders via the classic gliner library,
each swept over operating thresholds — threshold is part of the ENCODER
release, never the frozen gates. Output contract per prediction:
{text, start, end, label, score}. Downstream Polymath is not involved.

Metrics per arm: span recall/precision at IoU>=.8 (label-blind), typed
recall, the measured failure class (lowercase compound terms), wall time.
"""
from __future__ import annotations

import difflib
import json
import os
import sys
import time

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

os.environ["GRAPHIFY_FORCED_ADAPTERS"] = "cpcs_research_v1"

RAW = open("/Users/king/Downloads/rag_graph_entity_relation_test/source_document.md").read()
GOLD = [json.loads(l) for l in open(
    "/Users/king/Downloads/rag_graph_entity_relation_test/expected/entities.jsonl") if l.strip()]
ORACLE_LABELS = sorted({g["label"] for g in GOLD})

from services.extraction.graphify_normalization import normalize_document  # noqa: E402
from services.extraction.graphify_survey import survey_document  # noqa: E402
from services.extraction.graphify_census import build_census_windows  # noqa: E402

DOC = normalize_document("d", RAW)
SURVEY = survey_document(DOC)
WINDOWS = list(build_census_windows(DOC, SURVEY))
_blocks = difflib.SequenceMatcher(None, DOC.normalized_text, RAW, autojunk=False).get_matching_blocks()


def to_raw(start, end):
    for b in _blocks:
        if b.a <= start and end <= b.a + b.size:
            return start + (b.b - b.a), end + (b.b - b.a)
    return None


def iou(a, b):
    ov = max(0, min(a["char_end"], b["char_end"]) - max(a["char_start"], b["char_start"]))
    un = max(a["char_end"], b["char_end"]) - min(a["char_start"], b["char_start"])
    return ov / un if un else 0.0


def score(preds, arm):
    cands = sorted(((iou(p, g), pi, gi) for pi, p in enumerate(preds)
                    for gi, g in enumerate(GOLD) if iou(p, g) >= 0.8), reverse=True)
    mp, mg, pair = set(), set(), {}
    for _s, pi, gi in cands:
        if pi not in mp and gi not in mg:
            mp.add(pi); mg.add(gi); pair[gi] = pi
    A = len(mg)
    typed = sum(1 for gi, pi in pair.items() if preds[pi]["label"] == GOLD[gi]["label"])
    lc = [gi for gi, g in enumerate(GOLD) if g["text"].islower() and "-" in g["text"]]
    lc_hit = sum(1 for gi in lc if gi in mg)
    return {
        "arm": arm, "predictions": len(preds),
        "span_recall": round(A / len(GOLD), 3),
        "span_precision": round(A / len(preds), 3) if preds else 0.0,
        "typed_recall": round(typed / len(GOLD), 3),
        "lowercase_compound_recall": f"{lc_hit}/{len(lc)}",
    }


def run_gliner2(device, threshold):
    from scripts.ab_gliner2_device import DeviceProvider
    from services.extraction.graphify_census import select_schema_adapters
    provider = DeviceProvider(device)
    provider._model()
    adapters = select_schema_adapters(DOC, SURVEY)
    preds = []
    started = time.perf_counter()
    for i in range(0, len(WINDOWS), 4):
        batch = WINDOWS[i:i + 4]
        rows = provider.predict_entities(
            [w.text for w in batch], batch_size=4, threshold=threshold, adapters=adapters)
        for w, row in zip(batch, rows):
            for p in row:
                mapped = to_raw(w.normalized_start + p.start, w.normalized_start + p.end)
                if mapped:
                    label = (p.facet or p.entity_type).upper()
                    preds.append({"text": p.text, "char_start": mapped[0],
                                  "char_end": mapped[1], "label": label,
                                  "score": p.confidence})
    return preds, time.perf_counter() - started


def run_classic(model_id, device, threshold):
    from gliner import GLiNER
    label_strings = [l.replace("_", " ").lower() for l in ORACLE_LABELS]
    back = {l.replace("_", " ").lower(): l for l in ORACLE_LABELS}
    model = GLiNER.from_pretrained(model_id)
    try:
        model = model.to(device)
    except Exception:
        pass
    preds = []
    started = time.perf_counter()
    for w in WINDOWS:
        for e in model.predict_entities(w.text, label_strings, threshold=threshold):
            mapped = to_raw(w.normalized_start + e["start"], w.normalized_start + e["end"])
            if mapped:
                preds.append({"text": e["text"], "char_start": mapped[0],
                              "char_end": mapped[1],
                              "label": back.get(e["label"], e["label"].upper()),
                              "score": e["score"]})
    return preds, time.perf_counter() - started


def main() -> int:
    print(f"{len(WINDOWS)} windows | {len(GOLD)} gold | {len(ORACLE_LABELS)} labels")
    results = []
    for threshold in (0.5, 0.3):
        preds, secs = run_gliner2("mps", threshold)
        row = score(preds, f"gliner2@mps t={threshold}")
        row["seconds"] = round(secs, 1)
        results.append(row); print(json.dumps(row))
    for threshold in (0.3, 0.5):
        preds, secs = run_classic("knowledgator/gliner-bi-base-v2.0", "mps", threshold)
        row = score(preds, f"gliner-bi-base-v2.0@mps t={threshold}")
        row["seconds"] = round(secs, 1)
        results.append(row); print(json.dumps(row))
    json.dump(results, open("/Users/king/polymath_v3.3/data_eval/encoder_bakeoff.json", "w"), indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
