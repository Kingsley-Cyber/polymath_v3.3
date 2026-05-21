# Graph schema — Neo4j idiom + Foundry Ontology mapping

This doc expresses the Polymath knowledge graph in **Cypher / Neo4j conventions** (familiar idiom) and then maps each node and relationship to its **Foundry Ontology** equivalent (where it actually lives).

> **You do not run a Neo4j cluster.** The graph IS the Foundry Ontology. The Cypher notation is for designing and discussing the schema in a familiar form.

---

## 1. Nodes (Neo4j idiom)

```cypher
// Corpus — top-level grouping
(:Corpus {
  corpus_id: string,
  name: string,
  description: string,
  owner: string,
  created_at: datetime,
  document_count: int,
  default_chunking_profile: string         // 'fine' | 'balanced' | 'coarse'
})

// Source — origin of a Document
(:Source {
  source_id: string,
  kind: string,                            // 'web' | 'upload' | 'api' | 'drive'
  uri: string,
  label: string,                           // 'primary' | 'secondary' | 'news' | 'opinion' | 'dataset'
  last_fetched_at: datetime,
  health_score: float,
  status: string                           // 'active' | 'paused' | 'broken'
})

// Document — ingested artifact, versioned
(:Document {
  document_id: string,
  title: string,
  corpus_id: string,
  source_id: string,
  content_sha256: string,
  version: int,
  status: string,                          // 'draft' | 'indexed' | 'canon' | 'archived'
  ingested_at: datetime,
  token_count: int,
  summary: string,
  tags: list<string>
})

// Chunk — segment of a Document with an embedding
(:Chunk {
  chunk_id: string,
  document_id: string,
  corpus_id: string,                       // denormalized for index facet
  ordinal: int,
  text: string,
  token_count: int,
  embedding: list<float>,                  // 1024-dim
  chunk_type: string,                      // 'paragraph' | 'table' | 'list' | 'code' | 'heading'
  headings: list<string>,
  page: int,                               // nullable; PDF only
  bbox: string,                            // nullable; PDF only; JSON-encoded
  start_ms: int,                           // nullable; audio only
  end_ms: int                              // nullable; audio only
})

// Entity — named entity
(:Entity {
  entity_id: string,
  canonical_name: string,
  aliases: list<string>,
  entity_type: string,                     // 'person' | 'org' | 'place' | 'system' | 'concept' |
                                           // 'event' | 'product' | 'doctrine'
  canonical_uri: string,
  description: string,
  merged_into: string                      // nullable; if set, this entity is archived
})

// Claim — subject-predicate-object assertion
(:Claim {
  claim_id: string,
  statement: string,
  predicate: string,
  confidence: float,
  flagged: bool,
  flag_reason: string,                     // admin-only
  flagged_by: string
})

// Conversation — chat session
(:Conversation {
  conversation_id: string,
  owner: string,
  title: string,
  created_at: datetime,
  message_count: int,
  last_message_at: datetime
})

// Message — single turn
(:Message {
  message_id: string,
  conversation_id: string,
  role: string,                            // 'user' | 'assistant' | 'system'
  content: string,
  created_at: datetime,
  latency_ms: int,
  token_count: int,
  tools_used: list<string>
})

// Citation — pointer from a Message to a Chunk
(:Citation {
  citation_id: string,
  message_id: string,
  chunk_id: string,
  ordinal: int,
  span_start: int,
  span_end: int,
  rerank_score: float
})

// IngestionJob — operational record
(:IngestionJob {
  job_id: string,
  source_id: string,
  status: string,
  started_at: datetime,
  finished_at: datetime,
  documents_created: int,
  chunks_created: int,
  error: string,
  triggered_by_action: string
})

// EvalRun — eval suite run
(:EvalRun {
  eval_run_id: string,
  suite_name: string,
  started_at: datetime,
  finished_at: datetime,
  metrics: string,                         // JSON
  commit_ref: string,
  passed: bool
})
```

---

