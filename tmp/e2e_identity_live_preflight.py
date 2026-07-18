from __future__ import annotations

import asyncio
import ast
import json
import re
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient
from neo4j import AsyncGraphDatabase

from config import get_settings


SOURCE_ROOT = Path("/tmp/e2e-identity-candidate")
TARGETS = (
    "services/graph/neo4j_writer.py",
    "services/graph/neo4j_reader.py",
    "services/graph/queries.py",
    "services/graph/schema.py",
    "services/portability.py",
    "services/retriever/mode_a.py",
    "services/retriever/graph_decoration.py",
    "services/retriever/graph_rerank.py",
    "scripts/e2e_identity_repair.py",
)
PARAM_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


class _GuardedSession:
    def __init__(self, session: Any, failures: list[dict[str, Any]]):
        self._session = session
        self._failures = failures

    async def __aenter__(self):
        await self._session.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return await self._session.__aexit__(exc_type, exc, tb)

    async def run(self, query: str, **params: Any):
        try:
            return await self._session.run(query, **params)
        except Exception as exc:
            self._failures.append(
                {
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:320],
                    "query_head": " ".join(str(query).split())[:160],
                }
            )
            raise


class _GuardedDriver:
    def __init__(self, driver: Any, failures: list[dict[str, Any]]):
        self._driver = driver
        self._failures = failures

    def session(self, *args: Any, **kwargs: Any):
        return _GuardedSession(
            self._driver.session(*args, **kwargs),
            self._failures,
        )


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def parameter_value(name: str) -> Any:
    if name in {"rows"}:
        return [
            {
                "corpus_id": "preflight-corpus",
                "doc_id": "preflight-doc",
                "chunk_id": "preflight-chunk",
                "fact_id": "preflight-fact",
                "entity_id": "preflight-entity",
                "subject_id": "preflight-subject",
                "object_id": "preflight-object",
                "predicate": "related_to",
                "chunk_key": "preflight-corpus|preflight-chunk",
                "doc_key": "preflight-corpus|preflight-doc",
                "confidence": 0.5,
            }
        ]
    if name in {"chunk_refs", "seed_refs", "winning_chunk_refs"}:
        return [{"corpus_id": "preflight-corpus", "chunk_id": "preflight-chunk"}]
    if name == "triples":
        return [{"s": "preflight-subject", "t": "preflight-object", "p": "related_to"}]
    if name.endswith("_ids") or name in {
        "ids",
        "names",
        "cands",
        "linked",
        "generic_terms",
        "query_aliases",
        "stop_exact",
        "stop_prefixes",
        "stop_contains",
        "entity_ids",
        "frontier_ids",
        "seen_ids",
        "node_ids",
        "doc_keys",
        "chunk_keys",
        "remaining_doc_keys",
    }:
        return ["preflight-id"]
    if name in {
        "limit",
        "max_nodes",
        "max_edges",
        "batch_size",
        "neighbor_limit",
        "chunks_per_neighbor",
        "seed_entities_per_chunk",
        "chunk_limit",
        "hard_cap",
        "bridge_entity_cap",
    }:
        return 1
    if name.startswith("allow_"):
        return False
    if name in {
        "alpha",
        "min_edge_confidence",
        "hop_min_confidence",
        "generic_min_confidence",
    }:
        return 0.0
    if name == "separator":
        return "|"
    if name.endswith("_pattern"):
        return "(?!)"
    if name == "wanted_families":
        return []
    return f"preflight-{name}"


