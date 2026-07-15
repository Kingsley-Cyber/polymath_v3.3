# T9.3 Lane B B1/B2 design — substantive eligibility and atomic-claim packets

Status: **CERTIFIED by senior 2026-07-15 — B1/B2/B3 zero-spend phases closed.**

Production closure: B1 resolved 989 structural parents to 795 eligible. B2
materialized 3,493 immutable `canonical_write=false` child compilations with
84,586 claims, then superseded this note's proposed unbounded packet v1 with
the separately approved bounded five-field packet v2. Production resolves 793
packet-ready parents plus two permanent `source_child_without_atomic_claim`
exclusions. B3 is recorded in
`T9_3_B3_DETERMINISTIC_DOMAIN_AUTHORITY_2026-07-15.md`. Provider calls and
canonical writes remain zero; B4 remains separately gated.

Date: 2026-07-14

Authorities, in descending order:

1. Owner decision `lane b.` relayed in `COORDINATION.md`.
2. Senior Lane B execution order in `COORDINATION.md`: B1 eligibility, B2
   certified local atomic claims and packet-contract bump, B3 deterministic
   domain policy, B4 fresh 10-packet preflight before Phase 2.
3. `CODEX_MISSION.md`, `PROGRESS.md`, the checklist anti-gaming rules, and the
   structured-gateway/local-extraction contracts already published in this
   repository.

## Decision summary

**PROPOSED:** replace mark's current structural-only parent eligibility with a
content-neutral `semantic_parent_eligibility.v2` rule: a structurally valid
parent is eligible only when it is not heading-only and contains at least 256
normalized substantive UTF-8 bytes. This changes the live read-only census
from 989 to **795** parents.

**PROPOSED:** compile the unique child chunks owned by those 795 parents with
the already-certified deterministic claim spine in the pinned
`local_ghost_b/.venv` runtime (spaCy 3.8.14, `en_core_web_sm` 3.8.0). Persist
the typed `ClaimCompilationV1` results only in a new additive, noncanonical
input collection. The paid runner then builds
`semantic_parent_packet.atomic_claims.v1` packets from those validated
compilations. There is no whole-parent fallback: a parent with missing,
invalid, or zero atomic claims is explicitly non-packet-ready.

**PROPOSED:** keep `parent-digest.v6` / `parent-digest-repair.v3` unchanged.
The packet input and cache/job identities change because the packet body
changes; the prompt instructions already require input claim IDs and do not
depend on the old interim-claim shape.

**PROPOSED:** preserve all 66 accepted v5/v6 purchases exactly as the senior
ruled. Historical acceptance is verified against each artifact's historical
interim packet, then used as a selection/coverage ledger only. It is never
misrepresented as an atomic-claim artifact and is never rewritten. Of the 66,
52 overlap B1 v2 eligibility and count toward the 795-parent denominator; 14
remain valid historical purchases but are outside the new eligible population.

## The 5Ws

- **Why:** the old transport/schema gate admitted bare headings and short
  promotional shells, and its single whole-parent evidence handle allowed a
  valid ID without atomic support. B1 removes deterministic input noise before
  spend; B2 makes every new proposal cite a real compiled claim.
- **What:** one versioned eligibility recipe, one read-only census, one
  noncanonical child-compilation materialization boundary, and one strict
  atomic parent-packet contract.
- **Who:** Python owns eligibility, claim compilation, evidence closure,
  identity, and validation. The LLM remains proposal-only inside
  `SemanticDigestV1`. The deterministic T9.1 resolver remains domain authority
  at activation; LLM domain proposals are auxiliary candidates.
- **Where:** pure rules/models live under `backend/services/ingestion` and
  `backend/models`; versioned recipe/golden data live under
  `backend/registries` and `backend/evals`; operational materialization stays
  in `backend/scripts`; durable candidate inputs live in a new noncanonical
  Mongo collection. Qdrant, Neo4j, canonical semantic artifacts, summaries,
  and current retrieval are untouched.
- **When:** B1 code/tests/census first; B2 compiler materialization and packet
  validation second; B3 policy receipt third; B4 fresh 10-packet provider
  preflight only after all zero-provider gates are green and reviewed.

## Verified current state

All facts below are read-only live observations. No count is inferred from a
sample.

