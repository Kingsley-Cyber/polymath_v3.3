# Reliable Event Processing in a Rebuildable Data Platform

A production event pipeline is easier to operate when the system distinguishes **authoritative evidence** from the indexes and projections built for retrieval. This chapter follows a fictional platform, **Harbor**, operated by **Northstar Labs**, to show how that separation affects ingestion, replay, graph projection, and failure recovery. Harbor accepts telemetry from industrial sensors deployed at a regional facility in **El Paso**. The design is intentionally ordinary: a gateway accepts requests, workers normalize events, durable storage preserves evidence, and downstream services build query-oriented views.

The important architectural choice is that **MongoDB stores Harbor's canonical event evidence**. Search and graph databases are treated as rebuildable projections rather than independent sources of truth. **Harbor uses Qdrant for semantic retrieval**, while **Neo4j is part of the graph projection layer**. If either projection is lost, operators should be able to reconstruct it from canonical records without asking the original devices to retransmit data.

## 7.1 Admission and normalization

Every device request first reaches the **Harbor Gateway**. The Harbor Gateway uses **Envoy** for request routing and depends on the **Identity Service** for authentication. The Identity Service uses **PostgreSQL** to store service accounts and credential metadata. After authentication, the Harbor Gateway produces an **Event Envelope** containing the device identifier, an event timestamp, a schema identifier, and the payload bytes.

The **Schema Registry** defines the Event Envelope contract. The contract is versioned because sensor firmware is not upgraded everywhere at the same time. A new producer may emit version 4 while an older controller still emits version 2. The gateway therefore records the schema version instead of silently converting the payload.

Accepted envelopes are consumed by the **Ingestion Worker**. The Ingestion Worker uses **Pydantic** for contract validation and depends on **Kafka** for durable event transport. Kafka supports the **Event Transport Process**, which decouples admission from later processing. The worker produces a **Normalized Event** only after the envelope passes structural validation. A Normalized Event is derived from its Event Envelope; it is not a replacement for the original evidence.

The normalization code also uses **spaCy** to identify free-text equipment references when a device embeds an operator note. Those mentions are enrichment, not authority. If the note says, “pump seven may be overheating,” Harbor stores the original note even when the enrichment service fails to recognize the equipment name.

This distinction matters during incidents. A parser bug can cause malformed aliases, and malformed aliases can cause duplicate graph nodes. Deleting the source message would make the mistake difficult to repair. Preserving the Event Envelope means the team can correct normalization logic and replay the same bytes through a newer version of the worker.

> **Operational rule:** derived records may be discarded and rebuilt; canonical evidence must remain recoverable.

## 7.2 Canonical storage and projection

Once validation succeeds, the Ingestion Worker writes the accepted envelope and its normalized representation to the **Canonical Event Store** in MongoDB. The **Canonical Storage Process** uses MongoDB and produces the **Canonical Event Dataset**. Northstar Labs owns the Canonical Event Dataset, and the **Data Retention Guide** defines the retention policy for that dataset.

Downstream processing begins from a durable offset. The **Projection Worker** consumes the Canonical Event Dataset and produces **Projection Assertions**. Each assertion records a subject, a predicate, an object, and the source event that supports the statement. The Projection Worker implements the **Assertion Compilation Process**. It also depends on the Schema Registry so that field meanings are interpreted under the same contract used during admission.

Projection Assertions are not written directly to the graph. They are consumed by the **Validation Service**, which applies identity rules, predicate constraints, and provenance checks. The Validation Service produces a **Validated Assertion Batch**. The **Graph Writer** consumes Validated Assertion Batches and uses Neo4j to materialize graph relationships. Neo4j supports **Graph Traversal**, while Qdrant supports **Vector Search**. The **Query Service** uses both Neo4j and Qdrant to answer retrieval requests.

The two projections serve different access patterns. Vector Search is related to **Semantic Retrieval**, and Graph Traversal is related to **Relational Retrieval**. The Query Service implements the **Hybrid Retrieval Process**, which combines candidate passages from Qdrant with related entities and assertions from Neo4j. The Query Service depends on the **Authorization Service** before it returns protected evidence to a caller.

