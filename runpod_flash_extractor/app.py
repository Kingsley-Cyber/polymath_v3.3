"""Runpod Flash worker for Polymath joint entity/relation extraction.

Deploy from this directory with ``flash deploy``. The worker is deliberately
stateless: it accepts text plus an ontology and returns staged JSON. Database
credentials and write access never leave the Polymath backend.
"""

from __future__ import annotations

import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

from runpod_flash import Endpoint, GpuType, ServerlessScalerType


_MODEL_CACHE: dict[tuple[str, str], Any] = {}
_MODEL_SOURCE_CACHE: dict[tuple[str, str], str] = {}
_SPACY_CACHE: dict[str, Any] = {}
_CONTRACT_VERSION = "polymath.runpod_gliner_relex.v2"
_HF_CACHE_ROOT = Path("/runpod-volume/huggingface-cache/hub")


def _label_key(value: str) -> str:
    return " ".join(
        re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(value or ""))
        .replace("_", " ")
        .replace("-", " ")
        .lower()
        .split()
    )


def _inference_label(value: str) -> str:
    """Render canonical ontology identifiers as model-friendly English."""

    return _label_key(value)


def _canonical_label(value: str, label_map: dict[str, str]) -> str:
    return label_map.get(_label_key(value), str(value or "").strip())


def _entity_label_subset(
    rows: list[dict[str, Any]],
    *,
    allowed_labels: set[str],
    max_labels: int,
) -> tuple[str, ...]:
    scores: dict[str, float] = {}
    for row in rows or []:
        label = _inference_label(str(row.get("label") or ""))
        if label not in allowed_labels:
            continue
        scores[label] = max(scores.get(label, 0.0), float(row.get("score") or 0.0))
    ranked = sorted(scores, key=lambda label: (-scores[label], label))
    return tuple(sorted(ranked[:max_labels]))


def _entity_lens_groups(
    rows_by_window: list[list[dict[str, Any]]],
    *,
    allowed_labels: set[str],
    max_labels: int,
) -> list[dict[str, Any]]:
    """Greedily batch windows while keeping each entity lens compact."""

    candidates: list[tuple[int, set[str]]] = []
    for index, rows in enumerate(rows_by_window):
        if len(rows or []) < 2:
            continue
        labels = set(
            _entity_label_subset(
                rows,
                allowed_labels=allowed_labels,
                max_labels=max_labels,
            )
        )
        if labels:
            candidates.append((index, labels))
    candidates.sort(key=lambda item: (-len(item[1]), item[0]))

    groups: list[dict[str, Any]] = []
    for index, labels in candidates:
        eligible: list[tuple[int, int]] = []
        for group_index, group in enumerate(groups):
            union = set(group["labels"]) | labels
            if len(union) <= max_labels:
                eligible.append((len(set(group["labels"]) & labels), group_index))
        if eligible:
            _overlap, group_index = max(eligible)
            groups[group_index]["labels"].update(labels)
            groups[group_index]["indices"].append(index)
        else:
            groups.append({"labels": set(labels), "indices": [index]})
    return [
        {
            "labels": sorted(group["labels"]),
            "indices": sorted(group["indices"]),
        }
        for group in groups
    ]


def _canonical_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower()
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())[:200]


def _cached_model_path(
    model_id: str,
    model_revision: str,
    *,
    cache_root: Path = _HF_CACHE_ROOT,
) -> Path | None:
    repo_dir = cache_root / f"models--{model_id.replace('/', '--')}"
    snapshots = repo_dir / "snapshots"
    if model_revision:
        pinned = snapshots / model_revision
        if pinned.is_dir():
            return pinned
    ref_names = [model_revision, "main"] if model_revision else ["main"]
    for ref_name in ref_names:
        if not ref_name:
            continue
        ref = repo_dir / "refs" / ref_name
        if not ref.is_file():
            continue
        resolved = snapshots / ref.read_text(encoding="utf-8").strip()
        if resolved.is_dir():
            return resolved
    if snapshots.is_dir():
        candidates = sorted(path for path in snapshots.iterdir() if path.is_dir())
        if len(candidates) == 1:
            return candidates[0]
    return None


def _nlp(pipeline: str):
    cached = _SPACY_CACHE.get(pipeline)
    if cached is not None:
        return cached
    import spacy

    if pipeline.startswith("blank:"):
        language = pipeline.split(":", 1)[1] or "en"
        nlp = spacy.blank(language)
        nlp.add_pipe("sentencizer")
    else:
        nlp = spacy.load(pipeline, exclude=["ner", "lemmatizer"])
        if not any(name in nlp.pipe_names for name in ("parser", "senter", "sentencizer")):
            nlp.add_pipe("sentencizer")
    _SPACY_CACHE[pipeline] = nlp
    return nlp