## 2. Relationships (Neo4j idiom)

```cypher
// Containment
(:Document)-[:HAS_CHUNK]->(:Chunk)
(:Document)-[:BELONGS_TO_CORPUS]->(:Corpus)
(:Document)-[:FROM_SOURCE]->(:Source)
(:Document)-[:PRODUCED_BY]->(:IngestionJob)

// Semantic extraction
(:Chunk)-[:MENTIONS {span_start: int, span_end: int, mention_text: string, score: float}]->(:Entity)
(:Chunk)-[:SUPPORTS {score: float}]->(:Claim)
(:Claim)-[:SUBJECT_OF]->(:Entity)
(:Claim)-[:OBJECT_OF]->(:Entity)
(:Entity)-[:RELATED_TO {predicate: string, confidence: float}]->(:Entity)

// Conversation
(:Conversation)-[:HAS_MESSAGE]->(:Message)
(:Message)-[:CITES]->(:Citation)
(:Citation)-[:EVIDENCE]->(:Chunk)
(:Conversation)-[:SCOPED_TO]->(:Corpus)
```

---

## 3. Indexes (Neo4j idiom)

```cypher
// Primary key constraints
CREATE CONSTRAINT corpus_pk        IF NOT EXISTS FOR (n:Corpus)        REQUIRE n.corpus_id        IS UNIQUE;
CREATE CONSTRAINT source_pk        IF NOT EXISTS FOR (n:Source)        REQUIRE n.source_id        IS UNIQUE;
CREATE CONSTRAINT document_pk      IF NOT EXISTS FOR (n:Document)      REQUIRE n.document_id      IS UNIQUE;
CREATE CONSTRAINT chunk_pk         IF NOT EXISTS FOR (n:Chunk)         REQUIRE n.chunk_id         IS UNIQUE;
CREATE CONSTRAINT entity_pk        IF NOT EXISTS FOR (n:Entity)        REQUIRE n.entity_id        IS UNIQUE;
CREATE CONSTRAINT claim_pk         IF NOT EXISTS FOR (n:Claim)         REQUIRE n.claim_id         IS UNIQUE;
CREATE CONSTRAINT conversation_pk  IF NOT EXISTS FOR (n:Conversation)  REQUIRE n.conversation_id  IS UNIQUE;
CREATE CONSTRAINT message_pk       IF NOT EXISTS FOR (n:Message)       REQUIRE n.message_id       IS UNIQUE;
CREATE CONSTRAINT citation_pk      IF NOT EXISTS FOR (n:Citation)      REQUIRE n.citation_id      IS UNIQUE;

// Lookup indexes
CREATE INDEX entity_canonical IF NOT EXISTS FOR (e:Entity)   ON (e.canonical_name, e.entity_type);
CREATE INDEX chunk_corpus     IF NOT EXISTS FOR (c:Chunk)    ON (c.corpus_id);
CREATE INDEX chunk_doc        IF NOT EXISTS FOR (c:Chunk)    ON (c.document_id);
CREATE INDEX document_status  IF NOT EXISTS FOR (d:Document) ON (d.status);
CREATE INDEX claim_flag       IF NOT EXISTS FOR (cl:Claim)   ON (cl.flagged);

// Full-text index for lexical retrieval
CREATE FULLTEXT INDEX chunk_text_fulltext IF NOT EXISTS FOR (c:Chunk) ON EACH [c.text];

// Vector index (Neo4j 5+ syntax)
CREATE VECTOR INDEX chunk_embedding_vec IF NOT EXISTS FOR (c:Chunk) ON c.embedding
  OPTIONS { indexConfig: { `vector.dimensions`: 1024, `vector.similarity_function`: 'cosine' } };
```

---

## 4. Common traversals (Cypher examples)

### 4.1 Find all chunks supporting claims about a given entity