| Fact | VERIFIED value | Receipt |
|---|---:|---|
| Current structural-only eligible parents | 989 | `/tmp/b1_candidate_256_census.log`, `EXIT=0` |
| Heading-only parents | 99 | same |
| Non-heading parents below 256 substantive bytes | 95 | same |
| Candidate v2 eligible parents | **795** | same |
| Accepted bare-heading rows recovered | 8/8 | same |
| Accepted mark purchases | 66 succeeded | `/tmp/b1_b2_historical_ledger_census.log`, `EXIT=0` |
| Accepted purchases overlapping v2 eligibility | 52 | same |
| Accepted purchases outside v2 eligibility | 14 | same |
| Purchased DLQs | 6 total; 4 eligible, 2 ineligible | same |
| Fresh atomic pool before B4 | 739 | same |
| Fresh Phase-2 pool after a 10-parent B4 selection | 729 | same |

The last two rows are the B1 eligibility-only ledger. After B2's two permanent
packet-readiness exclusions, the expected fresh packet-ready population is
737 before B4 and 727 after a ten-packet B4 selection. The zero-provider B4
preflight must recompute and prove those intersections before selection.

The 99 heading-only and 99 Description parents are distinct structural
families in this corpus. The byte rule excludes 95 short Description parents
without naming that heading. It retains four longer Description parents at
285, 844, 1,233, and 1,283 substantive bytes. The smallest retained
non-Description parent is 360 bytes. Therefore 256 creates a safety margin
below every transcript tail while avoiding a corpus-specific section-name
blacklist.

The exact eight known accepted rows all contain only `## Transcript`, have
zero substantive bytes, and share text SHA-256
`d74606d734f52cdccc45f576027d1db9b0723f8cca362a7e61de41dca6e2476b`:

| Parent ID |
|---|
| `0014fd0b27b94c150fef36768742b408ec8e29dbdfe91345b89603df21b903c4_parent_0001` |
| `02d1d93080f1ca97d29a0107f25f0976d7fc46606216569be5ab0f9f2fa94066_parent_0001` |
| `02e459af3d5105765bbca19b5077ca330f68e16ce3de6e8f096703b217204bb9_parent_0001` |
| `0658018339e53f90a759fd91c5922cb1650d276ed220a196bb04f5899b30a9fe_parent_0001` |
| `077f954daf38252f03aa2ce200be33f9828ea28b61b6d8bce480ecd325f1eeb5_parent_0001` |
| `0c4cf7dfb7c7eb6fbb86fab09613bdb9594a4f3b6c3f467ff9254d15dda25766_parent_0001` |
| `0c72a3455a3bdaa045cee61aac42505c0b1dd94bd8108e9e9b74cb8750efca7d_parent_0001` |
| `0e24bdfb56579a848f7164229d04e36c34916c77ca2afd9d619e345d991d7783_parent_0001` |

## B1 — `semantic_parent_eligibility.v2`

### Base population

B1 does not redefine structural validity. Its input is exactly the existing
mark discovery population:

- matching corpus ID discovered from the unique active corpus-name row;
- `parent_chunks.validation_status == "valid"`;
- nonempty `text`;
- at least one `child_id`.

Structural failures remain outside the population and are not relabeled as
content failures.

### Frozen recipe to implement after review

1. Normalize parent text with Unicode NFKC and trim only outer whitespace.
2. Split into lines; trim each line for classification; discard blank lines.
3. An ATX heading line matches `^\s{0,3}#{1,6}(?:\s+|$)`.
4. `heading_only=true` when at least one nonblank line exists and every
   nonblank line matches the heading rule.
5. Build substantive text from non-heading lines in source order.
6. Replace `https?://\S+` and `www\.\S+` runs with one space.
7. Replace each run outside Unicode word characters with one space, then trim.
8. `substantive_bytes` is the UTF-8 byte length of that normalized lexical
   text.
9. Reason precedence is `heading_only`, then
   `below_substantive_byte_min`, then `eligible`.
10. Eligibility is `not heading_only AND substantive_bytes >= 256`.

The recipe data will record its schema/version, exact regexes, normalization,
threshold, comparison operator, and reason precedence. Its canonical recipe
hash will be frozen in a golden after senior review. Production logic will not
contain corpus IDs, parent IDs, `Description`, `Transcript`, promotional
phrases, or evaluation keys.

### B1 implementation boundary

- Pure `ParentEligibilityDecision` typed result with recipe version/hash,
  reason, heading-only flag, and substantive-byte count.
