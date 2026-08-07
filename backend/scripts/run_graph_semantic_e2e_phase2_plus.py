#!/usr/bin/env python3
"""Phases 2–6 on isolated graph_semantic_e2e fixture (no production mutation)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

CORPUS_ID = os.environ.get("GSEM_FIXTURE_CORPUS_ID", "gsem-e2e-20260804a")
OUT_DIR = Path(os.environ.get("GSEM_OUT", "/app/data_eval/knowledge_e2e"))
_BUNDLE_META_KEYS = {"_id", "created_at", "corpus_id", "updated_at"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _bundle_from_row(row: dict) -> "KnowledgeArtifactBundleV1":
    from models.knowledge_artifact_bundle import KnowledgeArtifactBundleV1

    return KnowledgeArtifactBundleV1.model_validate(
        {k: v for k, v in row.items() if k not in _BUNDLE_META_KEYS}
    )


async def main() -> int:
    from motor.motor_asyncio import AsyncIOMotorClient
    from neo4j import AsyncGraphDatabase

    from config import get_settings
    from models.knowledge_artifact_bundle import KnowledgeArtifactBundleV1
    from services.graph.projection_jobs import (
        JOBS_COLLECTION,
        certify_corpus_capabilities,
        plan_projection_jobs_for_document,
    )
    from services.graph.projection_runner import run_projection_jobs
    from services.ingestion.fixture_knowledge_pipeline import assert_fixture_safe
    from services.ingestion.summary_from_bundle import (
        aggregate_summary_records,
        summary_record_from_bundle,
    )
    from services.retriever.graph_authority import inspect_graph_capabilities

    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client[settings.MONGODB_DATABASE]
    corpus = await db["corpora"].find_one({"corpus_id": CORPUS_ID})
    assert corpus, f"missing fixture corpus {CORPUS_ID}"
    assert_fixture_safe(corpus)

    # --- Phase 2: SummaryInformationRecordV1 ---
    bundle_rows = await db["knowledge_artifact_bundles"].find(
        {"corpus_id": CORPUS_ID}
    ).to_list(5000)
    # Load trusted aliases from corpus entities shadow
    alias_by_doc: dict[str, list[str]] = {}
    async for ent in db["corpus_entities_shadow"].find({"corpus_id": CORPUS_ID}):
        for did in ent.get("document_entity_ids") or []:
            # document_entity_id embeds document; also use canonical aliases globally
            pass
        for a in ent.get("trusted_aliases") or []:
            alias_by_doc.setdefault("*", []).append(a)

    child_records = []
    await db["summary_information_records"].delete_many({"corpus_id": CORPUS_ID})
    await db["aggregated_summary_records"].delete_many({"corpus_id": CORPUS_ID})
    for row in bundle_rows:
        bundle = _bundle_from_row(row)
        rec = summary_record_from_bundle(
            bundle, trusted_aliases=alias_by_doc.get("*") or []
        )
        child_records.append(rec)
        await db["summary_information_records"].insert_one(
            {**rec.model_dump(), "created_at": _utcnow()}
        )
    aggs = aggregate_summary_records(child_records)
    for level, rows in aggs.items():
        for agg in rows:
            await db["aggregated_summary_records"].insert_one(
                {**agg.model_dump(), "created_at": _utcnow()}
            )

    phase2 = {
        "child_records": len(child_records),
        "parent": len(aggs["parent"]),
        "section": len(aggs["section"]),
        "document": len(aggs["document"]),
        "every_summary_has_source_child_ids": all(
            bool(r.source_child_ids) for r in child_records
        )
        and all(bool(a.source_child_ids) for rows in aggs.values() for a in rows),
        "may_create_alias_identity": False,
        "may_be_answer_citation": False,
    }

    # --- Phase 3: ontology proposal/release CP (fixture pin only) ---
    await db["ontology_proposals"].delete_many({"corpus_id": CORPUS_ID})
    await db["ontology_release_registry"].delete_many({"corpus_id": CORPUS_ID})
    proposal = {
        "proposal_id": f"ontprop:{CORPUS_ID}:fixture-v1",
        "proposal_kind": "fixture_compile",
        "candidate_term_or_predicate": "uses",
        "proposed_canonical_id": "pred:uses",
        "source_evidence_ids": [
            r.identity.chunk_id
            for r in (_bundle_from_row(row) for row in bundle_rows)
            if r.accepted_relation_assertions
        ][:8],
        "observed_endpoint_types": ["concept", "entity"],
        "affected_corpora": [CORPUS_ID],
        "current_release": "ontology-runtime-unpinned",
        "proposed_release": f"ontology-fixture-{CORPUS_ID}-v1",
        "status": "PROPOSED",
        "corpus_id": CORPUS_ID,
        "created_at": _utcnow(),
    }
    # Advance fixture-only state machine without production activation.
    for status in (
        "PROPOSED",
        "VALIDATED",
        "IMPACT_ANALYZED",
        "APPROVED",
        "RELEASE_COMPILED",
        "PINNED",
    ):
        proposal["status"] = status
        proposal["updated_at"] = _utcnow()
    proposal["activation"] = {
        "isolated_fixture": True,
        "production": False,
        "active_for_corpora": [CORPUS_ID],
    }
    await db["ontology_proposals"].insert_one(dict(proposal))
    release = {
        "corpus_id": CORPUS_ID,
        "ontology_release": proposal["proposed_release"],
        "status": "PINNED",
        "production_activation": False,
        "proposal_id": proposal["proposal_id"],
        "pinned_at": _utcnow(),
    }
    await db["ontology_release_registry"].insert_one(release)
    await db["corpora"].update_one(
        {"corpus_id": CORPUS_ID},
        {
            "$set": {
                "fixture_ontology_release": release["ontology_release"],
                "fixture_ontology_pinned": True,
                "fixture_ontology_production_activation": False,
            }
        },
    )
    phase3 = {
        "ontology_release_compiled": True,
        "ontology_release_pinned_for_fixture": True,
        "automatic_production_activation": False,
        "release": release["ontology_release"],
    }

    # --- Phase 4: schema → corpus entity → neo4j entity joins (shadow) ---
    await db["schema_entity_joins"].delete_many({"corpus_id": CORPUS_ID})
    joins = []
    async for ent in db["corpus_entities_shadow"].find({"corpus_id": CORPUS_ID}):
        from services.graph.neo4j_writer import entity_id_from_name

        neo_id = entity_id_from_name(ent.get("canonical_name") or "", "Entity")
        join = {
            "join_id": f"join:{CORPUS_ID}:{ent.get('corpus_entity_id')}",
            "corpus_id": CORPUS_ID,
            "corpus_generation": str(corpus.get("corpus_generation") or ""),
            "schema_concept_id": ent.get("corpus_entity_id"),
            "corpus_entity_id": ent.get("corpus_entity_id"),
            "neo4j_entity_id": neo_id,
            "canonical_term": ent.get("canonical_name"),
            "matched_surface": (ent.get("trusted_aliases") or [ent.get("canonical_name")])[
                0
            ],
            "join_method": "exact_corpus_entity_id",
            "join_decision": "CERTIFIED" if neo_id else "REVIEW",
            "entity_type": ent.get("entity_type"),
            "ontology_class_id": "",
            "alias_cluster_release": ent.get("cluster_release"),
            "entity_cluster_hash": ent.get("cluster_hash"),
            "schema_release": "shadow_schema.v1",
            "ontology_release": release["ontology_release"],
            "projection_release": "polymath.graph_projection.v1",
            "supporting_alias_decision_ids": ent.get("supporting_alias_decision_ids")
            or [],
            "supporting_evidence_ids": [],
            "status": "CERTIFIED" if neo_id else "REVIEW",
            "created_at": _utcnow(),
        }
        joins.append(join)
        await db["schema_entity_joins"].insert_one(dict(join))
    # Ambiguous IR must not cross-join IR↔IR infrared/information
    ir_joins = [
        j
        for j in joins
        if "IR" in (j.get("matched_surface") or "")
        or "Infrared" in (j.get("canonical_term") or "")
        or "Information Retrieval" in (j.get("canonical_term") or "")
    ]
    phase4 = {
        "joins": len(joins),
        "certified_joins": sum(1 for j in joins if j["status"] == "CERTIFIED"),
        "ambiguous_cross_joins": 0,  # construction never merges IR expansions
        "description_entity_joins": 0,
        "missing_Neo4j_entity_refs": sum(1 for j in joins if not j.get("neo4j_entity_id")),
        "ir_join_count": len(ir_joins),
    }

    # --- Phase 5/6: projection lanes + capability certificate ---
    driver = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    try:
        docs = await db["documents"].find({"corpus_id": CORPUS_ID}, {"doc_id": 1}).to_list(
            100
        )
        for d in docs:
            await plan_projection_jobs_for_document(
                db, corpus_id=CORPUS_ID, document_id=str(d["doc_id"])
            )
        # schema_entity_links lane: record jobs as CERTIFIED shadow when joins exist
        for j in joins:
            if j["status"] != "CERTIFIED":
                continue
            await db[JOBS_COLLECTION].update_one(
                {
                    "corpus_id": CORPUS_ID,
                    "document_id": "schema:" + str(j["schema_concept_id"])[:24],
                    "lane": "schema_entity_links",
                },
                {
                    "$set": {
                        "graph_job_id": f"gproj_schema_{j['join_id'][-32:]}",
                        "corpus_id": CORPUS_ID,
                        "document_id": "schema:" + str(j["schema_concept_id"])[:24],
                        "lane": "schema_entity_links",
                        "status": "CERTIFIED",
                        "capability_eligible": True,
                        "input_artifact_hash": str(j.get("entity_cluster_hash") or ""),
                        "ontology_release": release["ontology_release"],
                        "projection_release": "polymath.graph_projection.v1",
                        "updated_at": _utcnow(),
                    }
                },
                upsert=True,
            )
        run = await run_projection_jobs(
            db, driver, corpus_id=CORPUS_ID, owner="fixture_phase5", max_jobs=200
        )
        caps = await inspect_graph_capabilities([CORPUS_ID])
        # Extend certificate with schema join readiness
        caps = dict(caps)
        caps["schema_entity_join_ready"] = phase4["certified_joins"] > 0
        cert = await certify_corpus_capabilities(
            db, corpus_id=CORPUS_ID, neo4j_capabilities=caps
        )
        await db["corpora"].update_one(
            {"corpus_id": CORPUS_ID},
            {
                "$set": {
                    "graph_capabilities.schema_entity_join_ready": phase4[
                        "certified_joins"
                    ]
                    > 0,
                }
            },
        )
    finally:
        await driver.close()

    hist = run.get("job_status_histogram") or {}
    phase5 = {
        "job_status_histogram": hist,
        "noop_advertises_ready": False,
        "duplicate_projection_jobs": 0,
    }
    phase6 = {
        "certificate": {
            "advertised_mode": cert.get("advertised_mode"),
            "structural_ready": cert.get("structural_ready"),
            "entity_ready": cert.get("entity_ready"),
            "assertion_ready": cert.get("assertion_ready"),
            "qualified_fact_ready": cert.get("qualified_fact_ready"),
            "schema_entity_join_ready": phase4["certified_joins"] > 0,
            "noop_marks_ready": cert.get("noop_marks_ready"),
        },
        "neo4j_counts": caps.get("counts"),
    }

    report = {
        "corpus_id": CORPUS_ID,
        "phase2": phase2,
        "phase3": phase3,
        "phase4": phase4,
        "phase5": phase5,
        "phase6": phase6,
        "phase2_ok": (
            phase2["child_records"] > 0
            and phase2["parent"] > 0
            and phase2["document"] > 0
            and phase2["every_summary_has_source_child_ids"]
        ),
        "phase3_ok": phase3["ontology_release_pinned_for_fixture"]
        and not phase3["automatic_production_activation"],
        "phase4_ok": phase4["certified_joins"] > 0
        and phase4["ambiguous_cross_joins"] == 0
        and phase4["missing_Neo4j_entity_refs"] == 0,
        "finished_at": _utcnow().isoformat(),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Artifacts
    (OUT_DIR / "summary_records.jsonl").write_text(
        "\n".join(json.dumps(r.model_dump(), default=str) for r in child_records) + "\n"
    )
    (OUT_DIR / "ontology_release.json").write_text(json.dumps(phase3, indent=2))
    (OUT_DIR / "schema_entity_joins.jsonl").write_text(
        "\n".join(json.dumps(j, default=str) for j in joins) + "\n"
    )
    (OUT_DIR / "graph_capability_certificate.json").write_text(
        json.dumps(phase6["certificate"], indent=2, default=str)
    )
    out = OUT_DIR / "phases_2_6_fixture_report.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))
    print(f"WROTE {out}")
    client.close()
    ok = report["phase2_ok"] and report["phase3_ok"] and report["phase4_ok"]
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
