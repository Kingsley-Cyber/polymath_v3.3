"""Universal Adapter Compiler: DocumentProfile + declarative packs → AdapterIR.

One fixed compiler; knowledge lives in config/ontology_adapter/. Pack
selection is evidence-based (cue hits against the profile), never
corpus-named. The compiler NEVER reads gold labels or answer keys.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from services.extraction.config_locator import find_config_dir
from services.ontology_adapter.adapter_ir import (
    COMPILER_VERSION,
    AdapterIR,
    EntityLabelIR,
    PredicateIR,
)
from services.ontology_adapter.profiler import DocumentProfile

_PACKS_DIRNAME = "ontology_adapter"
# Evidence thresholds: generic and small — a pack activates on repeated,
# independent vocabulary/structure evidence, not a single stray word.
_DOMAIN_MIN_HITS = 8
_GENRE_MIN_HITS = 6


def _packs_root() -> Path:
    return find_config_dir(Path(__file__)) / _PACKS_DIRNAME


def _load_packs(kind: str) -> dict[str, dict]:
    root = _packs_root() / kind
    packs = {}
    if root.is_dir():
        for path in sorted(root.glob("*.yaml")):
            packs[path.stem] = yaml.safe_load(path.read_text()) or {}
    return packs


def _pack_score(pack: dict, profile: DocumentProfile) -> int:
    score = profile.cue_hits(list(pack.get("cues") or []))
    for structure_key, minimum in (pack.get("structure_cues") or {}).items():
        if profile.structure.get(structure_key, 0) >= int(minimum):
            score += _GENRE_MIN_HITS  # structural evidence is strong evidence
    return score


def compile_adapter(profile: DocumentProfile, adapter_id: str) -> AdapterIR:
    domains = _load_packs("domains")
    genres = _load_packs("genres")
    selected_domains = tuple(sorted(
        name for name, pack in domains.items()
        if _pack_score(pack, profile) >= _DOMAIN_MIN_HITS
    ))
    selected_genres = tuple(sorted(
        name for name, pack in genres.items()
        if _pack_score(pack, profile) >= _GENRE_MIN_HITS
    ))

    entity_labels: dict[str, EntityLabelIR] = {}
    predicates: dict[str, PredicateIR] = {}
    pack_versions: list[tuple[str, str]] = []
    for kind, names, packs in (
        ("domains", selected_domains, domains), ("genres", selected_genres, genres),
    ):
        for name in names:
            pack = packs[name]
            pack_versions.append((f"{kind}/{name}", str(pack.get("version") or "0")))
            for label, row in (pack.get("entity_labels") or {}).items():
                entity_labels.setdefault(label, EntityLabelIR(
                    label=label,
                    description=str(row.get("description") or label),
                    canonical_core=str(row.get("core") or "concept"),
                    facet=str(row.get("facet") or label.lower()),
                    source_pack=f"{kind}/{name}",
                ))
            for surface, row in (pack.get("native_predicates") or {}).items():
                canonical = row.get("canonical")
                status = str(row.get("status") or ("OPEN" if canonical is None else "exact"))
                predicates.setdefault(surface.casefold(), PredicateIR(
                    surface=surface.casefold(),
                    canonical=str(canonical) if canonical else None,
                    status=status,
                    source_pack=f"{kind}/{name}",
                ))

    return AdapterIR(
        adapter_id=adapter_id,
        profile_domains=selected_domains,
        profile_genres=selected_genres,
        entity_labels=tuple(entity_labels[k] for k in sorted(entity_labels)),
        predicates=tuple(predicates[k] for k in sorted(predicates)),
        compiler_version=COMPILER_VERSION,
        pack_versions=tuple(sorted(pack_versions)),
    )