- One strict registry loader for the versioned recipe.
- Mark discovery calls the shared function before extraction/claim lookup.
- A read-only census command publishes base count, reason counts, eligible
  count, and recipe hash. It emits no source text.
- The exact eight-row fixture is test/golden evidence only and is forbidden
  from production decision logic.

### B1 gates

1. Unit boundaries: empty/heading-only, mixed heading+body, URL-only body,
   Unicode NFKC, 255-byte rejection, and 256-byte acceptance.
2. Hash golden and fail-closed registry validation.
3. Exact 8/8 known-row golden under the generic rule.
4. Anti-gaming test: production module contains none of the eight IDs or mark
   section labels.
5. Live read-only census exactly closes `99 + 95 + 795 = 989` with true exit.

## B2 — certified deterministic atomic claims

### Authority and engine policy

B2 reuses the published claim spine without changing its owner-ratifiable
field sets:

```text
child text
  -> build_spacy_observation_bundle
  -> compile_local_extraction_v1
  -> compile_claim_records_v1
  -> ClaimCompilationV1 (candidate-only)
```

Runtime is pinned to spaCy 3.8.14 and `en_core_web_sm` 3.8.0 in
`local_ghost_b/.venv`. The certified compiler is `claim_compiler.v2`; exact
recipe/schema hashes are re-read and recorded at execution time.

The T8.5 C2 verdict `without_wins` remains binding. No GLiREL relation is
attached, no GLiNER mention is invented, and no open-label relation is mapped
into owner predicates. Existing accepted `ghost_b_extractions` entities may
remain in the separate packet `extraction_entities` field; they do not become
claim authority.

Each child gets its real `source_version_id` from the frozen identifier recipe
using the current document ID and durable source-content hash. Evidence IDs,
sentence quotes, claim IDs, qualifiers, polarity, modality, temporal cues, and
claim-to-claim `RESULTS_IN` links remain compiler-owned.

### Durable noncanonical materialization

**PROPOSED collection:** `semantic_digest_claim_compilations`.

This collection is an input cache, not a canonical claim store. Each row:

- has `_id == artifact_revision_id`;
- has `canonical_write=false` and `artifact_state=candidate`;
- contains a strict `ArtifactEnvelope[ClaimCompilationV1]`;
- records corpus/document/source-version/child ownership;
- binds the exact child text hash, observation recipe, normalization registry,
  compiler recipe, claim schema, parser/model versions, and run/work identity;
- is additive and immutable (`$setOnInsert` only); a rerun verifies and reuses
  byte-equivalent content rather than updating it;
- never writes `semantic_artifacts`, summaries, Qdrant, Neo4j, outbox rows, or
  retrieval payloads.

Because the trained model exists in the pinned host venv but not the canonical
backend image, materialization is two-stage and fail-closed:

1. The pinned venv compiles exact child input to a temporary JSONL file under
   `/tmp`; raw evidence never enters a receipt or commit.
2. The canonical backend image revalidates every typed body, identity, source
   hash, evidence round trip, recipe/runtime pin, and envelope before one
   additive insert phase.

No row is inserted unless the entire import file validates and count/hash
accounting closes. Existing matching revisions are verified, not overwritten.
A count-only run receipt records compiled/reused/inserted/rejected counts,
typed/untyped claims, qualifiers, links, and zero canonical writes.

### `semantic_parent_packet.atomic_claims.v1`

The vNext packet remains parent-scoped and keeps `parent_text` for summary
context. It changes the evidence shape:

- `claims`: deterministic claim-ID order; a strict packet projection of every
  `ClaimRecordV1` field except the repeated sentence quote;
- `evidence_sentences`: exact evidence ID/child ID/quote rows in evidence-ID
  order, deduplicated across same-sentence claims;
- `claim_links`: deterministic link-ID order from `ClaimLinkV1`;
- `extraction_entities`: the existing bounded entity projection, unchanged;
- `evidence_contract`: B1 version/hash, packet version, ClaimRecord and
  compilation schema hashes, compiler recipe hash, parser/model pins, source
  child IDs, `claims_interim=false`, and the no-GLiREL disposition.

For packet v1, each claim must reference exactly one evidence sentence and its
`proposition_text` must equal that exact stored quote. Deduplicating the quote
is therefore lossless: the typed ClaimRecord can be reconstructed exactly.
This prevents repeated multi-predicate sentences from multiplying provider
tokens while retaining distinct atomic claim IDs.

