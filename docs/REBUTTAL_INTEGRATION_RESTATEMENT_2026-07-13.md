# Rebuttal Integration — Corrected Restatement Before Edits (2026-07-13)

Status: APPROVED by owner 2026-07-14 (rulings C1-C4 confirmed); integration edits applied same day.
Source: owner's grounded rebuttal (delivered in-chat 2026-07-13/14). The full
motif/superframe/domain registry schemas are offered but not yet delivered —
requested as the companion input.

## 1. What was previously misunderstood (and is now corrected)

**M1 — Provisional material was locked out of retrieval; it must be a permissioned
soft-recall signal.** Codex's policy line "retrieval should initially consume only
parent/document aggregate profiles" is REPLACED: aggregate profiles are *stable
routing signals*; validated claim assignments, provisional latent concepts, and
motif candidates remain available as **weighted soft-recall signals**; only
validated source-backed claims may independently ground FACTUAL assertions.
Grounding-power and recall-visibility are different permissions, not one gate.

**M2 — Validation is not a binary accept/discard.** "LLM proposals → Python
validation → accepted assignments" is REPLACED by:
`LLM proposals → Python normalization → retained semantic candidates →
differentiated permissions → possible corroboration and promotion.`
Candidates are RETAINED with states; the state ladder controls what each artifact
may do (ground / recall / expand / explain), and corroboration promotes.

**M3 — GLiREL/Relex verdict needs re-scoping, not burial.** The 2026-07-13
benchmark (relation F1 0.174 oracle / 0.098 Relex) tested open-label relation
truth. The owner's architecture uses GLiREL under a **controlled relation-label
registry** (SIGNALS, MEASURES, COMPARES_AGAINST, CAUSES, INFLUENCES, INCREASES,
DECREASES, UPDATES, …) producing **candidates only**, which the Python claim
compiler cross-checks against the dependency parse and may reject. The correct
production gate is therefore: *compiled-claim quality with GLiREL candidates
under the controlled registry vs without*, on the gold fixture — not raw
span-pair F1. Until that re-benchmark passes, relations remain observation-only
(unchanged), but the architecture keeps GLiREL as Stage-4 owner.

**M4 — The digest call is per parent-packet and owns ALL generative outputs at
once**: summary, central thesis, cross-claim meaning, latent concept proposals,
implicit mechanism proposals, novel motif proposals, secondary-domain proposals —
one compact, grounded packet call (accepted local outputs only). The current
Ghost A `RetrievalSummary` remains typed separately until measured cutover
(consistent with P2.5b box "RetrievalSummary vs SemanticDigest").

## 2. The canonical processing pipeline (as ruled by the owner)

Document → Python+spaCy structure (sentences, grammar, negation, modality,
causal/comparison cues, temporal patterns, conditions) → GLiNER spans (real-world
entities + semantic entities; Python resolves aliases) → GLiREL explicit
relations (controlled labels; candidates) → **Python compiles atomic claims**
(grammar + spans + relations + negation + modality + conditions + temporal;
claim accepted only when subject/object identified, predicate supported,
qualifiers preserved, evidence offset exists, no dependency-parse conflict;
multiple claims per sentence with `RESULTS_IN` links; multi-sentence claims via
discourse rules + coreference, LLM escalation only for hard cases) →
deterministic domain + superframe mapping (registries + rule registry) →
parent evidence packet (5–10 children, accepted outputs only) → ONE LLM
semantic-digest call → Python normalization/validation/permission assignment →
motif compilation (normalized frame sequences, e.g. MF02→MF07→MF03→MF05) →
bottom-up aggregation (claims → parent → chapter → document profiles) →
cross-document connection mining (shared concept IDs, compatible superframes,
matching motif sequences, role bindings, embedding similarity, distinct-document
requirement; LLM explains, never asserts).

Ownership: GLiNER/GLiREL/spaCy/LLM produce candidates; **Python owns the accepted
semantic representation** (claim compiler, domain resolver, frame rules, motif
compiler, aggregation, IDs, writes).

## 3. Store layout ruled by the owner (slots into P2.5b ProjectionManifest)

- **Qdrant (5 dense families):** source-child; context-enriched child;
  parent-summary; latent-concept; motif/analogy embeddings.
