# Three-Schema Extraction Design — owner-delivered 2026-07-14 (design of record)

Owner framing: "example and concept only" — field NAMES finalize inside P2.5b
envelope integration, but the SEPARATION, ownership, vocabularies, and flow are
authoritative. Do not force entity/predicate/relation extraction into the
digest schema.

## The separation

```
child chunk  → LocalExtractionV1   (spaCy + GLiNER + GLiREL: mentions, predicates, relation candidates)
             → ClaimRecordV1       (Python claim compiler: atomic claims + qualifiers + evidence
                                    + domain candidates + frame instances w/ role_bindings)
parent packet→ SemanticDigestV1    (ONE LLM call: summary, underlying meanings, latent concepts,
                                    adjacent domains, motif proposals)
hard cases   → ClaimRepairV1       (constrained LLM repair; NEVER invents mention IDs/offsets —
                                    Python maps repaired text back to real spans)
Python       → MongoDB, Neo4j, and vector projections (uniform schema across all three)
```

## LocalExtractionV1 (owner-delivered pydantic, verbatim)

```python
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

# EntityType / PredicateType / Modality / Polarity literals:
# see backend/registries/extraction_vocabularies.v1.json (25 / 17 / 6 / 2)

class EntityMention(StrictModel):
    mention_id: str; text: str; entity_type: EntityType
    start_char: int; end_char: int; canonical_label: str; confidence: float

class PredicateMention(StrictModel):
    predicate_id: str; surface_text: str; lemma: str
    normalized_predicate: PredicateType; start_char: int; end_char: int
    negated: bool; modality: Modality; confidence: float

class RelationCandidate(StrictModel):
    relation_id: str; source_mention_id: str; predicate_id: str
    target_mention_id: str; relation_type: PredicateType
    condition_mention_ids: list[str]; temporal_mention_ids: list[str]
    evidence_sentence_ids: list[str]; confidence: float

class LocalExtractionV1(StrictModel):
    schema_version: Literal["local_extraction.v1"]
    document_id: str; child_id: str; sentence_ids: list[str]
    entities: list[EntityMention]; predicates: list[PredicateMention]
    relations: list[RelationCandidate]; unresolved_spans: list[str]
```

## Field ownership

| Field | Producer | Notes |
|---|---|---|
| sentence boundaries, subject/verb/object, negation, modality, dependencies, conditions, temporal modifiers | spaCy | first-pass linguistic structure |
| entities (mention_id, entity_type, canonical_label) | GLiNER | both real-world and semantic entities; Python resolves canonical_label via alias registry |
| predicates (surface → normalized_predicate) | spaCy surface + **Python predicate normalizer** ("lower" → DECREASES) | controlled PredicateType only |
| relations (mention-linked candidates) | GLiREL / GLiNER-Relex | candidates; Python may reject on dependency-parse conflict |
| compiled claims | **Python claim compiler** | entity/source + normalized predicate + entity/target + qualifiers + evidence = atomic claim; participant_roles (e.g. affected_agent) retained; multiple predicates → multiple claims + optional `claim RESULTS_IN claim` |
| domain candidates | Python (concept-to-domain registry + predicate types + section heading), each with derivation_method + evidence_refs | LLM adds non-obvious adjacent domains only |
| frame instances | Python frame rules over predicate + roles (e.g. DECREASES + repeated + BASELINE → MF15; UPDATES internal baseline → MF07), with role_bindings mapping mention IDs into frame roles | |
| digest (summary, latent, motifs, adjacent domains) | parent-level LLM, ONE call per packet | normalized/validated/permissioned by Python |

## LLM entry conditions (only these)

spaCy cannot resolve predicate structure · GLiNER/GLiREL disagree materially ·
claim spans multiple sentences · unresolved coreference · implicit relation.
Repair uses `ClaimRepairV1` (resolved_subject_text / normalized_predicate /
resolved_object_text / modality / negated / supporting_sentence_ids /
explanation) — Python re-anchors to real spans.

## Neo4j graph projection (edge contract, owner-delivered)

```
(Document)-[:HAS_CHAPTER]->(Chapter)
(Chapter)-[:HAS_PARENT]->(Parent)
(Parent)-[:HAS_CHILD]->(Child)
(Child)-[:SUPPORTS]->(Claim)
(Claim)-[:MENTIONS]->(Concept)
(Claim)-[:IN_DOMAIN]->(Domain)
(Claim)-[:EVOKES]->(FrameInstance)
(FrameInstance)-[:INSTANCE_OF]->(Superframe)
(Parent)-[:REALIZES]->(Motif)
(LatentConcept)-[:SUPPORTED_BY]->(Claim)
(Claim)-[:STRUCTURALLY_ANALOGOUS_TO]->(Claim)
(Claim)-[:SUPPORTS_CLAIM]->(Claim)
(Claim)-[:CONTRADICTS]->(Claim)
```

## Uniformity requirement

One schema/identity contract across Qdrant payloads, Neo4j nodes/edges, and
Mongo metadata: same IDs, same registry references, same assignment-state /
derivation-method fields everywhere — enforced through P2.5b's envelope,
ProjectionManifests, and outbox. The four Neo4j partitions (asserted claims /
validated semantic / provisional expansion / analogy) carry assignment_state so
permissioned query modes work identically across stores.
