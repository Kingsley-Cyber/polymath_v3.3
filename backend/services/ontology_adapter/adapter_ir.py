"""AdapterIR — the one provider-neutral compiled semantic contract.

Fixed code, variable knowledge: the compiler consumes declarative packs
(config/ontology_adapter/) and emits this IR. Provider shims translate it
into whatever each model expects; downstream Polymath never learns which
packs or providers were involved. Unknown → OPEN, never nearest-vague.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

COMPILER_VERSION = "ontology-adapter-compiler-v1"

MAPPING_STATUSES = ("exact", "narrower", "broader", "ambiguous", "OPEN")


@dataclass(frozen=True)
class EntityLabelIR:
    label: str                    # native label shown to the encoder
    description: str              # neutral definition (encoder conditioning)
    canonical_core: str           # frozen core entity type this folds into
    facet: str                    # native facet retained on mentions
    source_pack: str


@dataclass(frozen=True)
class PredicateIR:
    surface: str                  # native predicate surface offered to the encoder
    canonical: str | None         # frozen canonical predicate, or None
    status: str                   # one of MAPPING_STATUSES; None canonical => OPEN
    source_pack: str


@dataclass(frozen=True)
class AdapterIR:
    adapter_id: str
    profile_domains: tuple[str, ...]
    profile_genres: tuple[str, ...]
    entity_labels: tuple[EntityLabelIR, ...]
    predicates: tuple[PredicateIR, ...]
    open_policy: dict = field(default_factory=lambda: {
        "preserve_unknown_entities": True,
        "preserve_unknown_predicates": True,
    })
    calibration: dict = field(default_factory=dict)
    compiler_version: str = COMPILER_VERSION
    pack_versions: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict:
        return {
            "adapter_id": self.adapter_id,
            "profile": {"domains": list(self.profile_domains),
                        "genres": list(self.profile_genres)},
            "entity_labels": [vars(e) for e in self.entity_labels],
            "predicates": [vars(p) for p in self.predicates],
            "open_policy": dict(self.open_policy),
            "calibration": dict(self.calibration),
            "release": {
                "compiler_version": self.compiler_version,
                "pack_versions": [list(pair) for pair in self.pack_versions],
                "schema_hash": self.schema_hash(),
            },
        }

    def schema_hash(self) -> str:
        payload = json.dumps(
            {
                "compiler": self.compiler_version,
                "domains": sorted(self.profile_domains),
                "genres": sorted(self.profile_genres),
                "entities": sorted(
                    (e.label, e.description, e.canonical_core, e.facet)
                    for e in self.entity_labels
                ),
                "predicates": sorted(
                    (p.surface, p.canonical or "", p.status) for p in self.predicates
                ),
                "packs": sorted(self.pack_versions),
            },
            sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
