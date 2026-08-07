"""Release-state contracts: categorical release pins, graph-write predicate,
and route-aware readiness decisions (Slice 1 scaffolding, 2026-08-02).

Design rules (owner decision 2026-08-02):
- Release state is CATEGORICAL. Percentage estimates are never promotion
  criteria (see CONTINUITY/EXTRACTION_FREEZE_MANIFEST_20260802.md).
- ``graph_write_allowed`` is deny-by-default: canonical graph promotion may
  proceed only when EVERY mandatory state in the active release bundle is
  proven. Failure leaves the durable promotion job intact as
  ``blocked_no_release`` so it retries when the release advances.
- ``ReadinessDecision`` is route- and artifact-aware. ``query_ready`` is not
  one universal boolean: a corpus may legitimately serve entity lookup before
  it can serve canonical graph traversal or fully grounded synthesis.

This module is dependency-light (pydantic + stdlib only) so contract tests
run without the full backend stack.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RELEASE_STATE_SCHEMA_VERSION = "polymath.release_state.v1"

# The seven categorical release states. Values are "passed" / "pending" —
# never percentages. Mirrors the Final Status block of the freeze manifest.
ReleaseStatus = Literal["passed", "pending"]

_CATEGORICAL_FIELDS: tuple[str, ...] = (
    "engineering_freeze",
    "recoverability",
    "git_reproducibility",
    "closed_world_annotation",
    "calibration",
    "held_out_qualification",
    "graph_write_promotion",
)


class ReleasePin(BaseModel):
    """Active release bundle: categorical states + frozen-artifact hash proof.

    The hash booleans assert that the deployed artifacts byte-match the
    frozen snapshots recorded in the freeze manifest. A pin with any
    ``pending`` state or any ``False`` hash never permits graph writes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default=RELEASE_STATE_SCHEMA_VERSION)
    release_id: str = Field(min_length=1)

    engineering_freeze: ReleaseStatus = "pending"
    recoverability: ReleaseStatus = "pending"
    git_reproducibility: ReleaseStatus = "pending"
    closed_world_annotation: ReleaseStatus = "pending"
    calibration: ReleaseStatus = "pending"
    held_out_qualification: ReleaseStatus = "pending"
    graph_write_promotion: ReleaseStatus = "pending"

    # Frozen-artifact hash proof (see EXTRACTION_FREEZE_MANIFEST_20260802.md).
    extractor_hash_matches: bool = False
    ontology_hash_matches: bool = False
    acceptance_policy_hash_matches: bool = False
    schema_hash_matches: bool = False

    # Version stamps relayed on every graph result (shadow graph-read rule).
    extractor_release: str = ""
    ontology_release: str = ""
    acceptance_policy_release: str = ""


def graph_write_allowed(release: ReleasePin | None) -> bool:
    """Deny-by-default predicate for canonical graph promotion.

    Returns True only when the release bundle proves every mandatory state.
    ``None`` (no active release) is denied like any incomplete bundle.
    """

    if release is None or not isinstance(release, ReleasePin):
        return False
    return all(
        (
            release.engineering_freeze == "passed",
            release.recoverability == "passed",
            release.git_reproducibility == "passed",
            release.closed_world_annotation == "passed",
            release.calibration == "passed",
            release.held_out_qualification == "passed",
            release.graph_write_promotion == "passed",
            release.extractor_hash_matches,
            release.ontology_hash_matches,
            release.acceptance_policy_hash_matches,
            release.schema_hash_matches,
        )
    )


def missing_release_conditions(release: ReleasePin | None) -> tuple[str, ...]:
    """Name every unproven condition — the ``blocked_no_release`` payload.

    Deny-by-default: anything that is not a ``ReleasePin`` instance (None,
    a malformed registry entry, a descriptive stamp, any foreign object)
    is treated as a complete absence of release authority.
    """

    if release is None or not isinstance(release, ReleasePin):
        return (*_CATEGORICAL_FIELDS, "release_bundle_absent")
    missing: list[str] = [
        name for name in _CATEGORICAL_FIELDS if getattr(release, name) != "passed"
    ]
    for name in (
        "extractor_hash_matches",
        "ontology_hash_matches",
        "acceptance_policy_hash_matches",
        "schema_hash_matches",
    ):
        if not getattr(release, name):
            missing.append(name)
    return tuple(missing)