def cypher_candidates() -> list[tuple[str, int, str]]:
    candidates: list[tuple[str, int, str]] = []
    for relative in TARGETS:
        path = SOURCE_ROOT / relative
        tree = ast.parse(path.read_text(), filename=str(path))
        module_constants: dict[str, str] = {}
        for statement in tree.body:
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                continue
            targets = (
                statement.targets
                if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            value = statement.value
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    module_constants[target.id] = value.value

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr != "run":
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                query = first.value.strip()
            elif isinstance(first, ast.Name) and first.id in module_constants:
                query = module_constants[first.id].strip()
            else:
                continue
            upper = query.upper()
            if not any(
                token in upper for token in ("MATCH ", "MERGE ", "SHOW CONSTRAINTS")
            ):
                continue
            if not any(
                token in upper
                for token in ("RETURN ", "SET ", "DELETE ", "CREATE ", "SHOW ")
            ):
                continue
            if (
                "{" in query
                and "}" in query
                and "$" not in query
                and "MATCH" not in upper
            ):
                continue
            candidates.append((relative, int(getattr(first, "lineno", 0)), query))
    return candidates


async def main() -> None:
    settings = get_settings()
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    db = mongo[settings.MONGODB_DATABASE]
    neo4j = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    try:
        summary_indexes = await db["summary_tree"].index_information()
        summary_index_shape = {
            name: {
                "key": [list(value) for value in info.get("key", [])],
                "unique": bool(info.get("unique")),
            }
            for name, info in summary_indexes.items()
        }

        async with neo4j.session() as session:
            missing_required: dict[str, int] = {}
            for label, content_property in (
                ("Document", "doc_id"),
                ("Chunk", "chunk_id"),
                ("Fact", "fact_id"),
            ):
                required_result = await session.run(
                    f"MATCH (n:{label}) "
                    f"WHERE n.corpus_id IS NULL OR n.{content_property} IS NULL "
                    "RETURN count(n) AS missing"
                )
                row = await required_result.single()
                missing_required[label] = int(row["missing"] if row else -1)
            require(
                all(value == 0 for value in missing_required.values()),
                f"derived nodes missing identity properties: {missing_required}",
            )

            constraints_result = await session.run(
                "SHOW CONSTRAINTS YIELD name, labelsOrTypes, properties "
                "RETURN name, labelsOrTypes, properties"
            )
            constraints = [dict(row) async for row in constraints_result]

            compiled = 0
            failures: list[dict[str, Any]] = []
            seen: set[str] = set()
            for relative, line, query in cypher_candidates():
                if query in seen:
                    continue
                seen.add(query)
                params = {
                    name: parameter_value(name)
                    for name in sorted(set(PARAM_RE.findall(query)))
                }
                try:
                    result = await session.run("EXPLAIN\n" + query, **params)
                    await result.consume()
                    compiled += 1
                except Exception as exc:
                    failures.append(
                        {
                            "file": relative,
                            "line": line,
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:320],
                        }
                    )

        dynamic_failures: list[dict[str, Any]] = []
        guarded = _GuardedDriver(neo4j, dynamic_failures)
        from models.schemas import SourceChunk
        from services.graph.neo4j_reader import (
            get_entity_relations,
            get_full_corpora_graph,
            get_full_corpus_graph,
        )
        from services.graph.queries import get_book_drilldown
        from services.retriever.graph_decoration import GraphDecorator
        from services.retriever.graph_rerank import (
            apply_graph_degree_boost,
            apply_graph_degree_boost_metrics_aware,
        )
        from services.retriever.mode_a import ModeAExpansion

        fake_ref = {"corpus_id": "preflight-corpus", "chunk_id": "preflight-chunk"}
        await get_entity_relations(
            guarded,
            corpus_id="preflight-corpus",
            entity_id="preflight-entity",
            limit=1,
        )
        await get_full_corpus_graph(
            guarded,
            corpus_id="preflight-corpus",
            max_nodes=1,
            max_edges=1,
        )
        await get_full_corpora_graph(
            guarded,
            corpus_ids=["preflight-corpus"],
            max_nodes=1,
            max_edges=1,
        )
        await get_book_drilldown(
            guarded,
            "preflight-corpus",
            "preflight-doc",
            [],
            limit=1,
            chunk_limit=1,
        )

        decorator = GraphDecorator()
        decorator._driver = guarded
        decorator._settings.NEO4J_ENABLED = True
        await decorator._decorate_via_relates_to(
            winning_chunk_refs=[fake_ref],
            corpus_ids=["preflight-corpus"],
            wanted_families=None,
            neighbor_limit=1,
            chunks_per_neighbor=1,
        )
        await decorator._decorate_via_calls(
            winning_chunk_refs=[fake_ref],
            corpus_ids=["preflight-corpus"],
            neighbor_limit=1,
            chunks_per_neighbor=1,
        )

        expansion = ModeAExpansion()
        expansion._driver = guarded
        await expansion._expand_via_mentions(
            [fake_ref],
            ["preflight-corpus"],
            1,
        )
        await expansion._expand_via_calls(
            [fake_ref],
            ["preflight-corpus"],
            1,
        )

        fake_chunk = SourceChunk(
            corpus_id="preflight-corpus",
            doc_id="preflight-doc",
            chunk_id="preflight-chunk",
            parent_id="preflight-parent",
            text="preflight",
            score=0.5,
            source_tier="preflight",
        )
        await apply_graph_degree_boost(
            [fake_chunk.model_copy(deep=True)],
            ["preflight-corpus"],
            guarded,
        )
        await apply_graph_degree_boost_metrics_aware(
            [fake_chunk.model_copy(deep=True)],
            ["preflight-corpus"],
            guarded,
            db=None,
        )

        receipt = {
            "schema_version": "e2e_identity_live_preflight.v1",
            "read_only": True,
            "missing_required_identity_properties": missing_required,
            "summary_tree_indexes": summary_index_shape,
            "neo4j_constraints": constraints,
            "cypher_candidates_compiled": compiled,
            "cypher_compile_failures": failures,
            "dynamic_read_probes": 10,
            "dynamic_read_failures": dynamic_failures,
        }
        print(json.dumps(receipt, sort_keys=True, indent=2, default=str))
        require(not failures, f"Cypher EXPLAIN failures={len(failures)}")
        require(
            not dynamic_failures,
            f"dynamic read query failures={len(dynamic_failures)}",
        )
    finally:
        await neo4j.close()
        mongo.close()


if __name__ == "__main__":
    asyncio.run(main())
