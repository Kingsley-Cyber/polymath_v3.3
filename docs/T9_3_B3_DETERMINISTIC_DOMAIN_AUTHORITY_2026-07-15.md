# T9.3 B3 deterministic-domain authority policy

Status: **CONFIRMED — record-only policy; no build or activation**
Date: 2026-07-15
Authority: senior Lane B directive at 2026-07-14T22:5xZ and B1/B2 ruling at
2026-07-14T23:29:58Z in `COORDINATION.md`

## 5Ws

- **What:** domain coverage at activation comes from the deterministic T9.1
  resolver. `SemanticDigestV1.domain_proposals` are auxiliary, claim-grounded
  candidates; they are not the coverage mechanism.
- **Why:** the first 66 accepted mark digests proposed domains for only 13
  parents. Treating that proposal rate as coverage would make activation
  dependent on model verbosity and would incentivize unsupported output.
- **Who:** Python and the owner domain registry own resolution. The model may
  propose adjacent interpretations but cannot validate, promote, overwrite, or
  complete deterministic assignments.
- **Where:** this policy governs T9.3 generation, validation, retry decisions,
  acceptance metrics, later assignment compilation, and activation. It adds no
  Mongo, Qdrant, Neo4j, outbox, projection, or retrieval write.
- **When:** effective for B4 and every later mark packet. Existing accepted
  v5/v6 digests remain valid purchases; this policy changes neither their
  identities nor their acceptance state.

## Frozen authority boundary

| Concern | Authority | Required behavior |
|---|---|---|
| Domain coverage | T9.1 deterministic resolver | Exact normalized membership against `domain_registry.v1.json`; unresolved signals remain explicit. |
| LLM domain output | `SemanticDigestV1.domain_proposals` | Auxiliary candidate observations only; every emitted proposal must cite provider-visible claim IDs. |
| Registry validation | Python semantic validator | Known registry IDs are checked; unknown IDs remain candidate-only and cannot be promoted. |
| Empty proposal list | SemanticDigest schema and gateway | Lawful successful output. It is not a structural or semantic error. |
| Retry/repair | Gateway validation errors only | No retry, repair, parameter change, provider switch, or extra call because domain proposals are empty or sparse. |
| Matching | T9.1 versioned recipe | No prompt-invented alias, substring, stemming, fuzzy, embedding, inherited corpus label, or affinity prior may become an assignment. |
| Promotion | Separate governed compiler/activation lane | Model proposals never directly write an accepted domain or retrieval filter. |

## Merge and consumption rules

1. Deterministic assignments and model proposals remain separate typed inputs;
   neither list is silently unioned into accepted coverage.
2. A model proposal that exactly names an owner-registry ID is still a model
   candidate. It does not corroborate itself and cannot supersede contrary or
   absent deterministic evidence.
3. A model proposal outside the owner registry stays candidate-only for later
   governed review. Python does not fuzzy-map it to the nearest registry term.
4. An empty `domain_proposals` array is complete and acceptable when the model
   has no claim-grounded proposal. Fewer proposals are the correct result when
   support is uncertain.
5. Provider-visible proposal space is bounded to emitted packet-v2 claims.
   Sparse proposals are not automatically model failure, especially when
   locally durable claims were excluded by the deterministic byte bound.
6. Activation consumes deterministic T9.1 candidates under its own gates.
   Auxiliary model proposals may be measured or reviewed but may not become a
   hard retrieval filter without a separately versioned, owner-ratified rule.

## Acceptance and metrics

Report these independently:

- deterministic resolver assignment coverage and unresolved-signal rate;
- LLM domain proposal rate per accepted digest;
- proposal registry-ID validity and claim-reference validity;
- empty-domain-proposal count;
- proposal acceptance/rejection outcomes, if a later governed compiler exists.

Never combine deterministic assignments and model proposals into one coverage
numerator. Never fail the T9.3 canary solely because `domain_proposals` is
empty, and never raise proposal density by retrying sparse valid output.

## Existing enforcement evidence

- `docs/T9_1_DETERMINISTIC_RESOLVER_BOUNDARY_2026-07-14.md` freezes exact-only
  Python resolution, unresolved retention, and LLM non-promotion.
- `SemanticDigestV1` requires the `domain_proposals` property but lawfully
  accepts an empty array.
- `parent-digest.v6` and `parent-digest-repair.v3` explicitly state that empty
  proposal arrays are lawful and prefer omission over unsupported proposals.
- `semantic_validate` checks emitted proposals but adds no minimum count; the
  gateway repairs only structural or semantic validation failures.

## Non-effects and rollback

This record changes no prompt, schema, recipe hash, cache key, packet, provider
route, stored digest, activation state, or runtime. Rollback is removal of this
policy note only; any future behavior change requires its own versioned design,
tests, receipt, and owner-ratifiable authority.
