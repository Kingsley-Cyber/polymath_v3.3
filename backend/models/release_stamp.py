"""Release-identity stamp contract (Step 3 scaffolding).

The stamp records WHICH release produced or qualifies an artifact. It is
purely descriptive metadata at this stage:

- non-authoritative — nothing permits or denies behavior based on a stamp;
  the categorical release state (models/release_state.py) remains the sole
  normative authority for readiness and graph-write decisions;
- nullable — historical artifacts that lack a value keep null pins rather
  than receiving inferred ones;
- copied, never reinterpreted — adapters propagate the stored dict verbatim
  (``copy_stamp``); normalization is explicitly NOT part of propagation;
- immutable per artifact — stamped at artifact creation time;
- JSON-serializable — plain dict of ``str | None`` values.

Percentage estimates must never enter this contract. Values are filled by
the active release registry (a later control-plane slice); until then
``current_release_stamp()`` returns all-null pins honestly.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

RELEASE_STAMP_SCHEMA_VERSION = "polymath.release_stamp.v1"

#: Owner-mandated stamp shape — exact key set and order.
RELEASE_STAMP_FIELDS: tuple[str, ...] = (
    "extractor_release",
    "extractor_commit_sha",
    "extractor_config_hash",
    "ontology_release",
    "ontology_hash",
    "acceptance_policy_release",
    "acceptance_policy_hash",
    "schema_release",
    "schema_hash",
    "promotion_release",
    "certificate_id",
)


class ReleaseStamp(BaseModel):
    """Typed view over a release-identity stamp (all pins nullable)."""

    model_config = ConfigDict(frozen=True)

    extractor_release: str | None = None
    extractor_commit_sha: str | None = None
    extractor_config_hash: str | None = None
    ontology_release: str | None = None
    ontology_hash: str | None = None
    acceptance_policy_release: str | None = None
    acceptance_policy_hash: str | None = None
    schema_release: str | None = None
    schema_hash: str | None = None
    promotion_release: str | None = None
    certificate_id: str | None = None


def empty_release_stamp() -> dict[str, str | None]:
    """Canonical all-null stamp shape (JSON-compatible dict)."""

    return {field: None for field in RELEASE_STAMP_FIELDS}


def current_release_stamp() -> dict[str, str | None]:
    """The stamp attached to newly produced artifacts.

    Returns all-null pins until the active release registry exists (later
    control-plane slice). Missing pins stay null rather than being inferred
    from manifests, git state, or percentage estimates.
    """

    return empty_release_stamp()


def copy_stamp(raw: Any) -> dict[str, Any] | None:
    """Verbatim stamp propagation for adapters.

    Copies the stored dict WITHOUT reinterpretation: no key canonicalization,
    no value normalization, no inference. Anything that is not a dict (absent
    historical data) yields None — unknown stays unknown.
    """

    if not isinstance(raw, dict):
        return None
    return dict(raw)


def stamp_is_empty(stamp: dict[str, Any] | None) -> bool:
    """True when the stamp is absent or carries no non-null pin."""

    if not stamp:
        return True
    return not any(value is not None for value in stamp.values())


#: Owner-frozen stamp semantics (Step 3 closeout). The four states are the
#: ONLY interpretations any later slice may rely on:
#:
#:   legacy_uninstrumented  release_stamp key absent → legacy or
#:                          uninstrumented artifact
#:   pre_registry           stamp present with all-null fields →
#:                          instrumented artifact created before an active
#:                          release registry existed
#:   partial                some identities known; NO missing value may be
#:                          inferred
#:   complete               all canonical pins recorded — still
#:                          NON-authoritative
StampStatus = Literal["legacy_uninstrumented", "pre_registry", "partial", "complete"]


def stamp_status(stamp: Any) -> StampStatus:
    """Classify a stamp into exactly one of the four frozen semantics."""

    if not isinstance(stamp, dict):
        return "legacy_uninstrumented"
    known = [field for field in RELEASE_STAMP_FIELDS if stamp.get(field) is not None]
    if not known:
        return "pre_registry"
    if len(known) == len(RELEASE_STAMP_FIELDS):
        return "complete"
    return "partial"


def stamp_conflicts(
    first: dict[str, Any] | None, second: dict[str, Any] | None
) -> tuple[str, ...]:
    """Fields where both stamps carry a non-null value and disagree.

    Conflicts are reported, never silently normalized — a later slice decides
    policy (canonical vs shadow) while the evidence stays visible.
    """

    first = first or {}
    second = second or {}
    conflicts: list[str] = []
    for field in sorted(set(first) | set(second)):
        a, b = first.get(field), second.get(field)
        if a is not None and b is not None and a != b:
            conflicts.append(field)
    return tuple(conflicts)


def observed_release_stamps(chunks: Any) -> list[dict[str, Any]]:
    """Release stamps of the artifacts a query actually read.

    Collects ``metadata["release_stamp"]`` from chunk-like objects in read
    order, preserving values verbatim. Duplicate or conflicting stamps are
    NOT merged — the trace reports what was read, nothing more. Chunks with
    no stamp contribute nothing (unknown stays unknown).
    """

    observed: list[dict[str, Any]] = []
    for chunk in chunks or []:
        metadata = getattr(chunk, "metadata", None)
        if not isinstance(metadata, dict):
            continue
        stamp = metadata.get("release_stamp")
        if isinstance(stamp, dict):
            observed.append(dict(stamp))
    return observed
