"""Deterministic query-relative shelf-role engine (P1.5 v0 core).

Pure functions, no I/O. Given (a) resolved query concept/capability ids
(normalized snake_case strings), (b) candidate documents' ``librarian_card.v0``
dicts (see :mod:`services.librarian.card_builder`), and (c) the versioned
policy data in :mod:`services.librarian.shelf_policy_data`, assign
QUERY-RELATIVE shelf roles. Roles are computed per query and NEVER persisted
on documents — cards store universal facts only.

v0 eligibility (indexed field overlap ONLY; embedding scores are not inputs
and any score-like keys present on cards are ignored):

``direct``
    ``central_subjects`` overlap with the query concepts.
    ``candidate_latent_subjects`` (LLM-generated at ingest) may CORROBORATE a
    qualifying doc but can never solely qualify it.
``foundational``
    ``capabilities_developed`` overlap backed by ``mechanisms_taught``
    evidence (at least one mechanism entry with source ids).
``adjacent``
    shared ``capabilities_developed``/``mechanisms_taught`` ids with
    meaningfully DIFFERENT central subjects (subject overlap strictly below
    ``ADJACENT_MAX_SUBJECT_OVERLAP``).
``bridge``
    shared ``transferable_principles`` or ``mechanisms_taught`` ids, different
    subjects, AND evidence present on BOTH sides: the qualifying card entries
    must carry source ids, and the query side records the shared id list
    itself (``query_evidence_ids``). Every bridge exposes the chain
    ``document -> concept -> transferable principle -> user goal``.
``counterbalance``
    triggered ONLY by versioned policy data: when the query concepts or the
    direct-shelf subjects intersect ``HIGH_MISUSE_KEYS``, candidates whose
    ``central_subjects`` intersect ``COUNTERBALANCE_KEYS`` qualify. The role
    is skipped (with a recorded reason) when not triggered or when no
    candidate qualifies.

A document may hold MULTIPLE roles: output is deduplicated by document with
every validated role retained. Deterministic ordering everywhere (score desc,
doc_id asc). Imports are restricted to stdlib + ``normalize_identity`` +
``shelf_policy_data`` — no Mongo, no Qdrant, no retriever.
"""

from __future__ import annotations

from typing import Any

from services.ingestion.corpus_lexicon import normalize_identity
from services.librarian.shelf_policy_data import (
    COUNTERBALANCE_KEYS,
    HIGH_MISUSE_KEYS,
    POLICY_VERSION,
)

ROLE_DIRECT = "direct"
ROLE_FOUNDATIONAL = "foundational"
ROLE_ADJACENT = "adjacent"
ROLE_BRIDGE = "bridge"
ROLE_COUNTERBALANCE = "counterbalance"
ALL_ROLES = (
    ROLE_DIRECT,
    ROLE_FOUNDATIONAL,
    ROLE_ADJACENT,
    ROLE_BRIDGE,
    ROLE_COUNTERBALANCE,
)

# ── Named numeric bounds (v0) ────────────────────────────────────────────
# A direct doc must centrally cover at least a quarter of the query's resolved
# concept ids — one id on a <=4-concept query, proportionally more on broader
# stories.
DIRECT_MIN_OVERLAP = 0.25
# "Meaningfully different central subjects" = subject coverage strictly below
# this bound; set equal to DIRECT_MIN_OVERLAP so the subject axis cleanly
# partitions direct-eligible docs from different-subject roles.
ADJACENT_MAX_SUBJECT_OVERLAP = 0.25
# Foundational capability coverage sits on the same footing as direct subject
# coverage — one shared capability id on a <=4-concept query.
FOUNDATIONAL_MIN_CAPABILITY_OVERLAP = 0.25
# Bridges use the same different-subject bound as adjacent (one named notion
# of "different subjects" across roles).
BRIDGE_MAX_SUBJECT_OVERLAP = ADJACENT_MAX_SUBJECT_OVERLAP
# Every role requires at least one concrete matched id — ratios alone can
# never seat an empty match set.
MIN_MATCHED_IDS = 1
# Bound per-role evidence id lists (house style: every projection is bounded).
_MAX_EVIDENCE_IDS = 24