The packet builder must fail closed on:

- a missing/ambiguous/currently invalid child compilation;
- source text, source-version, parser, schema, recipe, or ownership drift;
- a claim or link outside the parent's child closure;
- duplicate claim/link/evidence IDs;
- missing evidence or a quote/coordinate round-trip mismatch;
- zero atomic claims for an otherwise B1-eligible parent;
- any attempt to substitute the whole parent or an interim claim ID.

Every atomic claim ID becomes a `ClaimScope(claim_id, parent_id)` in the
semantic validator. Consequently all new `supporting_claim_ids` cite atomic
claims. The packet input hash, cache key, and durable job ID change
automatically. Selection names also advance to explicit atomic-contract names
so old frozen/superseded rows cannot be mistaken for vNext work.

### Historical purchases and new selections

The 66 accepted v5/v6 artifacts are grandfathered exactly as purchased:

- Reconstruct and validate each against its historical interim packet and
  historical prompt/runtime/schema provenance.
- Never validate it against, rewrite it into, or label it as an atomic packet.
- Intersect historical accepted/attempted/DLQ ledgers with current B1 eligible
  IDs before computing current exclusion and coverage counts.
- Count 52 accepted eligible parents toward the `N=795` coverage denominator;
  retain 14 accepted ineligible parents only in the historical ledger.
- Exclude the four eligible historical DLQs from fresh B4/Phase 2 and reserve
  them for the tail policy; retain the two ineligible DLQs historically.
- Preserve all 939 superseded old Phase-2 rows as superseded. VNext uses new
  job and selection identities; it does not revive them.

With the observed ledger, B4 selects 10 from a preregistered fresh pool of
739. If B4 claims 10 distinct parents, the fresh Phase-2 pool is 729. These
are census consequences, not hardcoded production constants: every run must
recompute and compare them to preregistered expected values.

### B2 gates before B4

1. Pure packet-model and reconstruction tests, including multi-predicate
   sentence quote deduplication and distinct claim IDs.
2. Negative closure tests for every fail-closed condition above.
3. Host pinned-runtime compile twice: byte-identical bodies and recipe hashes.
4. Canonical-image full-file validation before insert; post-insert readback
   validates every current revision.
5. Full eligible-parent census: every B1 parent is either packet-ready or has
   one explicit deterministic exclusion reason; no fallback and no silent
   truncation.
6. Packet replay hashes are byte-identical across processes; all claim IDs in
   validator scope equal all packet claim IDs.
7. Historical purchase ledger closes 66 accepted / 6 DLQ / 939 superseded,
   with current eligible intersections reported separately.
8. Static, focused, adjacent gateway/job, secret, and no-canonical-drift gates.

Packet-ready `N`, claim counts, packet byte/token bands, and a new conservative
provider ceiling are published only after the full B2 run. No old per-packet
cost assumption or canaried packet-size ceiling is reused without measurement.

## B3 policy line to record after B2

Domain coverage is not a paid-output completeness target. The deterministic
T9.1 resolver owns domain coverage at activation. LLM domain proposals remain
auxiliary candidate observations and empty domain arrays are lawful. B1/B2 do
not add a prompt instruction, fuzzy domain rule, sparsity retry, or provider
call to increase domain counts.

## Rollback and non-effects

- B1/B2 remain dark to retrieval and projection until later activation gates.
- Rollback is selection/feature reversion to the old reader plus ignoring the
  additive noncanonical compilation collection; no canonical data rollback is
  required.
- Existing accepted digests and historical jobs are immutable.
- No ecom mutation, reingest, junk deletion, summary regeneration, heading
  repair, Qdrant/Neo4j write, semantic activation, provider call, or spend is
  authorized by this design.

## Senior rulings requested before code

1. Approve or change the generic 256-byte threshold and exact recipe above.
2. Approve or reject the additive noncanonical envelope collection and the
   pinned-host-compile/canonical-image-import boundary.
3. Approve the normalized packet projection with deduplicated exact evidence
   quotes while keeping prompt v6 unchanged.
4. Confirm the historical ledger treatment: 52 eligible accepted purchases
   count toward v2 coverage; 14 ineligible accepted purchases remain valid but
   do not enter the denominator; four eligible DLQs remain tail-only.
