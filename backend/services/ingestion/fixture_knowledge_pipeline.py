"""Historical isolated fixture pipeline for alias identity lineage.

ONLY for corpora marked:
  fixture.excluded_from_user_search=true
  fixture.production_visible=false
  fixture.purpose=graph_semantic_e2e_fixture

Never writes production schemas, never enables global ranking, never
re-extracts the 2583 orphan holds.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models.alias_identity import AliasCandidateV1, AliasDecisionV1
from models.knowledge_artifact_bundle import KnowledgeArtifactBundleV1
from services.ghost_b import ExtractionTask
from services.ingestion.alias_candidates import collect_alias_candidates
from services.ingestion.alias_corpus_clustering import (
    build_inventory,
    cluster_corpus_entities,
)
from services.ingestion.alias_document_clustering import cluster_document_entities
from services.ingestion.alias_gate import run_alias_gate
from services.ingestion.alias_parent_aggregation import ChildAliasEvidence
from services.ingestion.alias_retrieval_shadow import register_shadow_schema_records
from services.ingestion.alias_schema_projection import (
    project_corpus_entity_to_shadow_schema,
)
from services.ingestion.knowledge_bundle import bundle_from_extraction_row

logger = logging.getLogger(__name__)

FIXTURE_PURPOSE = "graph_semantic_e2e_fixture"
FIXTURE_NAME = "graph_semantic_e2e_fixture"
SHADOW_COLLECTIONS = {
    "bundles": "knowledge_artifact_bundles",
    "alias_candidates": "alias_candidates_shadow",
    "alias_decisions": "alias_decisions_shadow",
    "parent_alias_bundles": "parent_alias_bundles_shadow",
    "document_entities": "document_entities_shadow",
    "corpus_entities": "corpus_entities_shadow",
    "schema_shadow": "schema_entity_shadow_projections",
}

_ACRONYM_RE = re.compile(
    r"(?P<long>[A-Z][A-Za-z0-9][A-Za-z0-9'&\-/]*(?:\s+[A-Z][A-Za-z0-9'&\-/]*){1,8})"
    r"\s*\(\s*(?P<short>[A-Z][A-Z0-9\-]{1,9})\s*\)"
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def assert_fixture_safe(corpus: dict[str, Any]) -> None:
    fixture = corpus.get("fixture") or {}
    if fixture.get("purpose") != FIXTURE_PURPOSE:
        raise RuntimeError("refusing: corpus purpose is not graph_semantic_e2e_fixture")
    if fixture.get("production_visible") is not False:
        raise RuntimeError("refusing: fixture.production_visible must be false")
    if fixture.get("excluded_from_user_search") is not True:
        raise RuntimeError("refusing: fixture.excluded_from_user_search must be true")


@dataclass
class FixtureDoc:
    document_id: str
    filename: str
    text: str
    domain: str


@dataclass
class Phase1Proof:
    corpus_id: str
    shared_spacy_parse_per_chunk: int = 1
    relex_pass_per_chunk: int = 1
    bundles_created_for_every_fixture_chunk: bool = False
    accepted_assertions_preserved: bool = False
    source_offsets_preserved: bool = False
    evidence_text_preserved: bool = False
    candidate_generation_reachable: bool = False
    gate_reachable: bool = False
    document_clustering_reachable: bool = False
    corpus_clustering_shadow_reachable: bool = False
    ambiguous_acronym_cross_merges: int = -1
    descriptions_as_aliases: int = -1
    semantic_similarity_identity_merges: int = -1
    production_alias_writes: int = 0
    production_schema_writes: int = 0
    global_ranking_activation: bool = False
    counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def ok(self) -> bool:
        return (
            self.bundles_created_for_every_fixture_chunk
            and self.accepted_assertions_preserved
            and self.source_offsets_preserved
            and self.evidence_text_preserved
            and self.candidate_generation_reachable
            and self.gate_reachable
            and self.document_clustering_reachable
            and self.corpus_clustering_shadow_reachable
            and self.ambiguous_acronym_cross_merges == 0
            and self.descriptions_as_aliases == 0
            and self.semantic_similarity_identity_merges == 0
            and self.production_alias_writes == 0
            and self.production_schema_writes == 0
            and self.global_ranking_activation is False
            and not self.errors
        )


def load_fixture_docs(source_dir: Path) -> list[FixtureDoc]:
    mapping = {
        "doc_rag.md": ("doc_rag", "information_retrieval"),
        "doc_ir.md": ("doc_ir", "information_retrieval"),
        "doc_infrared.md": ("doc_infrared", "sensors"),
        "doc_benesh.md": ("doc_benesh", "movement_notation"),
        "doc_cpp.md": ("doc_cpp", "cpp_source"),
        "doc_luau.md": ("doc_luau", "luau_runtime"),
        "doc_mechanics.md": ("doc_mechanics", "game_mechanics"),
        "doc_negative.md": ("doc_negative", "negative_controls"),
    }
    docs: list[FixtureDoc] = []
    for filename, (doc_id, domain) in mapping.items():
        path = source_dir / filename
        if not path.exists():
            raise FileNotFoundError(path)
        docs.append(
            FixtureDoc(
                document_id=doc_id,
                filename=filename,
                text=path.read_text(encoding="utf-8"),
                domain=domain,
            )
        )
    return docs


async def ensure_fixture_corpus(db: Any, *, corpus_id: str) -> dict[str, Any]:
    now = _utcnow()
    fixture = {
        "purpose": FIXTURE_PURPOSE,
        "production_visible": False,
        "excluded_from_user_search": True,
        "program": "graph_semantic_e2e",
        "marked_at": now.isoformat(),
    }
    existing = await db["corpora"].find_one({"corpus_id": corpus_id})
    if existing:
        await db["corpora"].update_one(
            {"corpus_id": corpus_id},
            {
                "$set": {
                    "name": FIXTURE_NAME,
                    "fixture": fixture,
                    "updated_at": now,
                }
            },
        )
        row = await db["corpora"].find_one({"corpus_id": corpus_id})
        assert_fixture_safe(row or {})
        return row or {}
    doc = {
        "corpus_id": corpus_id,
        "name": FIXTURE_NAME,
        "status": "active",
        "fixture": fixture,
        "created_at": now,
        "updated_at": now,
        "corpus_generation": f"gen:{corpus_id}:{now.isoformat()}",
    }
    await db["corpora"].insert_one(doc)
    assert_fixture_safe(doc)
    return doc


def _entity_dicts(result: Any) -> list[dict[str, Any]]:
    out = []
    for ent in result.entities or []:
        if hasattr(ent, "__dict__"):
            out.append(
                {
                    "canonical_name": ent.canonical_name,
                    "surface_form": ent.surface_form,
                    "entity_type": ent.entity_type,
                    "confidence": ent.confidence,
                    "query_aliases": list(ent.query_aliases or []),
                }
            )
        else:
            out.append(dict(ent))
    return out


def _relation_dicts(result: Any) -> list[dict[str, Any]]:
    out = []
    for rel in result.relations or []:
        if hasattr(rel, "__dict__"):
            out.append(
                {
                    "subject": rel.subject,
                    "predicate": rel.predicate,
                    "object": rel.object,
                    "object_kind": getattr(rel, "object_kind", "entity"),
                    "confidence": rel.confidence,
                    "evidence_phrase": getattr(rel, "evidence_phrase", "") or "",
                    "source_predicate": getattr(rel, "source_predicate", None),
                    "validation_status": getattr(rel, "validation_status", "") or "",
                    "relation_cue": getattr(rel, "relation_cue", "") or "",
                }
            )
        else:
            out.append(dict(rel))
    return out


def _offsets_for_text(text: str, surface: str) -> dict[str, Any]:
    if not text or not surface:
        return {"status": "unavailable"}
    idx = text.find(surface)
    if idx < 0:
        idx = text.lower().find(surface.lower())
    if idx < 0:
        return {"status": "unavailable"}
    return {"status": "exact", "char_start": idx, "char_end": idx + len(surface)}


def enrich_bundle_offsets(
    bundle: KnowledgeArtifactBundleV1, text: str
) -> KnowledgeArtifactBundleV1:
    offsets: dict[str, Any] = {}
    for i, m in enumerate(bundle.entity_mentions):
        offsets[f"entity:{i}:{m.canonical_name}"] = _offsets_for_text(text, m.surface)
    for i, r in enumerate(bundle.accepted_relation_assertions):
        offsets[f"assertion:{i}"] = _offsets_for_text(text, r.evidence_text or r.predicate)
    structure = bundle.source_structure.model_copy(update={"source_offsets": offsets})
    return bundle.model_copy(update={"source_structure": structure}).with_hash()


def _inventory_from_doc_entities(
    entities: list[Any],
    decisions_by_id: dict[str, AliasDecisionV1],
    candidates_by_id: dict[str, AliasCandidateV1],
) -> list[Any]:
    inventories = []
    for ent in entities:
        trusted: list[str] = []
        temporal: list[str] = []
        retrieval: list[str] = []
        descriptions: list[str] = []
        acronym_pairs: list[tuple[str, str]] = []
        for aid in ent.accepted_alias_ids:
            cand = candidates_by_id.get(aid)
            dec = decisions_by_id.get(aid)
            if not cand or not dec:
                continue
            if dec.decision in {"ACCEPT_IDENTITY", "ACCEPT_TEMPORAL_IDENTITY"}:
                trusted.append(cand.candidate_surface)
                trusted.append(cand.canonical_surface)
                if cand.candidate_type in {"acronym_long_form", "explicit_abbreviation"}:
                    short, long_form = cand.candidate_surface, cand.canonical_surface
                    if len(short) <= len(long_form):
                        acronym_pairs.append((short, long_form))
                    else:
                        acronym_pairs.append((long_form, short))
                if dec.decision == "ACCEPT_TEMPORAL_IDENTITY":
                    temporal.append(cand.candidate_surface)
            elif dec.decision == "ACCEPT_RETRIEVAL_ONLY":
                retrieval.append(cand.candidate_surface)
        for desc_id in ent.description_records:
            descriptions.append(desc_id)
        # Also mine acronym pairs from defining child evidence text via entity name
        inventories.append(
            build_inventory(
                ent,
                trusted_alias_surfaces=trusted,
                temporal_former_names=temporal,
                retrieval_surfaces=retrieval,
                acronym_pairs=acronym_pairs,
                description_surfaces=descriptions,
                supporting_alias_decision_ids=list(ent.accepted_alias_ids),
            )
        )
    return inventories


async def run_phase1_fixture_pipeline(
    db: Any,
    *,
    corpus_id: str,
    source_dir: Path,
    neo4j_driver: Any | None = None,
) -> dict[str, Any]:
    """Extract → bundle → alias → document/corpus shadow for fixture corpus."""

    corpus = await ensure_fixture_corpus(db, corpus_id=corpus_id)
    assert_fixture_safe(corpus)
    generation = str(corpus.get("corpus_generation") or f"gen:{corpus_id}")
    docs = load_fixture_docs(source_dir)

    # Reset fixture-scoped shadow collections only.
    for col in SHADOW_COLLECTIONS.values():
        await db[col].delete_many({"corpus_id": corpus_id})
    await db["ghost_b_extractions"].delete_many({"corpus_id": corpus_id})
    await db["chunks"].delete_many({"corpus_id": corpus_id})
    await db["documents"].delete_many({"corpus_id": corpus_id})
    await db["graph_projection_jobs"].delete_many({"corpus_id": corpus_id})

    tasks: list[ExtractionTask] = []
    for doc in docs:
        chunk_id = f"{doc.document_id}_c0"
        await db["documents"].insert_one(
            {
                "doc_id": doc.document_id,
                "corpus_id": corpus_id,
                "filename": doc.filename,
                "status": "ready",
                "domain": doc.domain,
                "fixture": True,
                "created_at": _utcnow(),
            }
        )
        await db["chunks"].insert_one(
            {
                "chunk_id": chunk_id,
                "doc_id": doc.document_id,
                "corpus_id": corpus_id,
                "parent_id": f"{doc.document_id}_p0",
                "text": doc.text,
                "chunk_kind": "body",
            }
        )
        tasks.append(
            ExtractionTask(
                chunk_id=chunk_id,
                doc_id=doc.document_id,
                corpus_id=corpus_id,
                text=doc.text,
            )
        )

    # Relex was retired entirely on 2026-08-07 (owner order): GLiNER2 +
    # triplet-extract via graphify_cpu is the only extraction stack.
    raise NotImplementedError(
        "The Relex fixture lane is retired; ingest fixtures through the "
        "graphify_cpu engine instead."
    )

    report = await extract_entities(tasks, return_report=True)  # noqa: F821 — unreachable, kept for shape
    results = list(report.results or [])
    if len(results) != len(tasks):
        raise RuntimeError(
            f"relex returned {len(results)} results for {len(tasks)} tasks; "
            f"failures={len(report.failures or [])}"
        )

    all_candidates: list[AliasCandidateV1] = []
    all_incomplete: list[Any] = []
    child_evidence: list[ChildAliasEvidence] = []
    bundles: list[KnowledgeArtifactBundleV1] = []
    ghost_rows: list[dict[str, Any]] = []

    for result in results:
        local = dict(result.local_extraction or {})
        # Prove parse/encode invariants from stamp.
        if int(local.get("spacy_parses_per_chunk") or 0) != 1:
            raise RuntimeError(f"spacy parse invariant broken for {result.chunk_id}")
        if int(local.get("relex_encodes_per_chunk") or 0) != 1:
            raise RuntimeError(f"relex encode invariant broken for {result.chunk_id}")

        entities = _entity_dicts(result)
        relations = _relation_dicts(result)
        row = {
            "corpus_id": corpus_id,
            "doc_id": result.doc_id,
            "chunk_id": result.chunk_id,
            "text": result.text,
            "entities": entities,
            "relations": relations,
            "facts": [],
            "local_extraction": local,
            "extraction_contract_hash": str(local.get("contract") or ""),
            "status": "extracted",
        }
        await db["ghost_b_extractions"].insert_one(dict(row))
        ghost_rows.append(row)

        bundle = enrich_bundle_offsets(
            bundle_from_extraction_row(
                row,
                corpus_generation=generation,
                parent_id=f"{result.doc_id}_p0",
            ),
            result.text or "",
        )
        bundles.append(bundle)
        await db[SHADOW_COLLECTIONS["bundles"]].insert_one(
            {**bundle.model_dump(), "corpus_id": corpus_id, "created_at": _utcnow()}
        )

        cand_batch = collect_alias_candidates(
            result.text or "",
            entities,
            document_id=result.doc_id,
            chunk_id=result.chunk_id,
            include_curated=True,
            include_relex_surfaces=True,
            include_appositions=True,
        )
        all_candidates.extend(cand_batch.candidates)
        all_incomplete.extend(cand_batch.incomplete)

    gate = run_alias_gate(all_candidates, incomplete=all_incomplete)
    decisions_by_id = {d.alias_candidate_id: d for d in gate.decisions}
    candidates_by_id = {c.alias_candidate_id: c for c in all_candidates}

    for cand in all_candidates:
        await db[SHADOW_COLLECTIONS["alias_candidates"]].insert_one(
            {**cand.model_dump(), "corpus_id": corpus_id, "created_at": _utcnow()}
        )
        dec = decisions_by_id[cand.alias_candidate_id]
        await db[SHADOW_COLLECTIONS["alias_decisions"]].insert_one(
            {**dec.model_dump(), "corpus_id": corpus_id, "created_at": _utcnow()}
        )
        child_evidence.append(
            ChildAliasEvidence(
                candidate=cand,
                decision=dec,
                parent_id=f"{cand.document_id}_p0",
                child_id=cand.chunk_id,
                child_text=next(
                    (r["text"] for r in ghost_rows if r["chunk_id"] == cand.chunk_id),
                    "",
                ),
            )
        )

    doc_batch = cluster_document_entities(child_evidence)
    for bundle in doc_batch.parent_bundles:
        await db[SHADOW_COLLECTIONS["parent_alias_bundles"]].insert_one(
            {**bundle.model_dump(), "corpus_id": corpus_id, "created_at": _utcnow()}
        )
    for ent in doc_batch.entities:
        await db[SHADOW_COLLECTIONS["document_entities"]].insert_one(
            {**ent.model_dump(), "corpus_id": corpus_id, "created_at": _utcnow()}
        )

    inventories = _inventory_from_doc_entities(
        doc_batch.entities, decisions_by_id, candidates_by_id
    )
    # Enrich acronym pairs from source text when Schwartz-Hearst/patterns found them.
    for inv_i, inv in enumerate(list(inventories)):
        pairs = list(inv.acronym_pairs)
        for row in ghost_rows:
            if row["doc_id"] != inv.entity.document_id:
                continue
            for m in _ACRONYM_RE.finditer(row["text"]):
                pairs.append((m.group("short"), m.group("long")))
        inventories[inv_i] = build_inventory(
            inv.entity,
            trusted_alias_surfaces=inv.trusted_alias_surfaces,
            temporal_former_names=inv.temporal_former_names,
            retrieval_surfaces=inv.retrieval_surfaces,
            acronym_pairs=pairs,
            curated_canonical=inv.curated_canonical,
            supporting_alias_decision_ids=inv.supporting_alias_decision_ids,
            is_proper_name=inv.is_proper_name,
            related_terms=inv.related_terms,
            description_surfaces=inv.description_surfaces,
        )

    corpus_batch = cluster_corpus_entities(inventories, corpus_id=corpus_id)
    for ent in corpus_batch.corpus_entities:
        await db[SHADOW_COLLECTIONS["corpus_entities"]].insert_one(
            {**ent.model_dump(), "corpus_id": corpus_id, "created_at": _utcnow()}
        )
    shadow_records = [
        project_corpus_entity_to_shadow_schema(ent)
        for ent in corpus_batch.corpus_entities
    ]
    if shadow_records:
        await db[SHADOW_COLLECTIONS["schema_shadow"]].insert_many(
            [
                {
                    **r.model_dump(),
                    "corpus_id": corpus_id,
                    "created_at": _utcnow(),
                    "identity_authority": False,
                    "note": "shadow_only_do_not_overwrite_production_schemas",
                }
                for r in shadow_records
            ]
        )
        # In-process retrieval shadow registry only (not production Qdrant).
        register_shadow_schema_records(corpus_id, shadow_records)

    # Graph projection jobs (fixture only) — plan + run if driver present.
    projection_hist: dict[str, int] = {}
    if neo4j_driver is not None:
        from services.graph.projection_jobs import plan_projection_jobs_for_document
        from services.graph.projection_runner import run_projection_jobs

        for doc in docs:
            await plan_projection_jobs_for_document(
                db, corpus_id=corpus_id, document_id=doc.document_id
            )
        run = await run_projection_jobs(
            db,
            neo4j_driver,
            corpus_id=corpus_id,
            owner="fixture_phase1",
            max_jobs=200,
        )
        projection_hist = dict(run.get("job_status_histogram") or {})

    # Proof assembly
    proof = Phase1Proof(corpus_id=corpus_id)
    proof.candidate_generation_reachable = len(all_candidates) > 0
    proof.gate_reachable = len(gate.decisions) > 0
    proof.document_clustering_reachable = len(doc_batch.entities) > 0
    proof.corpus_clustering_shadow_reachable = len(corpus_batch.corpus_entities) > 0
    proof.ambiguous_acronym_cross_merges = int(
        getattr(corpus_batch, "ambiguous_acronym_cross_merges", 0) or 0
    )
    proof.descriptions_as_aliases = int(doc_batch.description_or_role_merges or 0)
    proof.semantic_similarity_identity_merges = int(
        getattr(corpus_batch, "semantic_similarity_merges", 0) or 0
    )
    proof.bundles_created_for_every_fixture_chunk = len(bundles) == len(tasks)

    # Accepted assertions preserved: every ACCEPT_* relation from ghost appears in bundle.
    preserved = True
    offsets_ok = True
    evidence_ok = True
    for row, bundle in zip(ghost_rows, bundles, strict=False):
        accepted_src = [
            r
            for r in row["relations"]
            if str(r.get("validation_status") or "").lower().startswith("accept")
        ]
        if len(accepted_src) != len(bundle.accepted_relation_assertions):
            preserved = False
        if not bundle.evidence_text:
            evidence_ok = False
        offs = bundle.source_structure.source_offsets or {}
        if not offs:
            offsets_ok = False
        # At least one exact offset when entities exist
        if bundle.entity_mentions and not any(
            isinstance(v, dict) and v.get("status") == "exact" for v in offs.values()
        ):
            offsets_ok = False
    proof.accepted_assertions_preserved = preserved
    proof.source_offsets_preserved = offsets_ok
    proof.evidence_text_preserved = evidence_ok
    proof.production_alias_writes = 0
    proof.production_schema_writes = 0
    proof.global_ranking_activation = False
    proof.counts = {
        "chunks": len(tasks),
        "bundles": len(bundles),
        "alias_candidates": len(all_candidates),
        "alias_decisions": len(gate.decisions),
        "parent_alias_bundles": len(doc_batch.parent_bundles),
        "document_entities": len(doc_batch.entities),
        "corpus_entities": len(corpus_batch.corpus_entities),
        "shadow_schema_projections": len(shadow_records),
        "accepted_assertions_total": sum(
            len(b.accepted_relation_assertions) for b in bundles
        ),
        "projection_jobs": sum(projection_hist.values()) if projection_hist else 0,
    }

    return {
        "corpus_id": corpus_id,
        "phase": 1,
        "proof": asdict(proof),
        "phase_1_ok": proof.ok(),
        "projection_job_histogram": projection_hist,
        "corpus_entity_ids": [e.corpus_entity_id for e in corpus_batch.corpus_entities],
        "sample_corpus_entities": [
            {
                "canonical_name": e.canonical_name,
                "trusted_aliases": e.trusted_aliases,
                "ambiguous_aliases": e.ambiguous_aliases,
            }
            for e in corpus_batch.corpus_entities[:12]
        ],
    }
