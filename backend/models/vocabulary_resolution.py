"""Typed wrapper for CorpusVocabularyResolver output (Slice 1 scaffolding).

The resolver (services/retriever/vocabulary.py) stays untouched in Slice 1:
``VocabularyResolutionBundle.from_resolution`` wraps the existing dict so
downstream stages (ontology resolution, IR stamping, traces) consume a
typed contract instead of an untyped mapping. Round-trip equality with the
resolver dict is the contract guarantee (see tests).

Slice 1 does NOT change the resolver's return type; call-site adoption is a
later step per the owner-mandated order.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

VOCABULARY_BUNDLE_SCHEMA_VERSION = "polymath.vocabulary_resolution_bundle.v1"


class GlobalSearchRecord(BaseModel):
    """The fanout/merge record proving corpus-scoped resolution."""

    model_config = ConfigDict(frozen=True, extra="allow")

    mode: str = ""
    selected_corpus_ids: tuple[str, ...] = ()
    represented_corpus_ids: tuple[str, ...] = ()
    per_corpus_reservation: int = 0
    match_count: int = 0


class VocabularyResolutionBundle(BaseModel):
    """Typed view over ``CorpusVocabularyResolver.resolve`` output.

    Extra fields are PRESERVED (``extra="allow"``) so wrapping never drops
    diagnostics the resolver adds in future versions — the wrapper must stay
    lossless while the resolver keeps evolving independently.
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    schema_version: str = Field(default=VOCABULARY_BUNDLE_SCHEMA_VERSION)
    version: str = ""
    query: str = ""
    matches: tuple[dict[str, Any], ...] = ()
    per_corpus: dict[str, Any] = Field(default_factory=dict)
    document_profiles: tuple[dict[str, Any], ...] = ()
    raptor_ancestors: tuple[dict[str, Any], ...] = ()
    rejected_expansions: tuple[dict[str, Any], ...] = ()
    global_search: GlobalSearchRecord | None = None
    cache: dict[str, Any] = Field(default_factory=dict)
    duration_s: float = 0.0

    @classmethod
    def from_resolution(cls, resolution: dict[str, Any]) -> "VocabularyResolutionBundle":
        """Wrap the resolver dict without mutating it."""

        payload = dict(resolution or {})
        payload.pop("schema_version", None)
        return cls.model_validate(payload)

    def to_resolution_dict(self) -> dict[str, Any]:
        """Lossless dump back into the resolver's dict contract.

        ``mode="json"`` restores the resolver's plain-list shape (pydantic
        coerces typed sequence fields to tuples on validation).
        """

        return self.model_dump(mode="json", exclude_none=False)