```cypher
MATCH (e:Entity {canonical_name: "Maven Smart System"})
      <-[:SUBJECT_OF|OBJECT_OF]-(cl:Claim)<-[:SUPPORTS]-(c:Chunk)<-[:HAS_CHUNK]-(d:Document)
WHERE d.status IN ['indexed', 'canon']
  AND d.corpus_id IN $corpus_ids
RETURN c.chunk_id, c.text, d.title, cl.statement, cl.confidence
ORDER BY cl.confidence DESC
LIMIT 20;
```

### 4.2 Two-hop entity neighbors

```cypher
MATCH (e:Entity {canonical_name: "NATO"})-[:RELATED_TO*1..2]-(neighbor:Entity)
WHERE neighbor.canonical_name <> "NATO"
RETURN DISTINCT neighbor.canonical_name, neighbor.entity_type
LIMIT 50;
```

### 4.3 Show all evidence for a given assistant Message

```cypher
MATCH (m:Message {message_id: $message_id})-[:CITES]->(cit:Citation)-[:EVIDENCE]->(c:Chunk)
      <-[:HAS_CHUNK]-(d:Document)-[:FROM_SOURCE]->(s:Source)
RETURN cit.ordinal, c.text, c.page, d.title, s.uri, s.label
ORDER BY cit.ordinal;
```

### 4.4 Find contested (flagged) claims and which Messages cited them

```cypher
MATCH (cl:Claim {flagged: true})<-[:SUPPORTS]-(c:Chunk)<-[:EVIDENCE]-(cit:Citation)
      <-[:CITES]-(m:Message)
RETURN cl.statement, count(DISTINCT m) AS impacted_messages
ORDER BY impacted_messages DESC;
```

### 4.5 Hybrid retrieval (vector + filter)

```cypher
CALL db.index.vector.queryNodes('chunk_embedding_vec', 40, $query_vector)
YIELD node AS c, score
MATCH (c)<-[:HAS_CHUNK]-(d:Document)
WHERE d.status IN ['indexed', 'canon']
  AND d.corpus_id IN $corpus_ids
RETURN c.chunk_id, c.text, score
ORDER BY score DESC
LIMIT 40;
```

### 4.6 Surface high-coverage entities for a corpus

```cypher
MATCH (e:Entity)<-[:MENTIONS]-(c:Chunk)<-[:HAS_CHUNK]-(d:Document)
WHERE d.corpus_id = $corpus_id AND d.status IN ['indexed', 'canon']
RETURN e.canonical_name, e.entity_type, count(DISTINCT d) AS doc_coverage
ORDER BY doc_coverage DESC
LIMIT 25;
```

---

## 5. Mapping — Neo4j → Foundry Ontology

The same shape lives in Foundry as object types and link types. Translation table:

### 5.1 Nodes → Object types

| Neo4j label | Foundry object type | Backing dataset |
|---|---|---|
| `:Corpus` | `Corpus` | `/Polymath/clean/corpora` |
| `:Source` | `Source` | `/Polymath/clean/sources` |
| `:Document` | `Document` | `/Polymath/clean/documents` |
| `:Chunk` | `Chunk` | `/Polymath/clean/chunks_embedded` |
| `:Entity` | `Entity` | `/Polymath/clean/entities` |
| `:Claim` | `Claim` | `/Polymath/clean/claims` |
| `:Conversation` | `Conversation` | `/Polymath/conversations/conversations` |
| `:Message` | `Message` | `/Polymath/conversations/messages` |
| `:Citation` | `Citation` | `/Polymath/conversations/citations` |
| `:IngestionJob` | `IngestionJob` | `/Polymath/ops/ingestion_jobs` |
| `:EvalRun` | `EvalRun` | `/Polymath/ops/eval_runs` |

### 5.2 Relationships → Link types

