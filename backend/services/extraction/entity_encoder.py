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


RELEX_ENTITY_PROVIDER_RELEASE = "relex-large-sidecar-entities-v1"


def _relex_thresholds() -> dict:
    """Calibration-only overrides (saturation Matrix A). Defaults = the
    sidecar release pins; after the Pareto point is pinned these envs are
    retired from use."""
    out = {}
    for env, key in (("RELEX_ENTITY_THRESHOLD", "entity_threshold"),
                     ("RELEX_RELATION_THRESHOLD", "relation_threshold")):
        raw = os.environ.get(env, "").strip()
        if raw:
            try:
                out[key] = float(raw)
            except ValueError:
                pass
    return out



class RelexSidecarEntityProvider:
    """Entity candidates from the host-MPS Relex sidecar (production candidate).

    Same downstream contract as every provider: EntityPrediction rows with
    core types + facets from the ACTIVE schema. Transport and model
    compatibility live behind the sidecar boundary (relex-infer-v1); this
    provider only translates schema labels out and spans back.
    """

    release = RELEX_ENTITY_PROVIDER_RELEASE

    def predict_joint(
        self,
        texts: Sequence[str],
        *,
        adapters: tuple[str, ...] = (),
    ) -> tuple[list[list[EntityPrediction]], list[list[dict]]]:
        """ONE sidecar pass per text: entities under the ACTIVE schema AND
        relation candidates under the canonical predicate labels.

        The single-pass consolidation (owner-ordered): the census consumes
        the entity rows exactly as predict_entities returns them; the
        relation rows (text-local offsets, raw label+score) ride the census
        artifact to the relation lane, which then makes no second pass.
        """
        from services.extraction import relex_sidecar_client
        from services.extraction.graphify_relations import _relex_relation_labels

        active = schema_descriptions(adapters)
        label_strings = sorted(label.replace("_", " ").lower() for label in active)
        back = {label.replace("_", " ").lower(): label for label in active}
        # Transport batching only: the sidecar scores each window
        # independently, so slicing the call changes nothing semantic — but
        # a whole book's windows in ONE call exceeds any sane HTTP timeout
        # (O5 soak finding: 3.3MB books died at the 300s bound forever).
        try:
            batch = max(1, int(os.environ.get("RELEX_INFER_BATCH", "16")))
        except ValueError:
            batch = 16
        # Sharded across the replica pool when one is routed (2026-08-10):
        # order-preserving, semantics-free — degrades to the serial loop on
        # a single-URL pool. This is the fleet's extraction-throughput
        # multiplier; the serial sidecar was the measured ceiling.
        results = relex_sidecar_client.infer_sharded(
            list(texts), batch_size=batch, entity_labels=label_strings,
            relation_labels=_relex_relation_labels(),
            **_relex_thresholds(),
        )
        entity_rows: list[list[EntityPrediction]] = []
        relation_rows: list[list[dict]] = []
        for result in results:
            text = texts[len(entity_rows)]
            row: list[EntityPrediction] = []
            for item in result.entities:
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
            entity_rows.append(row)
            relation_rows.append([
                {
                    "head_start": r.head_start, "head_end": r.head_end,
                    "head_text": r.head_text,
                    "tail_start": r.tail_start, "tail_end": r.tail_end,
                    "tail_text": r.tail_text,
                    "label": r.label, "score": r.score,
                }
                for r in result.relations
            ])
        return entity_rows, relation_rows

    def predict_entities(
        self,
        texts: Sequence[str],
        *,
        batch_size: int = 4,
        threshold: float = 0.5,
        adapters: tuple[str, ...] = (),
    ) -> list[list[EntityPrediction]]:
        del batch_size, threshold  # release properties; sidecar pins them
        from services.extraction.relex_sidecar_client import infer

        active = schema_descriptions(adapters)
        label_strings = sorted(label.replace("_", " ").lower() for label in active)
        back = {label.replace("_", " ").lower(): label for label in active}
        output: list[list[EntityPrediction]] = []
        for result in infer(list(texts), entity_labels=label_strings, relation_labels=[],
                            **_relex_thresholds()):
            row: list[EntityPrediction] = []
            for item in result.entities:
                start, end = int(item["start"]), int(item["end"])
                surface = str(item["text"])
                text = texts[len(output)]
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


