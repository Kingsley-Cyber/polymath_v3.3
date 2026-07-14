# Semantic Extraction Production-Readiness Report

**Date:** 2026-07-13  
**Status:** architecture feasibility proven; extraction promotion blocked  
**Decision scope:** deterministic Python/spaCy, GLiNER, GLiREL, joint
GLiNER-Relex on RunPod, DeepSeek V4 Flash, LongCat, and the proposed
provider-neutral semantic contract  
**Evidence boundary:** repository code, the author's research, one human-curated
fixture, local execution, and authenticated provider/RunPod canaries. No outside
research was added.

## Executive verdict

The new research plan is a real architectural improvement over the current
future plan, but the improvement is the **hybrid boundary**, not any one model:

```text
durable hierarchy/text
  -> deterministic exact EvidenceRef
  -> trained spaCy observations and scoped claim candidates
  -> optional zero-shot span candidates
  -> optional schema-grounded LLM ambiguity refinement
  -> deterministic validation
  -> candidate ClaimAssertion
  -> later human/eval-gated acceptance and projection
```

That composition should replace the plan's implied expectation that either the
current GLiNER/GLiREL stack or the joint GLiNER-Relex worker will become the
semantic relation authority after scale testing. Both relation paths failed the
quality gate even though they returned schema-valid output. They remain useful
observation/candidate producers only.

The production answer is therefore **no, this extraction layer is not yet
enough for production-scale semantic acceptance**. The local stack demonstrated
adequate repeated-input capacity, and all live lanes completed without transport
failures, but semantic quality, corpus diversity, parser selection, frame/role
evaluation, and full operational scale have not cleared production gates.

Do not refactor Polymath wholesale yet. Land the provider-neutral contract and
annotate-only UGO seam, expand the labeled set, and refactor behind adapters only
after the new artifact proves replay, quality, and retrieval value.

## Updated 5Ws

- **Who:** deterministic Python owns identity, evidence validation, registry
  resolution, persistence state, and promotion. spaCy, GLiNER, GLiREL/Relex,
  DeepSeek, and LongCat are observation or proposal producers. Mongo remains
  authoritative; Qdrant and Neo4j consume rebuildable projections. Humans own
  registry and policy promotion.
- **What:** preserve the existing hierarchy, summaries, lexicon, and retrieval
  lanes, then add a provider-neutral `ObservationBundle`, exact `EvidenceRef`,
  and qualified `ClaimAssertionCandidate`. Use LLMs only for bounded,
  schema-grounded refinement; no model can self-label a candidate as accepted.
- **When:** freeze full envelope/hash/adapter contracts first; run UGO
  annotate-only next; expand and label the mark/ecommerce PoC slice; then run
  canary/100/500/5,000 gates only after quality clears. Production projection
  remains later than semantic acceptance.
- **Where:** source text and semantic truth stay in Mongo; the trained spaCy
  lane may remain local or become a pinned service; RunPod remains optional
  burst compute; model observations stay in observation records; only validated
  IDs and compact routing fields reach Qdrant/Neo4j under manifests.
- **Why:** the current ERE lanes can identify some entities and relations but do
  not reliably preserve modal force, negation, attribution, condition,
  exception, or proposition scope. The hybrid plan makes those distinctions
  explicit and replayable without turning model confidence into truth.

## What was implemented for this feasibility test

The implementation is deliberately bounded to contracts, deterministic
compilation, scoring, and read-only benchmarks. It is not a deployment or data
migration.

| Artifact | Purpose |
|---|---|
| `backend/models/semantic_artifacts.py` | canonical JSON/domain hashing, exact `EvidenceRef`, provider-neutral observations, and candidate-only claims |
| `backend/services/ingestion/semantic_observations.py` | trained-spaCy dependency/qualifier capture and deterministic claim-candidate compilation |
| `backend/evals/semantic_extraction_gold_v1.json` | 9 adversarial hand-labeled texts with 29 entities, 9 relations, and 12 qualified claims |
| `backend/evals/semantic_extraction_scoring.py` | entity/relation PRF, exact evidence/endpoint checks, and target-aware claim/qualifier scoring |
| `scripts/benchmark_semantic_extraction_local.py` | separate spaCy, GLiNER, oracle-entity GLiREL, composed GLiNER->GLiREL, and repeated capacity runs |
| `backend/scripts/benchmark_semantic_extraction_remote.py` | identical-fixture sidecar, RunPod, DeepSeek, LongCat, and optional claim-refinement runs |
| `backend/tests/test_semantic_observations.py` | exact evidence, reference integrity, qualification scope, and anti-self-promotion tests |

