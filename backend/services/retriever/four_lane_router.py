"""Default-off four-lane document routing ahead of evidence retrieval.

The router is a scope prior. It never emits answer evidence and never changes
chunk scores. Every selected document must still descend through the existing
summary-tree/parent/child path before it can enter the final evidence packet.

Lanes:
  lexical      BM25 over durable title, document summary, and headings
  semantic     legacy document-summary vectors plus authorized digest vectors
  child_rollup aggregate existing child-vector hits by document
  associative  T9.1 query domains/affinity frames against digest ontology

Fusion is deterministic, quota based, and exposes complete per-lane
attribution. Missing lanes spill their seats; weak lanes abstain.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from qdrant_client import models

from models.document_semantic_profile import (
    PROFILE_COLLECTION,
    T91DocumentProfileV1,
)
from models.schemas import SourceChunk
from models.semantic_digest import SemanticDigestV1
from models.semantic_resolution import DomainSignalV1
from services.ingestion.corpus_lexicon import normalize_identity
from services.ingestion.document_semantic_profile import (
    current_profile_recipe_hash,
)
from services.ingestion.semantic_resolution import (
    build_domain_affinity_serve_view,
    resolve_domains,
)
from services.ingestion.tier0 import SHARED_DOCSUM
from services.retriever.tier0_router import DocumentRoute
from services.retriever.query_semantics import CONCEPT_STOP_WORDS, query_tokens
from services.storage.record_status import with_active_records

ROUTER_VERSION = "four_lane_tier0_router.v1"
BRIDGE_SUBQUERY = "what underlying crafts/concepts does this task depend on?"
LANE_ORDER = ("associative", "lexical", "semantic", "child_rollup")
LANE_QUOTAS = {
    "associative": 2,
    "lexical": 1,
    "semantic": 1,
    "child_rollup": 1,
}
LANE_THRESHOLDS = {
    "associative": 0.18,
    "lexical": 0.08,
    "semantic": 0.30,
    "child_rollup": 0.08,
}
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_HASH_RE = re.compile(r"^(?:sha256|rev|projm):[0-9a-f]{64}$")


@dataclass
class DocumentProfile:
    corpus_id: str
    doc_id: str
    title: str = ""
    summary: str = ""
    headings: tuple[str, ...] = ()
    section_ids: tuple[str, ...] = ()
    concepts: tuple[str, ...] = ()
    domains: set[str] = field(default_factory=set)
    frames: set[str] = field(default_factory=set)
    motif_frames: set[str] = field(default_factory=set)
    motifs: set[str] = field(default_factory=set)
    concept_terms: set[str] = field(default_factory=set)
    latent_terms: set[str] = field(default_factory=set)
    digest_parent_ids: set[str] = field(default_factory=set)
    t91_profile_ids: set[str] = field(default_factory=set)
    t91_profile_hashes: set[str] = field(default_factory=set)

    @property
    def lexical_text(self) -> str:
        return " ".join((self.title, self.summary, *self.headings))

    @property
    def has_ontology(self) -> bool:
        return bool(
            self.domains
            or self.frames
            or self.motif_frames
            or self.motifs
            or self.concept_terms
            or self.latent_terms
        )


@dataclass(frozen=True)
class QueryOntology:
    domains: frozenset[str]
    frames: frozenset[str]
    terms: frozenset[str]
    resolver_recipe_hash: str = ""

    @property
    def active(self) -> bool:
        return bool(self.domains or self.frames or self.terms)


def _tokens(value: Any) -> list[str]:
    return [
        token for token in _TOKEN_RE.findall(str(value or "").lower()) if len(token) > 1
    ]


def _normalized_terms(values: Iterable[Any]) -> set[str]:
    return {
        normalized
        for value in values
        if (normalized := normalize_identity(str(value or "")))
    }


def add_bridge_subquery_lane(lanes: Iterable[Any]) -> list[Any]:
    """Append the one fixed bridge probe exactly once when its toggle is ON."""

    from services.retriever.query_plan import QueryLane
    from services.retriever.query_semantics import lexical_terms

    output = [
        lane
        for lane in lanes
        if str(getattr(lane, "lane_id", "")) != "router_bridge_underlying_crafts"
    ]
    output.append(
        QueryLane(
            lane_id="router_bridge_underlying_crafts",
            role="core",
            query=BRIDGE_SUBQUERY,
            dense_text=BRIDGE_SUBQUERY,
            lexical_terms=tuple(lexical_terms(BRIDGE_SUBQUERY)),
            required=False,
        )
    )
    return output


def bm25_document_scores(
    query: str,
    profiles: Iterable[DocumentProfile],
) -> dict[tuple[str, str], float]:
    """Small-corpus BM25 used only at the document routing tier."""

    rows = list(profiles)
    query_terms = _tokens(query)
    if not rows or not query_terms:
        return {}
    documents = [_tokens(profile.lexical_text) for profile in rows]
    avg_len = sum(len(tokens) for tokens in documents) / max(1, len(documents))
    document_frequency = Counter(
        term for tokens in documents for term in set(tokens) if term in set(query_terms)
    )
    raw: dict[tuple[str, str], float] = {}
    for profile, terms in zip(rows, documents):
        counts = Counter(terms)
        score = 0.0
        for term in set(query_terms):
            tf = counts.get(term, 0)
            if not tf:
                continue
            df = document_frequency.get(term, 0)
            inverse = math.log(1.0 + (len(rows) - df + 0.5) / (df + 0.5))
            denominator = tf + 1.2 * (
                1.0 - 0.75 + 0.75 * len(terms) / max(avg_len, 1.0)
            )
            score += inverse * (tf * 2.2 / denominator)
        if score > 0:
            raw[(profile.corpus_id, profile.doc_id)] = score
    ceiling = max(raw.values(), default=0.0)
    return {key: value / ceiling for key, value in raw.items() if ceiling > 0}


def child_rollup_scores(
    chunks: Iterable[SourceChunk],
) -> dict[tuple[str, str], float]:
    """Aggregate child hits without embedding any parent summary."""

    grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
    for chunk in chunks:
        corpus_id = str(chunk.corpus_id or "")
        doc_id = str(chunk.doc_id or "")
        if corpus_id and doc_id:
            grouped[(corpus_id, doc_id)].append(max(0.0, float(chunk.score or 0.0)))
    raw = {
        key: max(scores) + 0.05 * sum(sorted(scores, reverse=True)[1:4])
        for key, scores in grouped.items()
        if scores
    }
    ceiling = max(raw.values(), default=0.0)
    return {key: value / ceiling for key, value in raw.items() if ceiling > 0}


def resolve_query_ontology(
    query: str,
    vocabulary_matches: Iterable[dict[str, Any]] = (),
) -> QueryOntology:
    """Use the T9.1 exact domain resolver and its quarantined affinity view."""

    from models.registry_loader import load_all

    vocabulary_terms = [
        value
        for row in vocabulary_matches
        for value in (
            row.get("term"),
            row.get("canonical_name"),
            *(row.get("aliases") or []),
        )
        if value
    ]
    surfaces = {normalize_identity(query), *_normalized_terms(vocabulary_terms)}
    raw_query_tokens = _tokens(query)
    filtered_query_tokens = query_tokens(query, stop_words=CONCEPT_STOP_WORDS)
    for size in range(1, min(5, len(raw_query_tokens)) + 1):
        surfaces.update(
            " ".join(raw_query_tokens[index : index + size])
            for index in range(len(raw_query_tokens) - size + 1)
        )

    registry = load_all()["domain"]
    known_terms = {
        normalize_identity(term): str(row["domain_id"])
        for row in registry["domains"]
        for term in (row["name"], *(row.get("members") or []))
    }
    matched = sorted(surface for surface in surfaces if surface in known_terms)
    signals = [
        DomainSignalV1(
            schema_version="domain_signal.v1",
            signal_id=f"query-domain:{index}:{normalize_identity(surface)}",
            label=surface,
            signal_kind="section_heading",
            evidence_ref_ids=[f"query:{index}"],
            supporting_claim_ids=[],
        )
        for index, surface in enumerate(matched)
    ]
    resolution = resolve_domains(
        target_artifact_id="query:" + normalize_identity(query),
        signals=signals,
    )
    affinity = build_domain_affinity_serve_view(resolution)
    return QueryOntology(
        domains=frozenset(item.domain_id for item in resolution.assignments),
        frames=frozenset(
            frame_id
            for prior in affinity.priors
            for frame_id in prior.dominant_superframe_ids
        ),
        terms=frozenset(
            _normalized_terms(
                [
                    *vocabulary_terms,
                    *filtered_query_tokens,
                ]
            )
        ),
        resolver_recipe_hash=resolution.resolution_recipe_hash,
    )


def associative_document_scores(
    query: QueryOntology,
    profiles: Iterable[DocumentProfile],
) -> tuple[dict[tuple[str, str], float], dict[tuple[str, str], dict[str, Any]],]:
    scores: dict[tuple[str, str], float] = {}
    traces: dict[tuple[str, str], dict[str, Any]] = {}
    if not query.active:
        return scores, traces
    for profile in profiles:
        if not profile.has_ontology:
            continue
        domain_hits = sorted(query.domains & profile.domains)
        frame_hits = sorted(query.frames & (profile.frames | profile.motif_frames))
        latent_vocabulary = {
            token
            for value in profile.latent_terms
            for token in (value, *_tokens(value))
        }
        concept_vocabulary = {
            token
            for value in profile.concept_terms
            for token in (value, *_tokens(value))
        }
        latent_hits = sorted(query.terms & latent_vocabulary)
        concept_hits = sorted(query.terms & concept_vocabulary)
        domain_score = len(domain_hits) / max(1, len(query.domains))
        frame_score = len(frame_hits) / max(1, len(query.frames))
        term_score = min(1.0, len(set(latent_hits) | set(concept_hits)) / 2.0)
        score = 0.40 * domain_score + 0.35 * frame_score + 0.25 * term_score
        key = (profile.corpus_id, profile.doc_id)
        traces[key] = {
            "domains": domain_hits,
            "frames": frame_hits,
            "latent_terms": latent_hits,
            "concept_terms": concept_hits,
            "motifs": sorted(profile.motifs),
            "digest_parent_ids": sorted(profile.digest_parent_ids),
            "t91_profile_ids": sorted(profile.t91_profile_ids),
            "t91_profile_hashes": sorted(profile.t91_profile_hashes),
            "ontology_present": True,
            "score": round(score, 6),
        }
        if score > 0:
            scores[key] = score
    return scores, traces


def fuse_document_lanes(
    *,
    profiles: dict[tuple[str, str], DocumentProfile],
    lane_scores: dict[str, dict[tuple[str, str], float]],
    associative_traces: dict[tuple[str, str], dict[str, Any]],
    query_ontology_active: bool,
    max_documents: int,
) -> tuple[list[DocumentRoute], dict[str, Any]]:
    """Quota reservation with threshold spillover and associative protection."""

    keys = {key for scores in lane_scores.values() for key in scores}
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for key in keys:
        profile = profiles.get(key)
        if profile is None:
            continue
        contributions = {
            lane: float(scores[key])
            for lane, scores in lane_scores.items()
            if key in scores
        }
        divergent = bool(
            query_ontology_active
            and profile.has_ontology
            and contributions.get("lexical", 0.0) > 0
            and contributions.get("associative", 0.0) == 0
        )
        effective_contributions = dict(contributions)
        if divergent:
            effective_contributions["lexical"] = (
                effective_contributions.get("lexical", 0.0) * 0.55
            )
        fused = sum(
            {
                "lexical": 0.25,
                "semantic": 0.25,
                "child_rollup": 0.20,
                "associative": 0.30,
            }.get(lane, 0.0)
            * score
            for lane, score in effective_contributions.items()
        )
        candidates[key] = {
            "contributions": contributions,
            "effective_contributions": effective_contributions,
            "fused": fused,
            "divergent_profile": divergent,
        }

    selected: list[tuple[str, str]] = []
    seat_owner: dict[tuple[str, str], str] = {}
    lane_seats: dict[str, list[str]] = {lane: [] for lane in LANE_ORDER}
    spillover = 0
    for lane in LANE_ORDER:
        eligible = sorted(
            (
                key
                for key, row in candidates.items()
                if key not in selected
                and row["effective_contributions"].get(lane, 0.0)
                >= LANE_THRESHOLDS[lane]
            ),
            key=lambda key: (
                -candidates[key]["effective_contributions"][lane],
                -candidates[key]["fused"],
                key,
            ),
        )
        taken = eligible[: LANE_QUOTAS[lane]]
        selected.extend(taken)
        for key in taken:
            seat_owner[key] = lane
            lane_seats[lane].append(f"{key[0]}:{key[1]}")
        spillover += max(0, LANE_QUOTAS[lane] - len(taken))

    fill_budget = max(0, int(max_documents) - len(selected))
    remaining = sorted(
        (
            key
            for key, row in candidates.items()
            if key not in selected
            and any(
                row["effective_contributions"].get(lane, 0.0) >= LANE_THRESHOLDS[lane]
                for lane in LANE_ORDER
            )
        ),
        key=lambda key: (-candidates[key]["fused"], key),
    )
    for key in remaining[:fill_budget]:
        selected.append(key)
        seat_owner[key] = "spillover"
    selected = selected[: max(1, int(max_documents))]

    routes: list[DocumentRoute] = []
    rows: list[dict[str, Any]] = []
    for key in selected:
        profile = profiles[key]
        candidate = candidates[key]
        trace = {
            "router_version": ROUTER_VERSION,
            "seat_owner": seat_owner[key],
            "lane_scores": {
                lane: round(float(candidate["contributions"].get(lane, 0.0)), 6)
                for lane in LANE_ORDER
            },
            "effective_lane_scores": {
                lane: round(
                    float(candidate["effective_contributions"].get(lane, 0.0)),
                    6,
                )
                for lane in LANE_ORDER
            },
            "associative": associative_traces.get(key, {}),
            "divergent_profile_demoted": candidate["divergent_profile"],
            "fused_score": round(candidate["fused"], 6),
        }
        routes.append(
            DocumentRoute(
                lane_id="",
                corpus_id=profile.corpus_id,
                doc_id=profile.doc_id,
                score=float(candidate["fused"]),
                title=profile.title,
                summary=profile.summary,
                concepts=profile.concepts,
                section_ids=profile.section_ids,
                routing_trace=trace,
            )
        )
        rows.append(
            {
                "corpus_id": profile.corpus_id,
                "doc_id": profile.doc_id,
                "title": profile.title,
                **trace,
            }
        )
    return routes, {
        "lane_seats": lane_seats,
        "spillover_seats": spillover,
        "routes": rows,
    }


def _valid_digest_payload(payload: dict[str, Any]) -> bool:
    required = (
        "corpus_id",
        "doc_id",
        "parent_id",
        "artifact_id",
        "artifact_revision_id",
        "projection_manifest_id",
        "projection_profile_hash",
        "source_cache_key",
        "source_job_id",
        "source_version_id",
        "schema_hash",
        "prompt_hash",
        "output_hash",
        "projected_payload_hash",
    )
    return bool(
        payload.get("chunk_type") == "semantic_digest"
        and payload.get("projection_role") == "semantic_digest"
        and all(payload.get(key) for key in required)
        and all(
            _HASH_RE.fullmatch(str(payload.get(key) or ""))
            for key in (
                "artifact_revision_id",
                "projection_manifest_id",
                "projection_profile_hash",
                "schema_hash",
                "prompt_hash",
                "output_hash",
                "projected_payload_hash",
            )
        )
    )


def _source_versions_by_document(
    documents: Iterable[dict[str, Any]],
) -> dict[tuple[str, str], str]:
    from services.ingestion.semantic_digest_claim_inputs import (
        document_source_version_id,
    )

    source_versions: dict[tuple[str, str], str] = {}
    for row in documents:
        key = (
            str(row.get("corpus_id") or ""),
            str(row.get("doc_id") or ""),
        )
        try:
            source_versions[key] = document_source_version_id(row)
        except Exception:
            # Missing/noncanonical source closure rejects only that document's
            # digest authorization; it must not take down the routing request.
            continue
    return source_versions


def apply_t91_document_profiles(
    *,
    profiles: dict[tuple[str, str], DocumentProfile],
    rows: Iterable[dict[str, Any]],
    current_source_versions: dict[tuple[str, str], str],
    expected_profile_recipe_hash: str,
) -> dict[str, int]:
    """Attach only valid, current T9.1 rows to associative profile fields."""

    diagnostics = {"seen": 0, "valid": 0, "current": 0, "applied": 0}
    for row in rows:
        diagnostics["seen"] += 1
        try:
            t91_profile = T91DocumentProfileV1.model_validate(row)
        except Exception:
            continue
        diagnostics["valid"] += 1
        key = (t91_profile.corpus_id, t91_profile.doc_id)
        profile = profiles.get(key)
        if (
            profile is None
            or t91_profile.source_version_id != current_source_versions.get(key)
            or t91_profile.profile_recipe_hash != expected_profile_recipe_hash
        ):
            continue
        diagnostics["current"] += 1
        profile.domains.update(t91_profile.domain_ids)
        profile.frames.update(t91_profile.superframe_ids)
        profile.motifs.update(t91_profile.motif_ids)
        profile.motif_frames.update(
            frame_id
            for motif in t91_profile.motif_evidence
            for frame_id in motif.frame_ids
        )
        profile.concept_terms.update(t91_profile.concept_terms)
        profile.t91_profile_ids.add(t91_profile.profile_id)
        profile.t91_profile_hashes.add(t91_profile.profile_hash)
        diagnostics["applied"] += 1
    return diagnostics


class FourLaneDocumentRouter:
    async def _load_profiles(
        self,
        *,
        db: Any,
        corpus_ids: list[str],
    ) -> dict[tuple[str, str], DocumentProfile]:
        if db is None or not corpus_ids:
            return {}
        documents, parent_profiles, jobs, t91_profile_rows = await _gather(
            db["documents"]
            .find(
                with_active_records({"corpus_id": {"$in": corpus_ids}}),
                {"_id": 0},
            )
            .to_list(length=None),
            db["parent_chunks"]
            .aggregate(
                [
                    {"$match": with_active_records({"corpus_id": {"$in": corpus_ids}})},
                    {
                        "$project": {
                            "_id": 0,
                            "corpus_id": 1,
                            "doc_id": 1,
                            "heading_path": 1,
                            "summary": 1,
                        }
                    },
                    {
                        "$unwind": {
                            "path": "$heading_path",
                            "preserveNullAndEmptyArrays": True,
                        }
                    },
                    {
                        "$group": {
                            "_id": {
                                "corpus_id": "$corpus_id",
                                "doc_id": "$doc_id",
                            },
                            "headings": {"$addToSet": "$heading_path"},
                            "summary": {"$first": "$summary"},
                        }
                    },
                ],
                allowDiskUse=True,
            )
            .to_list(length=None),
            db["semantic_digest_jobs"]
            .find(
                {
                    "corpus_id": {"$in": corpus_ids},
                    "status": "succeeded",
                },
                {
                    "_id": 0,
                    "corpus_id": 1,
                    "doc_id": 1,
                    "parent_id": 1,
                    "cache_key": 1,
                    "job_id": 1,
                },
            )
            .to_list(length=None),
            db[PROFILE_COLLECTION]
            .find(
                {
                    "schema_version": "t91_document_profile.v1",
                    "corpus_id": {"$in": corpus_ids},
                    "assignment_state": "candidate",
                    "canonical_write": False,
                },
                {"_id": 0},
            )
            .to_list(length=None),
        )
        profiles: dict[tuple[str, str], DocumentProfile] = {}
        for row in documents:
            corpus_id = str(row.get("corpus_id") or "")
            doc_id = str(row.get("doc_id") or "")
            if not corpus_id or not doc_id:
                continue
            doc_profile = row.get("doc_profile") or {}
            profiles[(corpus_id, doc_id)] = DocumentProfile(
                corpus_id=corpus_id,
                doc_id=doc_id,
                title=str(
                    row.get("original_filename")
                    or row.get("filename")
                    or row.get("title")
                    or doc_profile.get("title")
                    or ""
                ),
                summary=str(doc_profile.get("summary") or ""),
                section_ids=tuple(
                    str(value)
                    for value in (doc_profile.get("section_ids") or [])
                    if value
                ),
                concepts=tuple(
                    str(value) for value in (doc_profile.get("concepts") or []) if value
                ),
            )
        headings: dict[tuple[str, str], set[str]] = defaultdict(set)
        summaries: dict[tuple[str, str], list[str]] = defaultdict(list)
        for row in parent_profiles:
            identity = row.get("_id") or {}
            key = (
                str(identity.get("corpus_id") or ""),
                str(identity.get("doc_id") or ""),
            )
            if key not in profiles:
                continue
            headings[key].update(
                str(value) for value in (row.get("headings") or []) if value
            )
            if row.get("summary"):
                summaries[key].append(str(row["summary"]))
        for key, profile in profiles.items():
            profile.headings = tuple(sorted(headings.get(key, set())))
            if not profile.summary and summaries.get(key):
                profile.summary = " ".join(summaries[key][:8])

        current_source_versions = _source_versions_by_document(documents)
        apply_t91_document_profiles(
            profiles=profiles,
            rows=t91_profile_rows,
            current_source_versions=current_source_versions,
            expected_profile_recipe_hash=current_profile_recipe_hash(),
        )

        cache_keys = sorted(
            {str(row.get("cache_key") or "") for row in jobs if row.get("cache_key")}
        )
        outboxes = (
            await db["projection_outbox"]
            .find(
                {
                    "schema_version": "projection_outbox.v2",
                    "state": "applied",
                    "source.source_id": {"$in": cache_keys},
                },
                {
                    "_id": 0,
                    "source": 1,
                    "point_id": 1,
                    "application_receipt": 1,
                },
            )
            .to_list(length=None)
            if cache_keys
            else []
        )
        outbox_by_cache = {
            str((row.get("source") or {}).get("source_id") or ""): row
            for row in outboxes
            if (row.get("application_receipt") or {}).get("reconciled") is True
            and str((row.get("application_receipt") or {}).get("point_id") or "")
            == str(row.get("point_id") or "")
        }
        caches = (
            await db["semantic_digest_cache"]
            .find(
                {
                    "_id": {"$in": cache_keys},
                    "status": "accepted_cache",
                    "serving_eligible": {"$ne": False},
                },
                {"_id": 1, "digest": 1},
            )
            .to_list(length=None)
            if cache_keys
            else []
        )
        cache_by_key = {str(row["_id"]): row for row in caches}
        for job in jobs:
            key = (str(job.get("corpus_id") or ""), str(job.get("doc_id") or ""))
            profile = profiles.get(key)
            cache_key = str(job.get("cache_key") or "")
            cache = cache_by_key.get(cache_key)
            outbox = outbox_by_cache.get(cache_key)
            source = (outbox or {}).get("source") or {}
            if (
                profile is None
                or cache is None
                or outbox is None
                or str(source.get("corpus_id") or "") != key[0]
                or str(source.get("doc_id") or "") != key[1]
                or str(source.get("ownership_id") or "") != str(job.get("job_id") or "")
                or str(source.get("source_version_id") or "")
                != current_source_versions.get(key)
            ):
                continue
            try:
                digest = SemanticDigestV1.model_validate(cache.get("digest"))
            except Exception:
                continue
            if digest.parent_id != str(job.get("parent_id") or ""):
                continue
            if str(source.get("parent_id") or "") != digest.parent_id:
                continue
            profile.digest_parent_ids.add(digest.parent_id)
            profile.domains.update(
                item.registry_id
                for item in digest.domain_proposals
                if item.assignment_state in {"candidate", "corroborated", "validated"}
            )
            profile.frames.update(
                item.frame_id
                for item in digest.frame_proposals
                if item.assignment_state in {"candidate", "corroborated", "validated"}
            )
            for motif in digest.motif_proposals:
                profile.motifs.add(normalize_identity(motif.proposed_label))
                profile.motif_frames.update(motif.frame_sequence)
            for latent in digest.latent_concepts:
                if latent.assignment_state in {
                    "candidate",
                    "corroborated",
                    "validated",
                }:
                    profile.latent_terms.update(
                        _normalized_terms([latent.preferred_label, *latent.aliases])
                    )
        return profiles

    async def _digest_semantic_scores(
        self,
        *,
        db: Any,
        qdrant_client: Any,
        vector: list[float] | None,
        corpus_ids: list[str],
    ) -> tuple[dict[tuple[str, str], float], dict[str, int]]:
        diagnostics = {
            "seen": 0,
            "payload_valid": 0,
            "outbox_authorized": 0,
        }
        if vector is None or qdrant_client is None or db is None:
            return {}, diagnostics
        response = await qdrant_client.query_points(
            collection_name=SHARED_DOCSUM,
            query=vector,
            using="dense",
            query_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="corpus_id",
                        match=models.MatchAny(any=corpus_ids),
                    ),
                    models.FieldCondition(
                        key="chunk_type",
                        match=models.MatchValue(value="semantic_digest"),
                    ),
                ]
            ),
            limit=96,
            with_payload=True,
        )
        hits = list(response.points or [])
        diagnostics["seen"] = len(hits)
        shaped = [hit for hit in hits if _valid_digest_payload(hit.payload or {})]
        diagnostics["payload_valid"] = len(shaped)
        point_ids = [str(hit.id) for hit in shaped]
        receipts = (
            await db["projection_outbox"]
            .find(
                {
                    "point_id": {"$in": point_ids},
                    "state": "applied",
                    "schema_version": "projection_outbox.v2",
                },
                {
                    "_id": 0,
                    "point_id": 1,
                    "manifest_id": 1,
                    "projected_payload_hash": 1,
                    "source": 1,
                    "application_receipt": 1,
                },
            )
            .to_list(length=None)
            if point_ids
            else []
        )
        receipts_by_point = {str(row.get("point_id") or ""): row for row in receipts}
        document_terms = sorted(
            {
                (
                    str((hit.payload or {}).get("corpus_id") or ""),
                    str((hit.payload or {}).get("doc_id") or ""),
                )
                for hit in shaped
            }
        )
        documents = (
            await db["documents"]
            .find(
                with_active_records(
                    {
                        "$or": [
                            {"corpus_id": corpus_id, "doc_id": doc_id}
                            for corpus_id, doc_id in document_terms
                        ]
                    }
                ),
                {"_id": 0},
            )
            .to_list(length=None)
            if document_terms
            else []
        )
        source_versions = _source_versions_by_document(documents)
        authorized: set[str] = set()
        for hit in shaped:
            point_id = str(hit.id)
            payload = hit.payload or {}
            row = receipts_by_point.get(point_id) or {}
            receipt = row.get("application_receipt") or {}
            source = row.get("source") or {}
            key = (str(payload["corpus_id"]), str(payload["doc_id"]))
            if (
                row
                and str(row.get("manifest_id") or "")
                == str(payload["projection_manifest_id"])
                and str(row.get("projected_payload_hash") or "")
                == str(payload["projected_payload_hash"])
                and str(source.get("source_id") or "")
                == str(payload["source_cache_key"])
                and str(source.get("ownership_id") or "")
                == str(payload["source_job_id"])
                and str(source.get("source_version_id") or "")
                == str(payload["source_version_id"])
                == source_versions.get(key)
                and receipt.get("reconciled") is True
                and str(receipt.get("point_id") or "") == point_id
                and str(receipt.get("target_collection") or "") == SHARED_DOCSUM
                and str(receipt.get("vector_name") or "") == "dense"
                and str(receipt.get("projected_payload_hash") or "")
                == str(payload["projected_payload_hash"])
            ):
                authorized.add(point_id)
        diagnostics["outbox_authorized"] = len(authorized)
        scores: dict[tuple[str, str], float] = {}
        for hit in shaped:
            if str(hit.id) not in authorized:
                continue
            payload = hit.payload or {}
            key = (str(payload["corpus_id"]), str(payload["doc_id"]))
            scores[key] = max(scores.get(key, 0.0), float(hit.score or 0.0))
        return scores, diagnostics

    async def route_lanes(
        self,
        *,
        query_by_lane: dict[str, str],
        lane_vectors: dict[str, list[float] | None],
        child_hits_by_lane: dict[str, list[SourceChunk]],
        legacy_semantic_routes: dict[str, list[DocumentRoute]],
        corpus_ids: list[str],
        vocabulary_matches: Iterable[dict[str, Any]],
        db: Any,
        qdrant_client: Any,
        max_documents: int = 6,
    ) -> tuple[dict[str, list[DocumentRoute]], dict[str, Any]]:
        profiles = await self._load_profiles(db=db, corpus_ids=corpus_ids)
        output: dict[str, list[DocumentRoute]] = {}
        lane_diagnostics: dict[str, Any] = {}
        for lane_id, query in query_by_lane.items():
            semantic = {
                (route.corpus_id, route.doc_id): float(route.score)
                for route in legacy_semantic_routes.get(lane_id, [])
            }
            digest_scores, digest_diagnostics = await self._digest_semantic_scores(
                db=db,
                qdrant_client=qdrant_client,
                vector=lane_vectors.get(lane_id),
                corpus_ids=corpus_ids,
            )
            for key, score in digest_scores.items():
                semantic[key] = max(semantic.get(key, 0.0), score)
            ontology = resolve_query_ontology(query, vocabulary_matches)
            associative, associative_traces = associative_document_scores(
                ontology,
                profiles.values(),
            )
            lane_scores = {
                "lexical": bm25_document_scores(query, profiles.values()),
                "semantic": semantic,
                "child_rollup": child_rollup_scores(
                    child_hits_by_lane.get(lane_id, [])
                ),
                "associative": associative,
            }
            routes, diagnostics = fuse_document_lanes(
                profiles=profiles,
                lane_scores=lane_scores,
                associative_traces=associative_traces,
                query_ontology_active=ontology.active,
                max_documents=max_documents,
            )
            output[lane_id] = [
                DocumentRoute(
                    lane_id=lane_id,
                    corpus_id=route.corpus_id,
                    doc_id=route.doc_id,
                    score=route.score,
                    title=route.title,
                    summary=route.summary,
                    concepts=route.concepts,
                    section_ids=route.section_ids,
                    routing_trace=route.routing_trace,
                )
                for route in routes
            ]
            lane_diagnostics[lane_id] = {
                **diagnostics,
                "query_ontology": {
                    "domains": sorted(ontology.domains),
                    "frames": sorted(ontology.frames),
                    "terms": sorted(ontology.terms),
                    "resolver_recipe_hash": ontology.resolver_recipe_hash,
                },
                "candidate_counts": {
                    lane: len(scores) for lane, scores in lane_scores.items()
                },
                "semantic_digest_authorization": digest_diagnostics,
            }
        return output, {
            "enabled": True,
            "version": ROUTER_VERSION,
            "profile_count": len(profiles),
            "lanes": lane_diagnostics,
            "routes": {
                lane_id: [
                    {
                        "corpus_id": route.corpus_id,
                        "doc_id": route.doc_id,
                        "score": round(route.score, 6),
                        "title": route.title,
                        "routing_trace": dict(route.routing_trace or {}),
                    }
                    for route in routes
                ]
                for lane_id, routes in output.items()
            },
            "routed_doc_count": len(
                {
                    (route.corpus_id, route.doc_id)
                    for routes in output.values()
                    for route in routes
                }
            ),
        }


async def _gather(*awaitables):
    import asyncio

    return await asyncio.gather(*awaitables)


four_lane_document_router = FourLaneDocumentRouter()
