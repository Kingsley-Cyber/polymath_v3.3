"""Ontology profile resolution: CORE + domain modules, composed not replaced.

Owner architecture 2026-08-12 (second-brain design). The governing rule:

    corpus != ontology

A second-brain corpus may hold fifty domains, so the ontology that governs
extraction must be chosen by what the CONTENT is about, not by which corpus
a document happens to live in. A profile is therefore:

    active vocabulary = core  +  selected domain modules

Composition, never replacement. The measured failure this fixes: binding
film_production to a corpus REPLACED the universal vocabulary, and the
corpus lost Location, Event and TimeReference outright — zero of each
across 6,000 rows, in a film-history library full of "shot in Budapest"
and "premiered in 1975".

Two design constraints held deliberately:

  * The MASTER ontology may be enormous; the ACTIVE vocabulary must stay
    small (~15-40 labels). A zero-shot span encoder degrades when handed
    hundreds of labels, and degrades badly when the labels are abstract.
  * Core is present on EVERY chunk. That is what makes cross-domain
    traversal possible later: two facts from unrelated domains still share
    Technology/Person/Location as semantic bridges.

Resolution is a pure function of file content and the requested module
list, so the same profile request always yields the same vocabulary and the
same signatures — byte-identical, order-independent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Sequence

CORE_MODULE = "core"
# A zero-shot encoder loses discrimination as the label set grows. Profiles
# that exceed this are truncated deterministically (core first, then modules
# in requested order) and the drop is reported rather than silent.
MAX_ACTIVE_LABELS = 40


@dataclass(frozen=True)
class OntologyProfile:
    profile_id: str
    modules: tuple[str, ...]
    entity_labels: tuple[str, ...]
    relation_labels: tuple[str, ...]
    signatures: dict[str, frozenset[tuple[str, str]]] = field(default_factory=dict)
    dropped_labels: tuple[str, ...] = ()

    def as_metrics(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "modules": list(self.modules),
            "entity_labels": len(self.entity_labels),
            "relation_labels": len(self.relation_labels),
            "signed_predicates": len(self.signatures),
            "dropped_labels": list(self.dropped_labels),
        }


def _module_path(module: str):
    from services.extraction.canonical import find_config_dir

    safe = "".join(ch for ch in str(module or "") if ch.isalnum() or ch in "_-")
    if not safe:
        return None
    return find_config_dir(__file__) / "ontology" / f"{safe}.yaml"


def load_module(module: str) -> dict[str, Any]:
    """Read one ontology file. Returns {} for unknown modules — an absent
    module is a gap to report, not an exception to crash on."""
    path = _module_path(module)
    if path is None or not path.exists():
        return {}
    try:
        import yaml

        return yaml.safe_load(path.read_text()) or {}
    except Exception:  # noqa: BLE001
        return {}


def _entity_labels(data: dict[str, Any]) -> list[str]:
    """Explicit entity_types, else the types named by the signatures."""
    declared = data.get("entity_types")
    if declared:
        return [str(x).strip() for x in declared if str(x).strip()]
    seen: list[str] = []
    for spec in (data.get("predicates") or {}).values():
        for pair in (spec or {}).get("allowed_pairs") or []:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                for side in pair:
                    label = str(side).strip()
                    if label and label not in seen:
                        seen.append(label)
    return seen


def _relation_labels(data: dict[str, Any]) -> list[str]:
    declared = data.get("relation_types")
    if declared:
        return [str(x).strip() for x in declared if str(x).strip()]
    return [str(k).strip() for k in (data.get("predicates") or {})]


def _signatures(data: dict[str, Any]) -> dict[str, frozenset[tuple[str, str]]]:
    out: dict[str, frozenset[tuple[str, str]]] = {}
    for name, spec in sorted((data.get("predicates") or {}).items()):
        pairs = (spec or {}).get("allowed_pairs") or []
        legal = {
            (str(p[0]).strip().lower(), str(p[1]).strip().lower())
            for p in pairs
            if isinstance(p, (list, tuple)) and len(p) == 2
        }
        if legal:
            out[str(name).strip().lower()] = frozenset(legal)
    return out


def resolve_profile(
    modules: Sequence[str] | None = None,
    *,
    max_labels: int | None = None,
) -> OntologyProfile:
    """Compose core + requested modules into one active vocabulary.

    Core always leads, so if truncation is needed the universal backbone
    survives and the most specialised labels are what get dropped.
    Signatures MERGE per predicate (union of legal pairs) rather than
    overwrite, so a domain widening `uses` never narrows the core meaning.
    """
    requested = [str(m).strip().lower() for m in (modules or []) if str(m).strip()]
    ordered = [CORE_MODULE] + [m for m in requested if m != CORE_MODULE]

    entity: list[str] = []
    relation: list[str] = []
    signatures: dict[str, set[tuple[str, str]]] = {}
    loaded: list[str] = []

    for module in ordered:
        data = load_module(module)
        if not data:
            continue
        loaded.append(module)
        for label in _entity_labels(data):
            if label not in entity:
                entity.append(label)
        for label in _relation_labels(data):
            if label not in relation:
                relation.append(label)
        for predicate, legal in _signatures(data).items():
            signatures.setdefault(predicate, set()).update(legal)

    cap = max_labels or _max_labels()
    dropped: list[str] = []
    if len(entity) > cap:
        dropped = entity[cap:]
        entity = entity[:cap]

    return OntologyProfile(
        profile_id="+".join(loaded) or CORE_MODULE,
        modules=tuple(loaded),
        entity_labels=tuple(entity),
        relation_labels=tuple(relation),
        signatures={k: frozenset(v) for k, v in sorted(signatures.items())},
        dropped_labels=tuple(dropped),
    )


def _max_labels() -> int:
    try:
        return max(10, int(os.environ.get("ONTOLOGY_MAX_ACTIVE_LABELS", "") or MAX_ACTIVE_LABELS))
    except ValueError:
        return MAX_ACTIVE_LABELS


def profile_for_document(
    *,
    corpus_domain: str | None = None,
    document_domain: str | None = None,
    chunk_domain: str | None = None,
) -> OntologyProfile:
    """Pick the profile by CONTENT, with the documented precedence.

    chunk > document > corpus. A chunk that has clearly changed topic
    overrides its document; a document overrides the corpus default. When
    nothing is known, core alone is used — the fallback vocabulary — rather
    than guessing a specialisation.
    """
    for candidate in (chunk_domain, document_domain, corpus_domain):
        name = str(candidate or "").strip().lower()
        if name and name != CORE_MODULE:
            return resolve_profile([name])
    return resolve_profile([])
