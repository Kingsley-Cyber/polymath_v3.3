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
from services.extraction.graphify_unit_kind import (
    classify_document_blocks,
    semantic_segments,
)

CENSUS_RELEASE = "graphify-entity-census-v1"
IDENTIFIER_MINER_RELEASE = "graphify-identifier-miner-v1"
TITLE_MINER_RELEASE = "graphify-title-miner-v1"
# Deterministic identifier minting (#3, owner-ratified): observable syntax the
# entity model should never have to rediscover — an uppercase code prefix,
# hyphen, digits (AR-17, INC-4821, RFC-9110, ISO-9001). Format-shaped and
# corpus-blind; facets refine later.
_MINT_IDENTIFIER_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d{1,6}(?:[A-Z0-9-]*[A-Z0-9])?\b")
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
    # Single-pass consolidation: relation candidates captured in the SAME
    # sidecar call as the entity census (doc-global offsets). The relation
    # lane consumes these instead of making a second neural pass.
    relex_relations: tuple[dict, ...] = ()


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
    """Window the document's SEMANTIC segments for GLiNER2.

    unit.kind routing (owner-ratified 2026-08-07): navigation/metadata/code/
    table blocks never reach the entity model — deterministic lanes own them.
    Every byte keeps its block and provenance; only NLP eligibility changes.
    """
    if not 1 <= min_tokens <= target_tokens <= max_tokens:
        raise ValueError("window token bounds must be ordered and positive")
    text = document.normalized_text
    classified = classify_document_blocks(document, survey)
    segments = semantic_segments(classified, len(text))
    windows: list[ExtractionWindowV1] = []
    sequence = 0
    for segment_start, segment_end in segments:
        windows_for_segment, sequence = _windows_for_segment(
            document, survey, segment_start, segment_end, sequence,
            min_tokens=min_tokens, target_tokens=target_tokens, max_tokens=max_tokens,
        )
        windows.extend(windows_for_segment)
    return tuple(windows)


def _windows_for_segment(
    document: NormalizedDocumentV1,
    survey: DocumentSurveyV1,
    segment_start: int,
    segment_end: int,
    sequence: int,
    *,
    min_tokens: int,
    target_tokens: int,
    max_tokens: int,
) -> tuple[list[ExtractionWindowV1], int]:
    text = document.normalized_text
    tokens = [
        span for span in _token_spans(text)
        if segment_start <= span[0] and span[1] <= segment_end
    ]
    if not tokens:
        return [], sequence
    boundaries = tuple(
        value for value in _structural_boundaries(document, survey)
        if segment_start <= value <= segment_end
    )
    windows: list[ExtractionWindowV1] = []
    token_index = 0
    char_start = tokens[0][0]
    while token_index < len(tokens):
        remaining = len(tokens) - token_index
        if remaining <= target_tokens:
            char_end = segment_end
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
    return windows, sequence