- **MongoDB (hybrid):** exact source text; explicit aliases; generated aliases;
  domain labels; superframe labels; latent concepts; assignment states.
- **Neo4j (4 partitions):** asserted claim graph; validated semantic graph;
  provisional expansion graph; analogy graph.
Each family gets a ProjectionManifest (schema hash, representation role,
embedding profile, payload schema, rollback predecessor) — P2.5b unchanged.

## 4. Query modes (retrieval permission policies)

FACTUAL (observed/derived/validated only) · EXPLANATORY (+validated frames,
mechanisms, motifs) · CROSS_DOMAIN (+secondary-domain & motif expansion) ·
EXPLORATORY (+provisional latent concepts & analogies) · CREATIVE_TRANSFER
(maximize distant structural connections) · CONTRAST (similar frames, different
outcomes/conditions). Modes select permission mixes over lanes/indexes; they are
a new axis alongside answer-shape routing (P1.6), not a replacement.

## 5. Confirmed invariants (unchanged from both prior documents)

Document labels never auto-copied onto children · claims are the primary
evidence-grounded unit · domain ≠ superframe ≠ motif · motifs derive from ordered
frame patterns · UNRESOLVED / NO_MECHANISM are legitimate outcomes · profiles
aggregate bottom-up from accepted claims · LLM output = proposal/synthesis ·
every assignment retains evidence/supporting-claim refs · hierarchical domain
fallback · hard cardinalities (1 primary + fixed-N secondary) are POLICY
DEFAULTS, never immutable schema · chunks connect through compatible
superframes/roles/causal direction/motifs/conditions/concept mappings — never
mere topical similarity · unknown concepts go to the parent LLM as unresolved
candidates, never auto-become new domains · a document cannot form cross-book
connections alone.

## 6. Open conflicts flagged for owner ruling (the only ones found)

**C1 — Chunk sizing.** Rebuttal: child 150–300 tokens; parent = 5–10 related
children. Current system: 128-token children (A/B-validated ChildTokenBudget)
and structural parents. PROPOSED RESOLUTION: keep current chunking; treat the
"parent evidence packet" as a *packet assembler* over existing structure (a
parent, or a 5–10-child window when structural parents are larger/smaller) —
re-chunking would invalidate every existing artifact and the A/B evidence.
Confirm or overrule.

**C2 — GLiREL re-benchmark gate.** Adopt M3's re-scoped benchmark (controlled
registry + claim-compiler harness on the gold fixture) as the go/no-go for
Stage-4 GLiREL in production. Until then relations = candidates/observation-only.
Confirm.

**C3 — "Context-enriched child embeddings"** = a new representation family.
PROPOSED: implement as a P2.2 representation point kind under the existing
multi-point prereqs (P1.1 ✓, P1.7 ✓), not a new parallel mechanism. Confirm.

**C4 — Registry schemas.** The full motif/superframe/domain schemas are the
remaining authoritative input (MF-ids seen: MF02 signaling, MF03
measurement/comparison, MF04 causal influence, MF05 valuation/preference, MF07
belief update, MF15 accumulation/threshold/path-dependence; domain examples:
D06.marketing.pricing, D04.cognitive_psychology.judgment). Deliver when ready —
registry snapshot formats in P2.5b consume them verbatim as versioned data.

## 7. Document changes this restatement implies (edits held until approval)

1. FINAL_SCHEMA_METADATA_ARCHITECTURE: replace the two policy sentences (M1, M2);
   add permission-state ladder to the accepted-lane definition; add the 5+7+4
   store families to the projection section; add query-mode permission table.
2. Checklist: P2.5b gains registry-snapshot boxes fed by owner schemas; S11
   stage list updated to the Stage-1..11 pipeline; GLiREL re-benchmark box added
   (C2); index-family manifests enumerated.
3. PLAN_CRITIQUE / EXECUTION_PLAN: S11 rows re-pointed at the staged pipeline;
   affected ledger rows get updated actions (dispositions unchanged elsewhere).
4. Assignment record shape: owner's `score_components` + roles
   (dominant/supporting/adjacent) merged with the already-agreed
   derivation_method / assignment_state split.
