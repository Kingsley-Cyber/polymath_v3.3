"""P2.5b hash taxonomy: one canonicalizer, fifteen frozen hash namespaces.

Contract (FINAL_SCHEMA_METADATA_ARCHITECTURE_2026-07-13.md + checklist P2.5b):
- One canonical JSON serializer with EXPLICIT set-valued handling, recursive
  key ordering, UTC timestamp rules, finite JSON numbers, and NO implicit
  ``default=str`` coercion — unsupported types are hard errors, never guesses.
- Distinct, frozen namespace names for every hash family so a run/revision/
  motif/projection hash can never be mistaken for semantic identity.

Builds on models.semantic_artifacts.domain_hash (sha256 over
``tag 0x1f canonical_json(value)``) — byte-compatible for values that were
already plain JSON; this module adds the normalization front-end and the
namespace registry.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
from typing import Any

from models.semantic_artifacts import UNIT_SEPARATOR, domain_hash  # noqa: F401

CANONICAL_VERSION = "canonical_json.v1"


def canonicalize(value: Any) -> Any:
    """Recursively normalize ``value`` into strict, deterministic JSON types.

    Rules (each is a hard rule, not a preference):
    - dict: keys must be str; values canonicalized recursively (key ordering
      is applied at serialization via sort_keys).
    - list/tuple: element-wise canonicalization, order PRESERVED (ordered data).
    - set/frozenset: canonicalized element-wise then SORTED by their canonical
      JSON form — set semantics are order-free, so the serialized form must be.
    - datetime: must be timezone-aware; converted to UTC and rendered
      ``YYYY-MM-DDTHH:MM:SS(.ffffff)Z``. Naive datetimes are an error: an
      ambiguous timestamp must never silently enter an identity hash.
    - date: ISO ``YYYY-MM-DD``.
    - float: must be finite (NaN/Inf rejected). Integral floats stay floats.
    - str/int/bool/None: passed through.
    - anything else: TypeError. No implicit str() coercion, ever.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float cannot enter a canonical hash")
        return value
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("naive datetime cannot enter a canonical hash; pass tz-aware UTC")
        utc = value.astimezone(_dt.timezone.utc)
        base = utc.strftime("%Y-%m-%dT%H:%M:%S")
        if utc.microsecond:
            base += f".{utc.microsecond:06d}"
        return base + "Z"
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [canonicalize(v) for v in value]
    if isinstance(value, (set, frozenset)):
        items = [canonicalize(v) for v in value]
        return sorted(items, key=lambda x: canonical_json_v1(x))
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError(f"dict keys must be str, got {type(k).__name__}")
            out[k] = canonicalize(v)
        return out
    raise TypeError(
        f"type {type(value).__name__} is not canonicalizable; "
        "convert explicitly at the call site (no implicit default=str)"
    )


def canonical_json_v1(value: Any) -> str:
    """Serialize an already-canonicalized (or canonicalizable) value."""
    return json.dumps(
        canonicalize(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


# The fifteen frozen namespaces (checklist P2.5b). The recipe strings document
# what the hashed value MUST be; they are contracts, not suggestions.
HASH_NAMESPACES: dict[str, str] = {
    "source-content": "raw source bytes digest context: {media_type, byte_sha256, byte_length}",
    "normalized-text": "post-normalization text: {normalizer_version, text}",
    "schema": "a JSON Schema document (e.g. SemanticDigestV1.model_json_schema())",
    "registry": "one versioned registry snapshot file content as parsed JSON",
    "recipe": "deterministic pipeline parameters: {name, version, params} — never identity fields",
    "input-set": "UNORDERED set/frozenset (or pre-sorted list) of input artifact ids",
    "body": "the immutable semantic body of one artifact (identity-bearing fields only)",
    "evidence-set": "UNORDERED set/frozenset of evidence_ref_ids supporting one artifact",
    "scope": "assertion scope qualifier object: {corpus_id, doc scope, conditions}",
    "motif": "ordered frame_sequence + qualifier of one motif instance (order preserved)",
    "projection-profile": "projection manifest: {store, collection_family, payload_schema_hash, embedding_profile, quantization}",
    "work": "deterministic work identity: {work_kind, input_set_hash, recipe_hash, schema_hash}",
    "raw-output": "verbatim model/tool output: {producer, output_text_or_json}",
    "logical-artifact": "stable logical identity seed: {artifact_kind, natural_keys}",
    "revision": "one immutable revision: {logical_artifact_hash, body_hash, supersedes}",
}


def namespace_hash(namespace: str, value: Any) -> str:
    """Hash ``value`` inside one of the fifteen frozen namespaces."""
    if namespace not in HASH_NAMESPACES:
        raise KeyError(
            f"unknown hash namespace {namespace!r}; valid: {sorted(HASH_NAMESPACES)}"
        )
    return domain_hash(namespace, canonicalize(value))
