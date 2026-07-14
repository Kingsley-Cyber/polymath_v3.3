"""Versioned owner-registry loader + resolver (P2.5b registries job).

Loads the owner-delivered registry snapshots from backend/registries/,
validates their internal shape AND cross-registry references, and exposes a
read-only resolver. Registry files are versioned immutable data: any change
must be a NEW version file — the frozen snapshot hashes in the golden tests
make silent edits tamper-evident.

Owner rules enforced here:
- affinity priors may reference only registered domains/superframes and can
  never force/forbid an assignment (this module only *serves* them);
- stage->superframe bindings: every stage has >=1 admissible entry and
  EXACTLY one dominant (set-valued, owner-approved 2026-07-14);
- unknown ids are hard errors, never silent defaults.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from models.hash_taxonomy import namespace_hash

REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registries"

FILES = {
    "domain": "domain_registry.v1.json",
    "superframe": "superframe_registry.v1.json",
    "affinity": "domain_superframe_affinity.v1.json",
    "motif": "motif_registry.v1.json",
    "vocab": "extraction_vocabularies.v1.json",
    "latent_policy": "latent_concept_policy.v1.json",
    "binding": "motif_stage_superframe_binding.v1.json",
    "embedding_instruction": "embedding_instruction_registry.v1.json",
}


class RegistryError(ValueError):
    """A registry file is malformed, inconsistent, or referenced id is unknown."""


def _read(name: str) -> dict[str, Any]:
    path = REGISTRY_DIR / FILES[name]
    if not path.exists():
        raise RegistryError(f"registry file missing: {path}")
    with path.open() as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def load_all() -> dict[str, dict[str, Any]]:
    """Load, validate, and cross-check every registry. Cached per process."""
    data = {name: _read(name) for name in FILES}

    domains = {d["domain_id"]: d for d in data["domain"]["domains"]}
    superframes = {m["mf_id"]: m for m in data["superframe"]["superframes"]}
    motifs = {m["motif_id"]: m for m in data["motif"]["motifs"]}

    if len(domains) != 16:
        raise RegistryError(f"expected 16 domains, found {len(domains)}")
    if len(superframes) != 16:
        raise RegistryError(f"expected 16 superframes, found {len(superframes)}")
    if len(motifs) != 12:
        raise RegistryError(f"expected 12 motifs, found {len(motifs)}")

    for row in data["affinity"]["affinities"]:
        if row["domain_id"] not in domains:
            raise RegistryError(f"affinity references unknown domain {row['domain_id']}")
        for mf in row["dominant_superframes"]:
            if mf not in superframes:
                raise RegistryError(
                    f"affinity[{row['domain_id']}] references unknown superframe {mf}"
                )

    bindings = {b["motif_id"]: b for b in data["binding"]["bindings"]}
    for motif_id, motif in motifs.items():
        if motif_id not in bindings:
            raise RegistryError(f"motif {motif_id} has no stage bindings")
        bound_stages = [s["stage"] for s in bindings[motif_id]["stages"]]
        if bound_stages != motif["stages"]:
            raise RegistryError(
                f"binding stages for {motif_id} do not match motif registry stages"
            )
        for stage in bindings[motif_id]["stages"]:
            adm = stage.get("admissible") or []
            if not adm:
                raise RegistryError(f"{motif_id}/{stage['stage']}: no admissible superframes")
            dominants = [a for a in adm if a.get("tier") == "dominant"]
            if len(dominants) != 1:
                raise RegistryError(
                    f"{motif_id}/{stage['stage']}: expected exactly 1 dominant, "
                    f"found {len(dominants)}"
                )
            for a in adm:
                if a["superframe"] not in superframes:
                    raise RegistryError(
                        f"{motif_id}/{stage['stage']}: unknown superframe {a['superframe']}"
                    )

    vocab = data["vocab"]
    for key in ("entity_types", "predicate_types", "modalities", "polarities"):
        if not vocab.get(key):
            raise RegistryError(f"extraction vocabulary missing {key}")

    budgets = data["latent_policy"]["budgets"]
    for level, spec in budgets.items():
        raw, kept = spec.get("raw_generated"), spec.get("retained_distinct")
        if isinstance(raw, list) and isinstance(kept, list):
            if not (raw[0] <= raw[1] and kept[0] <= kept[1] and kept[1] <= raw[1]):
                raise RegistryError(f"latent budget for {level} is incoherent: {spec}")

    embedding_instructions = data["embedding_instruction"]
    if embedding_instructions.get("registry") != "embedding_instruction_registry":
        raise RegistryError("embedding instruction registry has the wrong identity")
    if not embedding_instructions.get("version"):
        raise RegistryError("embedding instruction registry is missing its version")
    for profile_name in ("baseline_live_v0", "universal"):
        profile = embedding_instructions.get(profile_name)
        if not isinstance(profile, dict) or not str(profile.get("instruction") or "").strip():
            raise RegistryError(
                f"embedding instruction profile {profile_name!r} is missing canonical text"
            )

    return data


def registry_hashes() -> dict[str, str]:
    """Tamper-evident snapshot hash per registry (namespace: 'registry')."""
    return {name: namespace_hash("registry", _read(name)) for name in FILES}


# ── resolver API ─────────────────────────────────────────────────────────────

def domain(domain_id: str) -> dict[str, Any]:
    d = {x["domain_id"]: x for x in load_all()["domain"]["domains"]}
    if domain_id not in d:
        raise RegistryError(f"unknown domain {domain_id}")
    return d[domain_id]


def superframe(mf_id: str) -> dict[str, Any]:
    s = {x["mf_id"]: x for x in load_all()["superframe"]["superframes"]}
    if mf_id not in s:
        raise RegistryError(f"unknown superframe {mf_id}")
    return s[mf_id]


def motif(motif_id: str) -> dict[str, Any]:
    m = {x["motif_id"]: x for x in load_all()["motif"]["motifs"]}
    if motif_id not in m:
        raise RegistryError(f"unknown motif {motif_id}")
    return m[motif_id]


def admissible_superframes(motif_id: str, stage: str) -> list[dict[str, str]]:
    """[{superframe, tier}] for one motif stage; dominant listed first."""
    bindings = {b["motif_id"]: b for b in load_all()["binding"]["bindings"]}
    if motif_id not in bindings:
        raise RegistryError(f"unknown motif {motif_id}")
    for s in bindings[motif_id]["stages"]:
        if s["stage"] == stage:
            return sorted(s["admissible"], key=lambda a: a["tier"] != "dominant")
    raise RegistryError(f"unknown stage {stage!r} for motif {motif_id}")


def domain_affinity_priors(domain_id: str) -> list[str]:
    """Commonly-dominant superframes for a domain. PRIORS ONLY — callers must
    never use these to force or forbid an assignment (owner rule)."""
    rows = {r["domain_id"]: r for r in load_all()["affinity"]["affinities"]}
    if domain_id not in rows:
        raise RegistryError(f"unknown domain {domain_id}")
    return list(rows[domain_id]["dominant_superframes"])


def is_controlled_predicate(predicate: str) -> bool:
    return predicate in load_all()["vocab"]["predicate_types"]


def is_entity_type(entity_type: str) -> bool:
    return entity_type in load_all()["vocab"]["entity_types"]


def latent_budget(level: str) -> dict[str, Any]:
    budgets = load_all()["latent_policy"]["budgets"]
    if level not in budgets:
        raise RegistryError(f"unknown latent budget level {level!r}")
    return budgets[level]


def embedding_instruction_profile(profile_name: str) -> dict[str, str]:
    """Resolve one immutable Qwen3 query-instruction profile.

    ``baseline_live_v0`` retains its historical profile version so existing
    cache keys remain stable. New registry profiles derive a version from the
    immutable registry id/version/profile tuple; the canonical wording stays
    in registry data rather than Python conditionals.
    """

    registry = load_all()["embedding_instruction"]
    if profile_name not in {"baseline_live_v0", "universal"}:
        raise RegistryError(
            f"unknown embedding instruction profile {profile_name!r}; "
            "valid: ['baseline_live_v0', 'universal']"
        )
    profile = registry[profile_name]
    instruction_version = str(profile.get("profile_version") or "").strip()
    if not instruction_version:
        instruction_version = (
            f"{registry['registry']}.{registry['version']}.{profile_name}"
        )
    return {
        "profile_name": profile_name,
        "instruction": str(profile["instruction"]).strip(),
        "instruction_version": instruction_version,
    }
