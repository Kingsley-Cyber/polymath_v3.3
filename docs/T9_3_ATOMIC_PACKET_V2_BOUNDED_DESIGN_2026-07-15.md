# T9.3 atomic packet v2 bounded-projection design

Status: **CERTIFIED by senior 2026-07-15 — production census and replay green**
Date: 2026-07-15
Scope: mark `SemanticDigest` provider packet projection only

Production closure supersedes the design-probe estimates below: 795 eligible
parents resolve to 793 packet-ready plus two permanently identity-ledgered
`source_child_without_atomic_claim` exclusions. Across the ready population,
84,247 source claims produce 20,960 provider-visible claims; typed retention is
347/347, negative retention is 5,873/5,876, and the three negative capacity
exceptions are permanently ledgered and locally authoritative. Packet bytes
are p0/p50/p95/max 9,142/19,870/19,996/20,000. Two fresh processes produced
byte-identical complete receipts and packet-set hash
`sha256:00960dbeb9d1704421a79ea1abd3b71112e316c66143b2cfe507c709c624bf04`.
The senior accepted 99.9489% negative retention as the approved
skip-and-continue capacity tradeoff; the cap and selection recipe did not
change. B4 provider calls remain blocked pending its zero-provider preflight
and explicit GO arithmetic.

## Authority and non-effects

This note implements the senior's 2026-07-15 STOP-before-B4 ruling in
`COORDINATION.md`. It does not alter the approved B1 eligibility recipe, the
3,493 immutable noncanonical child compilations, `ClaimRecordV1`, prompt v6,
the semantic validator, historical purchases, or any canonical store. Atomic
packet v1 remains frozen and unused. No B4 selection, provider call, job,
projection, activation, or spend is authorized by this note.

The measurements below came from a read-only, `/tmp`-only projection probe
over all 795 B1-eligible mark parents. The final receipt is
`/tmp/b2_v2_projection_census_v5.log`, true `EXIT=0`. The probe is not product
code and is not committed.

## Five Ws

- **Why:** lossless atomic packet v1 is a new reliability regime: p50 301,642,
  p95 360,251, and max 549,701 bytes. Its all-ready conservative authority is
  `$213.46170615`, above the `$200` owner-park line. More importantly, the
  14–26x packet-size increase versus the canaried class risks attention
  dilution, context failure, and a larger semantic-repair surface.
- **What:** a new `semantic_parent_packet.atomic_claims.v2` provider projection
  containing bounded canonical claim text plus IDs and minimal flags. Exact
  quotes and full claims remain in the local noncanonical compilation store;
  Python retains citation authority.
- **Who:** deterministic Python owns selection, closure, hashes, and citation
  validation. The provider may propose only against emitted claim IDs. The LLM
  never selects claims or promotes semantics.
- **Where:** additive models and packet builder in the backend, consumed only
  by a new B4/vNext selection identity. Mongo remains authoritative; no
  Qdrant/Neo4j or canonical semantic-artifact write is added.
- **When:** model/recipe/tests, two-process full census, B3 policy record, then
  a separately receipted zero-provider B4 preflight. The ten calls remain
  blocked until that preflight and senior review are green.

## Measured decision

Quote removal alone is insufficient, so a deterministic cap is required.

| Projection | p50 bytes | p95 bytes | max bytes | Verdict |
|---|---:|---:|---:|---|
| v1 lossless atomic | 301,642 | 360,251 | 549,701 | frozen unused |
| slim claims, parent text retained | 49,325 | 58,768 | 108,421 | reject |
| slim claims, parent text removed | 44,752 | 53,712 | 99,830 | reject |
| bounded v2, no parent text | 19,869 | 19,996 | 20,000 | propose |

The 20,000-byte bound leaves 1,515 bytes (7.04%) below the previously canaried
21,515-byte packet maximum. This reserve protects against small schema-field
growth and avoids treating the historical maximum as a target.

