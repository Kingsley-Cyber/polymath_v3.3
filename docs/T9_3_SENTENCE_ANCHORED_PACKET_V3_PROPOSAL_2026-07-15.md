# T9.3 Sentence-Anchored Packet v3 Proposal — 2026-07-15

Status: **executor proposal for senior/owner review; zero-provider and not
approved for dispatch**.

## Verdict

Adopt a sentence-ordered hybrid, but not the literal “every sentence is a
claim” draft. The current materialization disproves one premise of that draft:
5,849 of 30,694 evidence sentences (19.06%) have no link to an atomic claim.
Treating those sentences as atomically mappable claims would make the contract
false. The proposed v3 therefore keeps every sentence as ordered prose, exposes
a deterministic `claim_id` only on the 24,845 sentence units that have a real
local atomic mapping, and makes unlabelled units context-only and uncitable.

This preserves coherent prose, satisfies the owner’s “claim IDs present in the
input” contract for every citable support, and keeps the existing ClaimRecordV1
materialization as the local evidence authority.

## 5Ws + how

- **Who:** deterministic Python constructs and validates packets; the provider
  only synthesizes the existing SemanticDigest; the senior approves design and
  the owner retains spend/ontology authority.
- **What:** `semantic_parent_packet.sentence_hybrid.v3` with ordered sentence
  units and optional sentence-claim IDs.
- **When:** only after senior approval, pure model/schema tests, live read-only
  preflight, corrected authority publication, and a fresh canary GO.
- **Where:** noncanonical gateway/cache lane over mark’s 793 packet-ready
  parents; no canonical Mongo/Qdrant/Neo4j projection during the canary.
- **Why:** prose packets succeeded 10/10; claims-only v2 produced five repeated
  empty-tool-argument failures and only 2/4 faithful accepted outputs.
- **How:** order exact source sentences, cite only mapped sentence IDs, validate
  against that emitted ID scope, then deterministically expand each cited
  sentence ID to its sorted local atomic ClaimRecord IDs after acceptance.

## Measured corpus facts

Read-only measurement covered all 795 eligible mark parents. Exactly 793 are
packet-ready; the same two parents remain excluded because a source child has
no atomic claim. It made zero writes and zero provider calls. Receipt:
`/tmp/t9_3_sentence_hybrid_v3_measure_v4.log`, true `EXIT=0`.

| Metric | min | p25 | p50 | p75 | p90 | max |
|---|---:|---:|---:|---:|---:|---:|
| Evidence sentences / parent | 3 | 23 | 42 | 51 | 58 | 115 |
| Atomic claims / parent | 8 | 98 | 109 | 120 | 131 | 283 |
| Atomic claims mapped per citable sentence | 1 | 1 | 2 | 4 | 7 | 45 |

Mapping coverage is 24,845 / 30,694 = 80.944158%. The long tail matters:
blindly expanding a cited sentence can attach up to 45 atomic candidates, so
v3 must retain both the sentence citation and the deterministic expansion
rather than pretending the expansion selects one uniquely intended atom.

## Shape comparison

All sizes are canonical UTF-8 JSON bytes across the same 793 packet-ready
parents and include current extraction entities.

| Provider packet shape | min | p25 | p50 | p75 | p90 | max | >20 KB |
|---|---:|---:|---:|---:|---:|---:|---:|
| Proven prose v1 | 3,786 | 15,128 | 15,628 | 16,237 | 16,771 | 24,960 | not measured in this field |
| Claims-only v2 (official census) | 9,142 | 19,791 | 19,870 | 19,951 | 19,991 | 20,000 | 0 |
| Literal sentence objects | 3,856 | 16,799 | 20,548 | 22,816 | 24,668 | 42,794 | 433 |
| All sentences as compact claims | 3,502 | 13,950 | 15,584 | 16,846 | 17,807 | 29,184 | 5 |
| **Proposed ordered units, optional claim ID** | **3,435** | **12,988** | **13,930** | **14,685** | **15,218** | **25,613** | **3** |
| Parent text + sentence-index map | 3,473 | 13,205 | 14,217 | 15,034 | 15,591 | 26,760 | 3 |
| Tagged text string | 3,384 | 12,491 | 13,181 | 13,751 | 14,173 | 23,298 | 2 |

The literal object-per-sentence draft does not project to the proven prose
size class and should be rejected. Tagged text is smallest, but it collapses
the typed packet boundary into prompt parsing. The proposed ordered-unit JSON
is only ~5.7% larger than tagged text at p50, is smaller than proven prose v1
from p25 through p90, and preserves a strict schema. A 26,000-byte cap covers
all measured packets without dropping sentences; the cap must be frozen and
replayed before any canary.

