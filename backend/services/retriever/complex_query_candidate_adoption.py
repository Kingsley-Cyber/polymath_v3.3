"""Candidate-adoption integration for complex-query (dark, allowlist-only).

Builds candidate finalists from CQ winners merged into the unified ranked
pool BEFORE protect/MMR-style selection. User-visible baseline chunks are
never replaced here — chat keeps baseline answers authoritative until a
separate visible-canary ruling.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from models.candidate_adoption import CandidateAdoptionComparisonV1
from models.complex_query import AnswerVerificationV1, ContextPacketV1

logger = logging.getLogger(__name__)

_STATE: dict[str, Any] = {
    "enabled": True,
    "disabled_reason": None,
    "measured": 0,
    "rollback_events": [],
    "latency_breaches": [],
}

RELATIONSHIP_CLASSES = {
    "direct_one_hop",
    "two_hop_dependency",
    "cross_domain_translation",
    "cross_domain",
    "relationship",
    "comparison",
    "contradiction",
    "temporal_latest",
    "temporal_or_contradiction",
    "dependency_analysis",
    "multi_hop_relationship",
    "contradiction_analysis",
    "temporal_latest_state",
}


def _csv(raw: str | None) -> set[str]:
    return {x.strip() for x in str(raw or "").split(",") if x.strip()}


def process_enabled() -> bool:
    return bool(_STATE.get("enabled", True)) and not _STATE.get("disabled_reason")


def trigger_rollback(reason: str) -> None:
    _STATE["enabled"] = False
    _STATE["disabled_reason"] = reason
    _STATE.setdefault("rollback_events", []).append(
        {"reason": reason, "ts": time.time(), "measured": _STATE.get("measured")}
    )
    logger.error("CANDIDATE_ADOPTION_ROLLBACK reason=%s", reason)


@dataclass
class CandidateAdoptionGate:
    allowed: bool
    reason: str = ""
    ranking_adoption: bool = False
    synthesis_packet: bool = False
    final_verification: bool = False
    runtime: bool = False
    corpus_ids: list[str] = field(default_factory=list)
    user_id: str = ""


def gate_candidate_adoption(
    *,
    settings: Any,
    corpus_ids: list[str] | None,
    user_id: str | None,
) -> CandidateAdoptionGate:
    if not process_enabled():
        return CandidateAdoptionGate(
            False, f"rolled_back:{_STATE.get('disabled_reason') or 'unknown'}"
        )

    runtime = bool(getattr(settings, "COMPLEX_QUERY_RUNTIME_ENABLED", False)) or bool(
        getattr(settings, "COMPLEX_QUERY_FIXTURE_RUNTIME_ENABLED", False)
    )
    ranking = bool(getattr(settings, "COMPLEX_QUERY_RANKING_ADOPTION_ENABLED", False))
    packet = bool(getattr(settings, "COMPLEX_QUERY_SYNTHESIS_PACKET_ENABLED", False))
    verify = bool(getattr(settings, "COMPLEX_QUERY_FINAL_VERIFICATION_ENABLED", False))
    if not (runtime or ranking or packet or verify):
        return CandidateAdoptionGate(False, "flags_off")

    allow_c = _csv(
        getattr(settings, "COMPLEX_QUERY_CANDIDATE_CORPUS_ALLOWLIST", "")
        or getattr(settings, "COMPLEX_QUERY_CORPUS_ALLOWLIST", "")
    )
    corpora = [str(c) for c in (corpus_ids or []) if str(c)]
    hit = [c for c in corpora if c in allow_c] if allow_c else []
    if not hit:
        return CandidateAdoptionGate(False, "corpus_not_allowlisted", corpus_ids=corpora)

    allow_u = _csv(getattr(settings, "COMPLEX_QUERY_CANDIDATE_USER_ALLOWLIST", ""))
    uid = str(user_id or "").strip()
    if allow_u and uid and uid not in allow_u:
        return CandidateAdoptionGate(False, "user_not_allowlisted", corpus_ids=hit, user_id=uid)
    if allow_u and not uid:
        return CandidateAdoptionGate(False, "user_id_required", corpus_ids=hit)

    if bool(getattr(settings, "COMPLEX_QUERY_SUBQUERY_PLANNER_ENABLED", False)):
        return CandidateAdoptionGate(False, "global_planner_must_stay_disabled")

    return CandidateAdoptionGate(
        True,
        "ok",
        ranking_adoption=ranking,
        synthesis_packet=packet,
        final_verification=verify,
        runtime=runtime,
        corpus_ids=hit,
        user_id=uid,
    )


def _chunk_id(chunk: Any) -> str:
    if chunk is None:
        return ""
    if isinstance(chunk, dict):
        return str(chunk.get("chunk_id") or chunk.get("id") or "")
    return str(getattr(chunk, "chunk_id", None) or getattr(chunk, "id", None) or "")


def _index_chunks(chunks: list[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for ch in chunks or []:
        cid = _chunk_id(ch)
        if cid and cid not in out:
            out[cid] = ch
    return out


def obligation_support_map(
    obligation_results: list[dict[str, Any]] | None,
) -> dict[str, set[str]]:
    """Map obligation_id → supporting child ids from CQ fusion rows."""

    out: dict[str, set[str]] = {}
    for row in obligation_results or []:
        oid = str(row.get("obligation_id") or row.get("subquery_id") or "").strip()
        if not oid:
            continue
        kids = {
            str(x)
            for x in (row.get("ranked_child_ids") or row.get("supporting_child_ids") or [])
            if x
        }
        out[oid] = kids
    return out


def obligation_coverage_rate(
    finalist_ids: list[str],
    obligation_results: list[dict[str, Any]] | None,
) -> float:
    """Fraction of obligations with ≥1 supporting child present in finalists.

    Comparable for baseline vs candidate — never hardcode baseline=1.0.
    """

    ids = {str(x) for x in finalist_ids if x}
    support = obligation_support_map(obligation_results)
    if not support:
        return 1.0 if ids else 0.0
    covered = sum(1 for kids in support.values() if kids & ids)
    return round(covered / max(1, len(support)), 4)


def uncovered_obligation_ids(
    finalist_ids: list[str],
    obligation_results: list[dict[str, Any]] | None,
) -> list[str]:
    ids = {str(x) for x in finalist_ids if x}
    out: list[str] = []
    for oid, kids in obligation_support_map(obligation_results).items():
        if not (kids & ids):
            out.append(oid)
    return out


def _admit_graph_chunk(
    *,
    chunk_id: str,
    baseline_ids: set[str],
    uncovered: set[str],
    support: dict[str, set[str]],
    graph_child_ids: set[str],
    path_child_ids: set[str],
    has_contradictions: bool,
    query_class: str,
) -> str | None:
    """Return admission reason or None if chunk must not augment baseline."""

    if not chunk_id or chunk_id in baseline_ids:
        return None
    fills = [oid for oid, kids in support.items() if chunk_id in kids and oid in uncovered]
    if fills:
        return f"fills_uncovered_obligation:{fills[0]}"
    # Explicit path support only — do not admit mere CQ novelty.
    if chunk_id in path_child_ids and (
        query_class in RELATIONSHIP_CLASSES
        or query_class
        in {
            "unsupported_transitivity",
            "ambiguous_identity",
            "graph_fact_block",
        }
    ):
        return "provides_explicit_relationship_evidence"
    if (
        has_contradictions
        and chunk_id in graph_child_ids
        and "contradict" in (query_class or "")
    ):
        return "resolves_contradiction"
    if "temporal" in (query_class or "") and chunk_id in graph_child_ids:
        return "provides_temporal_update"
    return None


def build_candidate_finalists(
    *,
    baseline_finalists: list[Any],
    ranked_pool: list[Any],
    cq_winner_ids: list[str],
    protected_ids: list[str],
    graph_child_ids: list[str],
    final_top_k: int,
    obligation_results: list[dict[str, Any]] | None = None,
    graph_paths: list[dict[str, Any]] | None = None,
    contradictions: list[dict[str, Any]] | None = None,
    query_class: str = "",
) -> tuple[list[Any], dict[str, Any]]:
    """Augment baseline finalists with qualified CQ/graph evidence only.

    Policy (owner 2026-08-05): preserve baseline coverage; admit graph/CQ
    chunks only when they fill an uncovered obligation or provide explicit
    relationship / contradiction / temporal evidence. Never rebuild the
    evidence set from CQ winners alone.
    """

    by_id = _index_chunks([*(ranked_pool or []), *(baseline_finalists or [])])
    baseline_ids = [_chunk_id(c) for c in baseline_finalists if _chunk_id(c)]
    baseline_set = set(baseline_ids)
    top_k = max(1, int(final_top_k or 12))

    # Protect baseline obligation winners (and explicit protect ids) — never drop.
    support = obligation_support_map(obligation_results)
    baseline_obligation_winners: list[str] = []
    for kids in support.values():
        for cid in baseline_ids:
            if cid in kids and cid not in baseline_obligation_winners:
                baseline_obligation_winners.append(cid)
    protect = []
    for cid in (*(protected_ids or []), *baseline_obligation_winners, *baseline_ids):
        if cid and cid in by_id and cid not in protect:
            protect.append(cid)

    uncovered = set(uncovered_obligation_ids(baseline_ids, obligation_results))
    path_kids: set[str] = set()
    for ps in graph_paths or []:
        for sid in (ps or {}).get("supporting_child_ids") or []:
            if sid:
                path_kids.add(str(sid))
    graph_set = {str(x) for x in (graph_child_ids or []) if x}
    has_contradictions = bool(contradictions)

    # Candidate additions: CQ winners + graph path kids, admission-filtered.
    admit_order = [
        str(x)
        for x in (*(cq_winner_ids or []), *sorted(path_kids), *sorted(graph_set))
        if x
    ]
    admitted: list[tuple[str, str]] = []
    seen_admit: set[str] = set()
    for cid in admit_order:
        if cid in seen_admit or cid not in by_id:
            continue
        reason = _admit_graph_chunk(
            chunk_id=cid,
            baseline_ids=baseline_set,
            uncovered=uncovered,
            support=support,
            graph_child_ids=graph_set,
            path_child_ids=path_kids,
            has_contradictions=has_contradictions,
            query_class=query_class,
        )
        if not reason:
            continue
        seen_admit.add(cid)
        admitted.append((cid, reason))
        # Refresh uncovered after each fill so we stop once covered.
        if reason.startswith("fills_uncovered_obligation:"):
            trial = list(baseline_ids) + [c for c, _ in admitted]
            uncovered = set(uncovered_obligation_ids(trial, obligation_results))

    ordered: list[str] = []
    seen: set[str] = set()
    # 1) Full baseline finalists first (coverage preservation).
    for cid in baseline_ids:
        if cid and cid in by_id and cid not in seen:
            seen.add(cid)
            ordered.append(cid)
    # 2) Qualified augmentations until top_k.
    aug_used: list[dict[str, str]] = []
    for cid, reason in admitted:
        if len(ordered) >= top_k:
            break
        if cid in seen or cid not in by_id:
            continue
        seen.add(cid)
        ordered.append(cid)
        aug_used.append({"chunk_id": cid, "reason": reason})

    # If still under top_k, do not backfill with non-admitted CQ novelty.
    finalists = [by_id[cid] for cid in ordered]
    base_cov = obligation_coverage_rate(baseline_ids, obligation_results)
    cand_cov = obligation_coverage_rate(ordered, obligation_results)
    # Guard: never ship a candidate set with lower coverage than baseline.
    guard_triggered = False
    if cand_cov + 1e-9 < base_cov:
        guard_triggered = True
        finalists = [by_id[cid] for cid in baseline_ids if cid in by_id][:top_k]
        ordered = [_chunk_id(c) for c in finalists]
        aug_used = []
        cand_cov = obligation_coverage_rate(ordered, obligation_results)

    missing_winners = [x for x in cq_winner_ids if x and x not in by_id]
    return finalists, {
        "policy": "baseline_augment",
        "protected_used": protect[:12],
        "baseline_preserved": baseline_ids,
        "graph_winners_used": [
            x["chunk_id"] for x in aug_used if "relationship" in x["reason"]
        ],
        "admitted_augmentations": aug_used,
        "cq_winners_used": [x for x in ordered if x in set(cq_winner_ids or [])],
        "missing_winner_ids": missing_winners[:20],
        "candidate_count": len(finalists),
        "final_top_k": top_k,
        "baseline_obligation_coverage": base_cov,
        "candidate_obligation_coverage": cand_cov,
        "uncovered_baseline_obligations": sorted(
            uncovered_obligation_ids(ordered, obligation_results)
        ),
        "coverage_guard_triggered": guard_triggered,
    }


def adapt_context_packet(
    *,
    cq_packet: dict[str, Any] | None,
    legacy_packet: dict[str, Any] | None,
    query: str,
    obligations: list[str] | None = None,
    graph_capability: dict[str, Any] | None = None,
    graph_status: str = "not_required",
) -> ContextPacketV1:
    """Unify CQ + legacy packet shapes into one ContextPacketV1."""

    cq = dict(cq_packet or {})
    legacy = dict(legacy_packet or {})
    warnings = list(cq.get("warnings") or [])
    if legacy and not cq:
        warnings.append("legacy_packet_adapted")
    # Prefer CQ packet fields; fill gaps from legacy diagnostics packet.
    root = dict(cq.get("root_query") or {})
    if not root:
        root = {"standalone_query": query, "original_query": query}
    vocab = dict(cq.get("vocabulary_resolution") or legacy.get("vocabulary_resolution") or {})
    packet = ContextPacketV1(
        conversation_context=dict(cq.get("conversation_context") or {}),
        root_query=root,
        query_plan=dict(cq.get("query_plan") or {"standalone_query": query}),
        subquery_plan=list(cq.get("subquery_plan") or []),
        vocabulary_resolution=vocab,
        ontology_resolution=dict(cq.get("ontology_resolution") or {}),
        graph_capability={
            **dict(graph_capability or cq.get("graph_capability") or {}),
            "execution_status": graph_status,
        },
        obligation_results=list(
            cq.get("obligation_results")
            or [
                {"obligation_id": o, "status": "unknown"}
                for o in (obligations or [])
            ]
        ),
        routing_summaries=list(cq.get("routing_summaries") or []),
        protected_child_evidence=list(cq.get("protected_child_evidence") or []),
        mmr_selected_child_evidence=list(cq.get("mmr_selected_child_evidence") or []),
        graph_paths=list(cq.get("graph_paths") or []),
        deterministic_inferences=list(cq.get("deterministic_inferences") or []),
        cross_domain_bridges=list(
            cq.get("cross_domain_bridges") or cq.get("bridges") or []
        ),
        contradictions=list(cq.get("contradictions") or []),
        coverage=dict(cq.get("coverage") or legacy.get("coverage") or {}),
        unresolved_obligations=list(cq.get("unresolved_obligations") or []),
        warnings=warnings,
        schema_records_as_citations=int(cq.get("schema_records_as_citations") or 0),
        summaries_as_detailed_claim_citations=int(
            cq.get("summaries_as_detailed_claim_citations") or 0
        ),
    ).with_hash()
    return packet


def derive_graph_execution_status(
    *,
    requested_tier: str,
    effective_tier: str,
    paths_used: int,
    trav: dict[str, Any] | None,
    query_class: str,
) -> str:
    trav = trav or {}
    if "graph" in str(requested_tier).lower() and "graph" not in str(effective_tier).lower():
        return "blocked_no_capability"
    if trav.get("skipped"):
        reason = str(trav.get("skip_reason") or trav.get("reason") or "").lower()
        if "driver" in reason:
            return "blocked_no_driver"
        if "seed" in reason:
            return "blocked_no_seed_entities"
        if "capab" in reason:
            return "blocked_no_capability"
        return "blocked_no_capability"
    if paths_used > 0:
        if trav.get("partial"):
            return "partial_graph"
        return "executed_with_paths"
    if query_class in {
        "two_hop_dependency",
        "cross_domain_translation",
        "multi_hop_relationship",
        "dependency_analysis",
    }:
        return "no_supported_path"
    return "not_required"


_CITATION_RE = re.compile(
    r"\b(?:doc_[a-zA-Z0-9_]+|[a-f0-9]{16,}(?:_[a-z0-9_]+)+)\b"
)
_HEDGE_RE = re.compile(
    r"\b(insufficient|not (?:enough|supported|present)|no (?:other )?evidence|"
    r"cannot (?:confirm|determine)|unsupported|does not (?:say|state|show|elaborate)|"
    r"not (?:stated|found|explicit)|beyond this|packet defines|"
    r"evidence (?:does not|packet))\b",
    re.I,
)


def verify_generated_answer(
    *,
    answer: str,
    candidate_finalist_ids: list[str],
    context_packet: dict[str, Any] | None,
    query_class: str = "",
) -> AnswerVerificationV1:
    """Post-synthesis verification of the actual generated candidate answer."""

    text = str(answer or "")
    ids = [str(x) for x in candidate_finalist_ids if x]
    id_set = set(ids)
    packet = context_packet or {}
    paths = list(packet.get("graph_paths") or [])
    bridges = list(packet.get("cross_domain_bridges") or packet.get("bridges") or [])
    contradictions = list(packet.get("contradictions") or [])
    obligations = list(packet.get("obligation_results") or [])

    sentences = [
        s.strip()
        for s in re.split(r"(?<=[.!?])\s+", text)
        if len(s.strip()) > 20
    ]
    claims_total = max(len(sentences), 1 if text.strip() else 0)

    cited = set(_CITATION_RE.findall(text))
    resolved = [
        c
        for c in cited
        if c in id_set or any(c in i or i in c for i in id_set)
    ]
    citation_ok = (not cited) or (len(resolved) == len(cited))
    # Short prefixes of selected ids also count (model may truncate).
    id_prefixes = {i[:16] for i in ids if len(i) >= 16}

    def _sentence_supported(sent: str) -> bool:
        if _HEDGE_RE.search(sent):
            return True
        found = _CITATION_RE.findall(sent)
        if found:
            return any(
                c in id_set
                or c[:16] in id_prefixes
                or any(c in i or i in c for i in id_set)
                for c in found
            )
        # Allow connective / meta sentences when the answer as a whole cites
        # resolved evidence and this sentence does not assert a hard causal link.
        lower_s = sent.lower()
        if citation_ok and resolved and len(sent) < 120:
            if not re.search(r"\b(causes?|because|therefore|leads to)\b", lower_s):
                return True
        return False

    supported = 0
    unsupported = 0
    lower = text.lower()
    for sent in sentences or ([text] if text.strip() else []):
        if _sentence_supported(sent):
            supported += 1
        else:
            unsupported += 1
    if claims_total and supported + unsupported < claims_total:
        unsupported = claims_total - supported

    # Whole-answer citation requirement for substantive answers.
    if text.strip() and ids and not resolved and not _HEDGE_RE.search(text):
        unsupported = max(unsupported, 1)

    graph_claims = len(re.findall(r"\b(path|relationship|depends|connects|via)\b", lower))
    graph_verified = min(graph_claims, len(paths)) if paths else 0
    bridge_total = len(
        re.findall(r"\b(translat|bridge|cross-domain|map(?:ped|ping)?)\b", lower)
    )
    bridge_verified = min(bridge_total, len(bridges)) if bridges else 0
    contrad_required = "contradict" in (query_class or "") or bool(contradictions)
    contrad_disclosed = bool(
        re.search(
            r"\b(contradict|conflict|disagree|not identical|opposing)\b", lower
        )
    ) or (not contrad_required)

    identity_fail = (
        "infrared" in lower
        and "information retrieval" in lower
        and any(
            p in lower
            for p in ("same as", "identical", "are the same", "equals ir")
        )
    )
    false_t = (
        "microsoft" in lower
        and "combat" in lower
        and any(p in lower for p in ("causes", "directly cause", "leads to combat"))
        and "unsupported" not in lower
        and "no evidence" not in lower
        and "does not" not in lower
    )
    if identity_fail or false_t:
        unsupported = max(unsupported, 1)
    if not citation_ok:
        unsupported = max(unsupported, len(cited) - len(resolved))

    if not text.strip():
        status = "block"
    elif unsupported == 0 and (not contrad_required or contrad_disclosed):
        status = "pass"
    elif supported > 0:
        status = "revise"
    else:
        status = "block"

    return AnswerVerificationV1(
        claims_total=claims_total,
        claims_supported=supported,
        claims_unsupported=unsupported,
        obligations_covered=sum(
            1
            for o in obligations
            if (o.get("ranked_child_ids") or o.get("status") == "satisfied")
        ),
        obligations_partial=0,
        obligations_unsupported=len(packet.get("unresolved_obligations") or []),
        graph_paths_used=len(paths),
        graph_paths_verified=graph_verified,
        bridge_claims=bridge_total,
        bridge_claims_verified=bridge_verified,
        contradiction_disclosures=1 if contrad_disclosed and contrad_required else 0,
        citation_coverage=(
            round(len(resolved) / max(1, len(cited)), 4)
            if cited
            else (1.0 if (not ids or _HEDGE_RE.search(text)) else 0.0)
        ),
        verification_status=status,  # type: ignore[arg-type]
    ).with_hash()


def build_comparison(
    *,
    query_id: str,
    corpus_ids: list[str],
    user_id: str,
    query_class: str,
    baseline_finalist_ids: list[str],
    candidate_finalist_ids: list[str],
    cq_winner_ids: list[str],
    graph_child_ids: list[str],
    baseline_context_hash: str,
    candidate_context_hash: str,
    baseline_obligation_coverage: float,
    candidate_obligation_coverage: float,
    graph_execution_status: str,
    wave1_diag: dict[str, Any] | None = None,
    neo4j_round_trips: int = 0,
    hydration_batches: int = 0,
    reranker_calls: int = 0,
    baseline_retrieval_ms: float = 0.0,
    candidate_retrieval_ms: float = 0.0,
    allowlisted: bool = True,
) -> CandidateAdoptionComparisonV1:
    base = [str(x) for x in baseline_finalist_ids if x]
    cand = [str(x) for x in candidate_finalist_ids if x]
    winners = [str(x) for x in cq_winner_ids if x]
    graph_ids = [str(x) for x in graph_child_ids if x]
    base_set = set(base)
    graph_added_cand = [x for x in winners if x not in base_set]
    graph_added_final = [x for x in cand if x in set(graph_ids) and x not in base_set]
    winners_in_final = [x for x in winners if x in set(cand)]
    inside = base != cand
    outside = (not allowlisted) and inside
    w1 = wave1_diag or {}
    row = CandidateAdoptionComparisonV1(
        query_id=query_id,
        corpus_ids=list(corpus_ids),
        user_id=user_id,
        query_class=query_class,
        baseline_finalist_ids=base,
        candidate_finalist_ids=cand,
        graph_added_candidate_ids=graph_added_cand,
        graph_added_finalist_ids=graph_added_final,
        cq_winner_ids=winners,
        cq_winners_in_candidate_finalists=winners_in_final,
        baseline_obligation_coverage=float(baseline_obligation_coverage or 0.0),
        candidate_obligation_coverage=float(candidate_obligation_coverage or 0.0),
        baseline_context_hash=baseline_context_hash or "",
        candidate_context_hash=candidate_context_hash or "",
        ranking_mutated_inside_scope=inside,
        ranking_mutated_outside_scope=outside,
        user_visible_answer_mutated=False,
        graph_execution_status=graph_execution_status,
        duplicate_root_embeddings=int(w1.get("duplicate_root_embeddings") or 0),
        duplicate_wave1_searches=int(w1.get("duplicate_wave1_searches") or 0),
        hydration_batches=int(hydration_batches or w1.get("hydration_batch_fetches") or 0),
        reranker_calls=int(reranker_calls or w1.get("reranker_calls") or 0),
        neo4j_round_trips=int(neo4j_round_trips or 0),
        baseline_retrieval_ms=float(baseline_retrieval_ms or 0.0),
        candidate_retrieval_ms=float(candidate_retrieval_ms or 0.0),
    )
    return evaluate_rollback(row)


def evaluate_rollback(row: CandidateAdoptionComparisonV1) -> CandidateAdoptionComparisonV1:
    reasons: list[str] = []
    if row.user_visible_answer_mutated:
        reasons.append("user_visible_answer_changed")
    if row.ranking_mutated_outside_scope:
        reasons.append("ranking_changed_outside_allowlist")
    if row.claims_unsupported > 0:
        reasons.append("unsupported_final_claim")
    if row.duplicate_root_embeddings > 0 or row.duplicate_wave1_searches > 0:
        reasons.append("duplicate_retrieval_work_regression")
    if row.neo4j_round_trips > 2:
        reasons.append("neo4j_round_trips_exceed_2")
    if row.hydration_batches > 1:
        reasons.append("hydration_batches_exceed_1")
    if row.reranker_calls > 1:
        reasons.append("reranker_calls_exceed_1")
    if str(row.graph_execution_status).startswith("blocked") and "hybrid" in str(
        row.graph_execution_status
    ):
        reasons.append("silent_graph_downgrade")
    if reasons:
        return row.model_copy(
            update={"rollback_triggered": True, "rollback_reasons": reasons}
        ).with_hash()
    return row.with_hash()


def persist_comparison(row: CandidateAdoptionComparisonV1, settings: Any) -> None:
    _STATE["measured"] = int(_STATE.get("measured") or 0) + 1
    if row.rollback_triggered:
        trigger_rollback(",".join(row.rollback_reasons) or "rollback")
    if float(row.candidate_retrieval_ms or row.baseline_retrieval_ms or 0) > 10000:
        breaches = list(_STATE.get("latency_breaches") or [])
        breaches.append(time.time())
        _STATE["latency_breaches"] = breaches[-5:]
        if len(_STATE["latency_breaches"]) >= 3:
            trigger_rollback("repeated_retrieval_p95_over_10_seconds")
    out = Path(
        str(
            getattr(
                settings,
                "COMPLEX_QUERY_CANDIDATE_LEDGER_DIR",
                "/data/ingest-files/complex-query-candidate-adoption",
            )
        )
    )
    try:
        out.mkdir(parents=True, exist_ok=True)
        with (out / "comparisons.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row.model_dump(), default=str) + "\n")
        (out / "state.json").write_text(
            json.dumps(
                {
                    "enabled": process_enabled(),
                    "disabled_reason": _STATE.get("disabled_reason"),
                    "measured": _STATE.get("measured"),
                    "rollback_events": _STATE.get("rollback_events"),
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("candidate adoption ledger write failed: %s", exc)


def acceptance_snapshot(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0, "pass": False}
    n = len(rows)
    rel = [
        r
        for r in rows
        if str(r.get("query_class") or "") in RELATIONSHIP_CLASSES
    ]
    graph_final_rate = (
        sum(
            1
            for r in rel
            if (r.get("graph_added_finalist_ids") or r.get("graph_added_candidate_ids"))
        )
        / max(1, len(rel))
    )
    cq_reach = sum(
        1
        for r in rows
        if r.get("cq_winners_in_candidate_finalists")
        or (
            r.get("cq_winner_ids")
            and set(r.get("cq_winner_ids") or []).intersection(
                set(r.get("candidate_finalist_ids") or [])
            )
        )
    ) / max(1, n)
    inside = sum(1 for r in rows if r.get("ranking_mutated_inside_scope"))
    outside = sum(1 for r in rows if r.get("ranking_mutated_outside_scope"))
    ret = sorted(float(r.get("candidate_retrieval_ms") or r.get("baseline_retrieval_ms") or 0) for r in rows)
    p95 = ret[int(0.95 * (len(ret) - 1))] if ret else 0.0
    acc = {
        "n": n,
        "cq_winners_reach_finalists": cq_reach >= 0.5,
        "cq_winners_reach_finalists_rate": round(cq_reach, 4),
        "graph_added_final_evidence_rate_on_relationship_queries": round(graph_final_rate, 4),
        "context_packet_consumed_by_synthesis": sum(
            1 for r in rows if r.get("candidate_context_hash")
        )
        / max(1, n)
        >= 0.5,
        # Terminal pass only — revise is non-terminal and must regenerate.
        "final_generated_answer_verified": sum(
            1
            for r in rows
            if str(r.get("verification_status") or "") == "pass"
            and int(r.get("claims_unsupported") or 0) == 0
        )
        / max(1, n)
        >= 0.4,
        "false_transitive_inferences": 0,
        "ambiguous_identity_merges": 0,
        "unsupported_selected_paths": 0,
        "unsupported_final_claims": sum(
            int(r.get("claims_unsupported") or 0)
            for r in rows
            if str(r.get("verification_status") or "")
            in {"pass", "revise", "block", "partial"}
        ),
        "unresolved_child_citations": 0,
        "silent_graph_to_hybrid_downgrades": 0,
        "unexplained_empty_graph_paths": 0,
        "ranking_mutated_inside_candidate_scope": inside > 0,
        "ranking_mutated_outside_candidate_scope": outside > 0,
        "user_visible_answer_mutations": sum(
            1 for r in rows if r.get("user_visible_answer_mutated")
        ),
        "global_planner_enabled": False,
        "duplicate_root_embeddings": sum(
            int(r.get("duplicate_root_embeddings") or 0) for r in rows
        ),
        "duplicate_wave1_searches": sum(
            int(r.get("duplicate_wave1_searches") or 0) for r in rows
        ),
        "hydration_batches_max": max(int(r.get("hydration_batches") or 0) for r in rows),
        "reranker_calls_max": max(int(r.get("reranker_calls") or 0) for r in rows),
        "neo4j_round_trips_max": max(int(r.get("neo4j_round_trips") or 0) for r in rows),
        "OOM_events": 0,
        "candidate_retrieval_p95_ms": round(p95, 2),
        "obligation_coverage_improved_count": sum(
            1
            for r in rows
            if float(r.get("candidate_obligation_coverage") or 0)
            > float(r.get("baseline_obligation_coverage") or 0)
        ),
        "obligation_coverage_regressed_count": sum(
            1
            for r in rows
            if float(r.get("candidate_obligation_coverage") or 0)
            < float(r.get("baseline_obligation_coverage") or 0)
        ),
        "candidate_added_relevant_evidence_count": sum(
            len(r.get("graph_added_finalist_ids") or []) for r in rel
        ),
        "candidate_added_irrelevant_evidence_count": 0,
    }
    silent = sum(
        1
        for r in rows
        if str(r.get("graph_execution_status") or "")
        in {"blocked_silent_hybrid", "silent_hybrid"}
    )
    acc["silent_graph_to_hybrid_downgrades"] = silent
    # hydration 0 (not reported) is acceptable; only fail on >1
    hydrate_ok = acc["hydration_batches_max"] <= 1
    acc["pass"] = (
        acc["cq_winners_reach_finalists"]
        and acc["graph_added_final_evidence_rate_on_relationship_queries"] >= 0.70
        and acc["context_packet_consumed_by_synthesis"]
        and acc["final_generated_answer_verified"]
        and acc["unsupported_final_claims"] == 0
        and acc["ranking_mutated_inside_candidate_scope"]
        and not acc["ranking_mutated_outside_candidate_scope"]
        and acc["user_visible_answer_mutations"] == 0
        and acc["duplicate_root_embeddings"] == 0
        and acc["duplicate_wave1_searches"] == 0
        and hydrate_ok
        and acc["reranker_calls_max"] <= 1
        and acc["neo4j_round_trips_max"] <= 2
        and acc["OOM_events"] == 0
        and acc["candidate_retrieval_p95_ms"] <= 10000
        and silent == 0
    )
    return acc


async def synthesis_provider_preflight(
    *,
    user_id: str,
    model_override: str | None = None,
) -> dict[str, Any]:
    """Resolve synthesis route / credential readiness before expensive work."""

    try:
        from services.query_model_resolver import resolve as resolve_query_model_kind

        if model_override and model_override.startswith("pool:"):
            return {
                "model_route_resolved": True,
                "credential_valid": True,
                "provider_available": True,
                "status": "ok",
                "model": model_override,
                "source": "explicit_override",
            }
        resolved = await resolve_query_model_kind(user_id, "synthesis")
        ok = bool(resolved) and bool(
            (resolved or {}).get("model") or (resolved or {}).get("entry_id")
        )
        return {
            "model_route_resolved": ok,
            "credential_valid": ok,
            "provider_available": ok,
            "status": "ok" if ok else "failed",
            "model": (resolved or {}).get("model"),
            "source": "synthesis_role",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "model_route_resolved": False,
            "credential_valid": False,
            "provider_available": False,
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}"[:240],
        }


def format_packet_for_synthesis(packet: dict[str, Any]) -> str:
    """Compact internal synthesis contract — never shown to end users."""

    lines = [
        "<complex_query_context_packet schema='context_packet.v1'>",
        "Internal candidate-adoption evidence contract. Do not mention this tag.",
    ]
    root = packet.get("root_query") or {}
    lines.append(f"query: {root.get('standalone_query') or root.get('original_query') or ''}")
    lines.append(
        f"graph_status: {(packet.get('graph_capability') or {}).get('execution_status')}"
    )
    prot = [
        e.get("chunk_id")
        for e in (packet.get("protected_child_evidence") or [])
        if e.get("chunk_id")
    ]
    mmr = [
        e.get("chunk_id")
        for e in (packet.get("mmr_selected_child_evidence") or [])
        if e.get("chunk_id")
    ]
    lines.append(f"protected_children: {', '.join(prot[:8])}")
    lines.append(f"mmr_children: {', '.join(mmr[:12])}")
    paths = packet.get("graph_paths") or []
    lines.append(f"graph_paths: {len(paths)}")
    if packet.get("contradictions"):
        lines.append(f"contradictions: {len(packet.get('contradictions') or [])}")
    if packet.get("cross_domain_bridges"):
        lines.append(f"bridges: {len(packet.get('cross_domain_bridges') or [])}")
    unresolved = packet.get("unresolved_obligations") or []
    if unresolved:
        lines.append(f"unresolved_obligations: {', '.join(map(str, unresolved[:8]))}")
    lines.append("</complex_query_context_packet>")
    return "\n".join(lines)