_CARD_FIELD_SUBJECTS = "central_subjects"
_CARD_FIELD_LATENT = "candidate_latent_subjects"
_CARD_FIELD_CAPABILITIES = "capabilities_developed"
_CARD_FIELD_MECHANISMS = "mechanisms_taught"
_CARD_FIELD_PRINCIPLES = "transferable_principles"


def _snake(key: str) -> str:
    """Present a normalized identity key as a snake_case id."""

    return key.replace(" ", "_")


def _normalize_keys(values: Any) -> list[str]:
    """Normalize an iterable of ids/values into sorted unique identity keys."""

    keys = {normalize_identity(value) for value in (values or [])}
    return sorted(k for k in keys if k)


def _entry_index(card: dict, field: str) -> dict[str, list[str]]:
    """Map a card field to {normalized value key -> sorted source ids}.

    Only ``value``/``value_key`` and ``source_ids`` are read — score-like keys
    (embedding/dense/rerank scores) on entries or cards are deliberately
    ignored; field overlap is the only input signal.
    """

    index: dict[str, set[str]] = {}
    for entry in card.get(field) or []:
        if not isinstance(entry, dict):
            continue
        key = normalize_identity(entry.get("value_key") or entry.get("value"))
        if not key:
            continue
        ids = {str(s) for s in (entry.get("source_ids") or []) if str(s or "").strip()}
        index.setdefault(key, set()).update(ids)
    return {key: sorted(ids) for key, ids in index.items()}


def _matched_ids(matched_keys: list[str]) -> list[str]:
    return [_snake(key) for key in matched_keys]


def _evidence_for(index: dict[str, list[str]], matched_keys: list[str]) -> list[str]:
    ids: set[str] = set()
    for key in matched_keys:
        ids.update(index.get(key, []))
    return sorted(ids)[:_MAX_EVIDENCE_IDS]


# ── Small pure helpers ───────────────────────────────────────────────────


def subject_overlap(
    query_keys: list[str], subject_keys: set[str] | dict[str, Any]
) -> tuple[list[str], float]:
    """Query-relative subject overlap: (sorted matched keys, |matched|/|query|)."""

    if not query_keys:
        return [], 0.0
    matched = sorted(k for k in query_keys if k in subject_keys)
    return matched, len(matched) / len(query_keys)


def capability_overlap(
    query_keys: list[str], capability_keys: set[str] | dict[str, Any]
) -> tuple[list[str], float]:
    """Query-relative capability overlap: (sorted matched keys, ratio)."""

    return subject_overlap(query_keys, capability_keys)


def principle_bridge(
    query_keys: list[str],
    principle_index: dict[str, list[str]],
    mechanism_index: dict[str, list[str]],
) -> list[dict[str, Any]]:
    """Evidence-backed bridge matches: shared principle/mechanism ids whose
    card-side entries carry source ids. Returns sorted match dicts."""

    matches: list[dict[str, Any]] = []
    for field, index in (
        (_CARD_FIELD_PRINCIPLES, principle_index),
        (_CARD_FIELD_MECHANISMS, mechanism_index),
    ):
        for key in query_keys:
            ids = index.get(key)
            if ids:  # card-side evidence required: entries without ids never bridge
                matches.append({"field": field, "key": key, "source_ids": ids})
    matches.sort(key=lambda m: (m["field"], m["key"]))
    return matches


# ── Role builders (per card) ─────────────────────────────────────────────


