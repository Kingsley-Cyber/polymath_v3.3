"""Fail-closed loader for the active release registry (release_pins.v1.json).

Control-plane slice 1A of the graph-write release-control program
(CONTINUITY/TEMPORAL_CONTRACT_V1.md, Shadow A closure block).

Contract (owner-frozen):

* The registry is read ONCE at graph-run start. ``RegistryRunHandle``
  carries the resolution plus the registry hash so the same run can
  verify the registry did not mutate underneath it.
* Exactly ONE entry may carry ``status: "active"``. Zero or more than
  one is ambiguity → no active release.
* The active entry must carry a complete categorical ``ReleasePin``:
  every mandatory state present, every state "passed", every artifact
  hash proof present and true. Anything less fails closed.
* ``entry_hash`` is sha256 over the canonical JSON serialization of the
  entry WITHOUT the ``entry_hash`` field; ``registry_hash`` is sha256 of
  the raw registry bytes. Any missing or mismatched hash fails closed.
* ``ReleaseStamp``, ``TemporalEnvelope``, Git state, manifests, or any
  inferred value are NEVER consumed as write authority. A pin payload
  with foreign fields is rejected outright (``ReleasePin`` forbids
  extras); nothing is guessed into shape.
* Every failure mode returns ``release_pin=None`` with an exact reason —
  deny-by-default then refuses every canonical graph write under
  enforcement. This loader never raises policy errors at callers.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from models.release_state import ReleasePin, graph_write_allowed

RELEASE_REGISTRY_SCHEMA_VERSION = "release_pins.v1"

#: Categorical fields that must be PRESENT in the registry payload —
#: omission is never defaulted into a passing state.
_REQUIRED_CATEGORICAL_FIELDS: tuple[str, ...] = (
    "engineering_freeze",
    "recoverability",
    "git_reproducibility",
    "closed_world_annotation",
    "calibration",
    "held_out_qualification",
    "graph_write_promotion",
)
_REQUIRED_HASH_PROOF_FIELDS: tuple[str, ...] = (
    "extractor_hash_matches",
    "ontology_hash_matches",
    "acceptance_policy_hash_matches",
    "schema_hash_matches",
)


class ReleaseRegistryResolution(BaseModel):
    """Outcome of one fail-closed registry resolution.

    ``status`` is "ok" only when exactly one valid active entry exists;
    every other outcome carries ``release_pin=None`` and the exact
    ``reason`` so shadow traces and blocked payloads stay auditable.
    """

    model_config = ConfigDict(frozen=True)

    status: str  # "ok" | "fail_closed"
    reason: str | None = None
    registry_version: str | None = None
    registry_hash: str | None = None
    entry_id: str | None = None
    entry_hash: str | None = None
    release_pin: ReleasePin | None = None
    loaded_at: str = ""

    def trace_identity(self) -> dict[str, Any]:
        """The run-trace identity block (owner-mandated output shape)."""

        return {
            "registry_version": self.registry_version,
            "registry_hash": self.registry_hash,
            "entry_id": self.entry_id,
            "entry_hash": self.entry_hash,
            "release_pin": self.release_pin.model_dump() if self.release_pin else None,
        }


class RegistryRunHandle:
    """Registry state pinned at run start.

    The decision for a run comes from exactly one resolution; the handle
    only re-checks that the registry bytes are unchanged so a mid-run
    mutation can never silently extend authority.
    """

    def __init__(self, resolution: ReleaseRegistryResolution, path: Path):
        self.resolution = resolution
        self.path = path

    def verify_unchanged(self) -> bool:
        """Fail closed if the registry bytes changed during this run.

        A fail-closed resolution has no hash to defend; it stays valid
        (nothing can be gained by mutating a registry that grants
        nothing). An "ok" resolution loses authority the moment the
        registry bytes no longer match its recorded hash.
        """

        if self.resolution.status != "ok":
            return True
        try:
            raw = self.path.read_bytes()
        except OSError:
            return False
        return (
            "sha256:" + hashlib.sha256(raw).hexdigest()
            == self.resolution.registry_hash
        )


def default_registry_path() -> Path:
    """backend/registries/release_pins.v1.json in this repository."""

    return Path(__file__).resolve().parents[2] / "registries" / "release_pins.v1.json"


def resolve_registry_path(explicit: str | Path | None = None) -> Path:
    """Explicit path > RELEASE_REGISTRY_PATH setting > repository default."""

    if explicit:
        return Path(str(explicit)).expanduser()
    try:
        from config import get_settings

        configured = str(get_settings().RELEASE_REGISTRY_PATH or "").strip()
        if configured:
            return Path(configured).expanduser()
    except Exception:  # noqa: BLE001 — settings unavailable in contract tests
        pass
    return Path(__file__).resolve().parents[2] / "registries" / "release_pins.v1.json"


def canonical_entry_bytes(entry: dict[str, Any]) -> bytes:
    """Deterministic serialization of an entry WITHOUT its entry_hash."""

    body = {k: v for k, v in entry.items() if k != "entry_hash"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_entry_hash(entry: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_entry_bytes(entry)).hexdigest()


def compute_registry_hash(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _fail_closed(reason: str) -> ReleaseRegistryResolution:
    return ReleaseRegistryResolution(
        status="fail_closed",
        reason=reason,
        loaded_at=datetime.now(timezone.utc).isoformat(),
    )


def load_release_registry(
    path: str | Path | None = None,
) -> ReleaseRegistryResolution:
    """Resolve the active release exactly once, fail-closed on any doubt."""

    resolved_path = resolve_registry_path(path)
    try:
        raw = resolved_path.read_bytes()
    except OSError:
        return _fail_closed("registry_missing")

    registry_hash = compute_registry_hash(raw)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _fail_closed("registry_malformed")
    if not isinstance(payload, dict):
        return _fail_closed("registry_malformed")

    if payload.get("schema_version") != RELEASE_REGISTRY_SCHEMA_VERSION:
        return _fail_closed("unsupported_version")

    entries = payload.get("entries")
    if not isinstance(entries, list):
        return _fail_closed("registry_malformed")

    active = [e for e in entries if isinstance(e, dict) and e.get("status") == "active"]
    if len(active) == 0:
        return _fail_closed("zero_active_entries")
    if len(active) > 1:
        return _fail_closed("multiple_active_entries")

    entry = active[0]
    entry_id = entry.get("entry_id")
    if not isinstance(entry_id, str) or not entry_id:
        return _fail_closed("malformed_active_entry")

    declared_hash = entry.get("entry_hash")
    if not isinstance(declared_hash, str) or not declared_hash:
        return _fail_closed("entry_hash_missing")
    if declared_hash != compute_entry_hash(entry):
        return _fail_closed("entry_hash_mismatch")

    pin_payload = entry.get("release_pin")
    if not isinstance(pin_payload, dict):
        return _fail_closed("malformed_active_entry")

    # Categorical completeness is checked on the RAW payload: omitted
    # fields must never be defaulted into existence before validation.
    missing_fields = [
        name
        for name in (*_REQUIRED_CATEGORICAL_FIELDS, *_REQUIRED_HASH_PROOF_FIELDS)
        if name not in pin_payload
    ]
    if missing_fields:
        return _fail_closed("categorical_state_missing")

    try:
        # extra="forbid": a ReleaseStamp / TemporalEnvelope / any foreign
        # payload is rejected outright — never inferred into a pin.
        pin = ReleasePin(**pin_payload)
    except ValidationError:
        return _fail_closed("malformed_active_entry")

    if not graph_write_allowed(pin):
        return _fail_closed("categorical_state_failed")

    return ReleaseRegistryResolution(
        status="ok",
        registry_version=RELEASE_REGISTRY_SCHEMA_VERSION,
        registry_hash=registry_hash,
        entry_id=entry_id,
        entry_hash=declared_hash,
        release_pin=pin,
        loaded_at=datetime.now(timezone.utc).isoformat(),
    )


def begin_registry_run(path: str | Path | None = None) -> RegistryRunHandle:
    """Resolve once at run start and pin the registry identity for the run."""

    resolved_path = resolve_registry_path(path)
    return RegistryRunHandle(load_release_registry(resolved_path), resolved_path)
