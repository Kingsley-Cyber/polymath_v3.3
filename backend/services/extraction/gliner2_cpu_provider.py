"""Sole warm entity provider for the canonical Graphify extraction path."""

from __future__ import annotations

import re

import hashlib
import importlib.metadata
import json
import threading
from dataclasses import dataclass
from pathlib import Path

import yaml
from typing import Any, Callable, Sequence

from services.extraction.canonical import canonical_entity_type

MODEL_ID = "fastino/gliner2-base-v1"
MODEL_REVISION = "f5b2ecedebe4381b088c1cf276f5bf72a52cac54"
MODEL_CHECKPOINT_SHA256 = "845fc4bd93c525b86124c58ab4f56c9eacf8587953086b14c501fab25957c007"
PROVIDER_RELEASE = "graphify-gliner2-cpu-v1"
DESCRIPTION_RELEASE = "graphify-entity-descriptions-v1"
DEFAULT_THRESHOLD = 0.5
DEFAULT_BATCH_SIZE = 4

ENTITY_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "Person": {"description": "A specifically named individual human person, author, researcher, executive, or historical figure."},
    "Organization": {"description": "A specifically named company, institution, agency, laboratory, team, or other formal organization."},
    "Location": {"description": "A named geographic place, country, city, region, physical site, or address."},
    "Event": {"description": "A specifically named occurrence, conference, incident, launch, campaign, or historical event."},
    "Concept": {"description": "A named idea, theory, discipline, technical concept, principle, quality measure, or metric."},
    "Method": {"description": "A named process, algorithm, technique, workflow, protocol, procedure, or research method."},
    "Product": {"description": "A specifically named commercial product, device, service offering, or manufactured system."},
    "Software": {"description": "Named software, programming language, database, library, framework, API, platform, application, or software service."},
    "Document": {"description": "A specifically named book, paper, report, specification, policy, manual, dataset, or other document artifact."},
    "Standard": {"description": "A named technical standard, formal specification, convention, or compliance framework."},
    "Rule": {"description": "A named operational rule, constraint, requirement, policy rule, or decision criterion."},
    "Law": {"description": "A named statute, regulation, legal doctrine, act, or other law."},
    "Artifact": {"description": "A named dataset, file, model artifact, component, resource, or produced technical object."},
    "TimeReference": {"description": "A specific date, year, time period, deadline, duration, or temporal reference."},
}


_SCHEMA_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "entity_schema.yaml"