def _model(model_id: str, model_revision: str):
    cache_key = (model_id, model_revision)
    cached = _MODEL_CACHE.get(cache_key)
    if cached is not None:
        return cached
    import torch
    from gliner import GLiNER

    cached_path = _cached_model_path(model_id, model_revision)
    model_source = str(cached_path) if cached_path is not None else model_id
    model = GLiNER.from_pretrained(
        model_source,
        revision=None if cached_path is not None else (model_revision or None),
        local_files_only=cached_path is not None,
    )
    if torch.cuda.is_available():
        model = model.to("cuda")
    model.eval()
    _MODEL_CACHE[cache_key] = model
    _MODEL_SOURCE_CACHE[cache_key] = (
        "runpod_cached_model" if cached_path is not None else "huggingface_hub"
    )
    return model


def _sentence_spans(text: str, nlp: Any) -> list[tuple[int, int]]:
    doc = nlp(text)
    spans = [(sent.start_char, sent.end_char) for sent in doc.sents if sent.text.strip()]
    return spans or [(0, len(text))]


def _windows(
    text: str,
    *,
    nlp: Any,
    max_words: int,
) -> tuple[list[tuple[str, int]], list[tuple[int, int]]]:
    if not text.strip():
        return [], []
    sentence_spans = _sentence_spans(text, nlp)
    if len(text.split()) <= max_words:
        return [(text, 0)], sentence_spans
    windows: list[tuple[str, int]] = []
    current: list[tuple[int, int]] = []
    current_words = 0
    for start, end in sentence_spans:
        words = len(text[start:end].split())
        if words > max_words:
            if current:
                left, right = current[0][0], current[-1][1]
                windows.append((text[left:right], left))
                current = []
                current_words = 0
            cursor = start
            tokens = list(nlp.make_doc(text[start:end]))
            for offset in range(0, len(tokens), max_words):
                block = tokens[offset : offset + max_words]
                if not block:
                    continue
                left = start + block[0].idx
                right = start + block[-1].idx + len(block[-1])
                windows.append((text[left:right], left))
                cursor = right
            if cursor < end and text[cursor:end].strip():
                windows.append((text[cursor:end], cursor))
            continue
        if current and current_words + words > max_words:
            left, right = current[0][0], current[-1][1]
            windows.append((text[left:right], left))
            current = current[-1:]
            current_words = len(text[current[0][0] : current[0][1]].split())
        current.append((start, end))
        current_words += words
    if current:
        left, right = current[0][0], current[-1][1]
        windows.append((text[left:right], left))
    return windows or [(text, 0)], sentence_spans


def _evidence(
    text: str,
    sentence_spans: list[tuple[int, int]],
    head_start: int,
    head_end: int,
    tail_start: int,
    tail_end: int,
) -> str:
    left = min(head_start, tail_start)
    right = max(head_end, tail_end)
    for start, end in sentence_spans:
        if start <= left and right <= end:
            return text[start:end][:500]
    return text[left:right][:500]