def _role_direct(
    query_keys: list[str],
    subject_index: dict[str, list[str]],
    latent_index: dict[str, list[str]],
) -> dict | None:
    matched, ratio = subject_overlap(query_keys, subject_index)
    if len(matched) < MIN_MATCHED_IDS or ratio < DIRECT_MIN_OVERLAP:
        # candidate_latent_subjects can NEVER solely qualify a direct seat —
        # the field is LLM-generated at ingest (generated provenance).
        return None
    matched_fields = {_CARD_FIELD_SUBJECTS: _matched_ids(matched)}
    reasons = [
        f"central_subject_overlap {len(matched)}/{len(query_keys)} "
        f">= DIRECT_MIN_OVERLAP={DIRECT_MIN_OVERLAP}"
    ]
    latent_matched, _ = subject_overlap(query_keys, latent_index)
    if latent_matched:
        matched_fields[_CARD_FIELD_LATENT] = _matched_ids(latent_matched)
        reasons.append(
            "corroborated_by_candidate_latent_subjects "
            "(generated provenance; never solely qualifying)"
        )
    return {
        "role": ROLE_DIRECT,
        "matched_fields": matched_fields,
        "evidence_ids": _evidence_for(subject_index, matched),
        "score": round(ratio, 4),
        "reasons": reasons,
    }


def _role_foundational(
    query_keys: list[str],
    capability_index: dict[str, list[str]],
    mechanism_index: dict[str, list[str]],
) -> dict | None:
    matched, ratio = capability_overlap(query_keys, capability_index)
    if len(matched) < MIN_MATCHED_IDS or ratio < FOUNDATIONAL_MIN_CAPABILITY_OVERLAP:
        return None
    # "Backed by mechanisms evidence": at least one mechanism entry carrying
    # source ids must exist on the card.
    backing_mechanisms = sorted(k for k, ids in mechanism_index.items() if ids)
    if not backing_mechanisms:
        return None
    evidence = sorted(
        set(_evidence_for(capability_index, matched))
        | set(_evidence_for(mechanism_index, backing_mechanisms))
    )[:_MAX_EVIDENCE_IDS]
    return {
        "role": ROLE_FOUNDATIONAL,
        "matched_fields": {
            _CARD_FIELD_CAPABILITIES: _matched_ids(matched),
            _CARD_FIELD_MECHANISMS: _matched_ids(backing_mechanisms)[
                :_MAX_EVIDENCE_IDS
            ],
        },
        "evidence_ids": evidence,
        "score": round(ratio, 4),
        "reasons": [
            f"capability_overlap {len(matched)}/{len(query_keys)} "
            f">= FOUNDATIONAL_MIN_CAPABILITY_OVERLAP="
            f"{FOUNDATIONAL_MIN_CAPABILITY_OVERLAP}",
            f"backed_by_mechanisms_evidence ({len(backing_mechanisms)} "
            "mechanism ids with source ids)",
        ],
    }


def _role_adjacent(
    query_keys: list[str],
    subject_ratio: float,
    capability_index: dict[str, list[str]],
    mechanism_index: dict[str, list[str]],
) -> dict | None:
    if subject_ratio >= ADJACENT_MAX_SUBJECT_OVERLAP:
        return None
    matched_caps, _ = capability_overlap(query_keys, capability_index)
    matched_mechs, _ = subject_overlap(query_keys, mechanism_index)
    shared = sorted(set(matched_caps) | set(matched_mechs))
    if len(shared) < MIN_MATCHED_IDS:
        return None
    matched_fields: dict[str, list[str]] = {}
    if matched_caps:
        matched_fields[_CARD_FIELD_CAPABILITIES] = _matched_ids(matched_caps)
    if matched_mechs:
        matched_fields[_CARD_FIELD_MECHANISMS] = _matched_ids(matched_mechs)
    evidence = sorted(
        set(_evidence_for(capability_index, matched_caps))
        | set(_evidence_for(mechanism_index, matched_mechs))
    )[:_MAX_EVIDENCE_IDS]
    ratio = len(shared) / len(query_keys)
    return {
        "role": ROLE_ADJACENT,
        "matched_fields": matched_fields,
        "evidence_ids": evidence,
        "score": round(ratio, 4),
        "reasons": [
            f"shared_capability_or_mechanism_ids {len(shared)}/{len(query_keys)}",
            f"subject_overlap {subject_ratio:.4f} < "
            f"ADJACENT_MAX_SUBJECT_OVERLAP={ADJACENT_MAX_SUBJECT_OVERLAP}",
        ],
    }