_RELEX_PROVIDER: RelexSidecarEntityProvider | None = None


ENTITY_SIDECAR_CONTRACT = "entity-predict-v1"


def _entity_sidecar_url() -> str:
    explicit = os.environ.get("ENTITY_SIDECAR_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    if os.path.exists("/.dockerenv"):
        return "http://host.docker.internal:8738"
    return "http://127.0.0.1:8738"


class SidecarEntityProvider:
    """One qualified entity provider executed on the host-MPS entity sidecar.

    The sidecar runs the EXISTING provider code (byte-identical decisions
    per the placement A/B); this class is transport only. kind selects
    which provider the sidecar executes: 'gliner2' or 'gliner_bi'.
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.release = f"entity-sidecar-mps-v1({kind})"

    def predict_entities(
        self,
        texts: Sequence[str],
        *,
        batch_size: int = 4,
        threshold: float = 0.5,
        adapters: tuple[str, ...] = (),
    ) -> list[list[EntityPrediction]]:
        import json as _json
        import urllib.request

        request = urllib.request.Request(
            _entity_sidecar_url() + "/predict",
            data=_json.dumps({
                "provider": self.kind, "texts": list(texts),
                "adapters": list(adapters), "threshold": threshold,
                "batch_size": batch_size,
            }).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=1800) as response:
            body = _json.loads(response.read())
        if body.get("contract") != ENTITY_SIDECAR_CONTRACT:
            raise RuntimeError(f"entity sidecar contract mismatch: {body.get('contract')!r}")
        return [
            [
                EntityPrediction(
                    text=item["text"], entity_type=item["entity_type"],
                    start=int(item["start"]), end=int(item["end"]),
                    confidence=float(item["confidence"]),
                    facet=item.get("facet") or "",
                )
                for item in row
            ]
            for row in body["results"]
        ]


def get_entity_encoder_provider():
    """The one selection seam. Default = frozen GLiNER2 baseline."""
    global _BI_PROVIDER
    global _COMPOSITE
    global _RELEX_PROVIDER
    choice = os.environ.get("GRAPHIFY_ENTITY_PROVIDER", "gliner2").strip().lower()
    if choice in ("gliner_bi", "gliner-bi", "bi"):
        if _BI_PROVIDER is None:
            _BI_PROVIDER = GLiNERBiProvider()
        return _BI_PROVIDER
    if choice in ("composite", "union"):
        if _COMPOSITE is None:
            _COMPOSITE = CompositeEntityProvider()
        return _COMPOSITE
    if choice in ("relex", "gliner_relex", "relex_sidecar"):
        if _RELEX_PROVIDER is None:
            _RELEX_PROVIDER = RelexSidecarEntityProvider()
        return _RELEX_PROVIDER
    if choice in ("composite_sidecar", "sidecar_composite"):
        global _COMPOSITE_SIDECAR
        if _COMPOSITE_SIDECAR is None:
            _COMPOSITE_SIDECAR = CompositeSidecarProvider()
        return _COMPOSITE_SIDECAR
    if choice in ("gliner2_sidecar",):
        global _GLINER2_SIDECAR
        if _GLINER2_SIDECAR is None:
            _GLINER2_SIDECAR = SidecarEntityProvider("gliner2")
        return _GLINER2_SIDECAR
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
class CompositeSidecarProvider(CompositeEntityProvider):
    """Composite arbitration unchanged; both generators execute on the
    host-MPS sidecar instead of in-process. Fold logic is inherited —
    reconciliation stays pipeline knowledge, transport stays dumb."""

    release = "entity-composite-sidecar-v1(gliner2+gliner-bi@mps)"

    def __init__(self) -> None:  # noqa: D401 — no in-process model loads
        self._incumbent = SidecarEntityProvider("gliner2")
        self._candidate = SidecarEntityProvider("gliner_bi")


_COMPOSITE_SIDECAR: CompositeSidecarProvider | None = None
_GLINER2_SIDECAR: SidecarEntityProvider | None = None