The bounded population emits 24,050/84,586 claims (28.4326%) and explicitly
excludes 60,536 (71.5674%) from provider visibility without deleting them.
Every source child is represented. The selection retains 349/349 typed claims,
5,901/5,901 negative claims, and 14,884/15,803 nuanced claims (94.1846%). The
cap applies to 774/795 parents. Emitted-claim p0/p50/p95/p100 is 8/30/33/43.

With current versioned LongCat price/parameter cards, one input token per UTF-8
byte, the full 8,192-token output cap, and 10% margin, authority is
`$0.43082628` for max-any-ten and `$34.09141890` for all 795. These are hard
conservative ceilings, not predicted spend, and do not reuse the old `$0.04`
assumption.

## Provider packet contract

### Top-level fields

`semantic_parent_packet.atomic_claims.v2` contains:

- corpus ID/name, document ID, and parent ID;
- `claims`, sorted by claim ID;
- current bounded `extraction_entities`, unchanged from v1;
- claim links only when both endpoints emit;
- a compact evidence/provenance contract; and
- a deterministic selection manifest.

`parent_text` is not sent. Exact evidence quote bodies are not sent. The model
receives claim texts, not duplicate full-source text. Both remain available to
Python through the source chunks and materialized compilation rows.

### Emitted claim fields

Each provider-visible claim contains exactly:

- `claim_id`;
- `canonical_claim_text`, defined as the source `ClaimRecordV1` field
  `canonical_proposition` without rewriting;
- `typing_status`;
- `polarity`; and
- the one `evidence_sentence_id` required by the current atomic compiler.

The canonical text already encodes normalized subject/object, predicate,
polarity, modality, conditions, exceptions, and temporal cues. Repeating full
argument spans, document/child IDs, predicate observation IDs, scope hashes,
candidate status fields, and quote bodies inside the provider packet is not
required for proposal reasoning or claim-ID citation.

### Compact evidence/provenance contract

The provider packet retains eligibility, ClaimRecord/ClaimCompilation schema,
compiler, parser, and relation-disposition identities. Repeated source child
and compilation revision lists become `input-set` hashes in the provider
projection. The durable job/input record retains the full lists for local
audit. `claims_interim=false` remains mandatory.

## Deterministic selection recipe

Proposed version: `atomic_claim_packet_selection.v2`.

1. Fully revalidate every source compilation and claim before selection.
2. Fail on duplicate claim/evidence/link IDs, ownership drift, source drift, or
   a parent whose child/compilation closure is incomplete.
3. Seed one claim per source child. Within each child, select the lowest tuple:
   typed first; negative first; nuanced first; normalized nullable claim type;
   claim ID.
4. Fill four priority lanes: typed, then negative, then nuanced, then all
   remaining ordinary claims. A claim already selected in an earlier lane is
   not repeated.
5. Within every lane, round-robin by sorted child ID. Within a child, order by
   normalized claim type then claim ID. This prevents an early child from
   consuming the remaining budget.
6. `nuanced` means nonempty conditions, exceptions, or temporal cues, or
   modality other than `asserted`.
7. Tentatively add one claim, reserialize canonical JSON, and keep it only if
   the complete provider packet stays at or below 20,000 UTF-8 bytes. If a
   large claim does not fit, continue deterministically; a later smaller claim
   may still fit.
8. Emit claims in claim-ID order. Emit a claim link only if both endpoint IDs
   emit and the link closes locally. Excluded links remain counted and hashed.
9. Fail closed if the one-per-child seed exceeds the bound, any child loses
   coverage, no claim emits, final bytes exceed 20,000, or replay differs.

The recipe is content-general. It contains no eval query, corpus-specific
concept, document name, semantic label, or random choice.

## Selection manifest and local citation authority

The packet selection manifest records:

- recipe version/hash and byte maximum;
- full source, emitted, and excluded claim counts plus `input-set` hashes of
  each ID set;