| Neo4j relationship | Foundry link type | Backing |
|---|---|---|
| `[:HAS_CHUNK]` | `has_chunks` | implicit via Chunk.document_id |
| `[:BELONGS_TO_CORPUS]` | `belongs_to_corpus` | implicit via Document.corpus_id |
| `[:FROM_SOURCE]` | `from_source` | implicit via Document.source_id |
| `[:PRODUCED_BY]` | `produced_by` | implicit via Document.ingestion_job_id |
| `[:MENTIONS]` | `mentions` | `/Polymath/links/chunk_mentions_entity` |
| `[:SUPPORTS]` | `supports` | `/Polymath/links/chunk_supports_claim` |
| `[:SUBJECT_OF]` | `subject_of` | implicit via Claim.subject_entity_id |
| `[:OBJECT_OF]` | `object_of` | implicit via Claim.object_entity_id |
| `[:RELATED_TO]` | `related_to` | `/Polymath/links/entity_related_to_entity` |
| `[:HAS_MESSAGE]` | `has_messages` | implicit via Message.conversation_id |
| `[:CITES]` | `cites` | implicit via Citation.message_id |
| `[:EVIDENCE]` | `evidence_chunk` | implicit via Citation.chunk_id |
| `[:SCOPED_TO]` | `scoped_to` | implicit via Conversation.corpora_scope |

### 5.3 Indexes → Foundry equivalents

| Neo4j construct | Foundry equivalent |
|---|---|
| Primary key constraint | Object type PK + dataset unique constraint |
| `CREATE INDEX … ON (…)` | Object Storage V2 secondary indexes (declared in Ontology Manager) |
| `CREATE FULLTEXT INDEX … ON c.text` | Foundry text index over `Chunk.text` |
| `CREATE VECTOR INDEX … ON c.embedding` | Foundry Vector Search Service, index `polymath_chunks` |

### 5.4 Queries → Foundry equivalents

| Neo4j idiom | Foundry equivalent |
|---|---|
| `MATCH (e:Entity)-[:RELATED_TO]-(n)` | `Entity.objects().traverse("related_to").all()` (OSDK / Function) |
| `CALL db.index.vector.queryNodes(...)` | `polymath_lib.vector_search.ann_search(...)` (used in `hybrid_search`) |
| `WHERE n.corpus_id IN $list` | Vector Search facet filter, or AIP object-query filter |
| `RETURN node, score` | Function returns typed list of Chunk objects |

---

## 6. Intent + Reasoning

- **Why use Neo4j idiom at all if we don't run Neo4j.** Two reasons. First, you (the operator) think in graph terms — Neo4j is a familiar lens for designing the schema. Second, anyone reviewing the schema later (an FDE, a teammate) will recognize the pattern even if they've never used Foundry.
- **Why the graph IS the Ontology, not a separate cluster.** Running a Neo4j cluster alongside Foundry means two writers, two indexes, two backup stories, two ACL models. Every operator complaint about Polymath v3.3 traced back to keeping Qdrant + Neo4j + MongoDB in sync. The Ontology unifies them.
- **Why some Neo4j relationships are "implicit via FK" in Foundry.** Foundry's link types come in two flavors: stored (a dedicated dataset) and implicit (computed from a foreign-key column). One-to-many parent/child relationships (Document→Chunk, Conversation→Message) work fine as implicit links and avoid a join table. Many-to-many relationships (Chunk↔Entity, Chunk↔Claim, Entity↔Entity) need explicit link datasets.
- **Why we keep span offsets on the `MENTIONS` edge.** A chunk can mention the same entity multiple times with different spans. The mention is a property of the relationship, not the entity.
- **Why `RELATED_TO` has a `predicate` property.** Two entities can be related in multiple ways ("A reports to B", "A trained B", "A succeeded B"). The predicate carries the relation type without exploding the schema into 50 named relationships.
- **Why the vector index lives on the `:Chunk` node and not as a side store.** Co-locating the embedding with the addressable unit means a single query returns chunk + score + everything else needed for citation. No second hop.
- **Why traversal queries are written in Cypher here and not Foundry's native query DSL.** Cypher is universally readable. The Foundry equivalents live in `polymath_lib` (a thin wrapper) so the Functions that call them stay terse.