def _role_bridge(
    query_keys: list[str],
    doc_id: str,
    subject_ratio: float,
    principle_index: dict[str, list[str]],
    mechanism_index: dict[str, list[str]],
) -> dict | None:
    if subject_ratio >= BRIDGE_MAX_SUBJECT_OVERLAP:
        return None
    matches = principle_bridge(query_keys, principle_index, mechanism_index)
    if len(matches) < MIN_MATCHED_IDS:
        return None
    shared_ids = sorted({_snake(m["key"]) for m in matches})
    matched_fields: dict[str, list[str]] = {}
    evidence: set[str] = set()
    chains: list[dict[str, str]] = []
    for match in matches:
        matched_fields.setdefault(match["field"], []).append(_snake(match["key"]))
        evidence.update(match["source_ids"])
        chains.append(
            {
                # document -> concept -> transferable principle -> user goal.
                # v0 joins on exact shared ids, so the card-side concept and
                # the shared principle/mechanism id coincide by construction.
                "document": doc_id,
                "concept": _snake(match["key"]),
                "transferable_principle": _snake(match["key"]),
                "user_goal": _snake(match["key"]),
                "via_field": match["field"],
            }
        )
    matched_fields = {
        field: sorted(set(ids)) for field, ids in sorted(matched_fields.items())
    }
    ratio = len(shared_ids) / len(query_keys)
    return {
        "role": ROLE_BRIDGE,
        "matched_fields": matched_fields,
        "evidence_ids": sorted(evidence)[:_MAX_EVIDENCE_IDS],
        # Query-side evidence record: the shared id list itself.
        "query_evidence_ids": shared_ids,
        "chains": chains,
        "score": round(ratio, 4),
        "reasons": [
            f"shared_transferable_principle_or_mechanism_ids {shared_ids}",
            f"subject_overlap {subject_ratio:.4f} < "
            f"BRIDGE_MAX_SUBJECT_OVERLAP={BRIDGE_MAX_SUBJECT_OVERLAP}",
            "evidence_present_on_card_side_and_query_side",
        ],
    }


def _role_counterbalance(
    subject_index: dict[str, list[str]],
    counterbalance_keys: set[str],
    trigger_ids: list[str],
) -> dict | None:
    matched = sorted(k for k in counterbalance_keys if k in subject_index)
    if len(matched) < MIN_MATCHED_IDS:
        return None
    # Deterministic coverage ratio over the policy vocabulary: cards covering
    # more counterbalance families rank higher.
    ratio = len(matched) / len(counterbalance_keys)
    return {
        "role": ROLE_COUNTERBALANCE,
        "matched_fields": {_CARD_FIELD_SUBJECTS: _matched_ids(matched)},
        "evidence_ids": _evidence_for(subject_index, matched),
        "score": round(ratio, 4),
        "reasons": [
            f"policy_triggered_by_high_misuse_keys {trigger_ids} "
            f"(policy={POLICY_VERSION})",
            f"subjects_intersect_counterbalance_keys {_matched_ids(matched)}",
        ],
    }


# ── Engine entry point ───────────────────────────────────────────────────


