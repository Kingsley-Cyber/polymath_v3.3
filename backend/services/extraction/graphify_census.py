"""Structure-aware, corpus-batched entity census with mention conservation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from models.graphify_contracts import (
    ExtractionWindowV1,
    MentionTerminalState,
    NormalizedDocumentV1,
    RawMentionV1,
    stable_digest,
    stable_id,
)
from services.extraction.gliner2_cpu_provider import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_THRESHOLD,
    PROVIDER_RELEASE,
    EntityPrediction,
    GLiNER2CPUProvider,
    SCHEMA_CONFIG,
    SCHEMA_RELEASE,
    schema_hash,
)
from services.extraction.graphify_normalization import to_original_span
from services.extraction.graphify_survey import DocumentSurveyV1

CENSUS_RELEASE = "graphify-entity-census-v1"
MIN_WINDOW_TOKENS = 512
TARGET_WINDOW_TOKENS = 512
MAX_WINDOW_TOKENS = 1024
_TOKEN_RE = re.compile(r"\S+")


class RawMentionSink(Protocol):
    def persist(self, mentions: Sequence[RawMentionV1]) -> int: ...


class InMemoryRawMentionSink:
    def __init__(self) -> None:
        self.records: dict[str, RawMentionV1] = {}

    def persist(self, mentions: Sequence[RawMentionV1]) -> int:
        for mention in mentions:
            existing = self.records.get(mention.mention_id)
            if existing is not None and existing != mention:
                raise RuntimeError(f"conflicting immutable raw mention {mention.mention_id}")
            self.records[mention.mention_id] = mention
        return len(mentions)


class JsonlRawMentionSink:
    """Append-only, idempotent raw mention sink for isolated runs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._records: dict[str, str] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line:
                    continue
                payload = json.loads(line)
                self._records[str(payload["mention_id"])] = json.dumps(
                    payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                )

    def persist(self, mentions: Sequence[RawMentionV1]) -> int:
        pending: list[str] = []
        for mention in mentions:
            payload = mention.model_dump(mode="json")
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            existing = self._records.get(mention.mention_id)
            if existing is not None:
                if existing != encoded:
                    raise RuntimeError(f"conflicting immutable raw mention {mention.mention_id}")
                continue
            self._records[mention.mention_id] = encoded
            pending.append(encoded)
        if pending:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                for line in pending:
                    handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return len(mentions)


@dataclass(frozen=True)
class CensusOutput:
    windows: tuple[ExtractionWindowV1, ...]
    mentions: tuple[RawMentionV1, ...]
    report: dict[str, object]


def _token_spans(text: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in _TOKEN_RE.finditer(text)]


def _structural_boundaries(document: NormalizedDocumentV1, survey: DocumentSurveyV1) -> tuple[int, ...]:
    values = {0, len(document.normalized_text)}
    for block in survey.blocks:
        values.add(block.start)
        values.add(block.end)
    for heading in survey.headings:
        values.add(heading.start)
        values.add(heading.end)
    return tuple(sorted(values))


def build_census_windows(
    document: NormalizedDocumentV1,
    survey: DocumentSurveyV1,
    *,
    min_tokens: int = MIN_WINDOW_TOKENS,
    target_tokens: int = TARGET_WINDOW_TOKENS,
    max_tokens: int = MAX_WINDOW_TOKENS,
) -> tuple[ExtractionWindowV1, ...]:
    if not 1 <= min_tokens <= target_tokens <= max_tokens:
        raise ValueError("window token bounds must be ordered and positive")
    text = document.normalized_text
    tokens = _token_spans(text)
    if not tokens:
        return ()
    boundaries = _structural_boundaries(document, survey)
    windows: list[ExtractionWindowV1] = []
    token_index = 0
    char_start = 0
    sequence = 0
    while token_index < len(tokens):
        remaining = len(tokens) - token_index
        if remaining <= target_tokens:
            char_end = len(text)
            next_token_index = len(tokens)
        else:
            lower_token = min(token_index + min_tokens, len(tokens) - 1)
            target_token = min(token_index + target_tokens, len(tokens) - 1)
            upper_token = min(token_index + max_tokens, len(tokens) - 1)
            lower_char = tokens[lower_token][0]
            target_char = tokens[target_token][0]
            upper_char = tokens[upper_token][0]
            candidates = [value for value in boundaries if lower_char <= value <= upper_char]
            char_end = min(candidates, key=lambda value: (abs(value - target_char), value)) if candidates else target_char
            next_token_index = token_index
            while next_token_index < len(tokens) and tokens[next_token_index][0] < char_end:
                next_token_index += 1
            if next_token_index == token_index:
                next_token_index = min(token_index + target_tokens, len(tokens))
                char_end = tokens[next_token_index - 1][1]
        window_text = text[char_start:char_end]
        token_count = sum(1 for _ in _TOKEN_RE.finditer(window_text))
        if token_count:
            original = to_original_span(document, char_start, char_end)
            heading_path: tuple[str, ...] = ()
            for heading in survey.headings:
                if heading.start <= char_start:
                    heading_path = heading.path
                else:
                    break
            window_hash = hashlib.sha256(window_text.encode("utf-8")).hexdigest()
            windows.append(ExtractionWindowV1(
                window_id=stable_id("window", document.document_id, sequence, char_start, char_end, window_hash),
                document_id=document.document_id,
                sequence=sequence,
                normalized_start=char_start,
                normalized_end=char_end,
                original_start=original.start if original.exact else None,
                original_end=original.end if original.exact else None,
                text=window_text,
                heading_path=heading_path,
                token_count=token_count,
                window_sha256=window_hash,
            ))
            sequence += 1
        char_start = char_end
        token_index = next_token_index
    return tuple(windows)