The executable contract enforces a critical boundary: an extractor-produced
claim has `knowledge_status=candidate`, and validation rejects any attempt by
that producer to set `validation_status=accepted`.

## Test design and its limits

The quality fixture contains causal, conditional, negated, recommended,
attributed/hypothetical, exception, temporal-revision, definition, and analogy
limitation cases. It is intentionally adversarial and human-curated, but it has
only 9 texts and 12 claims. It tests logical failure modes; it does not estimate
corpus-wide accuracy.

The 540-input local run repeats those 9 texts 60 times. It is a warm capacity
test only. It does not prove unique-document throughput, memory behavior under
long inputs, autoscaling, provider quotas, cost stability, distribution shift,
or production semantic quality.

The RunPod plan was staged correctly: canary quality came before 100/500/5,000
scale. Because the 9-sample relation gate failed at both tested thresholds, the
larger paid runs were stopped. Scaling a lane that is known to be semantically
wrong would measure cost, not readiness.

## Results

### Deterministic spaCy claim layer

Tested with spaCy 3.8.14 and `en_core_web_sm` 3.8.0 in the isolated local
extraction environment.

| Measure | Result |
|---|---:|
| Claim match | 11/12 (0.9167) |
| Matched-field accuracy | 0.9773 |
| Polarity accuracy | 1.0000 |
| Modal-force accuracy | 1.0000 |
| Claim-type accuracy | 1.0000 |
| Assertion-mode accuracy | 0.9091 |
| Qualifier precision / recall / F1 | 0.8500 / 1.0000 / 0.9189 |
| Condition recall | 1.0000 |
| Exception recall | 1.0000 |
| Evidence round-trip errors | 0 |

This result validates deterministic scoped observation capture as the primary
candidate compiler. It does not validate `en_core_web_sm` as the production
parser. The small model missed the second predicate `concern` in the semicolon
analogy-limitation case, and required a general dependency-shape repair for a
ROOT verb that it mistagged. A larger/trained pinned pipeline and a rule
fallback must be compared on the expanded labeled set before promotion.

### Local GLiNER and GLiREL separation

| Lane | Entity F1 | Relation P / R / F1 | 540 repeated-input throughput | Verdict |
|---|---:|---:|---:|---|
| GLiNER only | 0.5600 | n/a | 53.09 chunks/s | retain as span candidate source |
| GLiREL with oracle gold entities | 1.0000 by construction | 0.1429 / 0.2222 / 0.1739 | 30.46 chunks/s | relation quality fails even without entity error |
| GLiNER -> GLiREL | 0.5600 | 0.1667 / 0.1111 / 0.1333 | 47.77 chunks/s | do not promote relations |

The oracle-entity test is decisive: relation failure is not explained only by
GLiNER entity misses. The current fine-tuned GLiREL model/ontology does not
match this qualified semantic task closely enough.

### Remote and provider lanes on the identical fixture

| Lane | Entity F1 | Relation P / R / F1 | Wall throughput | Failures | Verdict |
|---|---:|---:|---:|---:|---|
| Existing local GLiNER/GLiREL sidecar | 0.5652 | 0.1667 / 0.1111 / 0.1333 | 10.78 chunks/s | 0 | parity with local quality failure |
| Joint GLiNER-Relex RunPod, threshold 0.75 | 0.5161 | 0 / 0 / 0 | 0.040 chunks/s | 0 | emitted no relations; blocked |
| Joint GLiNER-Relex RunPod, threshold 0.40 | 0.5161 | 0.0625 / 0.2222 / 0.0976 | 0.146 chunks/s | 0 | 2 true and 30 false relations; blocked |
| DeepSeek legacy ERE, corrected capability run | 0.7234 | 0.3000 / 0.3333 / 0.3158 | 0.962 chunks/s | 0 | candidate baseline only |
| LongCat legacy ERE | 0.8889 | 0.2308 / 0.3333 / 0.2727 | 0.407 chunks/s | 0 | strong entity candidate, relations blocked |

The joint RunPod threshold sweep rules out a simple calibration explanation:
0.75 suppressed every relation, while 0.40 produced 32 relations with only 2
true positives. This is a model/label/task alignment problem, not merely a
threshold problem. Its first full run also used 224 seconds and an estimated
USD 0.05468 for 9 short samples, so quality must be fixed before capacity or
cost tuning has value.