def blocked_no_release_state(release: ReleasePin | None) -> dict[str, Any]:
    """Durable job state for a graph promotion denied by release policy.

    Owner-frozen target shape. The job stays intact, retryable, and eligible
    for deterministic reconsideration after release promotion — WITHOUT
    creating repeated failed write attempts: ``write_attempted`` stays False
    because denial happens before any write is tried, and
    ``release_registry_entry`` stays null until the active release registry
    exists (artifact stamps never grant write authority).
    """

    return {
        "state": "blocked_no_release",
        "retryable": True,
        "write_attempted": False,
        "missing_conditions": list(missing_release_conditions(release)),
        "release_registry_entry": None,
    }


# ---------------------------------------------------------------------------
# Route-aware readiness
# ---------------------------------------------------------------------------

QueryRoute = Literal[
    "ingest_inspection",
    "extraction_inspection",
    "entity_lookup",
    "vector_search",
    "hybrid_search",
    "curated_chat",
    "graph_read",
    "graph_write",
    "ontology_admin_write",
]

# Minimum artifact set per route (owner-mandated route gate table).
# Artifact names follow the control-plane census vocabulary.
ROUTE_ARTIFACT_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "ingest_inspection": (),  # always allowed
    "extraction_inspection": ("extraction_artifact",),
    "entity_lookup": ("entity_lexicon",),
    "vector_search": ("child_vectors", "parent_records"),
    "hybrid_search": ("child_vectors", "parent_records", "summary_records"),
    "curated_chat": (
        "child_vectors",
        "parent_records",
        "summary_records",
        "evidence_obligations",
    ),
    "graph_read": ("graph_projection", "release_pin_match"),
    "graph_write": ("extraction_qualification", "graph_write_promotion"),
    "ontology_admin_write": (
        "proposal_approval",
        "idempotency_key",
        "authorization",
        "active_release",
    ),
}

ReadinessMode = Literal["full", "partial", "blocked"]


class ReadinessDecision(BaseModel):
    """Route- and artifact-aware readiness verdict for one corpus + route.

    ``allowed`` answers "may this operation run at all"; ``mode`` distinguishes
    a complete artifact set ("full") from legitimate partial access
    ("partial"). Blanket hard gates and purely advisory relays are both
    rejected by design.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    corpus_id: str = Field(min_length=1)
    route: str = Field(min_length=1)
    allowed: bool
    mode: ReadinessMode
    required_artifacts: tuple[str, ...] = ()
    missing_artifacts: tuple[str, ...] = ()
    certificate_id: str | None = None
    release_pins: dict[str, str] = Field(default_factory=dict)
    reasons: tuple[str, ...] = ()

    @classmethod
    def decide(
        cls,
        *,
        corpus_id: str,
        route: str,
        present_artifacts: set[str] | frozenset[str],
        certificate_id: str | None = None,
        release_pins: dict[str, str] | None = None,
        release: ReleasePin | None = None,
    ) -> "ReadinessDecision":
        """Deterministic decision from the route gate table.

        Graph writes additionally require ``graph_write_allowed(release)``;
        the artifact census alone is never sufficient for a write route.
        """

        required = ROUTE_ARTIFACT_REQUIREMENTS.get(route)
        if required is None:
            return cls(
                corpus_id=corpus_id,
                route=route,
                allowed=False,
                mode="blocked",
                reasons=("unknown_route",),
            )
        present = set(present_artifacts or ())
        missing = tuple(a for a in required if a not in present)

        if route == "graph_write" and not graph_write_allowed(release):
            missing = (*missing, *missing_release_conditions(release))

        allowed = not missing
        mode: ReadinessMode = (
            "full" if allowed else ("partial" if present else "blocked")
        )
        # Partial means SOME required artifacts exist but not all; a corpus
        # with zero of the required artifacts is blocked for that route while
        # remaining eligible for lower routes it does satisfy.
        if not allowed and not present.intersection(required):
            mode = "blocked"
        reasons = (
            ()
            if allowed
            else tuple(f"missing:{artifact}" for artifact in missing)
        )
        return cls(
            corpus_id=corpus_id,
            route=route,
            allowed=allowed,
            mode=mode,
            required_artifacts=required,
            missing_artifacts=missing,
            certificate_id=certificate_id,
            release_pins=dict(release_pins or {}),
            reasons=reasons,
        )
