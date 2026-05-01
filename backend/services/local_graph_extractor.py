"""Optional local-first GraphRAG extraction via GLiNER-style models.

This module intentionally has no hard dependency on gliner/torch at import
time. The backend must boot and the API LLM Ghost B path must keep working
even when local extraction packages or model weights are not installed yet.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from config import get_settings
from models.schemas import IngestionConfig
from services.ghost_b import (
    CandidateFactItem,
    EntityItem,
    ExtractionBatchReport,
    ExtractionFailureItem,
    ExtractionResult,
    ExtractionTask,
    RelationItem,
    SchemaContext,
    SchemaLens,
    compile_extraction_candidates,
    extract_entities,
    summarize_extraction_batch,
)

logger = logging.getLogger(__name__)


_LOCAL_RELATION_THRESHOLD = 0.5
_LOCAL_ENTITY_THRESHOLD = 0.35
_MAX_LOCAL_RELATION_SOURCE_ENTITIES = 4
_DEFAULT_LOCAL_RELATION_LABELS = [
    "uses",
    "part_of",
    "references",
    "supports",
    "produces",
    "stores",
    "measures",
    "tests",
    "defined_in",
    "applied_to",
]


class LocalGraphDependencyError(RuntimeError):
    """Raised when optional local graph extraction dependencies are missing."""


class LocalGraphOOMError(RuntimeError):
    """Raised when local graph extraction exhausts accelerator memory."""


class LocalGraphFatalCudaError(RuntimeError):
    """Raised when CUDA state is likely poisoned and the worker must stop."""


@dataclass(frozen=True)
class LocalWorkerSpec:
    device: str
    name: str
    batch_size: int
    weight: int


@dataclass
class LocalWorkerStats:
    device: str
    name: str
    chunks_processed: int = 0
    chunks_failed: int = 0
    oom_count: int = 0
    relation_oom_count: int = 0
    relation_disabled_count: int = 0
    cuda_fatal_count: int = 0
    relation_mode_chunks: int = 0
    relation_label_total: int = 0
    relation_label_max: int = 0
    model_token_count_total: int = 0
    model_token_count_observations: int = 0
    model_token_count_max: int = 0
    model_token_truncated_chunks: int = 0
    duration_seconds: float = 0.0
    current_batch_size: int = 1


class GlinerRelexAdapter:
    """Thin adapter around GLiNER-relex variants.

    The upstream community has used a few slightly different method names
    across examples/releases. This adapter tries the common batch methods and
    normalizes whatever comes back into this app's existing Ghost B dataclasses.
    Unit tests patch the adapter factory, so tests do not download model weights.
    """

    def __init__(self, model_name: str, device: str):
        self.model_name = model_name
        self.device = device
        self.model = self._load_model(model_name, device)

    @staticmethod
    def _load_model(model_name: str, device: str) -> Any:
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        os.environ.setdefault("TQDM_DISABLE", "1")
        try:
            from gliner import GLiNER  # type: ignore
        except Exception as exc:  # pragma: no cover - optional dependency
            raise LocalGraphDependencyError(
                "Optional package 'gliner' is not installed. Install the local "
                "graph extraction extras before enabling local_gliner."
            ) from exc
        model = GLiNER.from_pretrained(model_name)
        if hasattr(model, "to"):
            model = model.to(device)
        return model

    def infer_batch(
        self,
        texts: list[str],
        *,
        entity_labels: list[str],
        relation_labels: list[str],
        batch_size: int = 1,
        relation_mode: bool = True,
        source_entity_cap: int = _MAX_LOCAL_RELATION_SOURCE_ENTITIES,
    ) -> list[Any]:
        model = self.model
        batch_size = max(1, min(len(texts) or 1, int(batch_size or 1)))
        relation_labels = [label for label in relation_labels if label and label != "related_to"]

        # GLiNER-relex exposes an official joint NER+relation inference helper.
        # Prefer it because it avoids our older source<>relation label fanout.
        method = getattr(model, "inference", None)
        if callable(method) and relation_mode and relation_labels:
            kwargs = {
                "texts": texts,
                "labels": entity_labels,
                "relations": relation_labels,
                "threshold": _LOCAL_ENTITY_THRESHOLD,
                "adjacency_threshold": _LOCAL_RELATION_THRESHOLD,
                "relation_threshold": _LOCAL_RELATION_THRESHOLD,
                "batch_size": batch_size,
                "return_relations": True,
                "flat_ner": False,
            }
            for candidate_kwargs in (
                kwargs,
                {key: value for key, value in kwargs.items() if key != "flat_ner"},
                {
                    "texts": texts,
                    "labels": entity_labels,
                    "relations": relation_labels,
                    "return_relations": True,
                    "batch_size": batch_size,
                },
            ):
                try:
                    raw = method(**candidate_kwargs)
                    return _split_batch_raw(raw, len(texts))
                except TypeError:
                    continue
                except Exception as exc:
                    _raise_local_cuda_error(exc)
                    raise

        # Preferred relation-aware shapes first.
        for method_name in (
            "batch_predict_relations",
            "predict_relations",
            "extract_relations",
        ):
            method = getattr(model, method_name, None)
            if method is None or not relation_mode or not relation_labels:
                continue
            try:
                raw = method(
                    texts=texts,
                    labels=entity_labels,
                    relations=relation_labels,
                    return_relations=True,
                    batch_size=batch_size,
                )
            except TypeError:
                try:
                    raw = method(texts, entity_labels, relation_labels)
                except TypeError:
                    continue
                except Exception as exc:
                    _raise_local_cuda_error(exc)
                    raise
            except Exception as exc:
                _raise_local_cuda_error(exc)
                raise
            return _split_batch_raw(raw, len(texts))

        # GLiNER's multitask relation helper is implemented as two model.run()
        # passes: entities first, then "source <> relation" labels. Reimplement
        # the tiny inference path here so the production backend does not need
        # the optional datasets dependency imported by gliner.multitask.
        run = getattr(model, "run", None)
        if callable(run):
            try:
                entity_predictions = run(
                    texts,
                    entity_labels,
                    threshold=_LOCAL_ENTITY_THRESHOLD,
                    batch_size=batch_size,
                )
                entity_items = _split_batch_raw(entity_predictions, len(texts))
                if not relation_mode or not relation_labels:
                    return [{"entities": item, "relations": []} for item in entity_items]
                source_relation_labels = _source_relation_labels(
                    entity_items,
                    relation_labels,
                    max_sources=source_entity_cap,
                )
                if not any(source_relation_labels):
                    return [{"entities": item, "relations": []} for item in entity_items]
                relation_prompts = [
                    f"Extract relationships between entities from the text:\n{text}"
                    for text in texts
                ]
                try:
                    relation_predictions = run(
                        relation_prompts,
                        source_relation_labels,
                        threshold=_LOCAL_RELATION_THRESHOLD,
                        batch_size=batch_size,
                    )
                except Exception as exc:
                    _raise_local_cuda_error(exc)
                    raise
                relation_items = _relations_from_gliner_run_predictions(
                    _split_batch_raw(relation_predictions, len(texts))
                )
                return [
                    {
                        "entities": entity_items[i] if i < len(entity_items) else [],
                        "relations": relation_items[i] if i < len(relation_items) else [],
                    }
                    for i in range(len(texts))
                ]
            except (LocalGraphOOMError, LocalGraphFatalCudaError):
                raise
            except Exception as exc:
                _raise_local_cuda_error(exc)
                logger.info(
                    "phase=local_graph_relation_run_unavailable model=%s error=%s",
                    self.model_name,
                    exc,
                )

        # Entity-only GLiNER variants can still improve node coverage. The
        # relation compiler will preserve graph truthfulness by not inventing
        # edges when the local model did not produce one.
        method = getattr(model, "batch_predict_entities", None) or getattr(
            model, "predict_entities", None
        )
        if method is None:
            raise LocalGraphDependencyError(
                f"Model {self.model_name!r} does not expose a supported GLiNER inference method."
            )
        try:
            raw = method(texts, entity_labels)
        except TypeError:
            try:
                raw = [method(text, entity_labels) for text in texts]
            except Exception as exc:
                _raise_local_cuda_error(exc)
                raise
        except Exception as exc:
            _raise_local_cuda_error(exc)
            raise
        return [{"entities": item, "relations": []} for item in _split_batch_raw(raw, len(texts))]


class Gliner2Adapter:
    """Adapter for fastino/gliner2-* unified extraction models.

    GLiNER2 is optional and its API is still moving, so this adapter tries the
    common schema/relation method shapes and normalizes whatever comes back
    into the same raw {"entities": ..., "relations": ...} contract used by the
    existing compiler.
    """

    def __init__(self, model_name: str, device: str):
        self.model_name = model_name
        self.device = device
        self.model = self._load_model(model_name, device)

    @staticmethod
    def _load_model(model_name: str, device: str) -> Any:
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        os.environ.setdefault("TQDM_DISABLE", "1")
        try:
            from gliner2 import GLiNER2  # type: ignore
        except Exception as first_exc:  # pragma: no cover - optional dependency
            try:
                from gliner2 import GLiNER as GLiNER2  # type: ignore
            except Exception as second_exc:  # pragma: no cover - optional dependency
                raise LocalGraphDependencyError(
                    "Optional package 'gliner2' is not installed. Install the "
                    "local graph extraction extras before enabling local_gliner2."
                ) from second_exc
        model = GLiNER2.from_pretrained(model_name)
        if hasattr(model, "to"):
            model = model.to(device)
        return model

    def infer_batch(
        self,
        texts: list[str],
        *,
        entity_labels: list[str],
        relation_labels: list[str],
        batch_size: int = 1,
        relation_mode: bool = True,
        source_entity_cap: int = _MAX_LOCAL_RELATION_SOURCE_ENTITIES,
    ) -> list[Any]:
        del source_entity_cap
        model = self.model
        relations = [label for label in relation_labels if label and label != "related_to"]
        batch_size = max(1, min(len(texts) or 1, int(batch_size or 1)))

        batch_entities = getattr(model, "batch_extract_entities", None)
        batch_relations = getattr(model, "batch_extract_relations", None)
        if callable(batch_entities) and callable(batch_relations):
            try:
                entity_raw = batch_entities(
                    texts,
                    entity_labels,
                    batch_size=batch_size,
                    threshold=_LOCAL_ENTITY_THRESHOLD,
                    format_results=True,
                    include_confidence=True,
                    include_spans=True,
                )
                relation_raw = (
                    batch_relations(
                        texts,
                        relations,
                        batch_size=batch_size,
                        threshold=_LOCAL_RELATION_THRESHOLD,
                        format_results=True,
                        include_confidence=True,
                        include_spans=True,
                    )
                    if relation_mode and relations
                    else [{} for _ in texts]
                )
                ent_items = _split_batch_raw(entity_raw, len(texts))
                rel_items = _split_batch_raw(relation_raw, len(texts))
                normalized: list[dict[str, Any]] = []
                for i in range(len(texts)):
                    ent_payload = ent_items[i] if i < len(ent_items) else []
                    rel_payload = rel_items[i] if i < len(rel_items) else []
                    row: dict[str, Any] = {
                        "entities": ent_payload.get("entities") if isinstance(ent_payload, dict) else ent_payload,
                    }
                    if isinstance(rel_payload, dict) and "relation_extraction" in rel_payload:
                        row["relation_extraction"] = rel_payload.get("relation_extraction") or {}
                    else:
                        row["relations"] = rel_payload.get("relations") if isinstance(rel_payload, dict) else rel_payload
                    normalized.append(row)
                return normalized
            except Exception as exc:
                _raise_local_cuda_error(exc)
                raise

        schema = {
            "entities": entity_labels,
            "relations": relations if relation_mode else [],
        }
        kwargs_variants = (
            {
                "texts": texts,
                "labels": entity_labels,
                "relations": relations,
                "schema": schema,
                "batch_size": batch_size,
                "return_relations": bool(relation_mode and relations),
            },
            {
                "texts": texts,
                "schema": schema,
                "batch_size": batch_size,
            },
            {
                "text": texts,
                "schema": schema,
            },
        )
        for method_name in ("inference", "predict", "extract", "run"):
            method = getattr(model, method_name, None)
            if not callable(method):
                continue
            for kwargs in kwargs_variants:
                try:
                    raw = method(**kwargs)
                    return _split_batch_raw(raw, len(texts))
                except TypeError:
                    continue
                except Exception as exc:
                    _raise_local_cuda_error(exc)
                    raise
        raise LocalGraphDependencyError(
            f"Model {self.model_name!r} does not expose a supported GLiNER2 extraction method."
        )


_ADAPTER_CACHE: dict[tuple[str, str, str], Any] = {}
_ADAPTER_LOAD_LOCK = threading.Lock()
_ADAPTER_FACTORY: Callable[[str, str], GlinerRelexAdapter] = GlinerRelexAdapter
_GLINER2_ADAPTER_FACTORY: Callable[[str, str], Gliner2Adapter] = Gliner2Adapter


def _adapter(model_name: str, device: str, *, engine: str = "local_gliner") -> Any:
    normalized_engine = _normalize_local_engine(engine)
    key = (normalized_engine, model_name, device)
    if key not in _ADAPTER_CACHE:
        with _ADAPTER_LOAD_LOCK:
            if key not in _ADAPTER_CACHE:
                if normalized_engine == "local_gliner2":
                    _ADAPTER_CACHE[key] = _GLINER2_ADAPTER_FACTORY(model_name, device)
                elif normalized_engine == "local_glirel_optional":
                    raise LocalGraphDependencyError(
                        "local_glirel_optional is not implemented as a default production lane. "
                        "GLiREL is CC BY-NC-SA 4.0 and must be enabled through an explicit "
                        "evaluation-only adapter."
                    )
                else:
                    _ADAPTER_CACHE[key] = _ADAPTER_FACTORY(model_name, device)
    return _ADAPTER_CACHE[key]


def _split_batch_raw(raw: Any, expected: int) -> list[Any]:
    if isinstance(raw, tuple) and len(raw) == 2:
        entities, relations = raw
        ent_items = _split_batch_raw(entities, expected)
        rel_items = _split_batch_raw(relations, expected)
        return [
            {
                "entities": ent_items[i] if i < len(ent_items) else [],
                "relations": rel_items[i] if i < len(rel_items) else [],
            }
            for i in range(expected)
        ]
    if isinstance(raw, dict):
        if "results" in raw:
            return _split_batch_raw(raw["results"], expected)
        if expected == 1:
            return [raw]
        # Some APIs return {"entities": [[...]], "relations": [[...]]}.
        if isinstance(raw.get("entities"), list) or isinstance(raw.get("relations"), list):
            entities = _split_batch_raw(raw.get("entities") or [[] for _ in range(expected)], expected)
            relations = _split_batch_raw(raw.get("relations") or [[] for _ in range(expected)], expected)
            return [
                {
                    "entities": entities[i] if i < len(entities) else [],
                    "relations": relations[i] if i < len(relations) else [],
                }
                for i in range(expected)
            ]
        return [raw for _ in range(expected)]
    if isinstance(raw, list):
        if len(raw) == expected:
            return raw
        if expected == 1:
            return [raw]
        return raw[:expected] + [[] for _ in range(max(0, expected - len(raw)))]
    return [raw for _ in range(expected)]


def _entity_text_from_prediction(row: Any) -> str:
    if not isinstance(row, dict):
        return str(row or "").strip()
    return str(
        row.get("text")
        or row.get("canonical_name")
        or row.get("name")
        or row.get("entity")
        or row.get("span")
        or ""
    ).strip()


def _source_relation_labels(
    entity_predictions: list[Any],
    relation_labels: list[str],
    *,
    max_sources: int = _MAX_LOCAL_RELATION_SOURCE_ENTITIES,
) -> list[list[str]]:
    out: list[list[str]] = []
    max_sources = max(1, int(max_sources or _MAX_LOCAL_RELATION_SOURCE_ENTITIES))
    for raw_entities in entity_predictions:
        rows = raw_entities if isinstance(raw_entities, list) else []
        ranked = sorted(
            [row for row in rows if _entity_text_from_prediction(row)],
            key=lambda row: _coerce_confidence(row.get("score") if isinstance(row, dict) else None, 0.5),
            reverse=True,
        )
        seen: set[str] = set()
        sources: list[str] = []
        for row in ranked:
            text = _entity_text_from_prediction(row)
            key = text.lower()
            if not key or key in seen:
                continue
            seen.add(key)
            sources.append(text)
            if len(sources) >= max_sources:
                break
        labels: list[str] = []
        for source in sources:
            for relation in relation_labels:
                labels.append(f"{source} <> {relation}")
        out.append(labels)
    return out


def _relations_from_gliner_run_predictions(predictions: list[Any]) -> list[list[dict[str, Any]]]:
    out: list[list[dict[str, Any]]] = []
    for raw_prediction in predictions:
        rows = raw_prediction if isinstance(raw_prediction, list) else []
        relations: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            target = str(row.get("text") or row.get("target") or "").strip()
            label = str(row.get("label") or "").strip()
            if "<>" not in label or not target:
                continue
            source, relation = [part.strip() for part in label.split("<>", 1)]
            if not source or not relation or source.lower() == target.lower():
                continue
            relations.append(
                {
                    "source": source,
                    "relation": relation,
                    "target": target,
                    "score": _coerce_confidence(row.get("score"), 0.72),
                }
            )
        out.append(relations)
    return out


def _worker_specs(config: IngestionConfig) -> list[LocalWorkerSpec]:
    specs: list[LocalWorkerSpec] = []
    for row in getattr(config, "local_workers", None) or []:
        if not isinstance(row, dict):
            continue
        device = str(row.get("device") or "cpu")
        name = str(row.get("name") or device)
        batch_size = max(1, int(row.get("batch_size") or 1))
        weight = max(1, int(row.get("weight") or 1))
        specs.append(LocalWorkerSpec(device=device, name=name, batch_size=batch_size, weight=weight))
    return specs or [LocalWorkerSpec(device="cpu", name="cpu", batch_size=2, weight=1)]


def _normalize_local_engine(engine: str | None) -> str:
    value = str(engine or "local_gliner").strip().lower()
    if value in {"local_gliner", "local_gliner_relex"}:
        return "local_gliner_relex"
    if value == "local_gliner2":
        return "local_gliner2"
    if value == "hybrid_local_first":
        return "hybrid_local_first"
    return value


def _model_name_for_engine(config: IngestionConfig, engine: str) -> str:
    normalized = _normalize_local_engine(engine)
    if normalized == "local_gliner2":
        return str(
            getattr(config, "local_gliner2_model", None)
            or getattr(config, "local_extractor_model", None)
            or "fastino/gliner2-base-v1"
        )
    return str(
        getattr(config, "local_extractor_model", None)
        or "knowledgator/gliner-relex-large-v0.5"
    )


def _cuda_device_count() -> int | None:
    try:
        import torch  # type: ignore

        if not torch.cuda.is_available():
            return 0
        return int(torch.cuda.device_count())
    except Exception:
        return None


def _cuda_device_names() -> list[str] | None:
    try:
        import torch  # type: ignore

        if not torch.cuda.is_available():
            return []
        return [str(torch.cuda.get_device_name(i)) for i in range(torch.cuda.device_count())]
    except Exception:
        return None


def _tune_worker_for_detected_gpu(spec: LocalWorkerSpec, device_name: str | None) -> LocalWorkerSpec:
    """Prefer actual GPU capacity over possibly stale UI labels."""
    label = str(device_name or "").lower()
    if "3090" in label:
        return LocalWorkerSpec(
            device=spec.device,
            name="rtx_3090",
            batch_size=min(max(1, spec.batch_size), 8),
            weight=max(2, spec.weight),
        )
    if "4070" in label:
        return LocalWorkerSpec(
            device=spec.device,
            name="rtx_4070",
            batch_size=min(max(1, spec.batch_size), 4),
            weight=min(spec.weight, 1),
        )
    return spec


def _available_worker_specs(specs: list[LocalWorkerSpec]) -> list[LocalWorkerSpec]:
    cuda_count = _cuda_device_count()
    cuda_names = _cuda_device_names()
    if cuda_count is None:
        return specs
    available: list[LocalWorkerSpec] = []
    for spec in specs:
        if not spec.device.startswith("cuda"):
            available.append(spec)
            continue
        try:
            index = int(spec.device.split(":", 1)[1])
        except Exception:
            index = 0
        if cuda_count <= index:
            logger.warning(
                "phase=local_graph_worker_skip reason=cuda_device_missing worker=%s device=%s detected=%s",
                spec.name,
                spec.device,
                cuda_count,
            )
            continue
        detected_name = cuda_names[index] if cuda_names and index < len(cuda_names) else None
        tuned = _tune_worker_for_detected_gpu(spec, detected_name)
        if tuned != spec:
            logger.info(
                "phase=local_graph_worker_tuned device=%s detected=%s old=%s/%s/%s new=%s/%s/%s",
                spec.device,
                detected_name,
                spec.name,
                spec.batch_size,
                spec.weight,
                tuned.name,
                tuned.batch_size,
                tuned.weight,
            )
        available.append(tuned)
    if available:
        return available
    return [LocalWorkerSpec(device="cpu", name="cpu", batch_size=2, weight=1)]


def _estimated_tokens(text: str) -> int:
    return max(1, int(len(str(text or "").split()) * 1.3))


def _trim_to_budget(text: str, max_tokens: int, *, max_chars: int | None = None) -> str:
    raw = str(text or "")
    max_tokens = max(1, int(max_tokens or 1))
    max_chars = max(256, int(max_chars or max_tokens * 6))
    words = raw.split()
    if len(words) <= max_tokens and len(raw) <= max_chars:
        return raw

    chunks = [part.strip() for part in _SENTENCE_SPLIT_RE.split(raw) if part.strip()]
    selected: list[str] = []
    token_count = 0
    char_count = 0
    for chunk in chunks:
        # A single giant sentence can exceed the transformer budget by itself.
        # Break it into clauses before falling back to raw word clipping.
        pieces = [piece.strip() for piece in _CLAUSE_SPLIT_RE.split(chunk) if piece.strip()]
        for piece in pieces or [chunk]:
            piece_words = piece.split()
            next_tokens = token_count + len(piece_words)
            next_chars = char_count + len(piece) + (1 if selected else 0)
            if selected and (next_tokens > max_tokens or next_chars > max_chars):
                return " ".join(selected)
            if next_tokens > max_tokens or next_chars > max_chars:
                room = max(1, max_tokens - token_count)
                selected.extend(piece_words[:room])
                return " ".join(selected)
            selected.append(piece)
            token_count = next_tokens
            char_count = next_chars
    return " ".join(selected) if selected else " ".join(words[:max_tokens])[:max_chars]


_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_CLAUSE_SPLIT_RE = re.compile(r"\s+(?:and|but|while|whereas)\s+|[;:]")
_EXPLICIT_RELATION_CUES: list[tuple[str, tuple[str, ...]]] = [
    ("depends_on", (r"\bdepends?\s+on\b", r"\brequires?\b", r"\brelies?\s+on\b")),
    ("runs_on", (r"\bruns?\s+on\b", r"\brunning\s+on\b", r"\bdeployed\s+on\b", r"\boperates?\s+on\b")),
    ("uses", (r"\buses?\b", r"\busing\b")),
    ("stores", (r"\bstores?\b", r"\bstored\s+in\b", r"\bsaves?\b")),
    ("produces", (r"\bproduces?\b", r"\bgenerates?\b", r"\bcreates?\b")),
    ("calls", (r"\bcalls?\b", r"\binvokes?\b")),
    ("extracts", (r"\bextracts?\b", r"\breads?\s+from\b")),
    ("detects", (r"\bdetects?\b", r"\bidentifies?\b")),
    ("classifies", (r"\bclassifies?\b",)),
    ("measures", (r"\bmeasures?\b", r"\bevaluates?\b", r"\bquantifies?\b")),
    ("tests", (r"\btests?\b", r"\bvalidates?\b", r"\bchecks?\b")),
    ("references", (r"\breferences?\b", r"\bcites?\b")),
    ("supports", (r"\bsupports?\b", r"\benables?\b", r"\bprovides?\b")),
    ("defined_in", (r"\bdefined\s+in\b", r"\bspecified\s+in\b")),
    ("applied_to", (r"\bapplied\s+to\b", r"\bused\s+on\b")),
]


def _clean_markdown_for_local_extraction(text: str) -> str:
    """Remove markdown structures that tend to become noisy entity labels."""
    cleaned = _HTML_COMMENT_RE.sub(" ", str(text or ""))
    cleaned = _CODE_FENCE_RE.sub(" ", cleaned)
    lines: list[str] = []
    for raw in cleaned.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _TABLE_SEPARATOR_RE.match(line):
            continue
        # Keep short prose-like table rows, but drop wide data tables that
        # GLiNER often turns into pasted list entities.
        if line.count("|") >= 4:
            continue
        lines.append(line)
    return "\n".join(lines)


def format_task_text_for_local_model(
    task: ExtractionTask,
    *,
    max_tokens: int,
    max_chars: int | None = None,
) -> str:
    """Wrap child text with compact document/section context for GLiNER.

    The model sees the local markdown section boundary, but never the parent
    text or any giant document preamble. This keeps extraction grounded while
    preserving the app's parent/child chunking economics.
    """
    title = str(getattr(task, "document_title", None) or "").strip()
    heading_path = [
        str(item).strip()
        for item in (getattr(task, "heading_path", None) or [])
        if str(item).strip()
    ]
    chunk_kind = str(getattr(task, "chunk_kind", None) or "").strip()
    body = _clean_markdown_for_local_extraction(task.text)
    parts: list[str] = []
    if title:
        parts.append(f"Document: {title}")
    if heading_path:
        parts.append(f"Section: {' > '.join(heading_path[:6])}")
    if chunk_kind:
        parts.append(f"Chunk kind: {chunk_kind}")
    parts.append(f"Text:\n{body}")
    return _trim_to_budget("\n".join(parts), max_tokens, max_chars=max_chars)


def _model_tokenize(model: Any, text: str) -> list[Any]:
    processor = getattr(model, "data_processor", None)
    splitter = getattr(processor, "words_splitter", None)
    if callable(splitter):
        return list(splitter(text))
    tokenizer = getattr(model, "tokenizer", None) or getattr(processor, "tokenizer", None)
    encode = getattr(tokenizer, "encode", None)
    if callable(encode):
        try:
            return list(encode(text, add_special_tokens=False))
        except TypeError:
            return list(encode(text))
    return str(text or "").split()


def _model_detokenize(model: Any, tokens: list[Any]) -> str:
    tokenizer = getattr(model, "tokenizer", None) or getattr(getattr(model, "data_processor", None), "tokenizer", None)
    decode = getattr(tokenizer, "decode", None)
    if callable(decode) and tokens and all(isinstance(token, int) for token in tokens):
        try:
            return str(decode(tokens, skip_special_tokens=True)).strip()
        except TypeError:
            return str(decode(tokens)).strip()
        except Exception:
            pass
    return " ".join(str(token) for token in tokens).strip()


def _cap_text_for_model(adapter: Any, text: str, max_model_tokens: int) -> tuple[str, int, bool]:
    max_model_tokens = max(1, int(max_model_tokens or 384))
    raw = str(text or "")
    try:
        tokens = _model_tokenize(getattr(adapter, "model", None), raw)
    except Exception:
        fallback = _trim_to_budget(raw, max_model_tokens, max_chars=max_model_tokens * 6)
        return fallback, len(fallback.split()), fallback != raw
    token_count = len(tokens)
    if token_count <= max_model_tokens:
        return raw, token_count, False
    capped = _model_detokenize(getattr(adapter, "model", None), tokens[:max_model_tokens])
    if not capped:
        capped = _trim_to_budget(raw, max_model_tokens, max_chars=max_model_tokens * 6)
    return capped, token_count, True


def _relation_labels(schema: SchemaContext | None) -> list[str]:
    labels = list((schema.relation_schema if schema else None) or [])
    return [label for label in labels if label and label != "related_to"] or ["uses", "part_of", "references"]


def _configured_relation_label_cap(config: IngestionConfig) -> tuple[int, int]:
    soft = int(getattr(config, "local_relation_max_labels", 12) or 12)
    hard = int(getattr(config, "local_relation_hard_max_labels", 16) or 16)
    hard = max(1, min(32, hard))
    soft = max(1, min(soft, hard))
    return soft, hard


def _schema_lens_relation_hints(schema_lens: SchemaLens | dict | None) -> list[str]:
    if isinstance(schema_lens, SchemaLens):
        values = schema_lens.preferred_relations or []
    elif isinstance(schema_lens, dict):
        values = schema_lens.get("preferred_relations") or schema_lens.get("relations") or []
    else:
        values = []
    return [str(value).strip() for value in values if str(value or "").strip()]


def _relation_cue_matches(text: str) -> list[str]:
    lowered = str(text or "").lower()
    matches: list[str] = []
    for relation, patterns in _EXPLICIT_RELATION_CUES:
        if any(re.search(pattern, lowered) for pattern in patterns):
            matches.append(relation)
    return matches


def _select_relation_labels_for_batch(
    all_relation_labels: list[str],
    tasks: list[ExtractionTask],
    texts: list[str],
    *,
    schema_lens: SchemaLens | dict | None,
    config: IngestionConfig,
) -> list[str]:
    allowed = {str(label).strip(): None for label in all_relation_labels if str(label or "").strip()}
    allowed.pop("related_to", None)
    soft_cap, hard_cap = _configured_relation_label_cap(config)
    selected: list[str] = []

    def add(label: str) -> None:
        clean = str(label or "").strip()
        if not clean or clean == "related_to" or clean not in allowed or clean in selected:
            return
        selected.append(clean)

    for label in _schema_lens_relation_hints(schema_lens):
        add(label)
    for task, text in zip(tasks, texts):
        combined = "\n".join(
            [
                str(getattr(task, "document_title", "") or ""),
                " ".join(str(item) for item in (getattr(task, "heading_path", None) or [])),
                str(getattr(task, "chunk_kind", "") or ""),
                str(getattr(task, "text", "") or ""),
                str(text or ""),
            ]
        )
        for label in _relation_cue_matches(combined):
            add(label)
    for label in _DEFAULT_LOCAL_RELATION_LABELS:
        add(label)
    # Fill a couple of slots with ontology order only if the batch gave too
    # few hints. Never pass the whole relation vocabulary to GLiNER-relex.
    for label in all_relation_labels:
        if len(selected) >= soft_cap:
            break
        add(label)
    return selected[: min(hard_cap, soft_cap)]


def _entity_labels(schema: SchemaContext | None) -> list[str]:
    return list((schema.entity_schema if schema else None) or []) or [
        "Person",
        "Organization",
        "Product",
        "Concept",
        "Document",
        "other",
    ]


def _coerce_confidence(value: Any, default: float = 0.75) -> float:
    try:
        if isinstance(value, str):
            value = value.strip().rstrip("%")
        score = float(value)
        if score > 1.0:
            score = score / 100.0
        return max(0.0, min(1.0, score))
    except Exception:
        return default


def _text_from_endpoint(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("text", "name", "label", "value", "entity", "span"):
            if value.get(key):
                return str(value[key])
    return str(value or "")


def _short_evidence(text: str, words: int = 24) -> str:
    return " ".join(str(text or "").split()[:words])


_CONTEXT_ONLY_TERMS = {
    "document",
    "section",
    "chunk",
    "chunk kind",
    "kind",
    "text",
    "body",
}
_ENTITY_STOPWORDS = {
    "use",
    "uses",
    "using",
    "store",
    "stores",
    "stored",
    "run",
    "runs",
    "support",
    "supports",
    "detect",
    "detects",
    "classify",
    "classifies",
    "measure",
    "measures",
    "test",
    "tests",
}


def _norm_label(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _metadata_fragments(task: ExtractionTask | None) -> list[str]:
    if task is None:
        return []
    fragments = [str(getattr(task, "document_title", None) or "")]
    fragments.extend(str(item) for item in (getattr(task, "heading_path", None) or []))
    fragments.append(str(getattr(task, "chunk_kind", None) or ""))
    return [_norm_label(item) for item in fragments if _norm_label(item)]


def _is_metadata_only_candidate(
    value: str,
    task: ExtractionTask | None,
    *,
    entity_type: str | None = None,
) -> bool:
    candidate = _norm_label(value)
    if not candidate:
        return True
    if candidate in _CONTEXT_ONLY_TERMS:
        return True
    if candidate in _ENTITY_STOPWORDS:
        return True
    if task is None:
        return False
    body = _norm_label(task.text)
    if candidate and candidate in body:
        return False
    for fragment in _metadata_fragments(task):
        if candidate == fragment or candidate in fragment:
            return True
    return False


def _entities_from_raw(raw: Any, *, task: ExtractionTask | None = None) -> list[EntityItem]:
    rows: list[Any]
    if isinstance(raw, dict):
        rows = raw.get("entities") or raw.get("entity") or []
        if isinstance(rows, dict):
            flattened: list[dict[str, Any]] = []
            for label, values in rows.items():
                items = values if isinstance(values, list) else [values]
                for item in items:
                    if isinstance(item, dict):
                        text = item.get("text") or item.get("span") or item.get("value") or item.get("entity")
                        flattened.append({**item, "text": text, "label": item.get("label") or label})
                    else:
                        flattened.append({"text": str(item or ""), "label": label, "score": 0.75})
            rows = flattened
    else:
        rows = raw if isinstance(raw, list) else []
    out: list[EntityItem] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            text = str(row or "").strip()
            label = "Concept"
            score = 0.7
        else:
            text = str(
                row.get("canonical_name")
                or row.get("text")
                or row.get("name")
                or row.get("entity")
                or row.get("span")
                or ""
            ).strip()
            label = str(row.get("entity_type") or row.get("type") or row.get("label") or "Concept")
            score = _coerce_confidence(row.get("confidence", row.get("score")), 0.75)
        if not text:
            continue
        if _is_metadata_only_candidate(text, task, entity_type=label):
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(
            EntityItem(
                canonical_name=text,
                surface_form=text,
                entity_type=label,
                confidence=score,
            )
        )
    return out


def _relations_from_raw(
    raw: Any,
    text: str,
    *,
    task: ExtractionTask | None = None,
) -> list[RelationItem]:
    if isinstance(raw, dict):
        rows = raw.get("relations") or raw.get("relationships") or raw.get("triples") or []
        if not rows and isinstance(raw.get("relation_extraction"), dict):
            flattened: list[dict[str, Any]] = []
            for predicate, values in raw["relation_extraction"].items():
                items = values if isinstance(values, list) else [values]
                for item in items:
                    if isinstance(item, dict):
                        flattened.append(
                            {
                                "subject": item.get("head") or item.get("subject") or item.get("source"),
                                "predicate": predicate,
                                "object": item.get("tail") or item.get("object") or item.get("target"),
                                "confidence": item.get("confidence") or item.get("score"),
                                "evidence": item.get("evidence") or item.get("text"),
                            }
                        )
                    elif isinstance(item, (list, tuple)) and len(item) >= 2:
                        flattened.append(
                            {
                                "subject": item[0],
                                "predicate": predicate,
                                "object": item[1],
                                "confidence": item[2] if len(item) > 2 else 0.72,
                            }
                        )
            rows = flattened
    else:
        rows = []
    out: list[RelationItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        subject = (
            _text_from_endpoint(row.get("subject"))
            or _text_from_endpoint(row.get("head"))
            or _text_from_endpoint(row.get("source"))
            or _text_from_endpoint(row.get("from"))
        )
        obj = (
            _text_from_endpoint(row.get("object"))
            or _text_from_endpoint(row.get("tail"))
            or _text_from_endpoint(row.get("target"))
            or _text_from_endpoint(row.get("to"))
        )
        predicate = str(
            row.get("predicate")
            or row.get("relation")
            or row.get("label")
            or row.get("type")
            or "related_to"
        ).strip()
        if not subject or not obj or not predicate:
            continue
        if _is_metadata_only_candidate(subject, task) or _is_metadata_only_candidate(obj, task):
            continue
        score = _coerce_confidence(row.get("confidence", row.get("score")), 0.72)
        evidence = str(row.get("evidence") or row.get("evidence_phrase") or _short_evidence(text))
        atomic = str(row.get("atomic_fact") or f"{subject} {predicate} {obj}.")
        alternatives = row.get("alternative_predicates_considered") or []
        if isinstance(alternatives, str):
            alternatives = [alternatives]
        out.append(
            RelationItem(
                subject=subject,
                predicate=predicate,
                object=obj,
                object_kind="entity",
                confidence=score,
                evidence_phrase=_short_evidence(evidence),
                relation_cue=str(row.get("relation_cue") or predicate),
                source_predicate=predicate,
                predicate_confidence=_coerce_confidence(
                    row.get("predicate_confidence", row.get("confidence", row.get("score"))),
                    score,
                ),
                extraction_confidence=_coerce_confidence(
                    row.get("extraction_confidence", row.get("confidence", row.get("score"))),
                    score,
                ),
                alternative_predicates_considered=list(alternatives)[:2],
                rejection_reasoning=str(row.get("rejection_reasoning") or "")[:160],
                atomic_fact=atomic,
                candidate_subject=subject,
                candidate_predicate=predicate,
                candidate_object=obj,
            )
        )
    return out


def _explicit_cue_relations_from_text(
    entities: list[EntityItem],
    text: str,
    existing: list[RelationItem],
) -> list[RelationItem]:
    """Create conservative local relation candidates from explicit lexical cues.

    This is not semantic guessing. It only fires when two extracted entities
    occur in the same sentence and a cross-domain ontology cue appears between
    them, then the existing compiler still handles aliases, direction, and
    domain/range validation.
    """
    if len(entities) < 2:
        return []
    existing_keys = {
        (
            str(relation.subject or "").lower(),
            str(relation.predicate or "").lower(),
            str(relation.object or "").lower(),
        )
        for relation in existing
    }
    out: list[RelationItem] = []
    for sentence in _SENTENCE_SPLIT_RE.split(str(text or "")):
        clauses = [clause for clause in _CLAUSE_SPLIT_RE.split(sentence) if clause.strip()]
        for clause in clauses:
            sentence = " ".join(clause.split())
            if not sentence:
                continue
            lowered = sentence.lower()
            mentions: list[tuple[int, int, EntityItem]] = []
            for entity in entities:
                name = str(entity.canonical_name or "").strip()
                if not name or len(name) < 2:
                    continue
                idx = lowered.find(name.lower())
                if idx < 0:
                    continue
                mentions.append((idx, idx + len(name), entity))
            mentions.sort(key=lambda item: item[0])
            for i, (src_start, src_end, source) in enumerate(mentions):
                for obj_start, _obj_end, target in mentions[i + 1 :]:
                    if source.canonical_name.lower() == target.canonical_name.lower():
                        continue
                    between = lowered[src_end:obj_start]
                    predicate = ""
                    for candidate, patterns in _EXPLICIT_RELATION_CUES:
                        if any(re.search(pattern, between) for pattern in patterns):
                            predicate = candidate
                            break
                    if not predicate:
                        continue
                    key = (
                        source.canonical_name.lower(),
                        predicate.lower(),
                        target.canonical_name.lower(),
                    )
                    if key in existing_keys:
                        continue
                    existing_keys.add(key)
                    out.append(
                        RelationItem(
                            subject=source.canonical_name,
                            predicate=predicate,
                            object=target.canonical_name,
                            object_kind="entity",
                            confidence=0.72,
                            evidence_phrase=_short_evidence(sentence),
                            relation_cue=predicate,
                            source_predicate=predicate,
                            predicate_confidence=0.72,
                            extraction_confidence=min(
                                0.8,
                                max(0.55, (float(source.confidence) + float(target.confidence)) / 2),
                            ),
                            alternative_predicates_considered=[],
                            rejection_reasoning="Explicit cue between entities.",
                            atomic_fact=f"{source.canonical_name} {predicate} {target.canonical_name}.",
                            candidate_subject=source.canonical_name,
                            candidate_predicate=predicate,
                            candidate_object=target.canonical_name,
                        )
                    )
    return out


def _complete_endpoint_entities(entities: list[EntityItem], relations: list[RelationItem]) -> list[EntityItem]:
    by_key = {entity.canonical_name.lower(): entity for entity in entities}
    for relation in relations:
        for name in (relation.subject, relation.object):
            key = str(name or "").lower()
            if not key or key in by_key:
                continue
            by_key[key] = EntityItem(
                canonical_name=str(name),
                surface_form=str(name),
                entity_type="Concept",
                confidence=max(0.5, float(relation.extraction_confidence or relation.confidence or 0.5)),
            )
    return list(by_key.values())


def _candidate_facts_from_relations(relations: list[RelationItem]) -> list[CandidateFactItem]:
    return [
        CandidateFactItem(
            atomic_fact=relation.atomic_fact or f"{relation.subject} {relation.predicate} {relation.object}.",
            candidate_subject=relation.candidate_subject or relation.subject,
            candidate_predicate=relation.candidate_predicate or relation.source_predicate or relation.predicate,
            candidate_object=relation.candidate_object or relation.object,
            predicate_confidence=float(relation.predicate_confidence or relation.confidence or 0.0),
            extraction_confidence=float(relation.extraction_confidence or relation.confidence or 0.0),
            alternative_predicates_considered=list(relation.alternative_predicates_considered or [])[:2],
            rejection_reasoning=relation.rejection_reasoning,
            evidence_phrase=relation.evidence_phrase,
            object_kind=relation.object_kind,
            relation_cue=relation.relation_cue,
        )
        for relation in relations
    ]


def _result_from_local_raw(
    raw: Any,
    task: ExtractionTask,
    *,
    schema: SchemaContext | None,
    schema_lens: SchemaLens | dict | None,
    text: str,
) -> ExtractionResult:
    entities = _entities_from_raw(raw, task=task)
    relations = _relations_from_raw(raw, text, task=task)
    relations.extend(_explicit_cue_relations_from_text(entities, task.text, relations))
    entities = _complete_endpoint_entities(entities, relations)
    entities, relations, counters = compile_extraction_candidates(entities, relations, schema)
    lens = (
        schema_lens
        if isinstance(schema_lens, SchemaLens)
        else SchemaLens.from_dict(schema_lens if isinstance(schema_lens, dict) else None)
    )
    return ExtractionResult(
        schema_version="polymath.extract.local.v1",
        chunk_id=task.chunk_id,
        doc_id=task.doc_id,
        corpus_id=task.corpus_id,
        entities=entities,
        candidate_facts=_candidate_facts_from_relations(relations),
        relations=relations,
        entity_remap_count=counters["entity_remap_count"],
        entity_drop_count=counters["entity_drop_count"],
        relation_remap_count=counters["relation_remap_count"],
        relation_drop_count=counters["relation_drop_count"],
        domain_range_remap_count=counters["domain_range_remap_count"],
        domain_range_warn_count=counters["domain_range_warn_count"],
        endpoint_completion_count=counters["endpoint_completion_count"],
        evidence_cue_repair_count=counters["evidence_cue_repair_count"],
        direction_repair_count=counters["direction_repair_count"],
        schema_lens_id=lens.lens_id if lens else None,
    )


_CUDA_FATAL_PATTERNS = (
    "illegal memory access",
    "device-side assert",
    "unspecified launch failure",
    "context is destroyed",
    "context destroyed",
    "cuda error 700",
    "cuda error: an illegal memory access",
)
_CUDA_OOM_PATTERNS = (
    "out of memory",
    "cuda oom",
    "cublas",
    "cudnn",
    "cuda error 2",
)


def _cuda_error_type(exc: Exception) -> str | None:
    text = f"{exc.__class__.__name__}: {exc}".lower()
    if any(pattern in text for pattern in _CUDA_FATAL_PATTERNS):
        return "local_cuda_fatal"
    if any(pattern in text for pattern in _CUDA_OOM_PATTERNS):
        return "local_cuda_oom"
    return None


def _is_oom(exc: Exception) -> bool:
    return _cuda_error_type(exc) == "local_cuda_oom"


def _is_cuda_fatal(exc: Exception) -> bool:
    return _cuda_error_type(exc) == "local_cuda_fatal"


def _raise_local_cuda_error(exc: Exception) -> None:
    error_type = _cuda_error_type(exc)
    if error_type == "local_cuda_fatal":
        _clear_cuda_cache()
        raise LocalGraphFatalCudaError(str(exc)) from exc
    if error_type == "local_cuda_oom":
        _clear_cuda_cache()
        raise LocalGraphOOMError(str(exc)) from exc


def _clear_cuda_cache() -> None:
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        return


def _gpu_memory_snapshot(device: str) -> dict[str, Any]:
    if not str(device or "").startswith("cuda"):
        return {}
    try:
        import torch  # type: ignore

        if not torch.cuda.is_available():
            return {}
        index = int(str(device).split(":", 1)[1]) if ":" in str(device) else 0
        free_bytes, total_bytes = torch.cuda.mem_get_info(index)
        return {
            "gpu_memory_free_mb": round(free_bytes / (1024 * 1024), 2),
            "gpu_memory_total_mb": round(total_bytes / (1024 * 1024), 2),
            "gpu_memory_used_mb": round((total_bytes - free_bytes) / (1024 * 1024), 2),
        }
    except Exception:
        return {}


def _diagnostics_enabled(config: IngestionConfig) -> bool:
    return bool(getattr(config, "local_graph_diagnostics_enabled", False))


def _diagnostics_dir(config: IngestionConfig) -> Path:
    configured = str(
        getattr(config, "local_graph_diagnostics_dir", "")
        or os.getenv("LOCAL_GRAPH_DIAGNOSTICS_DIR", "")
        or "local_graph_diagnostics"
    )
    return Path(configured)


def _write_diagnostic_record(config: IngestionConfig, record: dict[str, Any]) -> None:
    if not _diagnostics_enabled(config):
        return
    try:
        directory = _diagnostics_dir(config)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{record.get('diagnostic_run_id', 'local_graph')}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception as exc:
        logger.warning("phase=local_graph_diagnostic_write_failed error=%s", exc)


def _text_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", errors="ignore")).hexdigest()[:16]


def _weighted_assign(tasks: list[ExtractionTask], specs: list[LocalWorkerSpec]) -> dict[int, list[ExtractionTask]]:
    slots: list[int] = []
    for idx, spec in enumerate(specs):
        slots.extend([idx] * max(1, spec.weight))
    assigned = {idx: [] for idx in range(len(specs))}
    for index, task in enumerate(tasks):
        assigned[slots[index % len(slots)]].append(task)
    return assigned


def _make_batches(
    tasks: list[ExtractionTask],
    *,
    batch_size: int,
    max_chunk_tokens: int,
    max_chunks_in_memory: int,
) -> list[list[ExtractionTask]]:
    batches: list[list[ExtractionTask]] = []
    current: list[ExtractionTask] = []
    current_tokens = 0
    token_budget = max(1, max_chunk_tokens * max(1, batch_size))
    for task in tasks:
        tokens = min(_estimated_tokens(task.text), max_chunk_tokens)
        if current and (len(current) >= batch_size or current_tokens + tokens > token_budget):
            batches.append(current)
            current = []
            current_tokens = 0
        current.append(task)
        current_tokens += tokens
    if current:
        batches.append(current)
    return batches


async def _extract_worker(
    *,
    worker_idx: int,
    spec: LocalWorkerSpec,
    tasks: list[ExtractionTask],
    config: IngestionConfig,
    schema: SchemaContext | None,
    schema_lens: SchemaLens | dict | None,
) -> tuple[list[ExtractionResult], list[ExtractionFailureItem], list[dict], LocalWorkerStats]:
    stats = LocalWorkerStats(
        device=spec.device,
        name=spec.name,
        current_batch_size=spec.batch_size,
    )
    results: list[ExtractionResult] = []
    failures: list[ExtractionFailureItem] = []
    call_metrics: list[dict] = []
    if not tasks:
        return results, failures, call_metrics, stats

    engine = _normalize_local_engine(getattr(config, "graph_extraction_engine", "local_gliner"))
    model_name = _model_name_for_engine(config, engine)
    started_load = time.perf_counter()
    try:
        adapter = await asyncio.to_thread(_adapter, model_name, spec.device, engine=engine)
    except LocalGraphDependencyError:
        raise
    except Exception as exc:
        error_type = _cuda_error_type(exc) or "local_extractor_load_error"
        stats.chunks_failed += len(tasks)
        failures.extend(
            ExtractionFailureItem(
                chunk_id=task.chunk_id,
                doc_id=task.doc_id,
                corpus_id=task.corpus_id,
                model=model_name,
                lane=worker_idx,
                attempts=0,
                error_type=error_type,
                error_message=str(exc)[:1000],
                retryable=True,
                retry_after=None,
                lane_state=spec.device,
            )
            for task in tasks
        )
        call_metrics.append(
            {
                "chunk_id": ",".join(task.chunk_id for task in tasks[:4]),
                "model": model_name,
                "lane": worker_idx,
                "attempt": 0,
                "duration_seconds": round(time.perf_counter() - started_load, 3),
                "total_tokens": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "success": False,
                "error_type": error_type,
                "recovery_mode": False,
                "max_tokens": 0,
                "local_graph": True,
                "gpu_device": spec.device,
                "batch_size": 0,
            }
        )
        logger.warning(
            "phase=local_graph_model_load_failed worker=%s device=%s model=%s error=%s",
            spec.name,
            spec.device,
            model_name,
            exc,
        )
        return results, failures, call_metrics, stats
    logger.info(
        "phase=local_graph_model_ready worker=%s device=%s model=%s load_or_cache=%.2fs",
        spec.name,
        spec.device,
        model_name,
        time.perf_counter() - started_load,
    )
    entity_labels = _entity_labels(schema)
    relation_labels = _relation_labels(schema)
    batch_size = max(1, spec.batch_size)
    max_chunk_tokens = int(getattr(config, "max_chunk_tokens_for_local_extractor", 768) or 768)
    max_input_chars = int(getattr(config, "local_gliner_max_input_chars", 3500) or 3500)
    max_model_tokens = int(getattr(config, "local_model_max_tokens", 384) or 384)
    max_chunks_in_memory = int(getattr(config, "max_chunks_in_memory", 100) or 100)
    source_entity_cap = int(
        getattr(config, "local_relation_max_source_entities", _MAX_LOCAL_RELATION_SOURCE_ENTITIES)
        or _MAX_LOCAL_RELATION_SOURCE_ENTITIES
    )
    relation_oom_disable_after = int(getattr(config, "local_relation_oom_disable_after", 1) or 1)
    entity_only_on_relation_oom = bool(getattr(config, "local_entity_only_on_relation_oom", True))
    relation_mode_enabled = True
    relation_disabled_reason: str | None = None
    diagnostic_run_id = (
        f"{tasks[0].doc_id[:12] if tasks else 'local'}-"
        f"{int(time.time() * 1000)}-w{worker_idx}"
    )
    model_token_counts_all: list[int] = []
    truncated_chunks = 0

    pending_batches = _make_batches(
        tasks,
        batch_size=batch_size,
        max_chunk_tokens=max_chunk_tokens,
        max_chunks_in_memory=max_chunks_in_memory,
    )
    while pending_batches:
        batch = pending_batches.pop(0)
        active_batch = batch
        retried_oom = False
        while active_batch:
            formatted_texts = [
                format_task_text_for_local_model(
                    task,
                    max_tokens=max_chunk_tokens,
                    max_chars=max_input_chars,
                )
                for task in active_batch
            ]
            capped: list[tuple[str, int, bool]] = [
                _cap_text_for_model(adapter, text, max_model_tokens)
                for text in formatted_texts
            ]
            texts = [item[0] for item in capped]
            model_token_counts = [int(item[1]) for item in capped]
            model_token_counts_all.extend(model_token_counts)
            batch_truncated = sum(1 for _text, _count, was_truncated in capped if was_truncated)
            truncated_chunks += batch_truncated
            stats.model_token_count_total += sum(model_token_counts)
            stats.model_token_count_observations += len(model_token_counts)
            stats.model_token_count_max = max(
                stats.model_token_count_max,
                max(model_token_counts or [0]),
            )
            stats.model_token_truncated_chunks += batch_truncated
            active_relation_labels = (
                _select_relation_labels_for_batch(
                    relation_labels,
                    active_batch,
                    texts,
                    schema_lens=schema_lens,
                    config=config,
                )
                if relation_mode_enabled
                else []
            )
            relation_label_count = len(active_relation_labels)
            pre_record = {
                "event": "pre_call",
                "diagnostic_run_id": diagnostic_run_id,
                "timestamp": datetime.utcnow().isoformat(),
                "doc_id": active_batch[0].doc_id if active_batch else None,
                "corpus_id": active_batch[0].corpus_id if active_batch else None,
                "chunk_ids": [task.chunk_id for task in active_batch],
                "text_hashes": [_text_hash(text) for text in texts],
                "char_counts": [len(text) for text in texts],
                "app_token_counts": [int(getattr(task, "token_count", 0) or _estimated_tokens(task.text)) for task in active_batch],
                "model_token_counts": model_token_counts,
                "model_token_max": max_model_tokens,
                "model_token_truncated": [item[2] for item in capped],
                "chunk_kinds": [str(getattr(task, "chunk_kind", "") or "") for task in active_batch],
                "relation_labels": active_relation_labels,
                "relation_label_count": relation_label_count,
                "source_entity_cap": source_entity_cap,
                "device": spec.device,
                "batch_size": len(active_batch),
                "model": model_name,
                "engine": engine,
                **_gpu_memory_snapshot(spec.device),
            }
            if bool(getattr(config, "local_graph_debug_log_text", False)):
                pre_record["texts"] = texts[: int(getattr(config, "local_graph_debug_text_limit", 3) or 3)]
            _write_diagnostic_record(config, pre_record)
            started = time.perf_counter()
            try:
                raw_items = await asyncio.to_thread(
                    adapter.infer_batch,
                    texts,
                    entity_labels=entity_labels,
                    relation_labels=active_relation_labels,
                    batch_size=min(batch_size, len(active_batch)),
                    relation_mode=relation_mode_enabled,
                    source_entity_cap=source_entity_cap,
                )
                duration = time.perf_counter() - started
                stats.duration_seconds += duration
                stats.chunks_processed += len(active_batch)
                stats.relation_label_total += relation_label_count * len(active_batch)
                stats.relation_label_max = max(stats.relation_label_max, relation_label_count)
                if relation_mode_enabled and active_relation_labels:
                    stats.relation_mode_chunks += len(active_batch)
                call_metrics.append(
                    {
                        "chunk_id": ",".join(task.chunk_id for task in active_batch[:4]),
                        "model": model_name,
                        "lane": worker_idx,
                        "attempt": 1,
                        "duration_seconds": round(duration, 3),
                        "total_tokens": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "success": True,
                        "error_type": None,
                        "recovery_mode": False,
                        "max_tokens": 0,
                        "local_graph": True,
                        "gpu_device": spec.device,
                        "batch_size": len(active_batch),
                        "relation_mode": relation_mode_enabled,
                        "relation_label_count": relation_label_count,
                        "relation_disabled_reason": relation_disabled_reason,
                        "model_token_truncated_chunks": batch_truncated,
                    }
                )
                _write_diagnostic_record(
                    config,
                    {
                        "event": "post_call",
                        "success": True,
                        "diagnostic_run_id": diagnostic_run_id,
                        "timestamp": datetime.utcnow().isoformat(),
                        "chunk_ids": [task.chunk_id for task in active_batch],
                        "duration_seconds": round(duration, 3),
                        **_gpu_memory_snapshot(spec.device),
                    },
                )
                for task, raw, text in zip(active_batch, raw_items, texts):
                    results.append(
                        _result_from_local_raw(
                            raw,
                            task,
                            schema=schema,
                            schema_lens=schema_lens,
                            text=text,
                        )
                )
                break
            except LocalGraphOOMError as exc:
                stats.oom_count += 1
                stats.relation_oom_count += 1
                _clear_cuda_cache()
                if (
                    getattr(config, "oom_retry_enabled", True)
                    and not retried_oom
                    and len(active_batch) > 1
                    and stats.relation_oom_count < relation_oom_disable_after
                ):
                    retried_oom = True
                    reduced = max(1, len(active_batch) // 2)
                    stats.current_batch_size = reduced
                    logger.warning(
                        "phase=local_graph_relation_oom device=%s batch=%d retry_batch=%d labels=%d error=%s",
                        spec.device,
                        len(active_batch),
                        reduced,
                        relation_label_count,
                        exc,
                    )
                    head = active_batch[:reduced]
                    tail = active_batch[reduced:]
                    if tail:
                        pending_batches.insert(0, tail)
                    active_batch = head
                    continue
                if entity_only_on_relation_oom:
                    relation_mode_enabled = False
                    relation_disabled_reason = "relation_oom"
                    stats.relation_disabled_count += 1
                    logger.warning(
                        "phase=local_graph_relation_disabled device=%s reason=oom chunks=%d labels=%d error=%s",
                        spec.device,
                        len(active_batch),
                        relation_label_count,
                        exc,
                    )
                    # Retry the same batch in entity-only mode. Deterministic
                    # cue extraction still runs after entity extraction.
                    continue
                raise
            except LocalGraphFatalCudaError as exc:
                duration = time.perf_counter() - started
                stats.cuda_fatal_count += 1
                stats.chunks_failed += len(active_batch)
                _clear_cuda_cache()
                retry_after = datetime.utcnow() + timedelta(
                    seconds=max(60, int(getattr(config, "local_cuda_fatal_cooldown_seconds", 900) or 900))
                )
                call_metrics.append(
                    {
                        "chunk_id": ",".join(task.chunk_id for task in active_batch[:4]),
                        "model": model_name,
                        "lane": worker_idx,
                        "attempt": 1,
                        "duration_seconds": round(duration, 3),
                        "total_tokens": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "success": False,
                        "error_type": "local_cuda_fatal",
                        "recovery_mode": False,
                        "max_tokens": 0,
                        "local_graph": True,
                        "gpu_device": spec.device,
                        "batch_size": len(active_batch),
                        "relation_mode": relation_mode_enabled,
                        "relation_label_count": relation_label_count,
                        "relation_disabled_reason": "cuda_context_poisoned",
                        "model_token_truncated_chunks": batch_truncated,
                    }
                )
                _write_diagnostic_record(
                    config,
                    {
                        "event": "post_call",
                        "success": False,
                        "diagnostic_run_id": diagnostic_run_id,
                        "timestamp": datetime.utcnow().isoformat(),
                        "chunk_ids": [task.chunk_id for task in active_batch],
                        "duration_seconds": round(duration, 3),
                        "error_type": "local_cuda_fatal",
                        "error": str(exc)[:1000],
                        **_gpu_memory_snapshot(spec.device),
                    },
                )
                for task in [*active_batch, *(item for batch in pending_batches for item in batch)]:
                    failures.append(
                        ExtractionFailureItem(
                            chunk_id=task.chunk_id,
                            doc_id=task.doc_id,
                            corpus_id=task.corpus_id,
                            model=model_name,
                            lane=worker_idx,
                            attempts=1 if task in active_batch else 0,
                            error_type="local_cuda_fatal",
                            error_message=str(exc)[:1000],
                            retryable=True,
                            retry_after=retry_after,
                            lane_state=f"{spec.device}:cuda_context_poisoned",
                        )
                    )
                pending_batches.clear()
                active_batch = []
                break
            except Exception as exc:
                cuda_error = _cuda_error_type(exc)
                if cuda_error == "local_cuda_fatal":
                    duration = time.perf_counter() - started
                    stats.cuda_fatal_count += 1
                    stats.chunks_failed += len(active_batch)
                    _clear_cuda_cache()
                    retry_after = datetime.utcnow() + timedelta(
                        seconds=max(60, int(getattr(config, "local_cuda_fatal_cooldown_seconds", 900) or 900))
                    )
                    call_metrics.append(
                        {
                            "chunk_id": ",".join(task.chunk_id for task in active_batch[:4]),
                            "model": model_name,
                            "lane": worker_idx,
                            "attempt": 1,
                            "duration_seconds": round(duration, 3),
                            "total_tokens": 0,
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "success": False,
                            "error_type": "local_cuda_fatal",
                            "recovery_mode": False,
                            "max_tokens": 0,
                            "local_graph": True,
                            "gpu_device": spec.device,
                            "batch_size": len(active_batch),
                            "relation_mode": relation_mode_enabled,
                            "relation_label_count": relation_label_count,
                            "relation_disabled_reason": "cuda_context_poisoned",
                            "model_token_truncated_chunks": batch_truncated,
                        }
                    )
                    _write_diagnostic_record(
                        config,
                        {
                            "event": "post_call",
                            "success": False,
                            "diagnostic_run_id": diagnostic_run_id,
                            "timestamp": datetime.utcnow().isoformat(),
                            "chunk_ids": [task.chunk_id for task in active_batch],
                            "duration_seconds": round(duration, 3),
                            "error_type": "local_cuda_fatal",
                            "error": str(exc)[:1000],
                            **_gpu_memory_snapshot(spec.device),
                        },
                    )
                    for task in [*active_batch, *(item for batch in pending_batches for item in batch)]:
                        failures.append(
                            ExtractionFailureItem(
                                chunk_id=task.chunk_id,
                                doc_id=task.doc_id,
                                corpus_id=task.corpus_id,
                                model=model_name,
                                lane=worker_idx,
                                attempts=1 if task in active_batch else 0,
                                error_type="local_cuda_fatal",
                                error_message=str(exc)[:1000],
                                retryable=True,
                                retry_after=retry_after,
                                lane_state=f"{spec.device}:cuda_context_poisoned",
                            )
                        )
                    pending_batches.clear()
                    active_batch = []
                    break
                if cuda_error == "local_cuda_oom" and getattr(config, "oom_retry_enabled", True) and not retried_oom and len(active_batch) > 1:
                    stats.oom_count += 1
                    _clear_cuda_cache()
                    retried_oom = True
                    reduced = max(1, len(active_batch) // 2)
                    stats.current_batch_size = reduced
                    logger.warning(
                        "phase=local_graph_oom device=%s batch=%d retry_batch=%d error=%s",
                        spec.device,
                        len(active_batch),
                        reduced,
                        exc,
                    )
                    # Reinsert the tail after the reduced head so the worker
                    # continues with smaller pieces instead of losing chunks.
                    head = active_batch[:reduced]
                    tail = active_batch[reduced:]
                    if tail:
                        pending_batches.insert(0, tail)
                    active_batch = head
                    continue
                duration = time.perf_counter() - started
                error_type = cuda_error or "local_extractor_error"
                call_metrics.append(
                    {
                        "chunk_id": ",".join(task.chunk_id for task in active_batch[:4]),
                        "model": model_name,
                        "lane": worker_idx,
                        "attempt": 1,
                        "duration_seconds": round(duration, 3),
                        "total_tokens": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "success": False,
                        "error_type": error_type,
                        "recovery_mode": False,
                        "max_tokens": 0,
                        "local_graph": True,
                        "gpu_device": spec.device,
                        "batch_size": len(active_batch),
                        "relation_mode": relation_mode_enabled,
                        "relation_label_count": relation_label_count,
                        "relation_disabled_reason": relation_disabled_reason,
                        "model_token_truncated_chunks": batch_truncated,
                    }
                )
                _write_diagnostic_record(
                    config,
                    {
                        "event": "post_call",
                        "success": False,
                        "diagnostic_run_id": diagnostic_run_id,
                        "timestamp": datetime.utcnow().isoformat(),
                        "chunk_ids": [task.chunk_id for task in active_batch],
                        "duration_seconds": round(duration, 3),
                        "error_type": error_type,
                        "error": str(exc)[:1000],
                        **_gpu_memory_snapshot(spec.device),
                    },
                )
                stats.chunks_failed += len(active_batch)
                for task in active_batch:
                    failures.append(
                        ExtractionFailureItem(
                            chunk_id=task.chunk_id,
                            doc_id=task.doc_id,
                            corpus_id=task.corpus_id,
                            model=model_name,
                            lane=worker_idx,
                            attempts=1,
                            error_type=error_type,
                            error_message=str(exc)[:1000],
                            retryable=True,
                            retry_after=None,
                            lane_state=spec.device,
                        )
                    )
                break
    return results, failures, call_metrics, stats


def _weak_or_ambiguous(result: ExtractionResult) -> bool:
    if not result.relations:
        return False
    for relation in result.relations:
        if relation.predicate == "related_to":
            return True
        try:
            if relation.predicate_confidence is not None and float(relation.predicate_confidence) < 0.6:
                return True
        except Exception:
            return True
        status = str(relation.validation_status or "")
        if "review_required" in status or "low_predicate_confidence" in status:
            return True
    return False


def _fallback_limit(config: IngestionConfig, total: int) -> int:
    percent = float(getattr(config, "llm_fallback_max_percent", 0.05) or 0.0)
    if percent <= 0.0 or total <= 0:
        return 0
    return max(1, int(math.ceil(total * percent)))


async def extract_entities_local_first(
    tasks: list[ExtractionTask],
    *,
    config: IngestionConfig,
    schema: SchemaContext | None,
    schema_lens: SchemaLens | dict | None = None,
    llm_extract_func: Callable[..., Awaitable[list[ExtractionResult] | ExtractionBatchReport]] = extract_entities,
    llm_kwargs: dict[str, Any] | None = None,
    return_report: bool = True,
) -> list[ExtractionResult] | ExtractionBatchReport:
    """Run local GLiNER extraction first, with bounded LLM fallback.

    The fallback is infrastructure/ambiguity scoped. It is not a semantic retry
    loop and is not allowed to chase a lower related_to ratio for its own sake.
    """

    if not tasks:
        empty_metrics = summarize_extraction_batch(
            total_chunks=0,
            results=[],
            failures=[],
            call_metrics=[],
            models=[],
            metrics_context={
                "graph_extraction_engine_used": "local_gliner",
                "local_graph_dependency_status": "not_needed",
                "local_graph_model_loaded": False,
                "local_graph_relation_oom_count": 0,
                "local_graph_relation_disabled_count": 0,
                "local_cuda_fatal_count": 0,
                "local_graph_relation_labels_avg": 0,
                "local_graph_relation_labels_max": 0,
                "model_token_truncated_chunks": 0,
                "llm_graph_calls": 0,
                "summary_llm_calls": 0,
            },
        )
        return ExtractionBatchReport([], [], empty_metrics) if return_report else []

    requested_engine = str(getattr(config, "graph_extraction_engine", "llm") or "llm").strip()
    engine = requested_engine
    llm_kwargs = dict(llm_kwargs or {})
    llm_fallback_enabled = bool(getattr(config, "llm_fallback_enabled", True))

    if not getattr(config, "local_graph_extraction_enabled", True):
        engine = "llm"

    if engine == "llm":
        return await llm_extract_func(tasks, **llm_kwargs)
    engine = _normalize_local_engine(engine)
    engine_label = requested_engine if requested_engine == "local_gliner" else engine

    specs = _available_worker_specs(_worker_specs(config))
    local_results: list[ExtractionResult] = []
    local_failures: list[ExtractionFailureItem] = []
    call_metrics: list[dict] = []
    worker_stats: list[LocalWorkerStats] = []
    model_name = _model_name_for_engine(config, engine)
    max_chunks_in_memory = max(1, int(getattr(config, "max_chunks_in_memory", 100) or 100))

    try:
        for offset in range(0, len(tasks), max_chunks_in_memory):
            task_window = tasks[offset : offset + max_chunks_in_memory]
            assigned = _weighted_assign(task_window, specs)
            worker_outputs = await asyncio.gather(
                *[
                    _extract_worker(
                        worker_idx=idx,
                        spec=spec,
                        tasks=assigned.get(idx, []),
                        config=config,
                        schema=schema,
                        schema_lens=schema_lens,
                    )
                    for idx, spec in enumerate(specs)
                    if assigned.get(idx)
                ]
            )
            for results, failures, metrics, stats in worker_outputs:
                local_results.extend(results)
                local_failures.extend(failures)
                call_metrics.extend(metrics)
                worker_stats.append(stats)
    except LocalGraphDependencyError as exc:
        if engine == "hybrid_local_first" and llm_fallback_enabled:
            logger.warning(
                "phase=local_graph_unavailable action=llm_fallback model=%s error=%s",
                model_name,
                exc,
            )
            report = await llm_extract_func(tasks, **llm_kwargs)
            if isinstance(report, ExtractionBatchReport):
                report.metrics = {
                    **report.metrics,
                    "graph_extraction_engine_used": "llm_fallback_local_unavailable",
                    "local_graph_dependency_status": "unavailable",
                    "local_graph_model_loaded": False,
                    "local_graph_dependency_error": str(exc),
                    "local_extractor_model": model_name,
                    "llm_graph_calls": len(tasks),
                }
            return report
        failures = [
            ExtractionFailureItem(
                chunk_id=task.chunk_id,
                doc_id=task.doc_id,
                corpus_id=task.corpus_id,
                model=model_name,
                lane=-1,
                attempts=0,
                error_type="local_extractor_unavailable",
                error_message=str(exc)[:1000],
                retryable=True,
                retry_after=None,
                lane_state="unavailable",
            )
            for task in tasks
        ]
        metrics = summarize_extraction_batch(
            total_chunks=len(tasks),
            results=[],
            failures=failures,
            call_metrics=[],
            models=[model_name],
            metrics_context={
                    "graph_extraction_engine_used": f"{engine_label}_unavailable",
                "local_graph_dependency_status": "unavailable",
                "local_graph_model_loaded": False,
                "local_extractor_model": model_name,
                "local_graph_dependency_error": str(exc),
                "local_graph_chunks_processed": 0,
                "local_graph_chunks_failed": len(failures),
                "local_graph_entity_only_chunks": 0,
                "local_graph_relation_chunks": 0,
                "local_graph_relation_mode_chunks": 0,
                "local_graph_relation_oom_count": 0,
                "local_graph_relation_disabled_count": 0,
                "local_cuda_fatal_count": 0,
                "local_graph_relation_labels_avg": 0,
                "local_graph_relation_labels_max": 0,
                "local_graph_model_load_failures": 0,
                "model_token_count_avg": 0.0,
                "model_token_count_max": 0,
                "model_token_truncated_chunks": 0,
                "llm_graph_calls": 0,
                "summary_llm_calls": 0,
            },
        )
        return ExtractionBatchReport([], failures, metrics) if return_report else []

    result_by_chunk = {result.chunk_id: result for result in local_results}
    failed_chunk_ids = {failure.chunk_id for failure in local_failures}
    fallback_candidates: list[ExtractionTask] = [
        task for task in tasks if task.chunk_id in failed_chunk_ids
    ]
    fallback_candidates.extend(
        task
        for task in tasks
        if task.chunk_id in result_by_chunk and _weak_or_ambiguous(result_by_chunk[task.chunk_id])
    )
    # Stable unique order.
    seen_fallback: set[str] = set()
    fallback_candidates = [
        task
        for task in fallback_candidates
        if not (task.chunk_id in seen_fallback or seen_fallback.add(task.chunk_id))
    ]
    fallback_cap = _fallback_limit(config, len(tasks))
    fallback_tasks = fallback_candidates[:fallback_cap] if llm_fallback_enabled else []
    llm_fallback_results = 0
    llm_fallback_failures = 0
    llm_fallback_metrics: dict[str, Any] = {}
    if fallback_tasks:
        logger.info(
            "phase=local_graph_llm_fallback chunks=%d cap=%d reason=failed_or_ambiguous",
            len(fallback_tasks),
            fallback_cap,
        )
        fallback_report = await llm_extract_func(fallback_tasks, **llm_kwargs)
        if isinstance(fallback_report, ExtractionBatchReport):
            for result in fallback_report.results:
                result_by_chunk[result.chunk_id] = result
            recovered = {result.chunk_id for result in fallback_report.results}
            local_failures = [
                failure for failure in local_failures if failure.chunk_id not in recovered
            ]
            for failure in fallback_report.failures:
                # A failed LLM review of an already-successful local result is
                # not a graph extraction failure. Keep the local evidence and
                # avoid making graph_status partial just because review failed.
                if failure.chunk_id in result_by_chunk and failure.chunk_id not in failed_chunk_ids:
                    continue
                local_failures.append(failure)
            llm_fallback_results = len(fallback_report.results)
            llm_fallback_failures = len(fallback_report.failures)
            llm_fallback_metrics = fallback_report.metrics
        else:
            for result in fallback_report:
                result_by_chunk[result.chunk_id] = result
            llm_fallback_results = len(fallback_report)

    final_results = list(result_by_chunk.values())
    relation_chunks = sum(1 for result in final_results if result.relations)
    entity_only_chunks = sum(
        1 for result in final_results if result.entities and not result.relations
    )
    stats_by_name: dict[str, LocalWorkerStats] = {}
    for stats in worker_stats:
        bucket = stats_by_name.get(stats.name)
        if bucket is None:
            stats_by_name[stats.name] = LocalWorkerStats(
                device=stats.device,
                name=stats.name,
                chunks_processed=stats.chunks_processed,
                chunks_failed=stats.chunks_failed,
                oom_count=stats.oom_count,
                relation_oom_count=stats.relation_oom_count,
                relation_disabled_count=stats.relation_disabled_count,
                cuda_fatal_count=stats.cuda_fatal_count,
                relation_mode_chunks=stats.relation_mode_chunks,
                relation_label_total=stats.relation_label_total,
                relation_label_max=stats.relation_label_max,
                model_token_count_total=stats.model_token_count_total,
                model_token_count_observations=stats.model_token_count_observations,
                model_token_count_max=stats.model_token_count_max,
                model_token_truncated_chunks=stats.model_token_truncated_chunks,
                duration_seconds=stats.duration_seconds,
                current_batch_size=stats.current_batch_size,
            )
            continue
        bucket.chunks_processed += stats.chunks_processed
        bucket.chunks_failed += stats.chunks_failed
        bucket.oom_count += stats.oom_count
        bucket.relation_oom_count += stats.relation_oom_count
        bucket.relation_disabled_count += stats.relation_disabled_count
        bucket.cuda_fatal_count += stats.cuda_fatal_count
        bucket.relation_mode_chunks += stats.relation_mode_chunks
        bucket.relation_label_total += stats.relation_label_total
        bucket.relation_label_max = max(bucket.relation_label_max, stats.relation_label_max)
        bucket.model_token_count_total += stats.model_token_count_total
        bucket.model_token_count_observations += stats.model_token_count_observations
        bucket.model_token_count_max = max(bucket.model_token_count_max, stats.model_token_count_max)
        bucket.model_token_truncated_chunks += stats.model_token_truncated_chunks
        bucket.duration_seconds += stats.duration_seconds
        bucket.current_batch_size = min(bucket.current_batch_size, stats.current_batch_size)
    per_gpu = {
        stats.name: {
            "device": stats.device,
            "chunks_processed": stats.chunks_processed,
            "chunks_failed": stats.chunks_failed,
            "avg_latency_seconds": (
                round(stats.duration_seconds / stats.chunks_processed, 4)
                if stats.chunks_processed
                else 0.0
            ),
            "oom_count": stats.oom_count,
            "cuda_fatal_count": stats.cuda_fatal_count,
            "relation_oom_count": stats.relation_oom_count,
            "relation_disabled_count": stats.relation_disabled_count,
            "model_token_count_max": stats.model_token_count_max,
            "model_token_truncated_chunks": stats.model_token_truncated_chunks,
            "adaptive_batch_size_current": stats.current_batch_size,
        }
        for stats in stats_by_name.values()
    }
    relation_mode_chunks = sum(stats.relation_mode_chunks for stats in stats_by_name.values())
    relation_label_total = sum(stats.relation_label_total for stats in stats_by_name.values())
    relation_label_avg = (
        round(relation_label_total / relation_mode_chunks, 3)
        if relation_mode_chunks
        else 0.0
    )
    model_token_observations = sum(stats.model_token_count_observations for stats in stats_by_name.values())
    model_token_total = sum(stats.model_token_count_total for stats in stats_by_name.values())
    context = {
        "graph_extraction_engine_used": engine_label,
        "local_graph_dependency_status": "ok",
        "local_graph_model_loaded": bool(worker_stats),
        "local_extractor_model": model_name,
        "local_graph_chunks_processed": sum(stats.chunks_processed for stats in stats_by_name.values()),
        "local_graph_chunks_failed": sum(stats.chunks_failed for stats in stats_by_name.values()),
        "local_graph_entity_only_chunks": entity_only_chunks,
        "local_graph_relation_chunks": relation_chunks,
        "local_graph_relation_mode_chunks": relation_mode_chunks,
        "local_graph_relation_oom_count": sum(stats.relation_oom_count for stats in stats_by_name.values()),
        "local_graph_relation_disabled_count": sum(stats.relation_disabled_count for stats in stats_by_name.values()),
        "local_cuda_fatal_count": sum(stats.cuda_fatal_count for stats in stats_by_name.values()),
        "local_graph_relation_labels_avg": relation_label_avg,
        "local_graph_relation_labels_max": max(
            [stats.relation_label_max for stats in stats_by_name.values()] or [0]
        ),
        "model_token_count_avg": (
            round(model_token_total / model_token_observations, 2)
            if model_token_observations
            else 0.0
        ),
        "model_token_count_max": max(
            [stats.model_token_count_max for stats in stats_by_name.values()] or [0]
        ),
        "model_token_truncated_chunks": sum(
            stats.model_token_truncated_chunks for stats in stats_by_name.values()
        ),
        "local_graph_model_load_failures": sum(
            1 for metric in call_metrics if metric.get("error_type") == "local_extractor_load_error"
        ),
        "llm_fallback_chunks": len(fallback_tasks),
        "llm_graph_calls": len(fallback_tasks),
        "summary_llm_calls": 0,
        "llm_fallback_extracted_chunks": llm_fallback_results,
        "llm_fallback_failed_chunks": llm_fallback_failures,
        "llm_fallback_max_percent": float(getattr(config, "llm_fallback_max_percent", 0.05) or 0.0),
        "per_gpu_graph_metrics": per_gpu,
        "per_gpu_chunks_processed": {
            stats.name: stats.chunks_processed for stats in stats_by_name.values()
        },
        "per_gpu_avg_latency": {
            stats.name: (
                round(stats.duration_seconds / stats.chunks_processed, 4)
                if stats.chunks_processed
                else 0.0
            )
            for stats in stats_by_name.values()
        },
        "per_gpu_oom_count": {stats.name: stats.oom_count for stats in stats_by_name.values()},
        "per_gpu_relation_oom_count": {
            stats.name: stats.relation_oom_count for stats in stats_by_name.values()
        },
        "per_gpu_cuda_fatal_count": {
            stats.name: stats.cuda_fatal_count for stats in stats_by_name.values()
        },
        "adaptive_batch_size_current": {
            stats.name: stats.current_batch_size for stats in stats_by_name.values()
        },
        "cuda_detected_devices": _cuda_device_count(),
    }
    if llm_fallback_metrics:
        context["llm_fallback_tokens"] = int(llm_fallback_metrics.get("total_tokens") or 0)
        context["llm_fallback_prompt_tokens"] = int(llm_fallback_metrics.get("prompt_tokens") or 0)
        context["llm_fallback_completion_tokens"] = int(llm_fallback_metrics.get("completion_tokens") or 0)
    metrics = summarize_extraction_batch(
        total_chunks=len(tasks),
        results=final_results,
        failures=local_failures,
        call_metrics=call_metrics,
        models=[model_name],
        metrics_context=context,
    )
    return ExtractionBatchReport(final_results, local_failures, metrics) if return_report else final_results