def _bucket_key(window: ExtractionWindowV1) -> tuple[int, str, int]:
    return ((window.token_count + 127) // 128, window.document_id, window.sequence)


def _raw_mention(
    document: NormalizedDocumentV1,
    window: ExtractionWindowV1,
    prediction: EntityPrediction,
    sequence: int,
    *,
    schema_release: str = "",
) -> RawMentionV1:
    local_valid = (
        0 <= prediction.start < prediction.end <= len(window.text)
        and window.text[prediction.start:prediction.end] == prediction.text
    )
    normalized_start = window.normalized_start + prediction.start
    normalized_end = window.normalized_start + prediction.end
    document_valid = (
        local_valid
        and normalized_end <= len(document.normalized_text)
        and document.normalized_text[normalized_start:normalized_end] == prediction.text
    )
    original_start: int | None = None
    original_end: int | None = None
    exact_original = False
    if document_valid:
        original = to_original_span(document, normalized_start, normalized_end)
        original_start = original.start if original.exact else None
        original_end = original.end if original.exact else None
        exact_original = original.exact
    errors = []
    if not local_valid:
        errors.append("local_surface_mismatch")
    if local_valid and not document_valid:
        errors.append("normalized_document_surface_mismatch")
    if document_valid and not exact_original:
        errors.append("original_span_not_exactly_representable")
    terminal = MentionTerminalState.ALIGNED if not errors else MentionTerminalState.ALIGNMENT_FAILURE
    return RawMentionV1(
        mention_id=stable_id(
            "raw-mention", document.document_id, window.window_id, prediction.start,
            prediction.end, prediction.text, prediction.entity_type, PROVIDER_RELEASE,
        ),
        document_id=document.document_id,
        window_id=window.window_id,
        sequence=sequence,
        surface=prediction.text,
        entity_type=prediction.entity_type,
        confidence=prediction.confidence,
        local_start=max(0, prediction.start),
        local_end=max(prediction.start + 1, prediction.end),
        normalized_start=normalized_start if local_valid else None,
        normalized_end=normalized_end if local_valid else None,
        original_start=original_start,
        original_end=original_end,
        terminal_state=terminal,
        alignment_error=";".join(errors),
        provider_release=PROVIDER_RELEASE,
        facet=prediction.facet,
        schema_release=schema_release,
    )


def select_schema_adapters(
    document: NormalizedDocumentV1, survey: DocumentSurveyV1,
) -> tuple[str, ...]:
    """Deterministic, survey-derived corpus-adapter selection.

    A document dense in key-value metadata lines or identifier-shaped tokens
    activates the metadata adapter. Thresholds come from versioned schema
    configuration; the decision is stamped in the census report.
    """
    text = document.normalized_text
    lines = [line for line in text.splitlines() if line.strip()]
    key_value = sum(
        1 for line in lines
        if re.match(r"^\s*(?:[-*]\s+)?(?:\*\*)?[A-Za-z_][A-Za-z0-9_ ]{0,40}(?:\*\*)?\s*:\s+\S", line)
    )
    key_value_ratio = key_value / len(lines) if lines else 0.0
    words = max(1, len(text.split()))
    identifiers = len(re.findall(
        r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+){2,}\b|\b[a-z]+(?:_[a-z0-9]+)+\b", text,
    ))
    identifier_density = identifiers / words
    selected: list[str] = []
    for name, adapter in SCHEMA_CONFIG["adapters"].items():
        rules = adapter.get("selection") or {}
        if (
            key_value_ratio >= float(rules.get("key_value_line_ratio_min", 2.0))
            or identifier_density >= float(rules.get("identifier_density_min", 2.0))
        ):
            selected.append(name)
    return tuple(sorted(selected))