def assign_shelf_roles(
    query_concepts: list[str],
    cards: list[dict],
    *,
    policy_version: str = POLICY_VERSION,
) -> dict:
    """Assign query-relative shelf roles to candidate cards (pure, no I/O).

    Returns ``{"assignments", "shelf_counts", "skipped_roles",
    "policy_version"}``. Assignments are deduplicated by document (every
    validated role retained) and ordered by best role score desc, then
    ``doc_id`` asc; roles within a document by score desc, then role name asc.
    """

    if policy_version != POLICY_VERSION:
        raise ValueError(
            f"unknown shelf policy version {policy_version!r}; "
            f"this engine implements {POLICY_VERSION!r}"
        )

    query_keys = _normalize_keys(query_concepts)
    misuse_keys = set(_normalize_keys(HIGH_MISUSE_KEYS))
    counterbalance_keys = set(_normalize_keys(COUNTERBALANCE_KEYS))

    # Dedupe candidate cards by (corpus_id, doc_id); deterministic regardless
    # of input order (cards for the same key are identical by the Mongo
    # upsert contract — keep the first after a total sort).
    unique_cards: dict[tuple[str, str], dict] = {}
    for card in sorted(
        (c for c in cards if isinstance(c, dict) and str(c.get("doc_id") or "")),
        key=lambda c: (str(c.get("corpus_id") or ""), str(c.get("doc_id") or "")),
    ):
        key = (str(card.get("corpus_id") or ""), str(card.get("doc_id") or ""))
        unique_cards.setdefault(key, card)

    skipped_roles: dict[str, str] = {}
    if not query_keys:
        return {
            "assignments": [],
            "shelf_counts": {role: 0 for role in ALL_ROLES},
            "skipped_roles": {role: "empty_query_concepts" for role in ALL_ROLES},
            "policy_version": policy_version,
        }

    # Pass 1: field-overlap roles + direct-shelf subject pool.
    per_doc: dict[tuple[str, str], dict[str, Any]] = {}
    direct_shelf_subject_keys: set[str] = set()
    for key, card in unique_cards.items():
        subject_index = _entry_index(card, _CARD_FIELD_SUBJECTS)
        latent_index = _entry_index(card, _CARD_FIELD_LATENT)
        capability_index = _entry_index(card, _CARD_FIELD_CAPABILITIES)
        mechanism_index = _entry_index(card, _CARD_FIELD_MECHANISMS)
        principle_index = _entry_index(card, _CARD_FIELD_PRINCIPLES)
        _, subject_ratio = subject_overlap(query_keys, subject_index)

        roles = [
            role
            for role in (
                _role_direct(query_keys, subject_index, latent_index),
                _role_foundational(query_keys, capability_index, mechanism_index),
                _role_adjacent(
                    query_keys, subject_ratio, capability_index, mechanism_index
                ),
                _role_bridge(
                    query_keys, key[1], subject_ratio, principle_index, mechanism_index
                ),
            )
            if role is not None
        ]
        if any(role["role"] == ROLE_DIRECT for role in roles):
            direct_shelf_subject_keys.update(subject_index)
        per_doc[key] = {"roles": roles, "subject_index": subject_index}

    # Pass 2: counterbalance — triggered ONLY by versioned policy data.
    trigger_keys = sorted((set(query_keys) | direct_shelf_subject_keys) & misuse_keys)
    if not trigger_keys:
        skipped_roles[ROLE_COUNTERBALANCE] = (
            "policy_not_triggered: neither query concepts nor direct-shelf "
            f"subjects intersect HIGH_MISUSE_KEYS ({POLICY_VERSION})"
        )
    else:
        trigger_ids = _matched_ids(trigger_keys)
        any_counterbalance = False
        for state in per_doc.values():
            role = _role_counterbalance(
                state["subject_index"], counterbalance_keys, trigger_ids
            )
            if role is not None:
                state["roles"].append(role)
                any_counterbalance = True
        if not any_counterbalance:
            skipped_roles[ROLE_COUNTERBALANCE] = (
                f"policy_triggered_by {trigger_ids} but no candidate's "
                f"subjects intersect COUNTERBALANCE_KEYS ({POLICY_VERSION})"
            )

    # Assemble: dedupe by doc (already keyed), deterministic ordering.
    assignments = []
    shelf_counts = {role: 0 for role in ALL_ROLES}
    for (corpus_id, doc_id), state in per_doc.items():
        roles = sorted(state["roles"], key=lambda r: (-r["score"], r["role"]))
        if not roles:
            continue
        for role in roles:
            shelf_counts[role["role"]] += 1
        assignments.append(
            {"doc_id": doc_id, "corpus_id": corpus_id, "roles": roles}
        )
    # doc_id asc per spec; corpus_id is a final tiebreaker because the same
    # content-hash doc_id may exist in two corpora.
    assignments.sort(
        key=lambda a: (-a["roles"][0]["score"], a["doc_id"], a["corpus_id"])
    )

    for role in ALL_ROLES:
        if shelf_counts[role] == 0 and role not in skipped_roles:
            skipped_roles[role] = "no_candidate_met_v0_eligibility"

    return {
        "assignments": assignments,
        "shelf_counts": shelf_counts,
        "skipped_roles": skipped_roles,
        "policy_version": policy_version,
    }
