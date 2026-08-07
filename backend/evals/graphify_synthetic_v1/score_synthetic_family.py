#!/usr/bin/env python3
"""Score Graphify against a synthetic regression family (development harness).

Usage:
    local_ghost_b/.venv/bin/python backend/evals/graphify_synthetic_v1/score_synthetic_family.py \
        04_identity_mechanics [05_endpoint_minting_negatives ...]

Runs each family fixture through the live Graphify CPU pipeline in an ISOLATED Mongo database and
an isolated Neo4j corpus namespace (skip_preclear, unique corpus_id), reads back the promoted
graph, and scores it against the family's gold sidecar. Development harness only — results are
never qualification evidence. Never point this at a production namespace.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path("/Users/king/polymath_v3.3")
BACKEND = ROOT / "backend"
FAMILY_DIR = ROOT / "backend" / "evals" / "graphify_synthetic_v1"
sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=False)

from config import get_settings
from motor.motor_asyncio import AsyncIOMotorClient
from neo4j import AsyncGraphDatabase
from services.extraction.graphify_pipeline import PIPELINE_RELEASE, run_graphify_pipeline
from services.graph.neo4j_writer import write_document_graph


@dataclass
class Child:
    chunk_id: str
    text: str
    chunk_kind: str = "body"
    language: str | None = None
    metadata: dict = field(default_factory=dict)


def norm(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value).casefold()))


def local_url(value: str, service: str, port: int) -> str:
    result = str(value)
    result = result.replace(f"://{service}:{port}", f"://127.0.0.1:{port}")
    result = result.replace(f"@{service}:{port}", f"@127.0.0.1:{port}")
    return result


def make_alias_map(gold: dict) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for group in gold.get("declared_aliases", []):
        canonical = norm(group["canonical"])
        for alias in group.get("aliases", []):
            aliases[norm(alias)] = canonical
    return aliases


def canon(value: str, aliases: dict[str, str]) -> str:
    normalized = norm(value)
    return aliases.get(normalized, normalized)


async def run_family(stem: str, results_root: Path) -> dict:
    fixture = FAMILY_DIR / f"{stem}.md"
    gold_path = FAMILY_DIR / f"{stem}.gold.json"
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    aliases = make_alias_map(gold)
    text = fixture.read_text(encoding="utf-8")

    run_id = uuid.uuid4().hex[:10]
    corpus_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"graphify-synthetic:{stem}:{run_id}"))
    doc_id = hashlib.sha256(fixture.read_bytes()).hexdigest()
    db_name = f"graphify_eval_syn_{stem.split('_')[0]}_{run_id}"
    child = Child(chunk_id=f"{doc_id}:whole", text=text)

    settings = get_settings()
    mongo = AsyncIOMotorClient(local_url(settings.MONGODB_URI, "mongodb", 27017))
    neo4j = AsyncGraphDatabase.driver(
        local_url(settings.NEO4J_URI, "neo4j", 7687),
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    db = mongo[db_name]
    started = time.perf_counter()
    try:
        await db.corpora.insert_one({"corpus_id": corpus_id, "name": f"synthetic {stem}"})
        await db.documents.insert_one(
            {"corpus_id": corpus_id, "doc_id": doc_id, "filename": fixture.name, "status": "active"}
        )
        output = await run_graphify_pipeline(
            db=db, corpus_id=corpus_id, doc_id=doc_id, text=text,
            children=[child], source_uri=str(fixture),
        )
        await write_document_graph(
            neo4j, doc_id, corpus_id, list(output.report.results),
            user_id="synthetic-family-eval", file_id=doc_id,
            all_chunk_ids=[child.chunk_id], filename=fixture.name, db=db,
            chunk_attributes={child.chunk_id: {"chunk_kind": "body", "language": None}},
            skip_preclear=True,
        )
        async with neo4j.session() as session:
            rows = await session.run(
                """
                MATCH (s:Entity)-[r:RELATES_TO]->(o:Entity)
                WHERE $corpus_id IN coalesce(r.corpus_ids, [])
                RETURN coalesce(s.canonical_name, s.name, s.entity_id) AS subject,
                       coalesce(r.predicate, 'related_to') AS predicate,
                       coalesce(o.canonical_name, o.name, o.entity_id) AS object
                ORDER BY subject, predicate, object
                """,
                corpus_id=corpus_id,
            )
            promoted_rows = [dict(r) async for r in rows]
        graph_nodes = sorted({str(r[k]) for r in promoted_rows for k in ("subject", "object")})

        # Non-positive lane records (qualified / review / reject) from the isolated stage ledgers.
        art = db["graphify_stage_artifacts"]
        nonpositive: list[dict] = []
        rc = await art.find_one({"stage": "RELATION_COMPILATION_COMPLETE"}, sort=[("created_at", -1)])
        if rc:
            payload = rc.get("payload") or {}
            mapped = {m.get("relation_id"): m for m in payload.get("mapped_relations", [])}
            for a in payload.get("assertions", []):
                decision = str(a.get("decision") or a.get("status") or "").lower()
                if decision not in ("accepted", "fact"):
                    rec = dict(a)
                    rec["_mapped"] = mapped.get(a.get("relation_id"), {})
                    rec["_lane"] = f"fast_path:{decision or 'unknown'}"
                    nonpositive.append(rec)
        oa = await art.find_one({"stage": "OPENIE_ASSERTION_ASSEMBLY_COMPLETE"}, sort=[("created_at", -1)])
        if oa:
            for a in (oa.get("payload") or {}).get("assertions", []):
                lane = str(a.get("lane") or a.get("decision") or "").upper()
                if lane != "FACT":
                    rec = dict(a)
                    rec["_lane"] = f"openie:{lane or 'UNKNOWN'}"
                    nonpositive.append(rec)
        mention_names: dict[str, str] = {}
        if rc:
            payload = rc.get("payload") or {}
            entity_names = {
                e.get("entity_id"): e.get("canonical_name", "")
                for e in payload.get("endpoint_entities", [])
            }
            red = await art.find_one({"stage": "ENTITY_REDUCTION_COMPLETE"}, sort=[("created_at", -1)])
            for e in ((red or {}).get("payload") or {}).get("entities", []):
                entity_names[e.get("entity_id")] = e.get("canonical_name", "")
            comp = await art.find_one({"stage": "MENTION_COMPLETION_COMPLETE"}, sort=[("created_at", -1)])
            for mn in ((comp or {}).get("payload") or {}).get("mentions", []):
                mention_names[mn.get("mention_id")] = entity_names.get(mn.get("entity_id"), mn.get("surface", ""))
            for mn in payload.get("endpoint_mentions", []):
                mention_names[mn.get("mention_id")] = entity_names.get(mn.get("entity_id"), mn.get("surface", ""))
        def enrich(record: dict) -> str:
            mapped = record.get("_mapped") or record
            extra = " ".join(
                mention_names.get(mapped.get(key), "")
                for key in ("subject_mention_id", "object_mention_id")
            )
            return norm(json.dumps(record, default=str) + " " + extra)
        nonpositive_texts = [enrich(r) for r in nonpositive]

        promoted = {
            (canon(r["subject"], aliases), norm(r["predicate"]), canon(r["object"], aliases))
            for r in promoted_rows
        }
        promoted_pairs = {(s, o) for s, _, o in promoted}
        node_canon = {canon(n, aliases) for n in graph_nodes}

        extracted_entities = sorted({
            entity.canonical_name
            for result in output.report.results
            for entity in result.entities
        })

        def positive_edge(subject: str, predicates: list[str], obj: str) -> bool:
            s, o = canon(subject, aliases), canon(obj, aliases)
            return any((s, norm(p), o) in promoted for p in predicates)

        def in_nonpositive(subject: str, obj: str) -> bool:
            s, o = canon(subject, aliases), canon(obj, aliases)
            return any(s in t and o in t for t in nonpositive_texts)

        item_results = []
        passed = failed = optional_failed = 0
        matched_gold_triples: set[tuple[str, str, str]] = set()
        for item in gold["items"]:
            expect = item.get("expect", {})
            etype = expect.get("type")
            optional = bool(expect.get("optional"))
            preds = expect.get("any_of") or ([expect["predicate"]] if expect.get("predicate") else [])
            verdict, detail = "PASS", ""
            if etype == "canonical":
                hit = positive_edge(expect["subject"], preds, expect["object"])
                reverse_hit = positive_edge(expect["object"], preds, expect["subject"])
                if hit:
                    for p in preds:
                        t = (canon(expect["subject"], aliases), norm(p), canon(expect["object"], aliases))
                        if t in promoted:
                            matched_gold_triples.add(t)
                    verdict = "PASS"
                elif reverse_hit:
                    verdict, detail = "FAIL", "direction reversed in graph"
                elif (canon(expect["subject"], aliases), canon(expect["object"], aliases)) in promoted_pairs:
                    verdict, detail = "FAIL", "pair promoted with wrong predicate"
                else:
                    verdict, detail = "FAIL", "no positive edge for pair"
            elif etype == "qualified":
                leaked = positive_edge(expect["subject"], preds or [expect.get("surface_predicate", "")], expect["object"]) or \
                         positive_edge(expect["object"], preds or [expect.get("surface_predicate", "")], expect["subject"])
                captured = in_nonpositive(expect["subject"], expect["object"])
                if leaked:
                    verdict, detail = "FAIL", "LEAKED to positive graph"
                elif not captured:
                    verdict, detail = "FAIL", "dropped entirely (no qualified/review record found)"
                else:
                    verdict, detail = "PASS", "contained in non-positive lane"
            elif etype == "no_edge":
                bad_nodes = [e for e in (item.get("must_not", {}).get("entities") or []) if canon(e, aliases) in node_canon]
                if bad_nodes:
                    verdict, detail = "FAIL", f"junk entities in graph: {bad_nodes}"
            elif etype == "open":
                # Families 04/05 have no open items; minimal support: count as captured if the pair
                # exists anywhere (positive graph with any predicate or non-positive record).
                pair_any = (canon(expect["subject"], aliases), canon(expect["object"], aliases)) in promoted_pairs
                if pair_any or in_nonpositive(expect["subject"], expect["object"]):
                    verdict = "PASS"
                else:
                    verdict, detail = "FAIL", "surface relation not discovered anywhere"
            if verdict == "FAIL":
                if optional:
                    optional_failed += 1
                else:
                    failed += 1
            else:
                passed += 1
            item_results.append({"id": item["id"], "check": item.get("check"), "verdict": verdict,
                                 "optional": optional, "detail": detail, "evidence": item.get("evidence", "")[:100]})

        neg_hits = sorted({e for e in gold.get("negative_entities", []) if canon(e, aliases) in node_canon})
        unexpected_promoted = sorted(
            t for t in promoted
            if t not in matched_gold_triples
        )
        dup_counts = {}
        for r in promoted_rows:
            key = (canon(r["subject"], aliases), norm(r["predicate"]), canon(r["object"], aliases))
            dup_counts[key] = dup_counts.get(key, 0) + 1
        duplicate_edges = {" | ".join(k): v for k, v in dup_counts.items() if v > 1}

        result = {
            "family": stem,
            "status": "PASS" if failed == 0 and not neg_hits else "FAIL",
            "runtime": {
                "pipeline_release": PIPELINE_RELEASE,
                "mongo_database": db_name,
                "corpus_id": corpus_id,
                "elapsed_seconds": round(time.perf_counter() - started, 2),
                "interpreter": sys.executable,
            },
            "items": {"passed": passed, "failed": failed, "optional_failed": optional_failed,
                      "total": len(gold["items"])},
            "negative_entities_in_graph": neg_hits,
            "unexpected_promoted_triples": [" | ".join(t) for t in unexpected_promoted],
            "duplicate_promoted_edges": duplicate_edges,
            "graph": {"nodes": graph_nodes, "promoted_edge_count": len(promoted_rows)},
            "extracted_entities": extracted_entities,
            "item_results": item_results,
        }
        out_dir = results_root / stem
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "score.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        (out_dir / "promoted_triples.json").write_text(json.dumps(promoted_rows, indent=2) + "\n", encoding="utf-8")
        (out_dir / "nonpositive_records.json").write_text(
            json.dumps(nonpositive, indent=2, default=str) + "\n", encoding="utf-8")
        return result
    finally:
        await neo4j.close()
        mongo.close()


async def main() -> int:
    stems = sys.argv[1:]
    if not stems:
        print("usage: score_synthetic_family.py <family_stem> [...]")
        return 2
    results_root = FAMILY_DIR / "results"
    exit_code = 0
    for stem in stems:
        result = await run_family(stem, results_root)
        summary = {k: result[k] for k in ("family", "status", "items", "negative_entities_in_graph")}
        summary["unexpected_promoted"] = len(result["unexpected_promoted_triples"])
        summary["elapsed_seconds"] = result["runtime"]["elapsed_seconds"]
        print(json.dumps(summary, indent=2))
        if result["status"] != "PASS":
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
