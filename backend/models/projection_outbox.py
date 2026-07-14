"""P2.5b projection outbox — durable intent/retry/reconciliation contract.

Rule (FINAL_SCHEMA §12 / checklist P2.5b): accepted artifacts are authoritative
in Mongo; every Qdrant/Neo4j write flows through a durable outbox entry so no
request path depends on untracked Mongo+Qdrant+Neo4j dual writes. Interruption
or retry must never create a duplicate semantic artifact — the outbox key is
deterministic over (artifact revision, manifest, op), so redelivery collapses.

This module is the CONTRACT (model + state machine + key recipe). The worker
loop that drains it is CP3 integration work and lives elsewhere.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from models.hash_taxonomy import namespace_hash

OUTBOX_VERSION = "projection_outbox.v1"

OutboxOp = Literal["upsert", "delete"]
OutboxState = Literal["pending", "in_flight", "applied", "failed", "dead"]

# Legal transitions; anything else is a programming error, not data.
_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"in_flight"},
    "in_flight": {"applied", "failed"},
    "failed": {"in_flight", "dead"},   # retry or give up
    "applied": set(),                   # terminal
    "dead": {"in_flight"},              # explicit operator revive only
}

MAX_ATTEMPTS_DEFAULT = 5


def outbox_key(artifact_revision_id: str, manifest_id: str, op: OutboxOp) -> str:
    """Deterministic identity: same revision+manifest+op == same entry."""
    digest = namespace_hash("work", {
        "kind": "projection_outbox",
        "artifact_revision_id": artifact_revision_id,
        "manifest_id": manifest_id,
        "op": op,
    }).split(":", 1)[1]
    return f"outbox:{digest}"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class OutboxEntry(StrictModel):
    schema_version: Literal["projection_outbox.v1"]
    outbox_id: str
    artifact_revision_id: str
    manifest_id: str
    op: OutboxOp
    state: OutboxState = "pending"
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=MAX_ATTEMPTS_DEFAULT, ge=1)
    last_error: Optional[str] = None

    def transition(self, new_state: OutboxState, *, error: Optional[str] = None) -> "OutboxEntry":
        """Return a new entry in ``new_state``; raise on illegal transitions.

        - entering in_flight increments attempt_count;
        - entering failed requires an error and auto-escalates to dead when
          attempt_count >= max_attempts (never silently retried past budget);
        - applied clears last_error.
        """
        if new_state not in _TRANSITIONS[self.state]:
            raise ValueError(f"illegal outbox transition {self.state} -> {new_state}")
        data = self.model_dump()
        if new_state == "in_flight":
            data["attempt_count"] = self.attempt_count + 1
        if new_state == "failed":
            if not error:
                raise ValueError("failed transition requires an error message")
            data["last_error"] = error
            if data["attempt_count"] >= self.max_attempts:
                new_state = "dead"
        if new_state == "applied":
            data["last_error"] = None
        data["state"] = new_state
        return OutboxEntry(**data)


def make_entry(artifact_revision_id: str, manifest_id: str, op: OutboxOp,
               *, max_attempts: int = MAX_ATTEMPTS_DEFAULT) -> OutboxEntry:
    return OutboxEntry(
        schema_version="projection_outbox.v1",
        outbox_id=outbox_key(artifact_revision_id, manifest_id, op),
        artifact_revision_id=artifact_revision_id,
        manifest_id=manifest_id,
        op=op,
        max_attempts=max_attempts,
    )
