# Graph Authority Repair Directive — ADOPTED 2026-08-04

**Status:** BINDING owner architecture  
**Fact gate:** NOT weakened (`facts=[]` ⇒ no canonical Fact nodes)  
**Correction:** facts=0 must NOT imply Entity=0 / RelationAssertion=0 / Graph=Hybrid

## Three authority planes

1. **Structural** — Document/Section/Parent/Chunk navigation  
2. **Extracted assertion** — Entity + RelationAssertion + supporting Chunk (Relex)  
3. **Qualified fact** — Fact nodes under existing release gate  

## Immediate repair sequence (execution order)

1. Terminalize or honestly mark stuck extraction jobs  
2. Readiness: noop / metadata_backfill ≠ graph capability ready  
3. Promote entities + MENTIONS independently of facts  
4. Convert accepted Relex relations → RelationAssertion  
5. Promote assertions + SUPPORTS_ASSERTION independently of Fact  
6. Capability-aware Graph readiness + retrieval (no silent Hybrid)  
7. q9 canary acceptance  

## Non-goals

- Do not invent Cypher from LLMs  
- Do not auto-activate ontology proposals  
- Do not weaken qualified-fact release gates  
- Do not redesign Qdrant topology / QueryIR planner  