def _extract_task(
    task: dict[str, Any],
    *,
    entities: list[dict[str, Any]],
    relations: list[dict[str, Any]],
    window_offsets: list[int],
    sentence_spans: list[tuple[int, int]],
    entity_label_map: dict[str, str] | None = None,
    relation_label_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    text = str(task.get("text") or "")
    entity_label_map = entity_label_map or {}
    relation_label_map = relation_label_map or {}
    entity_best: dict[tuple[str, str, int, int], dict[str, Any]] = {}
    window_entities: list[list[dict[str, Any]]] = []
    for window_index, rows in enumerate(entities):
        offset = window_offsets[window_index]
        converted: list[dict[str, Any]] = []
        for raw in rows or []:
            surface = str(raw.get("text") or "").strip()
            canonical = _canonical_name(surface)
            entity_type = _canonical_label(
                str(raw.get("label") or "other"), entity_label_map
            )
            start = offset + int(raw.get("start") or 0)
            end = offset + int(raw.get("end") or start + len(surface))
            if not surface or not canonical or text[start:end] != surface:
                continue
            item = {
                "canonical_name": canonical,
                "surface_form": surface,
                "entity_type": entity_type,
                "confidence": float(raw.get("score") or 0.0),
                "query_aliases": [],
                "definitional_phrase": "",
                "object_kind": "",
                "char_start": start,
                "char_end": end,
            }
            key = (canonical, entity_type, start, end)
            prior = entity_best.get(key)
            if prior is None or item["confidence"] > prior["confidence"]:
                entity_best[key] = item
            converted.append(item)
        window_entities.append(converted)

    relation_best: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for window_index, rows in enumerate(relations):
        offset = window_offsets[window_index]
        for raw in rows or []:
            head = raw.get("head") or {}
            tail = raw.get("tail") or {}
            subject = _canonical_name(str(head.get("text") or ""))
            object_ = _canonical_name(str(tail.get("text") or ""))
            predicate = _canonical_label(
                str(raw.get("relation") or ""), relation_label_map
            )
            head_start = offset + int(head.get("start") or 0)
            head_end = offset + int(head.get("end") or head_start)
            tail_start = offset + int(tail.get("start") or 0)
            tail_end = offset + int(tail.get("end") or tail_start)
            if not subject or not object_ or not predicate:
                continue
            evidence = _evidence(
                text,
                sentence_spans,
                head_start,
                head_end,
                tail_start,
                tail_end,
            )
            if not evidence:
                continue
            item = {
                "subject": subject,
                "predicate": predicate,
                "object": object_,
                "object_kind": "entity",
                "confidence": float(raw.get("score") or 0.0),
                "evidence_phrase": evidence,
                "relation_cue": "",
            }
            key = (subject, predicate, object_, evidence)
            prior = relation_best.get(key)
            if prior is None or item["confidence"] > prior["confidence"]:
                relation_best[key] = item

    return {
        "schema_version": "ghost_b_extraction.v1",
        "chunk_id": str(task.get("chunk_id") or ""),
        "doc_id": str(task.get("doc_id") or ""),
        "corpus_id": str(task.get("corpus_id") or ""),
        "entities": list(entity_best.values()),
        "relations": list(relation_best.values()),
        "facts": [],
        "text": text,
        "entity_drop_count": 0,
        "relation_drop_count": 0,
        "evidence_drop_count": 0,
        "fact_drop_count": 0,
        "schema_lens_id": None,
    }


@Endpoint(
    name=os.getenv("RUNPOD_FLASH_ENDPOINT_NAME", "polymath-gliner-relex"),
    gpu=[
        GpuType.NVIDIA_L4,
        GpuType.NVIDIA_RTX_A5000,
        GpuType.NVIDIA_GEFORCE_RTX_4090,
    ],
    workers=(
        int(os.getenv("RUNPOD_FLASH_MIN_WORKERS", "0")),
        int(os.getenv("RUNPOD_FLASH_MAX_WORKERS", "8")),
    ),
    max_concurrency=int(os.getenv("RUNPOD_FLASH_WORKER_CONCURRENCY", "1")),
    idle_timeout=int(os.getenv("RUNPOD_FLASH_IDLE_TIMEOUT", "60")),
    scaler_type=ServerlessScalerType.REQUEST_COUNT,
    scaler_value=int(os.getenv("RUNPOD_FLASH_SCALER_VALUE", "1")),
    execution_timeout_ms=int(
        os.getenv("RUNPOD_FLASH_EXECUTION_TIMEOUT_MS", "1800000")
    ),
    flashboot=True,
    accelerate_downloads=True,
    dependencies=[
        "torch>=2.4",
        "transformers>=4.48,<5.0",
        "gliner==0.2.27",
        "spacy>=3.8,<4.0",
        "numpy<2.3",
        "safetensors>=0.4",
    ],
)
def extract_batch(payload: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    if payload.get("contract_version") != _CONTRACT_VERSION:
        raise ValueError("unsupported extraction contract")
    tasks = list(payload.get("tasks") or [])
    entity_labels = list(payload.get("entity_labels") or [])
    relation_labels = list(payload.get("relation_labels") or [])
    if not tasks or not entity_labels or not relation_labels:
        raise ValueError("tasks, entity_labels, and relation_labels are required")

    model_id = str(
        payload.get("model_id") or "knowledgator/gliner-relex-large-v0.5"
    )
    model_revision = str(payload.get("model_revision") or "")
    nlp = _nlp(str(payload.get("spacy_pipeline") or "blank:en"))
    model = _model(model_id, model_revision)
    model_source = _MODEL_SOURCE_CACHE.get(
        (model_id, model_revision), "huggingface_hub"
    )
    max_window_words = max(80, min(800, int(payload.get("max_window_words") or 260)))
    entity_label_map = {
        _label_key(canonical): str(canonical) for canonical in entity_labels
    }
    relation_label_map = {
        _label_key(canonical): str(canonical) for canonical in relation_labels
    }
    inference_entity_labels = [
        _inference_label(canonical) for canonical in entity_labels
    ]
    inference_relation_labels = [
        _inference_label(canonical) for canonical in relation_labels
    ]

    flattened: list[str] = []
    task_windows: list[list[tuple[str, int]]] = []
    task_sentences: list[list[tuple[int, int]]] = []
    for task in tasks:
        windows, sentences = _windows(
            str(task.get("text") or ""), nlp=nlp, max_words=max_window_words
        )
        task_windows.append(windows)
        task_sentences.append(sentences)
        flattened.extend(text for text, _offset in windows)

    entity_threshold = float(payload.get("entity_threshold") or 0.4)
    adjacency_threshold = float(payload.get("adjacency_threshold") or 0.6)
    relation_threshold = float(payload.get("relation_threshold") or 0.75)
    model_batch_size = max(
        1, min(256, int(payload.get("model_batch_size") or 32))
    )
    entity_lens_enabled = bool(payload.get("entity_lens_enabled", True))
    entity_lens_max_labels = max(
        2, min(14, int(payload.get("entity_lens_max_labels") or 6))
    )
    lens_groups: list[dict[str, Any]] = []

    if flattened and entity_lens_enabled and len(inference_entity_labels) > entity_lens_max_labels:
        # GLiNER-Relex 0.2.27 has a separate entity-only execution path that
        # can stall on this relation-aware checkpoint. The broad pass therefore
        # uses the proven joint path and deliberately discards its diluted
        # relation output before the compact second pass.
        entities, _broad_relations = model.inference(
            texts=flattened,
            labels=inference_entity_labels,
            relations=inference_relation_labels,
            threshold=entity_threshold,
            adjacency_threshold=adjacency_threshold,
            relation_threshold=relation_threshold,
            batch_size=model_batch_size,
            return_relations=True,
            flat_ner=False,
            multi_label=False,
        )
        relations = [[] for _text in flattened]
        lens_groups = _entity_lens_groups(
            entities,
            allowed_labels=set(inference_entity_labels),
            max_labels=entity_lens_max_labels,
        )
        for group in lens_groups:
            indices = list(group["indices"])
            group_entities, group_relations = model.inference(
                texts=[flattened[index] for index in indices],
                labels=list(group["labels"]),
                relations=inference_relation_labels,
                threshold=entity_threshold,
                adjacency_threshold=adjacency_threshold,
                relation_threshold=relation_threshold,
                batch_size=model_batch_size,
                return_relations=True,
                flat_ner=False,
                multi_label=False,
            )
            for offset, window_index in enumerate(indices):
                if group_entities[offset]:
                    entities[window_index] = group_entities[offset]
                relations[window_index] = group_relations[offset]
    elif flattened:
        entities, relations = model.inference(
            texts=flattened,
            labels=inference_entity_labels,
            relations=inference_relation_labels,
            threshold=entity_threshold,
            adjacency_threshold=adjacency_threshold,
            relation_threshold=relation_threshold,
            batch_size=model_batch_size,
            return_relations=True,
            flat_ner=False,
            multi_label=False,
        )
    else:
        entities, relations = [], []

    results: list[dict[str, Any]] = []
    cursor = 0
    for task_index, task in enumerate(tasks):
        count = len(task_windows[task_index])
        results.append(
            _extract_task(
                task,
                entities=entities[cursor : cursor + count],
                relations=relations[cursor : cursor + count],
                window_offsets=[offset for _text, offset in task_windows[task_index]],
                sentence_spans=task_sentences[task_index],
                entity_label_map=entity_label_map,
                relation_label_map=relation_label_map,
            )
        )
        cursor += count

    duration = time.perf_counter() - started
    return {
        "contract_version": _CONTRACT_VERSION,
        "batch_id": payload.get("batch_id"),
        "results": results,
        "metrics": {
            "model": model_id,
            "model_revision": model_revision or None,
            "model_source": model_source,
            "chunks": len(tasks),
            "windows": len(flattened),
            "entities_emitted": sum(len(rows or []) for rows in entities),
            "relations_emitted": sum(len(rows or []) for rows in relations),
            "entity_lens_enabled": entity_lens_enabled,
            "entity_lens_groups": len(lens_groups),
            "entity_lens_second_pass_windows": sum(
                len(group["indices"]) for group in lens_groups
            ),
            "duration_seconds": round(duration, 4),
            "chunks_per_second": round(len(tasks) / duration, 3) if duration else 0.0,
        },
    }