def run_entity_census(
    documents: Sequence[NormalizedDocumentV1],
    surveys: Sequence[DocumentSurveyV1],
    provider: GLiNER2CPUProvider,
    sink: RawMentionSink,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    threshold: float = DEFAULT_THRESHOLD,
) -> CensusOutput:
    if len(documents) != len(surveys):
        raise ValueError("documents and surveys must have equal length")
    document_by_id = {document.document_id: document for document in documents}
    all_windows: list[ExtractionWindowV1] = []
    for document, survey in zip(documents, surveys):
        if document.document_id != survey.document_id:
            raise ValueError("survey document identity mismatch")
        all_windows.extend(build_census_windows(document, survey))
    adapter_by_document = {
        document.document_id: select_schema_adapters(document, survey)
        for document, survey in zip(documents, surveys)
    }
    bucketed = sorted(all_windows, key=_bucket_key)
    mentions: list[RawMentionV1] = []
    persisted_calls = 0
    inference_started = time.perf_counter()
    predictions: list[list[EntityPrediction]] = [[] for _ in bucketed]
    adapter_groups: dict[tuple[str, ...], list[int]] = {}
    for index, window in enumerate(bucketed):
        adapter_groups.setdefault(adapter_by_document[window.document_id], []).append(index)
    for adapters, indexes in sorted(adapter_groups.items()):
        group_predictions = provider.predict_entities(
            [bucketed[index].text for index in indexes],
            batch_size=batch_size,
            threshold=threshold,
            adapters=adapters,
        )
        for index, row in zip(indexes, group_predictions):
            predictions[index] = row
    for batch_start in range(0, len(bucketed), batch_size):
        batch = bucketed[batch_start:batch_start + batch_size]
        batch_predictions = predictions[batch_start:batch_start + batch_size]
        batch_mentions: list[RawMentionV1] = []
        for window, row in zip(batch, batch_predictions):
            document = document_by_id[window.document_id]
            for sequence, prediction in enumerate(row):
                batch_mentions.append(_raw_mention(
                    document, window, prediction, sequence,
                    schema_release=f"{SCHEMA_RELEASE}:{schema_hash(adapter_by_document[window.document_id])[:16]}",
                ))
        sink.persist(batch_mentions)
        persisted_calls += 1
        mentions.extend(batch_mentions)
    inference_seconds = time.perf_counter() - inference_started
    window_order = {window.window_id: index for index, window in enumerate(all_windows)}
    mentions.sort(key=lambda item: (
        window_order[item.window_id], item.local_start, item.local_end,
        item.entity_type, item.surface, item.mention_id,
    ))
    aligned = sum(item.terminal_state == MentionTerminalState.ALIGNED for item in mentions)
    failures = len(mentions) - aligned
    identity = stable_digest([item.model_dump(mode="json") for item in mentions])
    report: dict[str, object] = {
        "schema_version": "polymath.entity_census_report.v1",
        "status": "passed",
        "release": CENSUS_RELEASE,
        "documents": len(documents),
        "windows": len(all_windows),
        "emitted_predictions": len(mentions),
        "persisted_records": len(mentions),
        "aligned_mentions": aligned,
        "alignment_failures": failures,
        "schema_release": SCHEMA_RELEASE,
        "schema_adapters_by_document": {
            document_id: list(adapters)
            for document_id, adapters in sorted(adapter_by_document.items())
        },
        "schema_hashes": {
            document_id: schema_hash(adapters)
            for document_id, adapters in sorted(adapter_by_document.items())
        },
        "conservation": len(mentions) == aligned + failures,
        "strict_alignment_rate": aligned / len(mentions) if mentions else 1.0,
        "batch_size": batch_size,
        "persistence_batches": persisted_calls,
        "inference_seconds": inference_seconds,
        "identity_digest": identity,
        "deterministic_output_order": True,
        "stage_dependencies": ["normalization", "survey", "entity_provider"],
    }
    return CensusOutput(tuple(all_windows), tuple(mentions), report)