DeepSeek V4 Flash was also live-verified to support JSON object output but not
the OpenAI-style native JSON Schema response format used by its old provider
card. The card now selects `json_object` directly, avoiding one known HTTP 400
and fallback attempt per request. The corrected 9-sample run completed with no
failures.

### Schema-grounded LLM claim refinement

The optional refiners were separately asked for explicit evidence, predicate,
claim type, polarity, modality, assertion mode, conditions, exceptions, and
qualifier cues. Deterministic Python rejected off-schema or non-substring
output; valid model output remained candidate-only.

| Refiner | Claim match | Field accuracy | Qualifier F1 | Condition / exception recall | Validation drops |
|---|---:|---:|---:|---:|---:|
| DeepSeek V4 Flash | 0.7500 | 0.9167 | 0.7692 | 1.0000 / 1.0000 | 0 |
| LongCat | 0.9167 | 0.9545 | 0.7586 | 1.0000 / 1.0000 | 0 |

Both missed the subordinate `inspect` claim that deterministic spaCy captured.
LongCat matched the greatest number of claims; spaCy captured qualifiers more
completely. This is direct support for the author's modular design: combine
deterministic structure with bounded generative refinement instead of choosing
an LLM-only or relation-model-only architecture.

## Cross-reference against the current plan

| Current/future assumption | Evidence from this test | Plan disposition |
|---|---|---|
| GLiNER-Relex + spaCy can form one deterministic-first candidate lane | valid, but only if responsibilities are separated | adopt spaCy as scoped claim compiler; keep GLiNER as optional spans; withhold current Relex relations |
| Existing provider LLM should remain a comparison baseline | supported | retain DeepSeek/LongCat as candidate baselines and optional refiners |
| Parent semantic digest should consume accepted child claims | supported logically; not tested here | retain, but do not run until child acceptance exists |
| Model extraction can feed graph relations after schema/evidence checks | disproven as sufficient | add semantic precision/scope gates; schema-valid exact evidence alone is not promotion authority |
| RunPod 100/500/5,000 scale is the next action | disproven for current model/config | quality remediation and expanded labels precede further paid scale |
| Whole-system refactor is needed now | not supported | additive adapters and UGO annotate-only first |
| One provider-neutral schema can unify observations without hiding provenance | executable and unit-tested | adopt as the S11 front seam; finish the full envelope before capture |

## Production-readiness matrix

| Capability | Current state | Promotion decision |
|---|---|---|
| Exact chunk-local evidence | executable; 100% in fixture | continue toward UGO canary |
| Candidate-only anti-self-promotion | executable and tested | adopt |
| Scoped modal/negation/condition/exception capture | promising on 12 claims | expand labels; annotate-only |
| Entity candidate generation | usable, provider-dependent | retain multiple candidate lanes |
| Accepted relation extraction | low precision/recall across every tested lane | blocked |
| Frame and core-role extraction | not implemented or labeled | blocked |
| Sense/domain registry resolution | architecture only | blocked |
| Mongo envelope/outbox/projection integration | architecture only | blocked |
| Unique corpus-scale quality | not measured | blocked |
| RunPod burst cost/throughput | quality gate failed before scale | blocked |
| Retrieval improvement across Fast/Hybrid/Graph | not measured; annotate-only by design | blocked |

## Revised HOW plan

### H0. Freeze the semantic seam

1. Finish `polymath.artifact_envelope.v1`, full `ClaimAssertion`, registry
   snapshots, canonical hash golden vectors, adapters, Mongo indexes, and
   projection-manifest/outbox models.
2. Keep the executable `ObservationBundle` and `ClaimAssertionCandidate` as the
   candidate side of that contract; do not weaken the anti-self-promotion rule.
3. Record parser/model/prompt/ontology versions and exact evidence on every
   observation. Never infer source-global/page offsets from chunk-local text.

### H1. Build the annotate-only extractor

1. Run a pinned trained spaCy pipeline over each durable child and emit exact,
   target-aware predicates and qualifiers.
2. Allow GLiNER to add span candidates, but do not let it overwrite spaCy
   spans or accepted sense identity.
3. Disable promotion of current GLiREL/Relex relations. Store them only as
   versioned observations for comparison and later model/ontology work.