def _load_schema_config() -> dict[str, Any]:
    """Versioned schema configuration: core inventory + corpus adapters.

    Falls back to the built-in core descriptions if the config is absent so
    the provider never silently changes census behavior on a missing file.
    """
    try:
        payload = yaml.safe_load(_SCHEMA_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        payload = {}
    core = {
        str(label): {"description": str(desc)}
        for label, desc in (payload.get("core") or {}).items()
    } or dict(ENTITY_DESCRIPTIONS)
    adapters: dict[str, dict[str, Any]] = {}
    for name, adapter in (payload.get("adapters") or {}).items():
        adapters[str(name)] = {
            "labels": {
                str(label): {"description": str(desc)}
                for label, desc in (adapter.get("labels") or {}).items()
            },
            "facets": {
                str(label): {"core": str(row.get("core", "Concept")), "facet": str(row.get("facet", ""))}
                for label, row in (adapter.get("facets") or {}).items()
            },
            "selection": dict(adapter.get("selection") or {}),
        }
    return {
        "release": str(payload.get("release") or DESCRIPTION_RELEASE),
        "core": core,
        "adapters": adapters,
    }


SCHEMA_CONFIG = _load_schema_config()
SCHEMA_RELEASE = SCHEMA_CONFIG["release"]


def _anchor_span(text: str, surface: str, start: int, end: int) -> tuple[int, int]:
    """Re-anchor a misaligned model span on a unique exact occurrence."""
    if surface:
        occurrences = [
            match.start() for match in re.finditer(re.escape(surface), text)
        ]
        if len(occurrences) == 1:
            return occurrences[0], occurrences[0] + len(surface)
    return start, end


def schema_descriptions(adapters: tuple[str, ...] = ()) -> dict[str, dict[str, str]]:
    merged = dict(SCHEMA_CONFIG["core"])
    for name in adapters:
        adapter = SCHEMA_CONFIG["adapters"].get(name)
        if adapter is None:
            raise KeyError(f"unknown entity-schema adapter: {name}")
        merged.update(adapter["labels"])
    return merged


def schema_hash(adapters: tuple[str, ...] = ()) -> str:
    payload = json.dumps(
        {"release": SCHEMA_RELEASE, "adapters": sorted(adapters),
         "descriptions": schema_descriptions(adapters)},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _facet_for_label(label: str, adapters: tuple[str, ...]) -> tuple[str, str]:
    """Adapter labels normalize to (core_type, facet); core labels pass through."""
    for name in adapters:
        row = SCHEMA_CONFIG["adapters"][name]["facets"].get(label)
        if row is not None:
            return row["core"], row["facet"]
    return label, ""


@dataclass(frozen=True)
class EntityPrediction:
    text: str
    entity_type: str
    start: int
    end: int
    confidence: float
    facet: str = ""


_MODEL_LOCK = threading.Lock()
_INFERENCE_LOCK = threading.Lock()
_MODEL: Any | None = None
_SCHEMAS: dict[tuple[str, ...], Any] = {}
_MODEL_LOAD_COUNT = 0
_PROVIDER: "GLiNER2CPUProvider | None" = None


def description_hash() -> str:
    payload = json.dumps(ENTITY_DESCRIPTIONS, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def provider_release_hash() -> str:
    payload = {
        "provider_release": PROVIDER_RELEASE,
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "checkpoint_sha256": MODEL_CHECKPOINT_SHA256,
        "description_release": DESCRIPTION_RELEASE,
        "description_hash": description_hash(),
        "threshold": DEFAULT_THRESHOLD,
        "batch_size": DEFAULT_BATCH_SIZE,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _resolve_snapshot() -> Path:
    from huggingface_hub import snapshot_download

    path = snapshot_download(
        repo_id=MODEL_ID,
        revision=MODEL_REVISION,
        allow_patterns=(
            "config.json", "encoder_config/config.json", "model.safetensors",
            "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json",
            "added_tokens.json", "spm.model",
        ),
    )
    checkpoint = Path(path) / "model.safetensors"
    digest = hashlib.sha256()
    with checkpoint.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != MODEL_CHECKPOINT_SHA256:
        raise RuntimeError(
            f"GLiNER2 checkpoint hash mismatch: expected {MODEL_CHECKPOINT_SHA256}, got {actual}"
        )
    return Path(path)


def _default_loader() -> Any:
    from gliner2 import GLiNER2

    snapshot = _resolve_snapshot()
    return GLiNER2.from_pretrained(str(snapshot), map_location="cpu")


def _assert_cpu_tensors(model: Any) -> None:
    non_cpu: list[str] = []
    for name, tensor in model.named_parameters():
        if tensor.device.type != "cpu":
            non_cpu.append(f"parameter:{name}:{tensor.device}")
    for name, tensor in model.named_buffers():
        if tensor.device.type != "cpu":
            non_cpu.append(f"buffer:{name}:{tensor.device}")
    if non_cpu:
        raise RuntimeError("GLiNER2 has non-CPU tensors: " + ", ".join(non_cpu[:10]))


class GLiNER2CPUProvider:
    """Batched exact-span entity inference backed by one process-wide model."""

    def __init__(self, loader: Callable[[], Any] | None = None) -> None:
        self._loader = loader or _default_loader

    def _model(self) -> Any:
        global _MODEL, _MODEL_LOAD_COUNT
        if _MODEL is None:
            with _MODEL_LOCK:
                if _MODEL is None:
                    candidate = self._loader()
                    _assert_cpu_tensors(candidate)
                    _MODEL = candidate
                    _MODEL_LOAD_COUNT += 1
        _assert_cpu_tensors(_MODEL)
        return _MODEL

    @staticmethod
    def _schema(model: Any, adapters: tuple[str, ...] = ()) -> Any:
        key = tuple(sorted(adapters))
        if key not in _SCHEMAS:
            with _MODEL_LOCK:
                if key not in _SCHEMAS:
                    _SCHEMAS[key] = model.create_schema().entities(schema_descriptions(key))
        return _SCHEMAS[key]

    @property
    def load_count(self) -> int:
        return _MODEL_LOAD_COUNT

    def health(self) -> dict[str, Any]:
        model = self._model()
        _assert_cpu_tensors(model)
        return {
            "ready": True,
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "model_checkpoint_sha256": MODEL_CHECKPOINT_SHA256,
            "provider_release": PROVIDER_RELEASE,
            "provider_release_hash": provider_release_hash(),
            "description_release": DESCRIPTION_RELEASE,
            "description_hash": description_hash(),
            "entity_types": tuple(ENTITY_DESCRIPTIONS),
            "threshold": DEFAULT_THRESHOLD,
            "batch_size": DEFAULT_BATCH_SIZE,
            "device": "cpu",
            "model_load_count": self.load_count,
            "package_version": importlib.metadata.version("gliner2"),
        }

    def predict_entities(
        self,
        texts: Sequence[str],
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        threshold: float = DEFAULT_THRESHOLD,
        adapters: tuple[str, ...] = (),
    ) -> list[list[EntityPrediction]]:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be between zero and one")
        if not texts:
            return []
        model = self._model()
        schema = self._schema(model, adapters)
        active_labels = schema_descriptions(adapters)
        with _INFERENCE_LOCK:
            raw_results = model.batch_extract(
                list(texts), schema, batch_size=batch_size, threshold=threshold,
                include_confidence=True, include_spans=True,
            )
        if len(raw_results) != len(texts):
            raise RuntimeError("GLiNER2 returned a different number of result rows than inputs")
        output: list[list[EntityPrediction]] = []
        for text, result in zip(texts, raw_results):
            row: list[EntityPrediction] = []
            for label in active_labels:
                values = result.get("entities", {}).get(label, [])
                for item in values:
                    start = int(item["start"])
                    end = int(item["end"])
                    surface = str(item["text"])
                    if start < 0 or end <= start or end > len(text) or text[start:end] != surface:
                        # The model asserted a surface its own offsets do not
                        # anchor (observed on tiny adapter-schema windows).
                        # Deterministic recovery: re-anchor on a UNIQUE exact
                        # occurrence; otherwise pass the emission through so
                        # the census persists it as an ALIGNMENT_FAILURE row —
                        # an observation is never silently lost and never
                        # crashes the corpus.
                        start, end = _anchor_span(text, surface, start, end)
                    core_label, facet = _facet_for_label(label, adapters)
                    row.append(EntityPrediction(
                        text=surface,
                        entity_type=canonical_entity_type(core_label),
                        start=start,
                        end=end,
                        confidence=float(item.get("confidence", 0.0)),
                        facet=facet,
                    ))
            row.sort(key=lambda item: (item.start, item.end, item.entity_type, item.text))
            output.append(row)
        return output


def get_gliner2_cpu_provider() -> GLiNER2CPUProvider:
    global _PROVIDER
    if _PROVIDER is None:
        with _MODEL_LOCK:
            if _PROVIDER is None:
                _PROVIDER = GLiNER2CPUProvider()
    return _PROVIDER


def _reset_provider_for_tests() -> None:
    global _MODEL, _SCHEMA, _MODEL_LOAD_COUNT, _PROVIDER
    with _MODEL_LOCK:
        _MODEL = None
        _SCHEMA = None
        _MODEL_LOAD_COUNT = 0
        _PROVIDER = None
