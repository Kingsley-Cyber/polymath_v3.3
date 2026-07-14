# Retrieval Optimization Plan — query-side design (2026-07-14, senior draft on owner store layout)

Owner-delivered store layout (verbatim; extends the FINAL_SCHEMA family list —
claim vectors marked ILLUSTRATIVE by owner = candidate family, promotion-gated):

- **MongoDB:** documents · chapters · sections · parents · children · sentences
  · claims (illustrative) · spans · relations · domain assignments · frame
  instances · latent concepts · motif realizations · aggregate profiles ·
  cross-links · extraction runs
- **Vector (Qdrant), 8 families:** source-child · context-enriched child ·
  atomic claim (illustrative) · parent summary · document summary · latent
  concept · motif description · cross-domain analogy
- **Neo4j:** Document · Chapter · Parent · Child · Claim · Concept ·
  LatentConcept · Domain · FrameInstance · Superframe · Motif ·
  CrossDomainLink · entity relations

## 1. Instruction-aware embedding contract (Qwen3 asymmetric profile)

Rule of record: **documents/artifacts embed RAW; queries embed WITH a
per-family task instruction** (`Instruct: {task}\nQuery: {query}` format).
Each vector family is a distinct retrieval task, so each gets its own query
instruction. Instructions are VERSIONED RECIPE DATA
(backend/registries/embedding_instruction_registry.v1.PROPOSED.json — owner
approves like bindings), recorded in every ProjectionManifest
(embedding_profile = model + dims + quant + instruction_version), and cached
per (family, query) — one extra embed per family actually searched, amortized
by the P1.7 cache. Changing an instruction = new registry version + recall
A/B; never silent (it shifts the query point in vector space).

## 2. Mode → family matrix (permissions operationalized)

| Family | FACTUAL | EXPLANATORY | CROSS_DOMAIN | EXPLORATORY | CREATIVE_TRANSFER | CONTRAST |
|---|---|---|---|---|---|---|
| source-child | ● | ● | ● | ● | ○ | ● |
| context-enriched child | ● | ● | ● | ● | ○ | ● |
| atomic claim (when built) | ● | ● | ● | ○ | ○ | ● |
| parent summary | ● | ● | ● | ● | ● | ● |
| document summary (Tier-0 routing) | ● | ● | ● | ● | ● | ● |
| latent concept | ✗ | ○ validated-only | ● | ● incl. provisional | ● | ○ |
| motif description | ✗ | ○ validated-only | ● | ● | ● | ● (same-frames-different-outcome) |
| cross-domain analogy | ✗ | ✗ | ● | ● | ● maximized | ○ |

● = searched with full weight · ○ = searched at reduced weight · ✗ = not
searched. Grounding permission is SEPARATE from recall: whatever a family
recalls, only validated source-backed claims/evidence may independently ground
FACTUAL statements (owner permission ladder). This matrix is policy data
(versioned), never hardcoded branches.

## 3. Staged funnel with budgets (per query)

1. **Query understanding** (deterministic first): lexicon/alias resolution
   (S5 registry), constraint extraction (temporal etc.), mode classification
   (deterministic cues; planner escalation only when thin — P1.2 rules).
   Budget: ≤150ms cached / ≤2s planner path.
2. **Tier-0 routing**: document-summary vectors + librarian cards → candidate
   docs/shelves (existing seam, unchanged).
3. **Family fan-out**: parallel Qdrant searches over the mode's ● families,
   bounded k per family (initial: children 40, parents 20, claims 30, latent
   12, motif 8, analogy 8 — recipe params, tuned by A/B only). One
   instruction-embedded query vector per family (cached). Qdrant server time
   recorded separately per family (checklist requirement).
4. **Lineage dedupe + fusion**: collapse hits to parent lineage (a parent
   reachable via its child, its summary, AND a claim is ONE candidate with a
   family-hit profile, not three seats). Fusion = rank-based (RRF-style)
   within family, then family weights by mode matrix × assignment_state
   permission; raw cosine scores are NEVER compared across families
   (different tasks → incomparable distributions). Per-family calibration
   data recorded for later learned fusion.
5. **Seat selection**: existing calibrated corpus reservation + shelf roles
   (reservation_policy + shelf_engine — unchanged authority).
6. **Rerank**: cross-encoder = sole scoring authority over the fused
   shortlist (≤64 pairs; cascade per P1.9 when the 4B lands). Rerank sees the
   candidate's SOURCE TEXT (child/parent), never the latent/motif description
   that recalled it — recall lanes must not leak generated text into scoring.
7. **Hydration + grounding**: deterministic waterfall (unchanged); claims and
   evidence refs attach to seated parents; permission ladder enforced at
   answer assembly (FACTUAL grounding = validated source-backed only).

## 4. Graph roles per mode

Neo4j is expansion + verification, not primary recall: FACTUAL/EXPLANATORY may
verify claim-support paths (asserted/validated partitions only); CROSS_DOMAIN/
CREATIVE_TRANSFER traverse CrossDomainLink/Motif/LatentConcept (provisional
expansion partition permitted, results carry assignment_state); CONTRAST asks
for FrameInstances sharing superframe+roles with differing outcome/conditions.
Graph hop budget: ≤2 hops, ≤200 nodes per query (recipe params).

## 5. Gates before any of this goes live

- Family instruction registry owner-approved; A/B per family activation
  (frozen 58q + new lay-language additions at S10): each family must earn its
  seat — plain-language/cross-corpus doc-hit +N with no shape regression,
  else it stays recall-dark.
- Lineage-dedupe unit tests (one parent, many routes → one candidate).
- Fusion never lets a ✗-family contribute; permission tests per mode.
- Latency: per-stage budgets above; whole-query within +20% of current tier
  baselines at each activation step.
- ProjectionManifests updated with embedding_profile incl. instruction_version
  BEFORE any family is populated (P2.5b).

Checklist anchor: new subsection "P2.2c Query-Side Retrieval Optimization"
(boxes added 2026-07-14). Consumes: S5 alias registry, P2.2 representation
points, S11 semantic artifacts as they land — activation is family-by-family,
each behind its own A/B.