- source-child count/hash and covered-child count;
- typed/negative/nuanced/ordinary source/emitted/excluded counts;
- source/emitted/excluded claim-link counts and ID-set hashes; and
- `cap_applied`.

The semantic-validator context contains exactly the emitted claim IDs, because
the provider cannot cite a claim it did not receive. After generation, Python
requires every `supporting_claim_id` to exist in that emitted set, resolve to
one current materialized `ClaimRecordV1`, belong to the parent, and close to
its exact locally stored evidence ID/quote. Quote round-trip validation is
unchanged; only quote transport to the model is removed.

Excluded claims remain durable candidate inputs. They are not rejected,
deleted, relabeled, projected, or available to ground this provider output.

## B4 preflight and canary gates

Before any call:

1. focused positive/negative model and selection tests pass;
2. all 795 parents close with max bytes <=20,000, full child coverage, explicit
   cap accounting, and no fallback;
3. two fresh canonical processes produce identical packet-set and selection-
   manifest hashes;
4. every validator claim scope equals the emitted packet claim IDs;
5. historical ledger remains 52 eligible accepted + 4 eligible DLQ, with 14 +
   2 outside eligibility, and the fresh pool remains 739;
6. prompt remains `parent-digest.v6`, the new packet/cache/job/selection
   identities cannot reuse v1 or historical rows, and old accepted purchases
   remain grandfathered;
7. the current price-card ceiling is republished and explicitly below the
   owner-park line;
8. protected canonical census and the 3,493-row noncanonical disclosure are
   captured; calls/writes/spend remain zero.

Proposed B4 selection is ten fresh parents deterministically stratified over
the bounded packet-size distribution, including the upper tail, rather than
the first ten IDs. It uses packet size only—never semantic outcomes or eval
concepts—and is persisted before calls. This directly exercises the risk that
caused v1 to be rejected.

B4 then retains the existing bar: at least 9/10 accepted. Additionally, every
accepted output must be structurally valid, semantically valid, cite only
emitted atomic IDs, preserve complete telemetry/cost accounting, and leave
canonical stores unchanged. Any failure stops before Phase 2. The measured B4
authority ceiling is restated immediately before launch; no old ceiling is
reused.

## Risks and rollback

The cap intentionally withholds 71.5674% of compiled claims from the model.
This is the primary v2 risk. It is bounded by complete child coverage, priority
retention, exact exclusion accounting, and the fact that excluded candidates
remain locally durable. B4 tests provider reliability and semantic validation;
later retrieval measurement—not B4 alone—must decide whether this projection
is sufficient for activation.

Rollback is selection/reader reversion. V1 remains unused, v2 is additive,
and no compilation row is rewritten. There is no Qdrant, Neo4j, summary,
canonical semantic-artifact, or retrieval rollback.

## Proposed implementation order after approval

1. Add v2 strict models beside frozen v1 plus a versioned selection recipe.
2. Add the pure deterministic selector and local manifest/citation validators.
3. Add positive/negative/golden/cross-process tests, including byte-bound and
   one-per-child failure cases.
4. Run the full two-process 795-parent census and current-card cost authority.
5. Record B3's already-approved policy: deterministic T9.1 owns domain
   coverage; LLM domain proposals are auxiliary and empty arrays are lawful.
6. Build a zero-provider B4 selection/preflight receipt and request senior GO.
7. Only after GO, run ten B4 calls; Phase 2 remains separately gated.

## Senior rulings requested

1. Approve removing both evidence quote bodies and `parent_text` from the v2
   provider packet while retaining them for local Python validation.
2. Approve the 20,000-byte maximum and the exact child-fair priority recipe.
3. Approve emitted-only semantic-validator scope plus the full selection-
   manifest hashes/counts described above.
4. Approve size-stratified fresh B4 selection, including the upper tail, rather
   than the first ten fresh parent IDs.
