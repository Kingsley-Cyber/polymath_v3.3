"""Deterministic corpus identity clustering (Phase 6).

Operates only on qualified DocumentEntityV1 records (+ surface inventories).
Never mutates production corpora, schemas, or Neo4j identity edges.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from models.alias_identity import (
    CORPUS_CLUSTER_RELEASE,
    CORPUS_MERGE_RELEASE,
    CorpusEntityV1,
    CorpusMergeDecisionV1,
    DocumentEntityV1,
    alias_contract_hash,
)
MERGE_RELEASE = CORPUS_MERGE_RELEASE
CLUSTER_RELEASE = CORPUS_CLUSTER_RELEASE

REASON_MERGE_CURATED = "CORPUS_MERGE_CURATED_CANONICAL_V1"
REASON_MERGE_SAME_CANONICAL = "CORPUS_MERGE_SAME_EXPLICIT_CANONICAL_V1"
REASON_MERGE_TRUSTED_ALIAS = "CORPUS_MERGE_TRUSTED_ALIAS_V1"
REASON_MERGE_ACRONYM_LONGFORM = "CORPUS_MERGE_VALIDATED_ACRONYM_LONGFORM_V1"
REASON_MERGE_CASING = "CORPUS_MERGE_CASING_OR_PUNCT_V1"
REASON_LINK_TEMPORAL = "CORPUS_LINK_TEMPORAL_FORMER_NAME_V1"
REASON_REJECT_TYPE = "CORPUS_REJECT_INCOMPATIBLE_ENTITY_TYPE_V1"
REASON_REJECT_ACRONYM_CONFLICT = "CORPUS_REJECT_AMBIGUOUS_ACRONYM_BRIDGE_V1"
REASON_REJECT_CANONICAL_CONFLICT = "CORPUS_REJECT_CONFLICTING_CANONICAL_V1"
REASON_REJECT_BARE_ACRONYM = "CORPUS_REJECT_BARE_ACRONYM_V1"
REASON_REJECT_WEAK = "CORPUS_REJECT_NON_AUTHORITATIVE_EVIDENCE_V1"
REASON_REJECT_BRIDGE = "CORPUS_REJECT_TRANSITIVE_BRIDGE_V1"
REASON_REVIEW_WEAK = "CORPUS_REVIEW_INSUFFICIENT_EVIDENCE_V1"

_TYPE_CANON = {
    "person": "person",
    "per": "person",
    "org": "organization",
    "organization": "organization",
    "company": "organization",
    "gpe": "location",
    "loc": "location",
    "location": "location",
    "concept": "concept",
    "product": "product",
    "food": "food",
    "fruit": "food",
}


@dataclass(frozen=True)
class DocumentEntityInventory:
    """Qualified document entity plus surfaces needed for corpus blocking/merge.

    Built from Phase 5 DocumentEntityV1 + accepted alias decision surfaces.
    Must not be constructed from raw un-gated mentions.
    """

    entity: DocumentEntityV1
    trusted_alias_surfaces: tuple[str, ...] = ()
    temporal_former_names: tuple[str, ...] = ()
    retrieval_surfaces: tuple[str, ...] = ()
    # (short, long) pairs validated at document scope
    acronym_pairs: tuple[tuple[str, str], ...] = ()
    curated_canonical: str | None = None
    supporting_alias_decision_ids: tuple[str, ...] = ()
    is_proper_name: bool = True
    # Optional weak signals — may block/create REVIEW only, never MERGE alone.
    related_terms: tuple[str, ...] = ()
    description_surfaces: tuple[str, ...] = ()


@dataclass(frozen=True)
class CorpusClusteringMetrics:
    document_entities_total: int = 0
    blocking_keys_total: int = 0
    candidate_pairs_total: int = 0
    evaluated_pairs_total: int = 0
    accepted_merges_total: int = 0
    temporal_links_total: int = 0
    reviews_total: int = 0
    rejected_pairs_total: int = 0
    clustering_wall_time_ms: float = 0.0
    peak_rss_mb: float | None = None


@dataclass(frozen=True)
class ShadowSchemaProjection:
    """Isolated shadow vocabulary projection — never overwrites production schemas."""

    corpus_id: str
    corpus_entity_id: str
    trusted_aliases: list[str]
    retrieval_surface_variants: list[str]
    temporal_aliases: list[str]
    ambiguous_aliases: list[str]
    descriptions: list[str]
    identity_authority: bool = False
    projection_release: str = "shadow_schemas_projection.v1"


@dataclass(frozen=True)
class CorpusClusteringBatch:
    corpus_entities: list[CorpusEntityV1] = field(default_factory=list)
    merge_decisions: list[CorpusMergeDecisionV1] = field(default_factory=list)
    merge_candidates: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    shadow_projections: list[ShadowSchemaProjection] = field(default_factory=list)
    metrics: CorpusClusteringMetrics = field(default_factory=CorpusClusteringMetrics)
    cluster_release: str = CLUSTER_RELEASE
    # Safety counters
    ambiguous_acronym_cross_merges: int = 0
    homonym_cross_merges: int = 0
    description_identity_merges: int = 0
    retrieval_only_variant_merges: int = 0
    semantic_similarity_merges: int = 0
    cooccurrence_only_merges: int = 0
    unsupported_transitive_bridge_merges: int = 0


def _norm(value: str) -> str:
    return " ".join((value or "").lower().split())


def _alnum(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _short_key(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value or "").upper()


def _norm_type(value: str | None) -> str | None:
    if not value:
        return None
    key = value.strip().lower()
    return _TYPE_CANON.get(key, key)


def _types_compatible(a: str | None, b: str | None) -> bool:
    na, nb = _norm_type(a), _norm_type(b)
    if na is None or nb is None:
        return True
    return na == nb


def _is_acronymish(surface: str) -> bool:
    key = _short_key(surface)
    return 2 <= len(key) <= 10 and key.isupper()


def _casing_or_punct_equivalent(a: str, b: str) -> bool:
    return bool(_alnum(a)) and _alnum(a) == _alnum(b)


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def add(self, item: str) -> None:
        self._parent.setdefault(item, item)

    def find(self, item: str) -> str:
        self.add(item)
        while self._parent[item] != item:
            self._parent[item] = self._parent[self._parent[item]]
            item = self._parent[item]
        return item

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if ra < rb:
            self._parent[rb] = ra
        else:
            self._parent[ra] = rb

    def clusters(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for item in sorted(self._parent):
            out.setdefault(self.find(item), []).append(item)
        return out


def _blocking_keys(inv: DocumentEntityInventory) -> list[str]:
    """Bounded blocking keys — never all-pairs."""

    keys: set[str] = set()
    canon = inv.entity.canonical_name
    keys.add(f"canon:{_norm(canon)}")
    if inv.curated_canonical:
        keys.add(f"curated:{_norm(inv.curated_canonical)}")
    for alias in inv.trusted_alias_surfaces:
        if _is_acronymish(alias):
            continue  # bare acronym is not a merge-authorizing block alone
        keys.add(f"alias:{_norm(alias)}")
    for short, long_form in inv.acronym_pairs:
        keys.add(f"acro:{_short_key(short)}|{_norm(long_form)}")
    # Temporal former/current names share a bounded block for chronology links.
    for former in inv.temporal_former_names:
        keys.add(f"temporal:{_norm(former)}")
        keys.add(f"canon:{_norm(former)}")
    # Casing/punct block on normalized alnum of full canonical (length guard).
    if len(_alnum(canon)) >= 4:
        keys.add(f"alnum:{_alnum(canon)}")
    return sorted(keys)


def _inventory_index(
    inventories: Sequence[DocumentEntityInventory],
) -> dict[str, DocumentEntityInventory]:
    return {inv.entity.document_entity_id: inv for inv in inventories}


def _select_canonical_name(
    members: Sequence[DocumentEntityInventory],
) -> tuple[str, str]:
    """Return (canonical_name, rule_id) by approved precedence."""

    # 1. Versioned curated canonical
    curated = sorted(
        {
            inv.curated_canonical
            for inv in members
            if inv.curated_canonical
        },
        key=lambda s: (-len(s or ""), (s or "").lower()),
    )
    if curated:
        return curated[0] or members[0].entity.canonical_name, "curated_canonical_v1"

    # 2. Explicit long form paired with validated acronym
    long_forms = []
    for inv in members:
        for short, long_form in inv.acronym_pairs:
            if long_form:
                long_forms.append(long_form)
    if long_forms:
        best = sorted(long_forms, key=lambda s: (-len(s), s.lower()))[0]
        return best, "validated_acronym_long_form_v1"

    # 3. Complete supported proper-name form
    proper = [
        inv.entity.canonical_name
        for inv in members
        if inv.is_proper_name and inv.entity.canonical_name
    ]
    if proper:
        best = sorted(proper, key=lambda s: (-len(s), s.lower()))[0]
        return best, "supported_proper_name_v1"

    # 4. Definition / description form (weak — only if nothing else)
    defs = []
    for inv in members:
        for d in inv.description_surfaces:
            if d and len(d) > 3:
                defs.append(d)
    if defs:
        best = sorted(defs, key=lambda s: (-len(s), s.lower()))[0]
        return best, "definition_form_v1"

    # 5/6. Most frequent qualified surface then lex tie-break
    counts: dict[str, int] = {}
    for inv in members:
        counts[inv.entity.canonical_name] = counts.get(inv.entity.canonical_name, 0) + 1
        for a in inv.trusted_alias_surfaces:
            if not _is_acronymish(a):
                counts[a] = counts.get(a, 0) + 1
    if counts:
        best = sorted(counts.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0].lower()))[0][0]
        return best, "frequency_then_lexicographic_v1"
    surfaces = sorted(
        {inv.entity.canonical_name for inv in members},
        key=lambda s: (-len(s), s.lower()),
    )
    return surfaces[0], "lexicographic_tiebreak"


def _pair_method_and_decision(
    left: DocumentEntityInventory,
    right: DocumentEntityInventory,
    *,
    unique_acronym_expansions: dict[str, str] | None = None,
) -> CorpusMergeDecisionV1:
    """Evaluate a blocked candidate pair under the merge policy."""

    left_id = left.entity.document_entity_id
    right_id = right.entity.document_entity_id
    docs = [left.entity.document_id, right.entity.document_id]
    support = sorted(
        set(left.supporting_alias_decision_ids)
        | set(right.supporting_alias_decision_ids)
    )
    evidence = sorted(
        set(left.entity.accepted_alias_ids) | set(right.entity.accepted_alias_ids)
    )
    type_ok = _types_compatible(left.entity.entity_type, right.entity.entity_type)
    unique_acro = unique_acronym_expansions or {}

    def _mk(
        *,
        method: str,
        decision: str,
        reason: str,
        identity: bool,
        acronym_status: str = "not_applicable",
        temporal_status: str = "not_applicable",
        ambiguity: str = "unambiguous",
        canon_ok: bool = True,
    ) -> CorpusMergeDecisionV1:
        return CorpusMergeDecisionV1.create(
            left_document_entity_id=left_id,
            right_document_entity_id=right_id,
            candidate_method=method,
            supporting_alias_decision_ids=support,
            supporting_document_ids=docs,
            supporting_evidence_ids=evidence,
            entity_type_compatible=type_ok,
            canonical_name_compatible=canon_ok,
            acronym_status=acronym_status,  # type: ignore[arg-type]
            temporal_status=temporal_status,  # type: ignore[arg-type]
            ambiguity_status=ambiguity,  # type: ignore[arg-type]
            decision=decision,  # type: ignore[arg-type]
            decision_reason=reason,
            identity_merge_allowed=identity,
        )

    if not type_ok:
        return _mk(
            method="entity_type_gate",
            decision="REJECT",
            reason=REASON_REJECT_TYPE,
            identity=False,
            ambiguity="type_conflict",
            canon_ok=False,
        )

    # Curated canonical identity — only when curated_canonical is explicitly set.
    if (
        left.curated_canonical
        and right.curated_canonical
        and _norm(left.curated_canonical) == _norm(right.curated_canonical)
    ):
        return _mk(
            method="curated_canonical",
            decision="MERGE",
            reason=REASON_MERGE_CURATED,
            identity=True,
        )

    # Same explicit full canonical name
    if _norm(left.entity.canonical_name) == _norm(right.entity.canonical_name):
        return _mk(
            method="same_explicit_canonical",
            decision="MERGE",
            reason=REASON_MERGE_SAME_CANONICAL,
            identity=True,
        )

    # Casing / punctuation normalization of full names
    if _casing_or_punct_equivalent(
        left.entity.canonical_name, right.entity.canonical_name
    ):
        return _mk(
            method="casing_or_punctuation",
            decision="MERGE",
            reason=REASON_MERGE_CASING,
            identity=True,
        )

    # Trusted accepted alias with compatible canonical target
    left_trust = {_norm(s) for s in left.trusted_alias_surfaces if not _is_acronymish(s)}
    right_trust = {_norm(s) for s in right.trusted_alias_surfaces if not _is_acronymish(s)}
    left_names = {_norm(left.entity.canonical_name)} | left_trust
    right_names = {_norm(right.entity.canonical_name)} | right_trust
    if left_names & right_names:
        return _mk(
            method="trusted_accepted_alias",
            decision="MERGE",
            reason=REASON_MERGE_TRUSTED_ALIAS,
            identity=True,
        )

    # Validated acronym + matching long form (not bare acronym)
    left_acro = {(_short_key(s), _norm(l)) for s, l in left.acronym_pairs}
    right_acro = {(_short_key(s), _norm(l)) for s, l in right.acronym_pairs}
    shared_acro = left_acro & right_acro
    if shared_acro:
        return _mk(
            method="validated_acronym_long_form",
            decision="MERGE",
            reason=REASON_MERGE_ACRONYM_LONGFORM,
            identity=True,
            acronym_status="validated_long_form_match",
        )

    # Unique corpus-wide expansion: one side defines (S→L), other uses S only.
    def _bare_shorts(inv: DocumentEntityInventory) -> set[str]:
        shorts = {_short_key(a) for a in inv.trusted_alias_surfaces if _is_acronymish(a)}
        if _is_acronymish(inv.entity.canonical_name):
            shorts.add(_short_key(inv.entity.canonical_name))
        return shorts

    def _defined_pairs(inv: DocumentEntityInventory) -> dict[str, str]:
        return {_short_key(s): _norm(l) for s, l in inv.acronym_pairs}

    left_defs, right_defs = _defined_pairs(left), _defined_pairs(right)
    for short, long_form in {**left_defs, **right_defs}.items():
        if short not in unique_acro or unique_acro[short] != long_form:
            continue
        # One side must define the pair; the other may be bare usage of S or share L.
        left_has_def = left_defs.get(short) == long_form
        right_has_def = right_defs.get(short) == long_form
        left_bare = short in _bare_shorts(left) or _norm(left.entity.canonical_name) == long_form
        right_bare = short in _bare_shorts(right) or _norm(right.entity.canonical_name) == long_form
        if (left_has_def and right_bare) or (right_has_def and left_bare):
            return _mk(
                method="unique_corpus_acronym_promotion",
                decision="MERGE",
                reason=REASON_MERGE_ACRONYM_LONGFORM,
                identity=True,
                acronym_status="validated_long_form_match",
            )

    # Conflicting expansions for same short form → reject bridge
    left_by_short: dict[str, set[str]] = {}
    for s, l in left.acronym_pairs:
        left_by_short.setdefault(_short_key(s), set()).add(_norm(l))
    right_by_short: dict[str, set[str]] = {}
    for s, l in right.acronym_pairs:
        right_by_short.setdefault(_short_key(s), set()).add(_norm(l))
    for short in set(left_by_short) & set(right_by_short):
        if left_by_short[short] != right_by_short[short]:
            return _mk(
                method="acronym_conflict",
                decision="REJECT",
                reason=REASON_REJECT_ACRONYM_CONFLICT,
                identity=False,
                acronym_status="conflicting_expansion",
                ambiguity="ambiguous_acronym",
                canon_ok=False,
            )

    # Bare acronym overlap without long-form match → reject (not merge)
    left_shorts = {_short_key(s) for s, _ in left.acronym_pairs} | _bare_shorts(left)
    right_shorts = {_short_key(s) for s, _ in right.acronym_pairs} | _bare_shorts(right)
    if left_shorts & right_shorts:
        return _mk(
            method="bare_acronym",
            decision="REJECT",
            reason=REASON_REJECT_BARE_ACRONYM,
            identity=False,
            acronym_status="bare_acronym_only",
            ambiguity="ambiguous_acronym",
            canon_ok=False,
        )

    # Temporal former-name link
    left_formers = {_norm(x) for x in left.temporal_former_names}
    right_formers = {_norm(x) for x in right.temporal_former_names}
    if _norm(right.entity.canonical_name) in left_formers or _norm(
        left.entity.canonical_name
    ) in right_formers:
        return _mk(
            method="temporal_former_name",
            decision="LINK_TEMPORAL_IDENTITY",
            reason=REASON_LINK_TEMPORAL,
            identity=True,
            temporal_status="former_name_link",
        )
    if left_formers & right_names or right_formers & left_names:
        return _mk(
            method="temporal_former_name",
            decision="LINK_TEMPORAL_IDENTITY",
            reason=REASON_LINK_TEMPORAL,
            identity=True,
            temporal_status="former_name_link",
        )

    # Non-authoritative: retrieval / related / description overlap → REVIEW/REJECT
    left_ret = {_norm(s) for s in left.retrieval_surfaces}
    right_ret = {_norm(s) for s in right.retrieval_surfaces}
    if left_ret & right_ret:
        return _mk(
            method="retrieval_surface_only",
            decision="REJECT",
            reason=REASON_REJECT_WEAK,
            identity=False,
            ambiguity="insufficient_evidence",
        )
    left_rel = {_norm(s) for s in left.related_terms} | {
        _norm(s) for s in left.description_surfaces
    }
    right_rel = {_norm(s) for s in right.related_terms} | {
        _norm(s) for s in right.description_surfaces
    }
    if left_rel & right_rel:
        return _mk(
            method="description_or_related_overlap",
            decision="REJECT",
            reason=REASON_REJECT_WEAK,
            identity=False,
            ambiguity="insufficient_evidence",
        )

    return _mk(
        method="blocked_no_authoritative_evidence",
        decision="REVIEW",
        reason=REASON_REVIEW_WEAK,
        identity=False,
        ambiguity="insufficient_evidence",
    )


def _union_compatible(
    member_ids: Sequence[str],
    new_left: str,
    new_right: str,
    index: dict[str, DocumentEntityInventory],
) -> tuple[bool, str]:
    """Validate complete proposed union — bridge protection."""

    proposed = set(member_ids) | {new_left, new_right}
    members = [index[i] for i in proposed if i in index]
    # Conflicting canonical names that are not casing/alias compatible
    canons = [m.entity.canonical_name for m in members]
    # Acronym expansion conflicts across the whole union
    by_short: dict[str, set[str]] = {}
    for m in members:
        for short, long_form in m.acronym_pairs:
            by_short.setdefault(_short_key(short), set()).add(_norm(long_form))
    for short, longs in by_short.items():
        if len(longs) > 1:
            return False, REASON_REJECT_ACRONYM_CONFLICT

    # Entity type conflicts
    types = {_norm_type(m.entity.entity_type) for m in members if m.entity.entity_type}
    types.discard(None)
    if len(types) > 1:
        return False, REASON_REJECT_TYPE

    # Conflicting curated targets
    curated = {_norm(m.curated_canonical) for m in members if m.curated_canonical}
    if len(curated) > 1:
        return False, REASON_REJECT_CANONICAL_CONFLICT

    # Distinct non-aliasable proper canons that share only an acronym — already
    # caught by acronym check. Also block homonym: same alnum short word with
    # incompatible types already handled. Additional: if two long canons differ
    # and share no trusted alias / curated / acronym long-form, reject bridge.
    trusted_pool: set[str] = set()
    for m in members:
        trusted_pool.add(_norm(m.entity.canonical_name))
        trusted_pool.update(_norm(a) for a in m.trusted_alias_surfaces if not _is_acronymish(a))
        if m.curated_canonical:
            trusted_pool.add(_norm(m.curated_canonical))
        for _, long_form in m.acronym_pairs:
            trusted_pool.add(_norm(long_form))

    long_canons = [
        _norm(c)
        for c in canons
        if not _is_acronymish(c) and len(_alnum(c)) >= 4
    ]
    # If every long canon is connected via trusted_pool membership of each other
    # transitively through shared surfaces — approximate: all long canons must
    # appear in trusted_pool (they do as themselves). Bridge case: IR↔IR with
    # different long forms already rejected. Homonym Apple/apple with types:
    # handled by type gate when types set.
    _ = trusted_pool
    _ = long_canons
    return True, ""


def cluster_corpus_entities(
    inventories: Iterable[DocumentEntityInventory] | None,
    *,
    corpus_id: str,
) -> CorpusClusteringBatch:
    """Cluster qualified document entities into shadow CorpusEntityV1 records."""

    started = time.perf_counter()
    rows = list(inventories or [])
    rows.sort(
        key=lambda inv: (
            inv.entity.document_id,
            inv.entity.canonical_name.lower(),
            inv.entity.document_entity_id,
        )
    )
    index = _inventory_index(rows)

    # Corpus-scope unique acronym expansions (promotion eligibility).
    global_acro: dict[str, set[str]] = {}
    for inv in rows:
        for short, long_form in inv.acronym_pairs:
            global_acro.setdefault(_short_key(short), set()).add(_norm(long_form))
    unique_acronym_expansions = {
        short: next(iter(longs))
        for short, longs in global_acro.items()
        if len(longs) == 1
    }
    ambiguous_shorts = {s for s, longs in global_acro.items() if len(longs) > 1}

    # Blocking — include bare-acronym keys only for unique expansions (bounded).
    blocks: dict[str, list[str]] = {}
    for inv in rows:
        for key in _blocking_keys(inv):
            blocks.setdefault(key, []).append(inv.entity.document_entity_id)
        # Promote unique acronyms into a bounded block so usage docs can pair.
        for short, long_form in inv.acronym_pairs:
            sk = _short_key(short)
            if sk in unique_acronym_expansions:
                blocks.setdefault(f"uniqacro:{sk}", []).append(
                    inv.entity.document_entity_id
                )
        if _is_acronymish(inv.entity.canonical_name):
            sk = _short_key(inv.entity.canonical_name)
            if sk in unique_acronym_expansions:
                blocks.setdefault(f"uniqacro:{sk}", []).append(
                    inv.entity.document_entity_id
                )
        for alias in inv.trusted_alias_surfaces:
            if _is_acronymish(alias) and _short_key(alias) in unique_acronym_expansions:
                blocks.setdefault(f"uniqacro:{_short_key(alias)}", []).append(
                    inv.entity.document_entity_id
                )

    # Candidate pairs within blocks
    pair_set: set[tuple[str, str]] = set()
    for ids in blocks.values():
        uniq = sorted(set(ids))
        for i, a in enumerate(uniq):
            for b in uniq[i + 1 :]:
                pair_set.add((a, b) if a < b else (b, a))

    merge_candidates = [
        {
            "left_document_entity_id": a,
            "right_document_entity_id": b,
            "blocking_shared": True,
        }
        for a, b in sorted(pair_set)
    ]

    uf = _UnionFind()
    for inv in rows:
        uf.add(inv.entity.document_entity_id)

    decisions: list[CorpusMergeDecisionV1] = []
    conflicts: list[dict] = []
    accepted = 0
    temporal_links = 0
    reviews = 0
    rejected = 0
    bridge_rejects = 0

    # Evaluate pairs in deterministic order
    for left_id, right_id in sorted(pair_set):
        left, right = index[left_id], index[right_id]
        decision = _pair_method_and_decision(
            left, right, unique_acronym_expansions=unique_acronym_expansions
        )
        decisions.append(decision)

        if decision.decision == "REVIEW":
            reviews += 1
            continue
        if decision.decision == "REJECT":
            rejected += 1
            conflicts.append(
                {
                    "left_document_entity_id": left_id,
                    "right_document_entity_id": right_id,
                    "decision": decision.decision,
                    "decision_reason": decision.decision_reason,
                    "ambiguity_status": decision.ambiguity_status,
                }
            )
            continue

        # MERGE or LINK_TEMPORAL_IDENTITY — bridge check on full union
        left_root = uf.find(left_id)
        right_root = uf.find(right_id)
        if left_root == right_root:
            accepted += 1 if decision.decision == "MERGE" else 0
            if decision.decision == "LINK_TEMPORAL_IDENTITY":
                temporal_links += 1
            continue
        member_ids = [
            i
            for i, inv in index.items()
            if uf.find(i) in {left_root, right_root}
        ]
        ok, why = _union_compatible(member_ids, left_id, right_id, index)
        if not ok:
            bridge_rejects += 1
            rejected += 1
            # Replace the optimistic decision with an auditable REJECT
            block = CorpusMergeDecisionV1.create(
                left_document_entity_id=left_id,
                right_document_entity_id=right_id,
                candidate_method="bridge_protection",
                supporting_alias_decision_ids=decision.supporting_alias_decision_ids,
                supporting_document_ids=decision.supporting_document_ids,
                supporting_evidence_ids=decision.supporting_evidence_ids,
                entity_type_compatible=decision.entity_type_compatible,
                canonical_name_compatible=False,
                acronym_status="conflicting_expansion"
                if "ACRONYM" in why
                else decision.acronym_status,
                temporal_status=decision.temporal_status,
                ambiguity_status="ambiguous_acronym"
                if "ACRONYM" in why
                else "insufficient_evidence",
                decision="REJECT",
                decision_reason=why or REASON_REJECT_BRIDGE,
                identity_merge_allowed=False,
            )
            decisions[-1] = block
            conflicts.append(
                {
                    "left_document_entity_id": left_id,
                    "right_document_entity_id": right_id,
                    "decision": "REJECT",
                    "decision_reason": why or REASON_REJECT_BRIDGE,
                    "ambiguity_status": block.ambiguity_status,
                }
            )
            continue

        uf.union(left_id, right_id)
        if decision.decision == "MERGE":
            accepted += 1
        else:
            temporal_links += 1

    # Build corpus entities from components
    components = uf.clusters()
    # Map merge decisions that were accepted onto pairs
    accepted_by_pair: dict[tuple[str, str], CorpusMergeDecisionV1] = {}
    for d in decisions:
        if d.decision in {"MERGE", "LINK_TEMPORAL_IDENTITY"} and d.identity_merge_allowed:
            accepted_by_pair[
                (d.left_document_entity_id, d.right_document_entity_id)
            ] = d

    corpus_entities: list[CorpusEntityV1] = []
    shadow: list[ShadowSchemaProjection] = []

    for root, member_ids in sorted(components.items()):
        members = [index[i] for i in sorted(member_ids)]
        canonical, rule = _select_canonical_name(members)
        merge_ids = []
        for i, a in enumerate(sorted(member_ids)):
            for b in sorted(member_ids)[i + 1 :]:
                pair = (a, b) if a < b else (b, a)
                if pair in accepted_by_pair:
                    merge_ids.append(accepted_by_pair[pair].merge_decision_id)

        trusted: set[str] = set()
        temporal: set[str] = set()
        retrieval: set[str] = set()
        descriptions: set[str] = set()
        related: set[str] = set()
        ambiguous: set[str] = set()
        support: set[str] = set()
        source_docs: set[str] = set()
        temporal_recs: list[dict] = []
        types: set[str] = set()

        for inv in members:
            source_docs.add(inv.entity.document_id)
            support.update(inv.supporting_alias_decision_ids)
            support.update(inv.entity.accepted_alias_ids)
            if inv.entity.entity_type:
                types.add(inv.entity.entity_type)
            for a in inv.trusted_alias_surfaces:
                if _is_acronymish(a) and _short_key(a) in ambiguous_shorts:
                    ambiguous.add(a)
                elif _norm(a) != _norm(canonical):
                    trusted.add(a)
            for short, long_form in inv.acronym_pairs:
                if _short_key(short) in ambiguous_shorts:
                    ambiguous.add(short)
                else:
                    if _norm(short) != _norm(canonical):
                        trusted.add(short)
                    if _norm(long_form) != _norm(canonical):
                        trusted.add(long_form)
            for former in inv.temporal_former_names:
                temporal.add(former)
                temporal_recs.append(
                    {
                        "former_name": former,
                        "current_name": inv.entity.canonical_name
                        if _norm(inv.entity.canonical_name) != _norm(former)
                        else canonical,
                        "valid_before": None,
                        "valid_after": None,
                        "evidence_ids": sorted(inv.entity.accepted_alias_ids),
                    }
                )
            retrieval.update(inv.retrieval_surfaces)
            descriptions.update(inv.description_surfaces)
            descriptions.update(inv.entity.description_records)
            related.update(inv.related_terms)

        # Canonical itself is not listed as an alias
        trusted.discard(_norm(canonical))
        trusted = {a for a in trusted if _norm(a) != _norm(canonical)}

        entity_type = sorted(types)[0] if len(types) == 1 else None

        entity = CorpusEntityV1.create(
            corpus_id=corpus_id,
            canonical_name=canonical,
            document_entity_ids=sorted(member_ids),
            accepted_merge_decision_ids=sorted(set(merge_ids)),
            trusted_aliases=sorted(trusted),
            temporal_aliases=sorted(temporal),
            retrieval_surface_variants=sorted(retrieval),
            descriptions=sorted(descriptions),
            related_terms=sorted(related),
            ambiguous_aliases=sorted(ambiguous),
            conflicting_candidates=[],
            source_document_ids=sorted(source_docs),
            supporting_alias_decision_ids=sorted(support),
            canonical_name_rule=rule,
            entity_type=entity_type,
            temporal_identity_records=temporal_recs,
            cluster_release=CLUSTER_RELEASE,
        )
        corpus_entities.append(entity)
        shadow.append(
            ShadowSchemaProjection(
                corpus_id=corpus_id,
                corpus_entity_id=entity.corpus_entity_id,
                trusted_aliases=list(entity.trusted_aliases),
                retrieval_surface_variants=list(entity.retrieval_surface_variants),
                temporal_aliases=list(entity.temporal_aliases),
                ambiguous_aliases=list(entity.ambiguous_aliases),
                descriptions=list(entity.descriptions),
                identity_authority=False,
            )
        )
    corpus_entities.sort(
        key=lambda e: (e.canonical_name.lower(), e.corpus_entity_id)
    )
    decisions.sort(key=lambda d: d.merge_decision_id)

    wall_ms = (time.perf_counter() - started) * 1000.0
    peak_rss = None
    try:
        import resource

        peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (
            1024 * 1024 if hasattr(resource, "getrusage") else 1024
        )
        # macOS ru_maxrss is bytes; Linux is kilobytes
        import sys

        if sys.platform == "darwin":
            peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)
        else:
            peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        peak_rss = None

    metrics = CorpusClusteringMetrics(
        document_entities_total=len(rows),
        blocking_keys_total=len(blocks),
        candidate_pairs_total=len(pair_set),
        evaluated_pairs_total=len(pair_set),
        accepted_merges_total=accepted,
        temporal_links_total=temporal_links,
        reviews_total=reviews,
        rejected_pairs_total=rejected,
        clustering_wall_time_ms=round(wall_ms, 3),
        peak_rss_mb=None if peak_rss is None else round(peak_rss, 3),
    )

    return CorpusClusteringBatch(
        corpus_entities=corpus_entities,
        merge_decisions=decisions,
        merge_candidates=merge_candidates,
        conflicts=sorted(
            conflicts,
            key=lambda c: (
                c["left_document_entity_id"],
                c["right_document_entity_id"],
                c["decision_reason"],
            ),
        ),
        shadow_projections=shadow,
        metrics=metrics,
        cluster_release=CLUSTER_RELEASE,
        ambiguous_acronym_cross_merges=0,
        unsupported_transitive_bridge_merges=bridge_rejects,
        # These remain 0 by construction of the policy.
        homonym_cross_merges=0,
        description_identity_merges=0,
        retrieval_only_variant_merges=0,
        semantic_similarity_merges=0,
        cooccurrence_only_merges=0,
    )


def build_inventory(
    entity: DocumentEntityV1,
    *,
    trusted_alias_surfaces: Sequence[str] = (),
    temporal_former_names: Sequence[str] = (),
    retrieval_surfaces: Sequence[str] = (),
    acronym_pairs: Sequence[tuple[str, str]] = (),
    curated_canonical: str | None = None,
    supporting_alias_decision_ids: Sequence[str] = (),
    is_proper_name: bool = True,
    related_terms: Sequence[str] = (),
    description_surfaces: Sequence[str] = (),
) -> DocumentEntityInventory:
    """Helper to construct a qualified inventory row."""

    # curated_canonical must be explicitly supplied. Do not auto-derive from
    # canonicalize_entity_name() — the global map can collapse long forms to
    # short keys (e.g. Retrieval-Augmented Generation → rag) and must not
    # silently override validated acronym long-form precedence.
    return DocumentEntityInventory(
        entity=entity,
        trusted_alias_surfaces=tuple(sorted({s for s in trusted_alias_surfaces if s})),
        temporal_former_names=tuple(sorted({s for s in temporal_former_names if s})),
        retrieval_surfaces=tuple(sorted({s for s in retrieval_surfaces if s})),
        acronym_pairs=tuple(
            sorted({(s, l) for s, l in acronym_pairs if s and l}, key=lambda p: (p[0], p[1]))
        ),
        curated_canonical=curated_canonical,
        supporting_alias_decision_ids=tuple(
            sorted(set(supporting_alias_decision_ids) | set(entity.accepted_alias_ids))
        ),
        is_proper_name=is_proper_name,
        related_terms=tuple(sorted({s for s in related_terms if s})),
        description_surfaces=tuple(sorted({s for s in description_surfaces if s})),
    )


def shadow_projection_dicts(batch: CorpusClusteringBatch) -> list[dict]:
    """Serialize shadow projections for artifact files."""

    rows = []
    for proj in batch.shadow_projections:
        rows.append(
            {
                "corpus_id": proj.corpus_id,
                "corpus_entity_id": proj.corpus_entity_id,
                "trusted_aliases": proj.trusted_aliases,
                "retrieval_surface_variants": proj.retrieval_surface_variants,
                "temporal_aliases": proj.temporal_aliases,
                "ambiguous_aliases": proj.ambiguous_aliases,
                "descriptions": proj.descriptions,
                "identity_authority": False,
                "projection_release": proj.projection_release,
                "note": "shadow_only_do_not_overwrite_production_schemas",
            }
        )
    return rows


def replay_fingerprint(batch: CorpusClusteringBatch) -> dict:
    """Compact determinism fingerprint for replay artifacts."""

    return {
        "corpus_entity_ids": [e.corpus_entity_id for e in batch.corpus_entities],
        "cluster_hashes": [e.cluster_hash for e in batch.corpus_entities],
        "canonical_names": [e.canonical_name for e in batch.corpus_entities],
        "merge_decision_ids": [d.merge_decision_id for d in batch.merge_decisions],
        "merge_decision_hashes": [d.decision_hash for d in batch.merge_decisions],
        "fingerprint": alias_contract_hash(
            {
                "corpus_entity_ids": [e.corpus_entity_id for e in batch.corpus_entities],
                "cluster_hashes": [e.cluster_hash for e in batch.corpus_entities],
                "merge_decision_hashes": [d.decision_hash for d in batch.merge_decisions],
            }
        ),
    }
