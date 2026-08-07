#!/usr/bin/env python3
"""Regenerate the two deterministic Markdown fixtures and exact-offset gold sidecars."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def entity(surface: str, entity_type: str, status: str = "accept") -> dict:
    return {"surface": surface, "type": entity_type, "status": status}


def relation(subject: str, predicate: str | None, object_: str, lane: str = "accept", **kwargs) -> dict:
    value = {"subject": subject, "predicate": predicate, "object": object_, "lane": lane}
    value.update(kwargs)
    return value


def sentence(text: str, entities=(), relations=(), negative_entities=(), aliases=()) -> dict:
    return {
        "text": text,
        "entities": list(entities),
        "relations": list(relations),
        "negative_entities": list(negative_entities),
        "aliases": list(aliases),
    }


QUALITY_SECTIONS = [
    ("Core Systems and Canonical Predicates", [
        sentence("Polymath uses MongoDB to store canonical evidence.",
                 [entity("Polymath", "Software"), entity("MongoDB", "Software"), entity("canonical evidence", "Concept")],
                 [relation("Polymath", "uses", "MongoDB")]),
        sentence("Polymath depends on Qdrant for semantic retrieval.",
                 [entity("Polymath", "Software"), entity("Qdrant", "Software"), entity("semantic retrieval", "Concept")],
                 [relation("Polymath", "depends_on", "Qdrant")]),
        sentence("Qdrant supports vector search.",
                 [entity("Qdrant", "Software"), entity("vector search", "Concept")],
                 [relation("Qdrant", "supports", "vector search")]),
        sentence("Neo4j is part of the Graphify projection layer.",
                 [entity("Neo4j", "Software"), entity("Graphify projection layer", "Process")],
                 [relation("Neo4j", "part_of", "Graphify projection layer")]),
        sentence("The ingestion worker produces RelationAssertion records.",
                 [entity("ingestion worker", "Process"), entity("RelationAssertion records", "Dataset")],
                 [relation("ingestion worker", "produces", "RelationAssertion records")]),
        sentence("The graph writer consumes validated assertion batches.",
                 [entity("graph writer", "Process"), entity("validated assertion batches", "Dataset")],
                 [relation("graph writer", "consumes", "validated assertion batches")]),
        sentence("Northstar Labs owns the Atlas Dataset.",
                 [entity("Northstar Labs", "Org"), entity("Atlas Dataset", "Dataset")],
                 [relation("Northstar Labs", "owns", "Atlas Dataset")]),
        sentence("Malformed aliases cause duplicate nodes.",
                 [entity("Malformed aliases", "Concept"), entity("duplicate nodes", "Concept")],
                 [relation("Malformed aliases", "causes", "duplicate nodes")]),
        sentence("The derived index is derived from the canonical document store.",
                 [entity("derived index", "Dataset"), entity("canonical document store", "Software")],
                 [relation("derived index", "derived_from", "canonical document store")]),
        sentence("The Architecture Guide defines the rebuildability contract.",
                 [entity("Architecture Guide", "Document"), entity("rebuildability contract", "Concept")],
                 [relation("Architecture Guide", "defines", "rebuildability contract")]),
        sentence("Graphify implements the Projection API.",
                 [entity("Graphify", "Software"), entity("Projection API", "Service")],
                 [relation("Graphify", "implements", "Projection API")]),
        sentence("The Evidence Ledger is related to the Run Ledger.",
                 [entity("Evidence Ledger", "Document"), entity("Run Ledger", "Document")],
                 [relation("Evidence Ledger", "related_to", "Run Ledger")]),
    ]),
    ("Libraries, Services, Datasets, and Processes", [
        sentence("Graphify uses Pydantic for contract validation.",
                 [entity("Graphify", "Software"), entity("Pydantic", "Library"), entity("contract validation", "Process")],
                 [relation("Graphify", "uses", "Pydantic")]),
        sentence("Graphify uses spaCy for dependency parsing.",
                 [entity("Graphify", "Software"), entity("spaCy", "Library"), entity("dependency parsing", "Process")],
                 [relation("Graphify", "uses", "spaCy")]),
        sentence("The ingestion service depends on Redis for transient coordination.",
                 [entity("ingestion service", "Service"), entity("Redis", "Software"), entity("transient coordination", "Process")],
                 [relation("ingestion service", "depends_on", "Redis")]),
        sentence("The API Gateway supports the Query Service.",
                 [entity("API Gateway", "Service"), entity("Query Service", "Service")],
                 [relation("API Gateway", "supports", "Query Service")]),
        sentence("The Query Service consumes the Search Dataset.",
                 [entity("Query Service", "Service"), entity("Search Dataset", "Dataset")],
                 [relation("Query Service", "consumes", "Search Dataset")]),
        sentence("The Report Builder produces the Evaluation Report.",
                 [entity("Report Builder", "Software"), entity("Evaluation Report", "Document")],
                 [relation("Report Builder", "produces", "Evaluation Report")]),
        sentence("The Projection Worker implements the Graph Write Process.",
                 [entity("Projection Worker", "Service"), entity("Graph Write Process", "Process")],
                 [relation("Projection Worker", "implements", "Graph Write Process")]),
        sentence("The Graph Write Process is part of the ingestion workflow.",
                 [entity("Graph Write Process", "Process"), entity("ingestion workflow", "Process")],
                 [relation("Graph Write Process", "part_of", "ingestion workflow")]),
    ]),
    ("Passive Voice and Direction", [
        sentence("The Projection API is implemented by Graphify.",
                 [entity("Projection API", "Service"), entity("Graphify", "Software")],
                 [relation("Graphify", "implements", "Projection API", voice="passive")]),
        sentence("Validated assertion batches are consumed by the graph writer.",
                 [entity("Validated assertion batches", "Dataset"), entity("graph writer", "Process")],
                 [relation("graph writer", "consumes", "Validated assertion batches", voice="passive")]),
        sentence("The Atlas Dataset is owned by Northstar Labs.",
                 [entity("Atlas Dataset", "Dataset"), entity("Northstar Labs", "Org")],
                 [relation("Northstar Labs", "owns", "Atlas Dataset", voice="passive")]),
        sentence("Rebuildability is defined by the Architecture Guide.",
                 [entity("Rebuildability", "Concept"), entity("Architecture Guide", "Document")],
                 [relation("Architecture Guide", "defines", "Rebuildability", voice="passive")]),
        sentence("Semantic retrieval is supported by Qdrant.",
                 [entity("Semantic retrieval", "Concept"), entity("Qdrant", "Software")],
                 [relation("Qdrant", "supports", "Semantic retrieval", voice="passive")]),
        sentence("The query planner is implemented by the Query Service.",
                 [entity("query planner", "Process"), entity("Query Service", "Service")],
                 [relation("Query Service", "implements", "query planner", voice="passive")]),
    ]),
    ("Definitions, Aliases, and Lowercase Names", [
        sentence("The Architecture Guide defines recoverability as the ability to reconstruct projections from canonical evidence.",
                 [entity("Architecture Guide", "Document"), entity("recoverability", "Concept"), entity("canonical evidence", "Concept")],
                 [relation("Architecture Guide", "defines", "recoverability")]),
        sentence("Retrieval-Augmented Generation (RAG) is a named retrieval concept.",
                 [entity("Retrieval-Augmented Generation", "Concept"), entity("RAG", "Concept")],
                 aliases=[{"canonical": "Retrieval-Augmented Generation", "alias": "RAG"}]),
        sentence("RAG depends on vector search.",
                 [entity("RAG", "Concept"), entity("vector search", "Concept")],
                 [relation("RAG", "depends_on", "vector search")]),
        sentence("Neo 4J, also written Neo4j, supports graph traversal.",
                 [entity("Neo 4J", "Software"), entity("Neo4j", "Software"), entity("graph traversal", "Concept")],
                 [relation("Neo4j", "supports", "graph traversal")],
                 aliases=[{"canonical": "Neo4j", "alias": "Neo 4J"}]),
        sentence("The Service Level Objective (SLO) defines the P95 Latency target.",
                 [entity("Service Level Objective", "Concept"), entity("SLO", "Concept"), entity("P95 Latency", "Metric")],
                 [relation("Service Level Objective", "defines", "P95 Latency")],
                 aliases=[{"canonical": "Service Level Objective", "alias": "SLO"}]),
        sentence("Pydantic is a named Python library for data validation.",
                 [entity("Pydantic", "Library"), entity("Python", "Software"), entity("data validation", "Process")]),
        sentence("pandas is a named Python library used for tabular analysis.",
                 [entity("pandas", "Library"), entity("Python", "Software"), entity("tabular analysis", "Process")]),
        sentence("redis is a named software product used for transient coordination.",
                 [entity("redis", "Software"), entity("transient coordination", "Process")]),
    ]),
    ("Coordination", [
        sentence("Graphify uses Pydantic, spaCy, and pandas.",
                 [entity("Graphify", "Software"), entity("Pydantic", "Library"), entity("spaCy", "Library"), entity("pandas", "Library")],
                 [relation("Graphify", "uses", "Pydantic"), relation("Graphify", "uses", "spaCy"), relation("Graphify", "uses", "pandas")]),
        sentence("MongoDB and Qdrant support the ingestion workflow.",
                 [entity("MongoDB", "Software"), entity("Qdrant", "Software"), entity("ingestion workflow", "Process")],
                 [relation("MongoDB", "supports", "ingestion workflow"), relation("Qdrant", "supports", "ingestion workflow")]),
        sentence("The Query Service consumes the Search Dataset and the Audit Dataset.",
                 [entity("Query Service", "Service"), entity("Search Dataset", "Dataset"), entity("Audit Dataset", "Dataset")],
                 [relation("Query Service", "consumes", "Search Dataset"), relation("Query Service", "consumes", "Audit Dataset")]),
        sentence("Northstar Labs owns the Atlas Dataset and the Recovery Dataset.",
                 [entity("Northstar Labs", "Org"), entity("Atlas Dataset", "Dataset"), entity("Recovery Dataset", "Dataset")],
                 [relation("Northstar Labs", "owns", "Atlas Dataset"), relation("Northstar Labs", "owns", "Recovery Dataset")]),
        sentence("The Report Builder produces the Evaluation Report and the Recovery Report.",
                 [entity("Report Builder", "Software"), entity("Evaluation Report", "Document"), entity("Recovery Report", "Document")],
                 [relation("Report Builder", "produces", "Evaluation Report"), relation("Report Builder", "produces", "Recovery Report")]),
        sentence("Qdrant supports vector search and hybrid search.",
                 [entity("Qdrant", "Software"), entity("vector search", "Concept"), entity("hybrid search", "Concept")],
                 [relation("Qdrant", "supports", "vector search"), relation("Qdrant", "supports", "hybrid search")]),
    ]),
    ("Negation, Modality, Conditions, and Attribution", [
        sentence("Polymath does not depend on SQLite for canonical evidence.",
                 [entity("Polymath", "Software"), entity("SQLite", "Software"), entity("canonical evidence", "Concept")],
                 [relation("Polymath", "depends_on", "SQLite", lane="qualified", polarity="negative")]),
        sentence("Graphify may support PostgreSQL.",
                 [entity("Graphify", "Software"), entity("PostgreSQL", "Software")],
                 [relation("Graphify", "supports", "PostgreSQL", lane="qualified", modality="possible")]),
        sentence("Maya Chen claims that Qdrant causes no data loss.",
                 [entity("Maya Chen", "Person"), entity("Qdrant", "Software"), entity("data loss", "Concept")],
                 [relation("Qdrant", "causes", "data loss", lane="qualified", polarity="negative", attribution="Maya Chen")]),
        sentence("The Evaluation Report suggests that MongoDB supports temporal queries.",
                 [entity("Evaluation Report", "Document"), entity("MongoDB", "Software"), entity("temporal queries", "Concept")],
                 [relation("MongoDB", "supports", "temporal queries", lane="qualified", attribution="Evaluation Report")]),
        sentence("If Redis fails, the ingestion service may depend on the Local Queue.",
                 [entity("Redis", "Software"), entity("ingestion service", "Service"), entity("Local Queue", "Service")],
                 [relation("ingestion service", "depends_on", "Local Queue", lane="qualified", modality="conditional")]),
        sentence("Northstar Labs denied that it owns the Phantom Dataset.",
                 [entity("Northstar Labs", "Org"), entity("Phantom Dataset", "Dataset")],
                 [relation("Northstar Labs", "owns", "Phantom Dataset", lane="qualified", polarity="negative", attribution="Northstar Labs")]),
        sentence("The Architecture Guide recommends that Graphify use Pydantic.",
                 [entity("Architecture Guide", "Document"), entity("Graphify", "Software"), entity("Pydantic", "Library")],
                 [relation("Graphify", "uses", "Pydantic", lane="qualified", modality="recommended", attribution="Architecture Guide")]),
        sentence("The August Migration might cause temporary latency.",
                 [entity("August Migration", "Event"), entity("temporary latency", "Metric")],
                 [relation("August Migration", "causes", "temporary latency", lane="qualified", modality="possible")]),
    ]),
    ("Generic Noun and Pronoun Traps", [
        sentence("The successor was discussed in the directive.", negative_entities=["successor", "directive"]),
        sentence("This approach uses a process.", negative_entities=["This approach", "process"]),
        sentence("The owner owns the end goal.", negative_entities=["owner", "end goal"],
                 relations=[relation("owner", "owns", "end goal", lane="reject")]),
        sentence("Taylor & Francis produces information.",
                 [entity("Taylor & Francis", "Org")],
                 [relation("Taylor & Francis", "produces", "information", lane="reject")],
                 negative_entities=["information"]),
        sentence("The system supports the component.", negative_entities=["system", "component"]),
        sentence("The version depends on the process.", negative_entities=["version", "process"]),
        sentence("It uses MongoDB.", [entity("MongoDB", "Software")], negative_entities=["It"]),
        sentence("They support Qdrant.", [entity("Qdrant", "Software")], negative_entities=["They"]),
        sentence("A directive causes a change.", negative_entities=["directive", "change"]),
        sentence("The method implements the approach.", negative_entities=["method", "approach"]),
    ]),
    ("Ambiguous Surface Forms", [
        sentence("Go is a named programming language used by the Graphify CLI.",
                 [entity("Go", "Software"), entity("Graphify CLI", "Software")],
                 [relation("Graphify CLI", "uses", "Go", voice="passive")]),
        sentence("Operators go to the control room in El Paso.",
                 [entity("El Paso", "Location")], negative_entities=["go"]),
        sentence("Apple owns the Orion Lab.",
                 [entity("Apple", "Org"), entity("Orion Lab", "Org")],
                 [relation("Apple", "owns", "Orion Lab")]),
        sentence("The apple caused a stain on the table.", negative_entities=["apple", "stain"]),
        sentence("Python implements the parser prototype.",
                 [entity("Python", "Software"), entity("parser prototype", "Process")],
                 [relation("Python", "implements", "parser prototype")]),
        sentence("A python crossed the road in El Paso.",
                 [entity("El Paso", "Location")], negative_entities=["python"]),
        sentence("Rust supports the Command Line Service.",
                 [entity("Rust", "Software"), entity("Command Line Service", "Service")],
                 [relation("Rust", "supports", "Command Line Service")]),
        sentence("The rust caused a surface defect.", negative_entities=["rust", "surface defect"]),
        sentence("Oracle supports the Billing Service.",
                 [entity("Oracle", "Org"), entity("Billing Service", "Service")],
                 [relation("Oracle", "supports", "Billing Service")]),
        sentence("The oracle predicted an event.", negative_entities=["oracle", "event"]),
    ]),
    ("Metrics, Locations, and Events", [
        sentence("The Evaluation Report defines P95 Latency as 120 milliseconds.",
                 [entity("Evaluation Report", "Document"), entity("P95 Latency", "Metric"), entity("120 milliseconds", "Metric")],
                 [relation("Evaluation Report", "defines", "P95 Latency")]),
        sentence("The August Migration occurred in El Paso.",
                 [entity("August Migration", "Event"), entity("El Paso", "Location")],
                 [relation("August Migration", None, "El Paso", lane="open", surface_predicate="occurred in")]),
        sentence("The August Migration caused temporary latency.",
                 [entity("August Migration", "Event"), entity("temporary latency", "Metric")],
                 [relation("August Migration", "causes", "temporary latency")]),
        sentence("The Recovery Test Event produced the Recovery Report.",
                 [entity("Recovery Test Event", "Event"), entity("Recovery Report", "Document")],
                 [relation("Recovery Test Event", "produces", "Recovery Report")]),
        sentence("The Audit Process consumes the Recovery Dataset.",
                 [entity("Audit Process", "Process"), entity("Recovery Dataset", "Dataset")],
                 [relation("Audit Process", "consumes", "Recovery Dataset")]),
        sentence("The Deployment Process uses the Identity Service.",
                 [entity("Deployment Process", "Process"), entity("Identity Service", "Service")],
                 [relation("Deployment Process", "uses", "Identity Service")]),
        sentence("The Identity Service depends on the Projection API.",
                 [entity("Identity Service", "Service"), entity("Projection API", "Service")],
                 [relation("Identity Service", "depends_on", "Projection API")]),
        sentence("The Projection API supports the Deployment Process.",
                 [entity("Projection API", "Service"), entity("Deployment Process", "Process")],
                 [relation("Projection API", "supports", "Deployment Process")]),
    ]),
    ("Open Relations", [
        sentence("Maya Chen served in El Paso.",
                 [entity("Maya Chen", "Person"), entity("El Paso", "Location")],
                 [relation("Maya Chen", None, "El Paso", lane="open", surface_predicate="served in")]),
        sentence("Northstar Labs published the Atlas Dataset.",
                 [entity("Northstar Labs", "Org"), entity("Atlas Dataset", "Dataset")],
                 [relation("Northstar Labs", None, "Atlas Dataset", lane="open", surface_predicate="published")]),
        sentence("The Recovery Report was authored by Maya Chen.",
                 [entity("Recovery Report", "Document"), entity("Maya Chen", "Person")],
                 [relation("Maya Chen", None, "Recovery Report", lane="open", surface_predicate="authored", voice="passive")]),
        sentence("Graphify interoperates with the Identity Service.",
                 [entity("Graphify", "Software"), entity("Identity Service", "Service")],
                 [relation("Graphify", None, "Identity Service", lane="open", surface_predicate="interoperates with")]),
        sentence("The August Migration occurred after the Recovery Test Event.",
                 [entity("August Migration", "Event"), entity("Recovery Test Event", "Event")],
                 [relation("August Migration", None, "Recovery Test Event", lane="open", surface_predicate="occurred after")]),
        sentence("The Atlas Dataset was curated by Northstar Labs.",
                 [entity("Atlas Dataset", "Dataset"), entity("Northstar Labs", "Org")],
                 [relation("Northstar Labs", None, "Atlas Dataset", lane="open", surface_predicate="curated", voice="passive")]),
    ]),
    ("Complex Frames and Tail Cases", [
        sentence("Graphify's implementation of the Projection API depends on Pydantic.",
                 [entity("Graphify's implementation", "Process"), entity("Projection API", "Service"), entity("Pydantic", "Library")],
                 [relation("Graphify's implementation", "depends_on", "Pydantic")]),
        sentence("The Audit Worker that consumes the Audit Dataset produces the Evaluation Report.",
                 [entity("Audit Worker", "Service"), entity("Audit Dataset", "Dataset"), entity("Evaluation Report", "Document")],
                 [relation("Audit Worker", "consumes", "Audit Dataset"), relation("Audit Worker", "produces", "Evaluation Report")]),
        sentence("Qdrant, which supports vector search, depends on the Identity Service.",
                 [entity("Qdrant", "Software"), entity("vector search", "Concept"), entity("Identity Service", "Service")],
                 [relation("Qdrant", "supports", "vector search"), relation("Qdrant", "depends_on", "Identity Service")]),
        sentence("The Report Builder, a named software tool, produces reports.",
                 [entity("Report Builder", "Software")],
                 [relation("Report Builder", "produces", "reports", lane="reject")],
                 negative_entities=["reports"]),
        sentence("The Rebuild Process, which uses MongoDB, produces Neo4j projections.",
                 [entity("Rebuild Process", "Process"), entity("MongoDB", "Software"), entity("Neo4j projections", "Dataset")],
                 [relation("Rebuild Process", "uses", "MongoDB"), relation("Rebuild Process", "produces", "Neo4j projections")]),
        sentence("The Audit Service was built on top of Redis.",
                 [entity("Audit Service", "Service"), entity("Redis", "Software")],
                 [relation("Audit Service", "depends_on", "Redis", voice="passive")]),
        sentence("The Search Service is a component of the Query Platform.",
                 [entity("Search Service", "Service"), entity("Query Platform", "Software")],
                 [relation("Search Service", "part_of", "Query Platform")]),
        sentence("MongoDB, Qdrant, Neo4j, Redis, and PostgreSQL support the Data Platform, the Query Platform, and the Audit Platform.",
                 [entity("MongoDB", "Software"), entity("Qdrant", "Software"), entity("Neo4j", "Software"), entity("Redis", "Software"), entity("PostgreSQL", "Software"), entity("Data Platform", "Software"), entity("Query Platform", "Software"), entity("Audit Platform", "Software")],
                 [relation(s, "supports", o) for s in ["MongoDB", "Qdrant", "Neo4j", "Redis", "PostgreSQL"] for o in ["Data Platform", "Query Platform", "Audit Platform"]]),
    ]),
]


def locate(haystack: str, needle: str, start: int, end: int) -> tuple[int, int]:
    pos = haystack.find(needle, start, end)
    if pos < 0:
        raise ValueError(f"Could not locate {needle!r} in range {start}:{end}")
    return pos, pos + len(needle)


def build_quality() -> None:
    chunks = ["# Graphify Quality Fixture", "", "This fixed document exercises entity quality, exact offsets, predicate direction, assertion qualification, open relations, ambiguous names, and pathological coordination.", ""]
    sentence_specs = []
    for heading, sentences in QUALITY_SECTIONS:
        chunks.extend([f"## {heading}", ""])
        for spec in sentences:
            chunks.extend([spec["text"], ""])
            sentence_specs.append(spec)
    text = "\n".join(chunks).rstrip() + "\n"
    md_path = FIXTURES / "graphify_quality_fixture.md"
    md_path.write_text(text, encoding="utf-8")

    entities = []
    negative_entities = []
    relations = []
    aliases = []
    cursor = 0
    for index, spec in enumerate(sentence_specs, 1):
        sentence_start = text.find(spec["text"], cursor)
        if sentence_start < 0:
            raise ValueError(spec["text"])
        sentence_end = sentence_start + len(spec["text"])
        cursor = sentence_end
        local_entity_offsets = {}
        for item in spec["entities"]:
            start, end = locate(text, item["surface"], sentence_start, sentence_end)
            local_entity_offsets.setdefault(item["surface"], []).append((start, end))
            entities.append({
                "sentence_index": index,
                "surface": item["surface"],
                "type": item["type"],
                "status": item["status"],
                "start": start,
                "end": end,
                "evidence_start": sentence_start,
                "evidence_end": sentence_end,
                "evidence": spec["text"],
            })
        for surface in spec["negative_entities"]:
            start, end = locate(text, surface, sentence_start, sentence_end)
            negative_entities.append({
                "sentence_index": index,
                "surface": surface,
                "start": start,
                "end": end,
                "evidence_start": sentence_start,
                "evidence_end": sentence_end,
                "evidence": spec["text"],
            })
        for item in spec["relations"]:
            subject_start, subject_end = locate(text, item["subject"], sentence_start, sentence_end)
            object_start, object_end = locate(text, item["object"], sentence_start, sentence_end)
            enriched = {
                "sentence_index": index,
                **item,
                "subject_start": subject_start,
                "subject_end": subject_end,
                "object_start": object_start,
                "object_end": object_end,
                "evidence_start": sentence_start,
                "evidence_end": sentence_end,
                "evidence": spec["text"],
            }
            relations.append(enriched)
        for item in spec["aliases"]:
            canonical_start, canonical_end = locate(text, item["canonical"], sentence_start, sentence_end)
            alias_start, alias_end = locate(text, item["alias"], sentence_start, sentence_end)
            aliases.append({
                "sentence_index": index,
                **item,
                "canonical_start": canonical_start,
                "canonical_end": canonical_end,
                "alias_start": alias_start,
                "alias_end": alias_end,
                "evidence": spec["text"],
            })

    gold = {
        "fixture_version": "1.0.0",
        "document_id": "graphify_quality_fixture",
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "sentence_count": len(sentence_specs),
        "entities": entities,
        "negative_entities": negative_entities,
        "relations": relations,
        "aliases": aliases,
        "canonical_entity_types": ["Person", "Org", "Software", "Library", "Service", "Dataset", "Document", "Concept", "Process", "Metric", "Location", "Event"],
        "canonical_predicates": ["uses", "depends_on", "supports", "part_of", "produces", "consumes", "owns", "causes", "derived_from", "defines", "implements", "related_to"],
        "notes": [
            "Relations with lane=qualified must not become unconditional positive graph edges.",
            "Relations with lane=open preserve surface predicates without forced canonical mapping.",
            "Relations with lane=reject and negative_entities are adversarial negatives.",
            "The final coordination sentence intentionally creates a local tail case."
        ],
    }
    (FIXTURES / "graphify_quality_gold.json").write_text(json.dumps(gold, indent=2) + "\n", encoding="utf-8")


def throughput_section(i: int) -> tuple[str, list[dict], list[dict]]:
    service = f"Corpus Service {i:03d}"
    dataset = f"Evidence Dataset {i:03d}"
    report = f"Architecture Note {i:03d}"
    concept = f"Recovery Concept {i:03d}"
    process = f"Projection Process {i:03d}"
    batch = f"Assertion Batch {i:03d}"
    derived = f"Derived Index {i:03d}"
    canonical = f"Canonical Store {i:03d}"
    sentences = [
        f"{service} uses MongoDB and Qdrant.",
        f"{service} depends on Redis.",
        f"Qdrant supports Vector Search {i:03d}.",
        f"Neo4j is part of {process}.",
        f"{process} produces {batch}.",
        f"Graph Writer {i:03d} consumes {batch}.",
        f"{report} defines {concept}.",
        f"{derived} is derived from {canonical}.",
        f"Northstar Labs owns {dataset}.",
    ]
    entities = [
        (service, "Service"), ("MongoDB", "Software"), ("Qdrant", "Software"), ("Redis", "Software"),
        (f"Vector Search {i:03d}", "Concept"), ("Neo4j", "Software"), (process, "Process"),
        (batch, "Dataset"), (f"Graph Writer {i:03d}", "Process"), (report, "Document"),
        (concept, "Concept"), (derived, "Dataset"), (canonical, "Software"), ("Northstar Labs", "Org"),
        (dataset, "Dataset"),
    ]
    relations = [
        (service, "uses", "MongoDB", sentences[0]),
        (service, "uses", "Qdrant", sentences[0]),
        (service, "depends_on", "Redis", sentences[1]),
        ("Qdrant", "supports", f"Vector Search {i:03d}", sentences[2]),
        ("Neo4j", "part_of", process, sentences[3]),
        (process, "produces", batch, sentences[4]),
        (f"Graph Writer {i:03d}", "consumes", batch, sentences[5]),
        (report, "defines", concept, sentences[6]),
        (derived, "derived_from", canonical, sentences[7]),
        ("Northstar Labs", "owns", dataset, sentences[8]),
    ]
    block = [f"## Module {i:03d} — Corpus Projection", "", "Internal training copy — repeated footer — do not index.", ""]
    block.extend([item + "\n" for item in sentences])
    if i % 5 == 0:
        block.extend([
            "",
            "| Component | Role |",
            "|---|---|",
            f"| {service} | service |",
            f"| {dataset} | dataset |",
        ])
    if i % 7 == 0:
        block.extend([
            "",
            "```python",
            f"CONFIG_{i:03d} = {{\"service\": \"{service}\", \"dataset\": \"{dataset}\"}}",
            "```",
        ])
    if i % 10 == 0:
        block.extend([
            "",
            f"MongoDB, Qdrant, Neo4j, Redis, PostgreSQL, Pydantic, spaCy, pandas, and FastAPI support Data Platform {i:03d}, Query Platform {i:03d}, and Audit Platform {i:03d}.",
        ])
    if i % 20 == 0:
        block.extend([
            "",
            "### References",
            "",
            f"1. Repeated Reference Entry {i:03d}.",
            f"2. Repeated Reference Entry {i:03d}.",
            f"3. Repeated Reference Entry {i:03d}.",
        ])
    return "\n".join(block).rstrip() + "\n", entities, relations


def build_throughput() -> None:
    header = "# Graphify Throughput Fixture\n\nThis deterministic document measures corpus-stage throughput, relation eligibility, structural routing, repeated furniture, and p95/p99 tail behavior.\n\n"
    blocks = []
    section_meta = []
    for i in range(1, 161):
        block, entities, relations = throughput_section(i)
        blocks.append(block)
        section_meta.append((i, block, entities, relations))
    bibliography = "\n## Global Bibliography and Index\n\n" + "\n".join(f"- Bibliography item {i:04d}: generic reference information." for i in range(1, 501)) + "\n"
    text = header + "\n".join(blocks) + bibliography
    md_path = FIXTURES / "graphify_throughput_fixture.md"
    md_path.write_text(text, encoding="utf-8")

    samples = {1, 2, 5, 7, 10, 20, 40, 60, 80, 100, 120, 140, 150, 160}
    sampled_entities = []
    sampled_relations = []
    search_cursor = len(header)
    for i, block, entities, relations in section_meta:
        block_start = text.find(block, search_cursor)
        if block_start < 0:
            raise ValueError(f"section {i}")
        block_end = block_start + len(block)
        search_cursor = block_end
        if i not in samples:
            continue
        for surface, entity_type in entities:
            pos = text.find(surface, block_start, block_end)
            if pos >= 0:
                sampled_entities.append({"section": i, "surface": surface, "type": entity_type, "start": pos, "end": pos + len(surface)})
        for subject, predicate, object_, evidence in relations:
            evidence_start = text.find(evidence, block_start, block_end)
            if evidence_start < 0:
                raise ValueError(evidence)
            evidence_end = evidence_start + len(evidence)
            s_start = text.find(subject, evidence_start, evidence_end)
            o_start = text.find(object_, evidence_start, evidence_end)
            sampled_relations.append({
                "section": i,
                "subject": subject,
                "predicate": predicate,
                "object": object_,
                "subject_start": s_start,
                "subject_end": s_start + len(subject),
                "object_start": o_start,
                "object_end": o_start + len(object_),
                "evidence_start": evidence_start,
                "evidence_end": evidence_end,
                "evidence": evidence,
            })

    gold = {
        "fixture_version": "1.0.0",
        "document_id": "graphify_throughput_fixture",
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "bytes": len(text.encode()),
        "module_count": 160,
        "repeated_furniture_text": "Internal training copy — repeated footer — do not index.",
        "repeated_furniture_count": text.count("Internal training copy — repeated footer — do not index."),
        "code_block_count": text.count("```python"),
        "module_table_count": text.count("| Component | Role |"),
        "local_reference_section_count": text.count("### References"),
        "global_bibliography_items": 500,
        "minimum_expected_extraction_windows": 200,
        "sampled_sections": sorted(samples),
        "sampled_entities": sampled_entities,
        "sampled_relations": sampled_relations,
        "notes": [
            "The repeated footer is intentional furniture.",
            "Code, tables, local references, global bibliography, and coordination tails require explicit routing or splitting.",
            "Sampled gold is stratified; this fixture is primarily a fixed throughput and tail benchmark."
        ],
    }
    (FIXTURES / "graphify_throughput_gold.json").write_text(json.dumps(gold, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    build_quality()
    build_throughput()
    print(FIXTURES / "graphify_quality_fixture.md")
    print(FIXTURES / "graphify_throughput_fixture.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