A small Redis deployment coordinates leases for projection jobs. The Projection Worker uses **Redis** for transient coordination, but Redis does not own canonical evidence. If Redis is unavailable, existing events remain recoverable because queue leases can be recreated from durable projection state. This is why the architecture does not define Redis as part of the source of truth.

The following configuration fragment is illustrative rather than authoritative:

```yaml
projection:
  semantic_index: qdrant
  graph_index: neo4j
  lease_store: redis
  batch_size: 250
```

A book reader should not infer additional architectural relationships merely because names appear together in the example configuration.

## 7.3 Replay, derived indexes, and recovery

Harbor tracks projection progress in the **Run Ledger**. The Run Ledger is related to the **Evidence Ledger**, which records source identifiers and checksums for accepted evidence. The **Recovery Guide** defines the **Replay Process**. During replay, the Replay Process consumes the Canonical Event Dataset and produces a **Rebuild Dataset** containing the events selected for reprocessing.

The Rebuild Dataset is then consumed by the Projection Worker. A **Derived Search Index** is derived from the Canonical Event Store, and a **Derived Graph Projection** is also derived from the Canonical Event Store. Neither derived artifact is allowed to become the only copy of information needed for recovery. The **Rebuildability Contract** defines this requirement, and the Architecture Guide defines the Rebuildability Contract for Harbor deployments.

The recovery sequence is intentionally boring. Operators stop new graph writes, record the last durable offset, recreate the damaged projection, and restart from that offset. The **Recovery Test Process** uses the Replay Process and produces a **Recovery Report**. The Recovery Report defines the measured **Recovery Time Objective** for the exercise. Northstar Labs uses the Recovery Report during quarterly operational reviews.

Passive constructions can obscure direction if an extractor relies only on word order. In Harbor, **Validated Assertion Batches are consumed by the Graph Writer**, and **the Hybrid Retrieval Process is implemented by the Query Service**. Likewise, **the Rebuildability Contract is defined by the Architecture Guide**. These statements describe the same direction as the active-voice descriptions above and should not create reversed duplicates.

## 7.4 Failure semantics and operational metrics

A rebuildable system still needs failure boundaries. The **Incident Response Guide** defines the **Projection Failure Process**. A projection failure may cause temporary search staleness, but it should not cause loss of canonical evidence. The **August Recovery Event** occurred in El Paso after a storage host failed during a maintenance window. That event caused temporary query latency while the graph projection was rebuilt.

The team measures **P95 Query Latency** and **Projection Lag**. The **Service Level Objective** defines P95 Query Latency as 450 milliseconds for the normal interactive path. The Service Level Objective also defines Projection Lag as less than 90 seconds during steady-state operation. The **Observability Service** consumes runtime metrics and produces the **Operations Dashboard**. The Operations Dashboard is related to the Recovery Report because both are reviewed during post-incident analysis.

Not every sentence should become a fact. Engineers sometimes say that “the system owns the problem,” or that “this approach uses a process.” Those phrases do not identify useful graph entities. Pronouns are also dangerous when their antecedents are uncertain. For example: “It uses Redis, and they support the platform.” A conservative extractor should avoid inventing a subject for either clause unless surrounding evidence resolves the reference.

Negation and modality need separate treatment from asserted facts. Harbor **does not depend on SQLite for canonical event storage**. The team **may support ClickHouse for analytical reporting** in a future release, but that possibility is not part of the current production architecture. An internal review suggested that Qdrant could support a future recommendation feature; the suggestion does not establish that the feature exists. Similarly, Northstar Labs denied that it owns the **Phantom Dataset**. A graph can preserve these statements as qualified assertions, but it should not silently convert them into positive production facts.

The chapter's central rule is therefore simple: **Harbor depends on canonical evidence for recovery, and its projections depend on that evidence for reconstruction**. The Canonical Event Store supports the Replay Process, the Replay Process supports projection recovery, and projection recovery supports continued query service after a projection failure. This chain is more useful to operations than a graph containing every nearby noun phrase.

---

### Review notes

1. Canonical evidence is durable; projections are rebuildable.
2. Direction matters for passive voice and derived artifacts.
3. Configuration examples, generic prose, negated claims, and future possibilities require different treatment from asserted production facts.
