"""P2.5b legacy fixture adapters — LEGACY store rows -> contract-valid
envelope-era equivalents.

Checklist anchor (docs/RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md, P2.5b):
- "Ship adapters/dispositions for documents, source identity, parent/child
  hierarchy, Ghost B rows, parent summaries, ... `corpus_lexicon`, ..."
- Acceptance: "Legacy fixture adapters produce contract-valid equivalents
  without rewriting or relabeling legacy observations as accepted claims."

Contract sources (FINAL_SCHEMA_METADATA_ARCHITECTURE_2026-07-13.md):
- §Identifier recipes: NEVER infer version lineage. Without a strong external
  source key the current content-derived ``doc_id`` is preserved as the legacy
  identity and later versions bind only via explicit owner/source lineage.
- §Canonical artifact families 1: current ``doc_id`` retained as
  ``legacy_doc_id`` compatibility alias.
- §Canonical artifact families 4: legacy Ghost B ERE rows adapt into the
  OBSERVATION lane ("never relabel legacy triples as accepted claims");
  observation bundles have no ASSERTED status.
- §Canonical artifact families 10: current parent summaries stay explicitly
  typed ``RetrievalSummary`` — "useful now and ... not falsely relabeled as
  claim-grounded" — never ``SemanticDigest``.
- §Canonical artifact families 6: lexicon entries remain the canonical concept
  store; senses/mappings preserve ``canonical_key`` identity.

Design rules enforced here:
- Pure functions, no I/O: each adapter takes ONE legacy row (a mapping, e.g. a
  Mongo document) and returns a typed pydantic record. Input is never mutated.
- Strict about required legacy fields: missing/empty required fields raise
  :class:`LegacyAdapterError` listing every missing path — no silent defaults.
- Tolerant of extra fields: legacy rows are messy; unknown keys are ignored.
- No rewriting: legacy values are carried verbatim (including typo'd
  ``schema_version`` strings observed in production ghost_b rows, e.g.
  ``polymad.extract.v2``); no whitespace stripping on echoes.
- No relabeling: outputs never carry accepted/asserted/validated status.
  Contract-status fields are pinned to candidate-class Literals, legacy status
  values survive only under ``legacy_*`` provenance echo fields, and every
  adapter output is re-checked by :func:`assert_no_promoted_status` at
  construction time (fail-closed).
- No fabricated coordinates: legacy entities/relations/facts carry no offsets,
  so they adapt as offset-free candidate observations (family-4 adapter lane),
  NOT as exact-span ``SpanObservation``/``EvidenceRef`` records. Ghost B
  ``temporal_captures`` DO carry char offsets; the adapter round-trips them
  against the row text and marks ``offsets_verified`` honestly (verified
  live 290/290 on real rows, 2026-07-14). Graph promotion receipts
  (``graph_promotion_result``/``promoted_at``) are projection bookkeeping,
  not observation semantics, and are deliberately not carried.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.hash_taxonomy import canonicalize, namespace_hash
from models.identifier_recipes import logical_doc_id, source_version_id
from models.semantic_artifacts import domain_hash

ADAPTER_VERSION = "legacy_adapters.v1"

#: source_identity.v1 kinds that are STRONG external source keys. A
#: ``content_hash`` key is content-derived: two changed uploads of the same
#: logical document get different keys, so using it for logical identity would
#: BE lineage inference — exactly what §Identifier recipes forbids.
STRONG_SOURCE_KINDS = frozenset({"url", "youtube_video"})

#: Contract status keys that must never carry a promoted value in adapter
#: output. ``legacy_*``-prefixed echo fields are exempt by naming.
_STATUS_KEYS = frozenset(
    {"assignment_state", "validation_status", "knowledge_status", "status"}
)
_PROMOTED_VALUES = frozenset(
    {"accepted", "asserted", "valid", "validated", "promoted"}
)


class LegacyAdapterError(ValueError):
    """A legacy row is missing required fields. Lists ALL missing paths."""

    def __init__(self, collection: str, missing: list[str]):
        self.collection = collection
        self.missing = sorted(missing)
        super().__init__(
            f"{collection} row is missing required legacy fields: "
            + ", ".join(self.missing)
        )


def assert_no_promoted_status(payload: Any, path: str = "$") -> None:
    """Fail-closed no-relabeling guard.

    Recursively rejects any contract status key carrying a promoted value.
    Keys prefixed ``legacy_`` are verbatim provenance echoes and are exempt
    (by construction they are not contract status fields).
    """

    if isinstance(payload, Mapping):
        for key, value in payload.items():
            child = f"{path}.{key}"
            if (
                isinstance(key, str)
                and key in _STATUS_KEYS
                and isinstance(value, str)
                and value.lower() in _PROMOTED_VALUES
            ):
                raise ValueError(
                    f"adapter output relabels legacy data as promoted: "
                    f"{child}={value!r}"
                )
            assert_no_promoted_status(value, child)
    elif isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            assert_no_promoted_status(item, f"{path}[{index}]")


# ---------------------------------------------------------------------------
# helpers (pure)
# ---------------------------------------------------------------------------


def _get(row: Mapping[str, Any], path: str) -> Any:
    current: Any = row
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _require(row: Mapping[str, Any], collection: str, paths: list[str]) -> None:
    missing = [p for p in paths if _is_missing(_get(row, p))]
    if missing:
        raise LegacyAdapterError(collection, missing)


def _require_keys(row: Mapping[str, Any], collection: str, keys: list[str]) -> None:
    """Presence check for fields where an EMPTY list is legitimate data."""

    missing = [k for k in keys if k not in row]
    if missing:
        raise LegacyAdapterError(collection, missing)


def _mint(tag: str, prefix: str, value: Any) -> str:
    # Adapter outputs are logical compatibility artifacts, not a sixteenth
    # free-form hash family. Keep their identity inside the frozen
    # ``logical-artifact`` namespace and make the adapter kind explicit in the
    # natural-key seed.
    digest = namespace_hash(
        "logical-artifact",
        {"artifact_kind": tag, "natural_keys": canonicalize(value)},
    ).split(":", 1)[1]
    return f"{prefix}:{digest}"


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def _contract_sha256(value: Any) -> str:
    """Translate legacy ``content_sha256`` into the canonical hash spelling.

    ``source_identity.v1`` stores the exact raw-byte digest as 64 lowercase
    hexadecimal characters, while the envelope contract requires the
    algorithm-qualified ``sha256:`` form. This is a lossless representation
    translation, not a new digest and not content normalization. Malformed
    legacy values fail closed instead of entering source-version identity.
    """

    if not isinstance(value, str):
        raise TypeError("source_identity.content_sha256 must be a string")
    digest = value.removeprefix("sha256:")
    if not _SHA256_HEX_RE.fullmatch(digest):
        raise ValueError(
            "source_identity.content_sha256 must be 64 lowercase hex "
            "characters (optionally prefixed with 'sha256:')"
        )
    return f"sha256:{digest}"


def _iso_utc(value: Any) -> str | None:
    """Render a legacy timestamp deterministically.

    Mongo/BSON datetimes are UTC by contract (pymongo returns them tz-naive),
    so naive datetimes are rendered as UTC — that is the BSON contract, not a
    guess. Strings are echoed verbatim (no rewriting). Anything else is None.
    """

    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_dt.timezone.utc)
        utc = value.astimezone(_dt.timezone.utc)
        base = utc.strftime("%Y-%m-%dT%H:%M:%S")
        if utc.microsecond:
            base += f".{utc.microsecond:06d}"
        return base + "Z"
    if isinstance(value, str):
        return value
    return None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value != "" else None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, str)]


class _AdapterModel(BaseModel):
    # extra="forbid": adapter OUTPUT is a closed contract. Note: legacy INPUT
    # rows stay tolerant — adapters read named fields off plain dicts.
    # No whitespace stripping: echoes must not rewrite legacy values.
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# 1. documents -> logical-artifact seed
# ---------------------------------------------------------------------------


class AdaptedDocumentSeed(_AdapterModel):
    schema_version: Literal["polymath.legacy_adapter.document_seed.v1"] = (
        "polymath.legacy_adapter.document_seed.v1"
    )
    source_adapter: Literal["legacy_adapters.v1"] = ADAPTER_VERSION
    legacy_collection: Literal["documents"] = "documents"
    artifact_kind: Literal["source_document_seed"] = "source_document_seed"
    doc_id: str
    legacy_doc_id: str
    logical_doc_id_minted: bool
    needs_owner_lineage: bool
    corpus_id: str
    source_kind: str
    source_key: str
    strong_source_key: str | None
    legacy_content_sha256: str
    source_content_hash: str
    source_version_id: str
    logical_artifact_hash: str
    title: str | None = None
    author: str | None = None
    filename: str | None = None
    created_at: str | None = None

    @model_validator(mode="after")
    def validate_lineage_coherence(self) -> "AdaptedDocumentSeed":
        if self.needs_owner_lineage == self.logical_doc_id_minted:
            raise ValueError(
                "exactly one lineage path applies: minted logical id XOR "
                "legacy id pending owner lineage"
            )
        if self.logical_doc_id_minted:
            if self.strong_source_key is None:
                raise ValueError("minted logical doc_id requires a strong source key")
            if self.doc_id == self.legacy_doc_id:
                raise ValueError("minted logical doc_id must not equal the legacy id")
        else:
            if self.doc_id != self.legacy_doc_id:
                raise ValueError(
                    "without a strong source key the legacy doc_id IS the identity"
                )
        return self


def adapt_document(row: Mapping[str, Any]) -> AdaptedDocumentSeed:
    """documents row -> logical-artifact seed with compatibility aliases.

    Lineage rule (§Identifier recipes): only an external stable key
    (``url``/``youtube_video``) may seed a logical ``doc_id``. A
    ``content_hash`` source key is content-derived, so the legacy
    content-derived ``doc_id`` is kept as the identity and
    ``needs_owner_lineage=True`` is emitted — binding later versions is the
    owner's explicit call, never an inference.
    """

    collection = "documents"
    _require(
        row,
        collection,
        [
            "doc_id",
            "corpus_id",
            "source_identity.source_kind",
            "source_identity.source_key",
            "source_identity.content_sha256",
        ],
    )
    legacy_doc_id = row["doc_id"]
    corpus_id = row["corpus_id"]
    identity = row["source_identity"]
    source_kind = identity["source_kind"]
    source_key = identity["source_key"]
    legacy_content_sha256 = identity["content_sha256"]
    source_content_hash = _contract_sha256(legacy_content_sha256)

    if source_kind in STRONG_SOURCE_KINDS:
        strong_source_key: str | None = source_key
        effective_doc_id = logical_doc_id(corpus_id, source_key)
        minted = True
        needs_owner_lineage = False
        natural_keys: dict[str, str] = {
            "corpus_id": corpus_id,
            "strong_source_key": source_key,
        }
    else:
        strong_source_key = None
        effective_doc_id = legacy_doc_id
        minted = False
        needs_owner_lineage = True
        natural_keys = {
            "corpus_id": corpus_id,
            "legacy_doc_id": legacy_doc_id,
        }

    seed = AdaptedDocumentSeed(
        doc_id=effective_doc_id,
        legacy_doc_id=legacy_doc_id,
        logical_doc_id_minted=minted,
        needs_owner_lineage=needs_owner_lineage,
        corpus_id=corpus_id,
        source_kind=source_kind,
        source_key=source_key,
        strong_source_key=strong_source_key,
        legacy_content_sha256=legacy_content_sha256,
        source_content_hash=source_content_hash,
        source_version_id=source_version_id(effective_doc_id, source_content_hash),
        logical_artifact_hash=namespace_hash(
            "logical-artifact",
            {"artifact_kind": "source_document", "natural_keys": natural_keys},
        ),
        title=_str_or_none(row.get("title")),
        author=_str_or_none(row.get("author")),
        filename=_str_or_none(row.get("filename")),
        created_at=_iso_utc(row.get("created_at")),
    )
    assert_no_promoted_status(seed.model_dump())
    return seed


# ---------------------------------------------------------------------------
# 2. ghost_b_extractions -> observation-lane bundle
# ---------------------------------------------------------------------------


class LegacyEntityObservation(_AdapterModel):
    observation_kind: Literal["entity"] = "entity"
    assignment_state: Literal["candidate"] = "candidate"
    canonical_name: str
    surface_form: str | None = None
    entity_type: str | None = None
    object_kind: str | None = None
    confidence: float | None = None
    definitional_phrase: str | None = None
    query_aliases: list[str] = Field(default_factory=list)


class LegacyRelationObservation(_AdapterModel):
    observation_kind: Literal["relation"] = "relation"
    assignment_state: Literal["candidate"] = "candidate"
    subject: str
    predicate: str
    object: str
    object_kind: str | None = None
    confidence: float | None = None
    #: claimed evidence cue, verbatim. NOT an exact EvidenceRef: legacy rows
    #: carry no offsets, and a child ref without an exact span is supporting
    #: context only (FINAL_SCHEMA family 3).
    evidence_phrase: str | None = None
    relation_cue: str | None = None
    source_predicate: str | None = None
    #: verbatim normalization/repair marker from the legacy row (e.g.
    #: ``schema_predicate_alias``); provenance echo, never contract status.
    legacy_validation_status: str | None = None


class LegacyFactObservation(_AdapterModel):
    observation_kind: Literal["fact"] = "fact"
    assignment_state: Literal["candidate"] = "candidate"
    subject: str
    property_name: str
    value: str
    fact_type: str | None = None
    unit: str | None = None
    condition: str | None = None
    confidence: float | None = None
    evidence_phrase: str | None = None


class LegacyTemporalCaptureObservation(_AdapterModel):
    observation_kind: Literal["temporal_capture"] = "temporal_capture"
    assignment_state: Literal["candidate"] = "candidate"
    text: str
    char_start: int | None = None
    char_end: int | None = None
    detector: str | None = None
    role_candidates: list[str] = Field(default_factory=list)
    #: True only when row_text[char_start:char_end] == text — an honest
    #: round-trip against the row's own text, never a fabricated coordinate.
    offsets_verified: bool = False
    quote_hash: str | None = None


class AdaptedObservationBundle(_AdapterModel):
    """Observation-lane equivalent of one legacy ghost_b_extractions row.

    Conforms to the FINAL_SCHEMA family-4 ObservationBundle contract for the
    legacy ERE adapter lane: provider-neutral candidate observations with
    durable provenance and NO asserted/accepted status anywhere. It is a
    distinct schema id from ``polymath.observation_bundle.v1`` because legacy
    entities/relations/facts carry no exact offsets, and fabricating spans
    would violate the no-fabricated-coordinates rule.
    """

    bundle_id: str
    schema_version: Literal["polymath.observation_bundle.legacy_ere.v1"] = (
        "polymath.observation_bundle.legacy_ere.v1"
    )
    lane: Literal["observation"] = "observation"
    source_adapter: Literal["legacy_adapters.v1"] = ADAPTER_VERSION
    legacy_collection: Literal["ghost_b_extractions"] = "ghost_b_extractions"
    # compatibility aliases (FINAL_SCHEMA family 2: current doc/chunk ids ride
    # as aliases; no hierarchy_node_id is minted without a hierarchy recipe).
    chunk_id: str
    doc_id: str
    corpus_id: str
    text_length: int
    text_sha256: str
    producer: str
    #: verbatim legacy schema version — production rows include typo'd values
    #: (``polath.extract.v2``, ``polymad.extract.v2``, ...); preserved, never
    #: corrected, so replay/provenance stays byte-honest.
    legacy_schema_version: str
    legacy_status: str | None = None
    schema_lens_id: str | None = None
    extraction_contract_hash: str | None = None
    raw_output_artifact_id: str | None = None
    legacy_chunk_hash: str | None = None
    entities: list[LegacyEntityObservation] = Field(default_factory=list)
    relations: list[LegacyRelationObservation] = Field(default_factory=list)
    facts: list[LegacyFactObservation] = Field(default_factory=list)
    temporal_captures: list[LegacyTemporalCaptureObservation] = Field(
        default_factory=list
    )
    validation_drops: list[str] = Field(default_factory=list)


def adapt_ghost_b_extraction(row: Mapping[str, Any]) -> AdaptedObservationBundle:
    """ghost_b_extractions row -> observation-lane bundle (candidates only)."""

    collection = "ghost_b_extractions"
    _require(
        row,
        collection,
        ["chunk_id", "doc_id", "corpus_id", "schema_version", "extractor", "text"],
    )
    # entities/relations/facts must be PRESENT (an absent array signals a
    # truncated row) but may legitimately be empty; temporal_captures only
    # exists on a minority of rows and defaults honestly to [].
    _require_keys(row, collection, ["entities", "relations", "facts"])

    text = row["text"]
    missing_item_fields: list[str] = []

    entities: list[LegacyEntityObservation] = []
    for index, item in enumerate(row["entities"] or []):
        if not isinstance(item, Mapping):
            missing_item_fields.append(f"entities[{index}]")
            continue
        if _is_missing(item.get("canonical_name")):
            missing_item_fields.append(f"entities[{index}].canonical_name")
            continue
        entities.append(
            LegacyEntityObservation(
                canonical_name=item["canonical_name"],
                surface_form=_str_or_none(item.get("surface_form")),
                entity_type=_str_or_none(item.get("entity_type")),
                object_kind=_str_or_none(item.get("object_kind")),
                confidence=_float_or_none(item.get("confidence")),
                definitional_phrase=_str_or_none(item.get("definitional_phrase")),
                query_aliases=_str_list(item.get("query_aliases")),
            )
        )

    relations: list[LegacyRelationObservation] = []
    for index, item in enumerate(row["relations"] or []):
        if not isinstance(item, Mapping):
            missing_item_fields.append(f"relations[{index}]")
            continue
        row_missing = [
            f"relations[{index}].{field}"
            for field in ("subject", "predicate", "object")
            if _is_missing(item.get(field))
        ]
        if row_missing:
            missing_item_fields.extend(row_missing)
            continue
        relations.append(
            LegacyRelationObservation(
                subject=item["subject"],
                predicate=item["predicate"],
                object=item["object"],
                object_kind=_str_or_none(item.get("object_kind")),
                confidence=_float_or_none(item.get("confidence")),
                evidence_phrase=_str_or_none(item.get("evidence_phrase")),
                relation_cue=_str_or_none(item.get("relation_cue")),
                source_predicate=_str_or_none(item.get("source_predicate")),
                legacy_validation_status=_str_or_none(item.get("validation_status")),
            )
        )

    facts: list[LegacyFactObservation] = []
    for index, item in enumerate(row["facts"] or []):
        if not isinstance(item, Mapping):
            missing_item_fields.append(f"facts[{index}]")
            continue
        row_missing = [
            f"facts[{index}].{field}"
            for field in ("subject", "property_name", "value")
            if _is_missing(item.get(field))
        ]
        if row_missing:
            missing_item_fields.extend(row_missing)
            continue
        facts.append(
            LegacyFactObservation(
                subject=item["subject"],
                property_name=item["property_name"],
                value=item["value"],
                fact_type=_str_or_none(item.get("fact_type")),
                unit=_str_or_none(item.get("unit")),
                condition=_str_or_none(item.get("condition")),
                confidence=_float_or_none(item.get("confidence")),
                evidence_phrase=_str_or_none(item.get("evidence_phrase")),
            )
        )

    validation_drops: list[str] = []
    temporal_captures: list[LegacyTemporalCaptureObservation] = []
    for index, item in enumerate(row.get("temporal_captures") or []):
        if not isinstance(item, Mapping):
            missing_item_fields.append(f"temporal_captures[{index}]")
            continue
        capture_text = item.get("text")
        if _is_missing(capture_text):
            missing_item_fields.append(f"temporal_captures[{index}].text")
            continue
        start = item.get("char_start")
        end = item.get("char_end")
        verified = (
            isinstance(start, int)
            and isinstance(end, int)
            and 0 <= start < end <= len(text)
            and text[start:end] == capture_text
        )
        if not verified:
            validation_drops.append(
                f"temporal_captures[{index}]: offsets failed exact round-trip "
                "against row text; span kept as unverified candidate"
            )
        temporal_captures.append(
            LegacyTemporalCaptureObservation(
                text=capture_text,
                char_start=start if isinstance(start, int) else None,
                char_end=end if isinstance(end, int) else None,
                detector=_str_or_none(item.get("detector")),
                role_candidates=_str_list(item.get("role_candidates")),
                offsets_verified=verified,
                quote_hash=domain_hash("evidence-quote", capture_text)
                if verified
                else None,
            )
        )

    if missing_item_fields:
        raise LegacyAdapterError(collection, missing_item_fields)

    text_sha256 = _sha256_text(text)
    bundle = AdaptedObservationBundle(
        bundle_id=_mint(
            "legacy-observation-bundle",
            "legacyobs",
            {
                "legacy_collection": collection,
                "chunk_id": row["chunk_id"],
                "doc_id": row["doc_id"],
                "corpus_id": row["corpus_id"],
                "legacy_schema_version": row["schema_version"],
                "extraction_contract_hash": _str_or_none(
                    row.get("extraction_contract_hash")
                ),
                "text_sha256": text_sha256,
            },
        ),
        chunk_id=row["chunk_id"],
        doc_id=row["doc_id"],
        corpus_id=row["corpus_id"],
        text_length=len(text),
        text_sha256=text_sha256,
        producer=row["extractor"],
        legacy_schema_version=row["schema_version"],
        legacy_status=_str_or_none(row.get("status")),
        schema_lens_id=_str_or_none(row.get("schema_lens_id")),
        extraction_contract_hash=_str_or_none(row.get("extraction_contract_hash")),
        raw_output_artifact_id=_str_or_none(row.get("raw_output_artifact_id")),
        legacy_chunk_hash=_str_or_none(row.get("chunk_hash")),
        entities=entities,
        relations=relations,
        facts=facts,
        temporal_captures=temporal_captures,
        validation_drops=validation_drops,
    )
    assert_no_promoted_status(bundle.model_dump())
    return bundle


# ---------------------------------------------------------------------------
# 3. parent_chunks (with summary) -> RetrievalSummary record
# ---------------------------------------------------------------------------


class LegacyLatentConceptCapture(_AdapterModel):
    concept: str
    evidence_basis: str | None = None
    aliases: list[str] = Field(default_factory=list)


class LegacyCapturedFields(_AdapterModel):
    """Interim-v1 captured fields: LLM proposals, explicitly never validated."""

    derivation_method: Literal["llm_proposal"] = "llm_proposal"
    capture_contract: Literal["interim-v1"] = "interim-v1"
    validation_status: Literal["unvalidated"] = "unvalidated"
    latent_concepts: list[LegacyLatentConceptCapture] = Field(default_factory=list)
    temporal_class: str | None = None


class AdaptedRetrievalSummary(_AdapterModel):
    """RetrievalSummary-typed record (FINAL_SCHEMA family 10) — NOT a
    SemanticDigest: it is not claim-grounded and must never claim to be."""

    record_id: str
    schema_version: Literal["polymath.legacy_adapter.retrieval_summary.v1"] = (
        "polymath.legacy_adapter.retrieval_summary.v1"
    )
    artifact_kind: Literal["retrieval_summary"] = "retrieval_summary"
    source_adapter: Literal["legacy_adapters.v1"] = ADAPTER_VERSION
    legacy_collection: Literal["parent_chunks"] = "parent_chunks"
    parent_id: str
    doc_id: str
    corpus_id: str
    summary_id: str | None = None
    summary: str
    summary_model: str
    summary_created_at: str | None = None
    legacy_summary_schema_version: str | None = None
    legacy_summary_type: str | None = None
    #: verbatim legacy Ghost-A structural validation marker (observed values:
    #: valid / quarantined / legacy_stamped_heuristic_v1). Provenance echo
    #: ONLY — the adapted record itself stays ``unvalidated``.
    legacy_validation_status: str | None = None
    legacy_quality_score: float | None = None
    legacy_quality_flags: list[str] = Field(default_factory=list)
    source_child_ids: list[str] = Field(default_factory=list)
    source_hash: str | None = None
    captured_fields: LegacyCapturedFields
    validation_status: Literal["unvalidated"] = "unvalidated"

    @model_validator(mode="after")
    def forbid_claim_grounding(self) -> "AdaptedRetrievalSummary":
        # Mirror of ClaimAssertionCandidate.forbid_unvalidated_assertion: the
        # Literal already pins the value; this documents intent loudly.
        if self.validation_status != "unvalidated":  # pragma: no cover
            raise ValueError(
                "adapters cannot validate a retrieval summary; only the later "
                "claim/evidence validator may"
            )
        return self


def adapt_parent_summary(row: Mapping[str, Any]) -> AdaptedRetrievalSummary:
    """parent_chunks row (with summary) -> RetrievalSummary-typed record.

    ``latent_concepts``/``temporal_class`` ride as CAPTURED FIELDS under
    ``derivation_method="llm_proposal"`` (interim-v1) and are never validated
    here. Provenance comes from ``summary_model`` (required — an unattributed
    summary cannot carry provenance).
    """

    collection = "parent_chunks"
    _require(
        row, collection, ["parent_id", "doc_id", "corpus_id", "summary", "summary_model"]
    )

    latent_missing: list[str] = []
    latent: list[LegacyLatentConceptCapture] = []
    for index, item in enumerate(row.get("latent_concepts") or []):
        if not isinstance(item, Mapping):
            latent_missing.append(f"latent_concepts[{index}]")
            continue
        if _is_missing(item.get("concept")):
            latent_missing.append(f"latent_concepts[{index}].concept")
            continue
        latent.append(
            LegacyLatentConceptCapture(
                concept=item["concept"],
                evidence_basis=_str_or_none(item.get("evidence_basis")),
                aliases=_str_list(item.get("aliases")),
            )
        )
    if latent_missing:
        raise LegacyAdapterError(collection, latent_missing)

    summary_text = row["summary"]
    record = AdaptedRetrievalSummary(
        record_id=_mint(
            "legacy-retrieval-summary",
            "legacysum",
            {
                "legacy_collection": collection,
                "parent_id": row["parent_id"],
                "doc_id": row["doc_id"],
                "corpus_id": row["corpus_id"],
                "summary_sha256": _sha256_text(summary_text),
                "summary_model": row["summary_model"],
            },
        ),
        parent_id=row["parent_id"],
        doc_id=row["doc_id"],
        corpus_id=row["corpus_id"],
        summary_id=_str_or_none(row.get("summary_id")),
        summary=summary_text,
        summary_model=row["summary_model"],
        summary_created_at=_iso_utc(row.get("summary_created_at")),
        legacy_summary_schema_version=_str_or_none(row.get("schema_version")),
        legacy_summary_type=_str_or_none(row.get("summary_type")),
        legacy_validation_status=_str_or_none(row.get("validation_status")),
        legacy_quality_score=_float_or_none(row.get("quality_score")),
        legacy_quality_flags=_str_list(row.get("quality_flags")),
        source_child_ids=_str_list(row.get("source_child_ids")),
        source_hash=_str_or_none(row.get("source_hash")),
        captured_fields=LegacyCapturedFields(
            latent_concepts=latent,
            temporal_class=_str_or_none(row.get("temporal_class")),
        ),
    )
    assert_no_promoted_status(record.model_dump())
    return record


# ---------------------------------------------------------------------------
# 4. corpus_lexicon -> ConceptSense/mapping-shaped record
# ---------------------------------------------------------------------------


class AdaptedConceptMapping(_AdapterModel):
    """Identity mapping of the adapted sense to its own corpus_lexicon entry.

    ``exact`` here is identity by construction (the sense IS derived from the
    entry), not a semantic inference; validation still stays ``candidate`` —
    promotion belongs to the later validator, never an adapter.
    """

    mapping_type: Literal["exact"] = "exact"
    method: Literal["legacy_lexicon_identity"] = "legacy_lexicon_identity"
    target_lexicon_id: str
    target_canonical_key: str
    score: float = 1.0
    lexicon_schema_version: str | None = None
    validation_status: Literal["candidate"] = "candidate"


class AdaptedConceptSense(_AdapterModel):
    sense_id: str
    schema_version: Literal["polymath.legacy_adapter.concept_sense.v1"] = (
        "polymath.legacy_adapter.concept_sense.v1"
    )
    source_adapter: Literal["legacy_adapters.v1"] = ADAPTER_VERSION
    legacy_collection: Literal["corpus_lexicon"] = "corpus_lexicon"
    corpus_id: str
    lexicon_id: str
    #: canonical_key is the identity spine of the legacy lexicon — preserved
    #: byte-verbatim (FINAL_SCHEMA family 6: corpus_lexicon remains canonical).
    canonical_key: str
    canonical_name: str
    gloss: str | None = None
    retrieval_gloss: str | None = None
    aliases: list[str] = Field(default_factory=list)
    abbreviations: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)
    entity_types: list[str] = Field(default_factory=list)
    source_document_ids: list[str] = Field(default_factory=list)
    source_chunk_ids: list[str] = Field(default_factory=list)
    source_parent_ids: list[str] = Field(default_factory=list)
    legacy_lexicon_state: str | None = None
    mean_confidence: float | None = None
    mapping: AdaptedConceptMapping


def adapt_lexicon_entry(row: Mapping[str, Any]) -> AdaptedConceptSense:
    """corpus_lexicon row -> ConceptSense-shaped record + identity mapping."""

    collection = "corpus_lexicon"
    _require(
        row, collection, ["lexicon_id", "corpus_id", "canonical_key", "canonical_name"]
    )

    sense = AdaptedConceptSense(
        sense_id=_mint(
            "legacy-concept-sense",
            "legacysense",
            {
                "legacy_collection": collection,
                "corpus_id": row["corpus_id"],
                "lexicon_id": row["lexicon_id"],
                "canonical_key": row["canonical_key"],
            },
        ),
        corpus_id=row["corpus_id"],
        lexicon_id=row["lexicon_id"],
        canonical_key=row["canonical_key"],
        canonical_name=row["canonical_name"],
        gloss=_str_or_none(row.get("utility_gloss")),
        retrieval_gloss=_str_or_none(row.get("retrieval_gloss")),
        aliases=_str_list(row.get("aliases")),
        abbreviations=_str_list(row.get("abbreviations")),
        entity_ids=_str_list(row.get("entity_ids")),
        entity_types=_str_list(row.get("entity_types")),
        source_document_ids=_str_list(row.get("source_document_ids")),
        source_chunk_ids=_str_list(row.get("source_chunk_ids")),
        source_parent_ids=_str_list(row.get("source_parent_ids")),
        legacy_lexicon_state=_str_or_none(row.get("lexicon_state")),
        mean_confidence=_float_or_none(row.get("mean_confidence")),
        mapping=AdaptedConceptMapping(
            target_lexicon_id=row["lexicon_id"],
            target_canonical_key=row["canonical_key"],
            lexicon_schema_version=_str_or_none(row.get("schema_version")),
        ),
    )
    assert_no_promoted_status(sense.model_dump())
    return sense