def _bucket_key(window: ExtractionWindowV1) -> tuple[int, str, int]:
    return ((window.token_count + 127) // 128, window.document_id, window.sequence)


def _raw_mention(
    document: NormalizedDocumentV1,
    window: ExtractionWindowV1,
    prediction: EntityPrediction,
    sequence: int,
    *,
    schema_release: str = "",
    provider_release: str = PROVIDER_RELEASE,
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
            prediction.end, prediction.text, prediction.entity_type, provider_release,
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
        provider_release=provider_release,
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
    # Explicit adapter activation (adapter-compiler seam, 2026-08-08):
    # GRAPHIFY_FORCED_ADAPTERS names adapter packs to activate for this run.
    # Pure configuration — selection thresholds, gates, and candidate
    # construction are unchanged; the schema hash stamps the difference.
    forced = tuple(
        token.strip() for token in os.environ.get("GRAPHIFY_FORCED_ADAPTERS", "").split(",")
        if token.strip()
    )
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
    compiled = ()
    try:
        from services.ontology_adapter.providers.relex import adapter_for_document
        name = adapter_for_document(document.document_id)
        compiled = (name,) if name else ()
    except Exception:  # noqa: BLE001 — adapter layer absent = config adapters only
        compiled = ()
    return tuple(sorted(set(selected) | set(forced) | set(compiled)))


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
    joint = getattr(provider, "predict_joint", None)
    raw_relex_relations: dict[tuple, dict] = {}
    for adapters, indexes in sorted(adapter_groups.items()):
        if joint is not None:
            group_predictions, group_relations = joint(
                [bucketed[index].text for index in indexes],
                adapters=adapters,
            )
            for index, relation_row in zip(indexes, group_relations):
                window = bucketed[index]
                for relation in relation_row:
                    row = {
                        "document_id": window.document_id,
                        "head_start": window.normalized_start + int(relation["head_start"]),
                        "head_end": window.normalized_start + int(relation["head_end"]),
                        "head_text": relation["head_text"],
                        "tail_start": window.normalized_start + int(relation["tail_start"]),
                        "tail_end": window.normalized_start + int(relation["tail_end"]),
                        "tail_text": relation["tail_text"],
                        "label": relation["label"],
                        "score": float(relation["score"]),
                    }
                    key = (
                        row["document_id"], row["head_start"], row["head_end"],
                        row["tail_start"], row["tail_end"], row["label"],
                    )
                    kept = raw_relex_relations.get(key)
                    if kept is None or row["score"] > kept["score"]:
                        raw_relex_relations[key] = row
        else:
            group_predictions = provider.predict_entities(
                [bucketed[index].text for index in indexes],
                batch_size=batch_size,
                threshold=threshold,
                adapters=adapters,
            )
        for index, row in zip(indexes, group_predictions):
            predictions[index] = row
    folded_duplicates = 0
    for batch_start in range(0, len(bucketed), batch_size):
        batch = bucketed[batch_start:batch_start + batch_size]
        batch_predictions = predictions[batch_start:batch_start + batch_size]
        batch_mentions: list[RawMentionV1] = []
        for window, row in zip(batch, batch_predictions):
            document = document_by_id[window.document_id]
            # Two provider labels can normalize to the same (span, text, type)
            # with different confidence/facet — one observation, two readings.
            # mention_id identity excludes confidence/facet, so fold duplicates
            # deterministically (max confidence, then lexicographic facet)
            # BEFORE persistence; the immutable-sink invariant stays exact.
            folded: dict[tuple[int, int, str, str], EntityPrediction] = {}
            for prediction in row:
                key = (prediction.start, prediction.end, prediction.text, prediction.entity_type)
                incumbent = folded.get(key)
                if incumbent is None or (
                    prediction.confidence, incumbent.facet or "~",
                ) > (incumbent.confidence, prediction.facet or "~"):
                    folded[key] = prediction
            folded_duplicates += len(row) - len(folded)
            for sequence, prediction in enumerate(folded.values()):
                batch_mentions.append(_raw_mention(
                    document, window, prediction, sequence,
                    schema_release=f"{SCHEMA_RELEASE}:{schema_hash(adapter_by_document[window.document_id])[:16]}",
                    provider_release=getattr(provider, "release", PROVIDER_RELEASE),
                ))
        sink.persist(batch_mentions)
        persisted_calls += 1
        mentions.extend(batch_mentions)
    inference_seconds = time.perf_counter() - inference_started

    # Deterministic identifier mentions UNION with model mentions (#3): one
    # thin synthetic window per identifier occurrence, provenance-marked with
    # the miner release. The reducer merges them with model output; nothing
    # is ever rediscovered by a model that deterministic syntax already knows.
    identifier_mentions = 0
    for document in documents:
        text = document.normalized_text
        doc_sequence = 1 + max(
            (window.sequence for window in all_windows if window.document_id == document.document_id),
            default=-1,
        )
        for match in _MINT_IDENTIFIER_RE.finditer(text):
            surface = match.group(0)
            start, end = match.start(), match.end()
            original = to_original_span(document, start, end)
            window_hash = hashlib.sha256(surface.encode("utf-8")).hexdigest()
            window = ExtractionWindowV1(
                window_id=stable_id(
                    "identifier-window", document.document_id, start, end,
                    surface, IDENTIFIER_MINER_RELEASE,
                ),
                document_id=document.document_id,
                sequence=doc_sequence,
                normalized_start=start,
                normalized_end=end,
                original_start=original.start if original.exact else None,
                original_end=original.end if original.exact else None,
                text=surface,
                heading_path=(),
                token_count=1,
                window_sha256=window_hash,
            )
            doc_sequence += 1
            all_windows.append(window)
            mention = _raw_mention(
                document, window,
                EntityPrediction(
                    text=surface, entity_type="artifact", start=0, end=len(surface),
                    confidence=1.0, facet="document_identifier",
                ),
                0,
                schema_release=IDENTIFIER_MINER_RELEASE,
            )
            mention = mention.model_copy(update={"provider_release": IDENTIFIER_MINER_RELEASE})
            sink.persist([mention])
            mentions.append(mention)
            identifier_mentions += 1

    # Deterministic TITLE mentions (saturation Matrix C, owner-queued): the
    # document's top heading names the document — a structural fact, never a
    # model rediscovery. Minted like identifier mentions (provenance-marked,
    # arbitration decides promotion). Two generic naming conventions: the full
    # title span, and the pre-colon head ("Name: subtitle" => "Name").
    title_mentions = 0
    for document, survey in zip(documents, surveys):
        top = min(survey.headings, key=lambda h: (h.level, h.start), default=None) \
            if survey.headings else None
        # Frontmatter title is the same structural fact in metadata form:
        # a YAML block leading the document with a title: key names the
        # document (generic convention, any corpus).
        frontmatter_title = None
        fm = re.match(r"\s*---\n(.*?)\n---", document.normalized_text, re.S)
        if fm:
            tm = re.search(r"^title:\s*\"?([^\"\n]+)\"?\s*$", fm.group(1), re.M)
            if tm:
                value = tm.group(1).strip()
                value_start = document.normalized_text.find(value, fm.start(1), fm.end(1) + 8)
                if value_start >= 0 and len(value) >= 3:
                    frontmatter_title = (value_start, value_start + len(value), value)
        if (top is None or not top.text.strip()) and frontmatter_title is None:
            continue
        doc_sequence = 1 + max(
            (window.sequence for window in all_windows if window.document_id == document.document_id),
            default=-1,
        )
        candidates = []
        if frontmatter_title is not None:
            candidates.append(frontmatter_title)
        if top is not None and top.text.strip():
            title_text = top.text.strip()
            heading_slice = document.normalized_text[top.start:top.end]
            offset = heading_slice.find(title_text)
            if offset >= 0:
                title_start = top.start + offset
                candidates.append((title_start, title_start + len(title_text), title_text))
                head, sep, _tail = title_text.partition(":")
                if sep and len(head.split()) >= 2:
                    candidates.append((title_start, title_start + len(head), head.strip()))
        for start, end, surface in candidates:
            if len(surface) < 3 or document.normalized_text[start:end].strip() != surface:
                continue
            original = to_original_span(document, start, end)
            window = ExtractionWindowV1(
                window_id=stable_id(
                    "title-window", document.document_id, start, end,
                    surface, TITLE_MINER_RELEASE,
                ),
                document_id=document.document_id,
                sequence=doc_sequence,
                normalized_start=start,
                normalized_end=end,
                original_start=original.start if original.exact else None,
                original_end=original.end if original.exact else None,
                text=document.normalized_text[start:end],
                heading_path=(),
                token_count=max(1, len(surface.split())),
                window_sha256=hashlib.sha256(surface.encode("utf-8")).hexdigest(),
            )
            doc_sequence += 1
            all_windows.append(window)
            mention = _raw_mention(
                document, window,
                EntityPrediction(
                    text=surface, entity_type="document",
                    start=0, end=len(surface),
                    confidence=1.0, facet="document_title",
                ),
                0,
                schema_release=TITLE_MINER_RELEASE,
            )
            mention = mention.model_copy(update={"provider_release": TITLE_MINER_RELEASE})
            sink.persist([mention])
            mentions.append(mention)
            title_mentions += 1

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
        "folded_duplicate_predictions": folded_duplicates,
        "identifier_mentions_minted": identifier_mentions,
        "title_mentions_minted": title_mentions,
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
        "single_pass_relex_relations": len(raw_relex_relations),
        "stage_dependencies": ["normalization", "survey", "entity_provider"],
    }
    relex_relations = tuple(sorted(
        raw_relex_relations.values(),
        key=lambda r: (r["document_id"], r["head_start"], r["tail_start"], r["label"]),
    ))
    return CensusOutput(tuple(all_windows), tuple(mentions), report, relex_relations)
