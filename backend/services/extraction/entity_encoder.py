"""Provider-neutral entity-encoder boundary (owner-ratified 2026-08-08).

The permanent abstraction: downstream Polymath consumes only

    EntitySpan { text, start, end, label, score }

surfaced here as the census's EntityPrediction rows. Model compatibility is
the provider's problem; canonicalization, OpenIE, gates, Graphify, Mongo,
q8, and Neo4j never learn which encoder ran.

    EntityEncoderProvider
    ├── GLiNER2CPUProvider   (retained frozen baseline — gliner2_cpu_provider)
    └── GLiNERBiProvider     (production candidate: gliner-bi-base-v2.0 @ 0.30)

Release identity for the candidate (also pinned in
config/entity_provider_gliner_bi.yaml and the freeze manifest):

    entity_provider_release: gliner-bi-base-mps-v1
    model: knowledgator/gliner-bi-base-v2.0 @ immutable revision
    runtime: device from GLINER_BI_DEVICE (mps default), threshold 0.30
    output_contract: entity-span-v1

The operating threshold is an ENCODER-RELEASE property: the census's generic
threshold argument is ignored by this provider in favor of its pinned value
(override only via GLINER_BI_THRESHOLD for calibration experiments).

Selection: GRAPHIFY_ENTITY_PROVIDER=gliner2 (default, frozen baseline) or
gliner_bi. Downstream code calls get_entity_encoder_provider() and nothing
else changes.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Sequence

from services.extraction.canonical import canonical_entity_type
from services.extraction.gliner2_cpu_provider import (
    EntityPrediction,
    GLiNER2CPUProvider,
    _anchor_span,
    _facet_for_label,
    get_gliner2_cpu_provider,
    schema_descriptions,
)

logger = logging.getLogger(__name__)

GLINER_BI_PROVIDER_RELEASE = "gliner-bi-base-mps-v1"
GLINER_BI_MODEL_ID = "knowledgator/gliner-bi-base-v2.0"
GLINER_BI_DEFAULT_THRESHOLD = 0.30
OUTPUT_CONTRACT = "entity-span-v1"

_BI_LOCK = threading.Lock()
_BI_MODEL: Any | None = None


def _bi_device() -> str:
    return os.environ.get("GLINER_BI_DEVICE", "mps").strip() or "mps"


def _bi_threshold() -> float:
    raw = os.environ.get("GLINER_BI_THRESHOLD", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            logger.warning("GLINER_BI_THRESHOLD=%r not a float; using pinned", raw)
    return GLINER_BI_DEFAULT_THRESHOLD


class GLiNERBiProvider:
    """Candidate encoder behind the same predict_entities interface.

    Same downstream contract as the incumbent: EntityPrediction rows with
    core entity types + facets from the ACTIVE schema (core + adapters);
    spans validated against the input text with unique-occurrence re-anchor,
    otherwise passed through for the census to persist as ALIGNMENT_FAILURE.
    """

    release = GLINER_BI_PROVIDER_RELEASE

    def _model(self) -> Any:
        global _BI_MODEL
        if _BI_MODEL is None:
            with _BI_LOCK:
                if _BI_MODEL is None:
                    from gliner import GLiNER

                    model = GLiNER.from_pretrained(GLINER_BI_MODEL_ID)
                    device = _bi_device()
                    try:
                        model = model.to(device)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "gliner-bi: device %s unavailable (%s); staying on cpu",
                            device, exc,
                        )
                    _BI_MODEL = model
                    logger.info(
                        "gliner-bi warm: %s on %s @ threshold %.2f",
                        GLINER_BI_MODEL_ID, device, _bi_threshold(),
                    )
        return _BI_MODEL

    def predict_entities(
        self,
        texts: Sequence[str],
        *,
        batch_size: int = 4,
        threshold: float = 0.5,
        adapters: tuple[str, ...] = (),
    ) -> list[list[EntityPrediction]]:
        del batch_size, threshold  # encoder-release properties; see module doc
        model = self._model()
        active = schema_descriptions(adapters)
        label_strings = sorted(
            label.replace("_", " ").lower() for label in active
        )
        back = {label.replace("_", " ").lower(): label for label in active}
        pinned = _bi_threshold()
        output: list[list[EntityPrediction]] = []
        with _BI_LOCK:
            for text in texts:
                row: list[EntityPrediction] = []
                for item in model.predict_entities(text, label_strings, threshold=pinned):
                    start, end = int(item["start"]), int(item["end"])
                    surface = str(item["text"])
                    if (
                        start < 0 or end <= start or end > len(text)
                        or text[start:end] != surface
                    ):
                        start, end = _anchor_span(text, surface, start, end)
                    schema_label = back.get(str(item["label"]).lower(), "Concept")
                    core_label, facet = _facet_for_label(schema_label, adapters)
                    row.append(EntityPrediction(
                        text=surface,
                        entity_type=canonical_entity_type(core_label),
                        start=start,
                        end=end,
                        confidence=float(item.get("score", 0.0)),
                        facet=facet,
                    ))
                row.sort(key=lambda p: (p.start, p.end, p.entity_type, p.text))
                output.append(row)
        return output


_BI_PROVIDER: GLiNERBiProvider | None = None


def get_entity_encoder_provider():
    """The one selection seam. Default = frozen GLiNER2 baseline."""
    global _BI_PROVIDER
    global _COMPOSITE
    choice = os.environ.get("GRAPHIFY_ENTITY_PROVIDER", "gliner2").strip().lower()
    if choice in ("gliner_bi", "gliner-bi", "bi"):
        if _BI_PROVIDER is None:
            _BI_PROVIDER = GLiNERBiProvider()
        return _BI_PROVIDER
    if choice in ("composite", "union"):
        if _COMPOSITE is None:
            _COMPOSITE = CompositeEntityProvider()
        return _COMPOSITE
    return get_gliner2_cpu_provider()


COMPOSITE_PROVIDER_RELEASE = "entity-composite-v1(gliner2+gliner-bi)"


class CompositeEntityProvider:
    """Diagnostic/candidate ensemble: both encoders as candidate generators.

    Structural reconciliation only — never cross-model score comparison
    (confidence scales are not calibrated against each other):

      identical (span, text, type)  → one candidate (max score, same decision)
      same span, conflicting type   → BOTH retained; existing reducer/type
                                       arbitration decides, as it always has
      different/overlapping spans   → both retained until canonicalization
      provider-only spans           → retained

    Recall is the union; the deterministic gates remain the sole knowledge
    authority. Owner framing: this establishes the attainable upper bound
    before any primary+rescue optimization; the long-run constraint is
    still ONE heavy encoder.
    """

    release = COMPOSITE_PROVIDER_RELEASE

    def __init__(self) -> None:
        self._incumbent = get_gliner2_cpu_provider()
        self._candidate = GLiNERBiProvider()

    def predict_entities(
        self,
        texts: Sequence[str],
        *,
        batch_size: int = 4,
        threshold: float = 0.5,
        adapters: tuple[str, ...] = (),
    ) -> list[list[EntityPrediction]]:
        first = self._incumbent.predict_entities(
            texts, batch_size=batch_size, threshold=threshold, adapters=adapters,
        )
        second = self._candidate.predict_entities(
            texts, batch_size=batch_size, threshold=threshold, adapters=adapters,
        )
        merged: list[list[EntityPrediction]] = []
        for row_a, row_b in zip(first, second):
            folded: dict[tuple[int, int, str, str], EntityPrediction] = {}
            for prediction in [*row_a, *row_b]:
                key = (prediction.start, prediction.end, prediction.text, prediction.entity_type)
                incumbent = folded.get(key)
                if incumbent is None or prediction.confidence > incumbent.confidence:
                    folded[key] = prediction
            row = sorted(folded.values(), key=lambda p: (p.start, p.end, p.entity_type, p.text))
            merged.append(row)
        return merged


_COMPOSITE: CompositeEntityProvider | None = None