4. Call LongCat or DeepSeek only for ambiguity, missing scope, or bounded
   parent refinement. Require exact substring evidence and deterministic
   schema/registry validation.

### H2. Expand the gold set before changing production data

1. Label a diverse UGO plus mark/ecommerce set with atomic claims, qualifiers,
   source attribution, relation direction, entity/sense boundaries, abstention,
   and negative controls.
2. Include long/short chunks, lists, tables converted to text, quotations,
   semicolons, nested clauses, OCR noise, temporal statements, reversed
   causality, same-word/different-sense cases, and analogy break conditions.
3. Pre-register per-capability promotion gates from product risk. Keep 100%
   exact evidence and zero self-promotion as invariants; calibrate quality
   thresholds from the labeled corpus rather than copying research examples.
4. Compare at least the current small parser, one stronger pinned parser, and
   deterministic fallback rules. Record abstentions rather than forcing a
   claim.

### H3. UGO canary and replay

1. Write observations/candidates only to new Mongo collections; retrieval and
   graph behavior remain unchanged.
2. Prove byte-exact hashes, retry idempotency, source-version ownership,
   provider parity, validation drops, and deterministic replay.
3. Review false claims and missing scope manually. Update rule/registry recipe
   versions without changing semantic identity rules.

### H4. Quality-gated PoC and scale

1. Run the unique mark/ecommerce labeled canary only after H3 passes.
2. If and only if semantic gates pass, run 100 then 500 then 5,000 unique
   chunks while recording p50/p95, worker seconds, cost per 1,000, model load,
   cold/warm behavior, failure/retry, validation yield, and memory.
3. Re-run the quality sample inside every scale stage to detect a throughput
   configuration that changes output quality.
4. Capture the PoC pair once only after every required observation field is
   live. Do not re-extract `polymath_v2` under the current owner scope.

### H5. Shadow retrieval and bounded refactor

1. Promote only validated claims/frames through manifests and the outbox.
2. Shadow candidate routing across Fast, Hybrid, and Graph; preserve the
   original query and every current retrieval lane.
3. Measure cross-domain evidence gain, focused-query regression, harmful
   analogy controls, latency, and cost before any cutover.
4. Refactor legacy internals only where an adapter has proven parity and a
   rollback manifest exists.

## Reproducibility receipts

| Receipt | SHA-256 |
|---|---|
| `backend/evals/semantic_extraction_gold_v1.json` | `866d1b2104c7bc7d3a5696462058053a6bb041c8bd6784eae59a1f390d5e7816` |
| `docs/baselines/SEMANTIC_EXTRACTION_LOCAL_2026-07-13.json` | `aa86090f8d111717a38719c4755f7c89df0ddc583a3eef6aa5a1b2b442f9364f` |
| `docs/baselines/SEMANTIC_EXTRACTION_REMOTE_2026-07-13.json` | `92f20e143068c38616731e8bf855048658aecf12dc1a5529cb77415eb01d3a31` |
| `docs/baselines/SEMANTIC_EXTRACTION_RUNPOD_THRESHOLD_040_2026-07-13.json` | `530178135ae71240651c57ae3e34ea3a923e6444853ecd6e17e1709c4e5007c7` |
| `docs/baselines/SEMANTIC_EXTRACTION_DEEPSEEK_CAPABILITY_FIX_2026-07-13.json` | `01ef121f871b8c9672d3f2ad7cad95f80730031d0c03b02a5e3f556a464f6839` |

Security receipt: provider and RunPod credentials were resolved from encrypted
Settings. Benchmark artifacts contain neither plaintext credentials nor raw
provider responses. Credentials pasted into chat should still be rotated after
validation.

## Final decision

Adopt the research plan's modular architecture and update S11 accordingly:

- deterministic hierarchy, hashing, evidence, spaCy scope, and validation are
  the backbone;
- GLiNER contributes optional entity/span candidates;
- current GLiREL and joint GLiNER-Relex relations remain non-promotable
  observations;
- LongCat and DeepSeek may refine bounded candidate claims, never accept them;
- generative parent digests wait for accepted child claims;
- production refactor, graph projection, retrieval promotion, and RunPod scale
  remain blocked until the expanded labeled and replay gates pass.

This preserves the strongest parts of Polymath while replacing the weakest
future assumption: that a single extractor's confidence can stand in for a
qualified, evidence-backed semantic assertion.