## Proposed provider-visible contract

Conceptual shape (field names remain subject to senior approval):

```json
{
  "packet_schema_version": "semantic_parent_packet.sentence_hybrid.v3",
  "corpus_id": "...",
  "corpus_name": "...",
  "doc_id": "...",
  "parent_id": "...",
  "sentence_units": [
    {"claim_id": "evidence:...", "text": "Claim-bearing source sentence."},
    {"text": "Context sentence with no atomic mapping."}
  ],
  "extraction_entities": [],
  "evidence_contract": {
    "claims_interim": true,
    "order": "parent_child_order_then_source_offset",
    "context_only_units_uncitable": true,
    "provider_atomic_claims_visible": false,
    "post_validation_mapping": "sentence_claim_to_local_atomic_claims"
  }
}
```

The array order is the prose order. `claim_id` is the existing deterministic
evidence-sentence ID and is present only when the materialized compilation has
at least one ClaimRecordV1 referring to that ID. Context-only units keep prose
continuity but are excluded from `SemanticValidationContext.claims`; a digest
that cites one fails validation. No atomic claim text is sent to the provider.

## Deterministic post-validation mapping

For every accepted `supporting_claim_ids` list:

1. Require each ID to be an emitted, citable sentence-unit ID.
2. Preserve the ordered sentence IDs as the direct source-evidence citation.
3. Look up ClaimRecordV1 rows by the already materialized
   `evidence_sentence_ids` relation inside the same parent/child closure.
4. Record the sorted unique union as `supporting_atomic_claim_ids` in the
   noncanonical candidate artifact.
5. Preserve mapping cardinality and source compilation revision hashes; empty,
   cross-parent, stale-revision, or nondeterministic mappings fail closed.

This is expansion, not semantic selection. It must not silently claim that one
of 45 mapped atoms was the model’s intended proposition. Sentence citations
remain the faithfulness authority; atomic IDs provide local graph/retrieval
links and downstream drill-down.

## Corrected authorities

Every number uses route card `$0.75/M` uncached input, `$2.95/M` output,
8,192 output tokens, **two capped attempts**, and a 10% margin.

### Superseded claims-only v2 authorities, corrected for the record

| Scope | Old one-attempt value | Correct two-attempt value |
|---|---:|---:|
| Frozen selected 10 | `$0.42995425` | `$0.85990850` |
| Maximum any fresh 10 | `$0.43083040` | `$0.86166080` |
| Post-B4 727 remainder | `$31.19171060` | `$62.38342120` |
| All 793 ready | `$34.02797992` | `$68.05595984` |

These are historical correction receipts, not authorization to retry v2.

### Measured v3 proposal authorities

| Shape | Maximum any 10 | All 793 ready |
|---|---:|---:|
| Proposed ordered units | `$0.83486975` | `$59.93523899` |
| Tagged-text alternative | `$0.81104540` | `$58.99355264` |

The maximum-any-ten figure is a conservative design bound. A future selected-
ten authority must be recomputed from the exact frozen selection; neither
table grants spend authority. The standing `$49.45` umbrella is lower than
the all-793 worst-case envelope, so a full pass would also require an owner
budget ruling even if a canary passed.

## Required zero-provider acceptance before a fresh canary

- Strict v3 model with extra fields forbidden and schema-hash golden.
- Deterministic sentence order replay and packet-set hash replay.
- Full 793+2 accounting and a frozen 26,000-byte cap.
- Every exposed claim ID maps to >=1 local atomic ClaimRecord; every context-
  only unit exposes no claim ID and is absent from validator scope.
- Mapping cannot cross parent, child, source version, or compilation revision.
- Cited sentence IDs round-trip to exact quotes; expansion is sorted and
  deterministic; empty or stale maps fail closed.
- Fresh stratified ten-packet selection with document uniqueness and explicit
  longest-packet representation.
- Two-attempt authority and per-claim reservation rederived from the same
  price/parameter cards.
- No credential read, provider call, canonical write, or projection during
  preflight.

## Open decision

Senior/owner approval is required for the optional-ID sentence-unit contract
and 26,000-byte cap. If the literal “one claim ID on every sentence” rule is
required instead, the architecture must first decide how the unmapped 19.06%
is represented without falsely claiming an atomic link. No paid dispatch or
v3 implementation beyond pure proposal work is authorized by this note.
