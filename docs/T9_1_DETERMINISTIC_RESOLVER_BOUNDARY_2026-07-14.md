# T9.1 Deterministic Domain + Superframe Resolver Boundary

Status: CONFIRMED by senior 2026-07-14; registry freeze and implementation
authorized. Owner ontology files remain verbatim. The new recipes are
executor-proposed, owner-ratifiable.

## 5Ws

- **What:** a candidate-only Python boundary that (a) resolves explicit
  concept/heading signals to the existing 16-domain registry by exact registry
  membership and (b) maps typed claim predicates to superframe candidates
  through versioned rule data. It does not create FrameInstances; T9.2 owns
  role bindings and motif matching.
- **Why:** CP9 needs deterministic, evidence-linked domains and mechanism-frame
  candidates before the one parent digest call. Exact registry/rule resolution
  prevents topical inheritance, fuzzy-label drift, and LLM self-promotion.
- **Who:** Python is final authority. The owner files define domain and
  superframe membership. ClaimRecordV1 supplies candidate claims. The parent
  LLM may later propose adjacent domains, but cannot validate or promote them.
- **Where:** local, annotate-only semantic artifacts. No provider, Mongo,
  Qdrant, Neo4j, outbox, projection, or retrieval write is in T9.1.
- **When:** after CP8's deterministic claim spine and before T9.2
  FrameInstance/motif compilation. The T9.3 paid pass remains separately gated.

## Authority and non-negotiables

1. `domain_registry.v1.json`, `superframe_registry.v1.json`, and
   `domain_superframe_affinity.v1.json` are immutable owner ontology snapshots.
2. New matching rules are recipe/policy data with explicit
   `executor-proposed, owner-ratifiable` authority. They never rewrite owner
   registry entries or invent D/MF identifiers.
3. All outputs remain `assignment_state=candidate` or `unresolved`.
4. Unknown IDs hard-error. Unknown labels abstain and are retained unresolved.
5. Domain and superframe are independent axes. Predicate type alone never
   assigns a domain. Domain affinity never assigns or forbids a superframe.
6. Affinity rows are serve-only diagnostics/context. They are excluded from
   assignment IDs, rule matches, artifact identity, and acceptance.

## Domain resolution boundary

The owner domain snapshot contains 162 normalized domain names/member terms and
has zero collisions under the existing CP5 lexicon keyspace
`services.ingestion.corpus_lexicon.normalize_identity`: `NFKC → lowercase →
underscore/hyphen to space → ASCII non-alphanumeric to space → collapse
whitespace`. It supplies no aliases, stemming rules, fuzzy threshold, weights,
or concept-mapping rows. T9.1 therefore admits only exact normalized membership:

| Input signal | Exact registry hit | Output role | Evidence authority |
|---|---|---|---|
| claim concept/argument | domain name or member | dominant | claim-local |
| section heading | domain name or member | supporting | inherited context |
| predicate type | never domain-bearing | none | mechanism axis only |
| unknown text | no match | unresolved | retained for parent packet |

Same-domain matches merge evidence and raw `score_components`; a claim-local
match outranks heading-only context. The resolver emits no invented scalar
score and applies no cardinality cap. Later policy may rank/cap candidates from
the components without changing semantic identity.

Confirmed v1 score components:

- `exact_claim_concept_matches`
- `exact_heading_matches`
- `claim_evidence_ref_count`
- `context_evidence_ref_count`

No substring, token-overlap, stem, embedding, corpus label, parent label, or
document label may become a match without a future versioned mapping/policy.

Normalizer divergence is explicit: graph `entity_id_from_name` uses the legacy
`canonicalize_entity_name` path (NFKD, Unicode-word retention, and a curated
alias map), while domain resolution uses the corpus-lexicon identity keyspace.
T9.1 never silently translates between them. CP5 owns their future reconciliation
through the single alias registry; unresolved domain terms are counted evidence
for that work, not acted on here.

## Superframe rule boundary

Confirmed v1 direct predicate rules:

| PredicateType | Superframe | Rationale |
|---|---|---|
| SIGNALS | MF02 | signaling and interpretation |
| MEASURES, COMPARES_AGAINST | MF03 | measurement and comparison |
| CAUSES, INFLUENCES, INCREASES, DECREASES, ENABLES, INHIBITS, RESULTS_IN | MF04 | causal influence/intervention |
| USED_FOR | MF06 | goal, decision, and action |
| UPDATES | MF07 | learning and belief update |
| REQUIRES, CONSTRAINS, APPLIES_UNDER | MF09 | constraint/cost/trade-off |
| PART_OF | MF16 | composition/network/scale |
| ASSOCIATED_WITH | abstain | generic association is not a mechanism |

`USED_FOR→MF06` is the least exact direct mapping and is explicitly flagged for
owner attention in the ratification bundle.

Higher-priority terminal specialization from the owner design example:

```text
DECREASES
+ subject contains an exact marker in {repeated, recurring, cumulative}
+ an object entity mention has EntityType BASELINE
→ MF15 (terminal; do not also emit the MF04 base match)
```

The implementation interprets rules generically from versioned JSON. Python
contains condition operators, not predicate-specific branches. Each match
retains rule ID/hash, claim ID, evidence refs, PredicateType, frame ID,
priority, and candidate state. T9.2 converts eligible matches into
FrameInstances only after binding required roles to real claim arguments.
The predicate route reaches 8/16 superframes when MF15 specialization is
included. That is limited route coverage, not a claim that eight other frames
are unreachable: T9.2 role/frame rules and the CP9 digest lane supply distinct
candidate paths.

## Output separation

1. `DomainResolutionV1`: assignments, unresolved signals, exact evidence,
   score components, registry/recipe identity.
2. `SuperframeRuleResolutionV1`: candidate rule matches or an explicit
   abstention reason; no FrameInstance claim.
3. `DomainAffinityServeViewV1`: domain-keyed owner priors, labeled serve-only
   and excluded from semantic identity.

## Required failure proofs

- fuzzy/substring/stem near-matches abstain;
- document/heading context cannot dominate claim-local evidence;
- unknown domain/MF/predicate IDs hard-error;
- every controlled predicate is covered by a direct rule or explicit
  abstention;
- ASSOCIATED_WITH never becomes a frame;
- MF15 specialization is terminal and data-driven;
- changing affinity input changes only the serve view, never domain IDs,
  assignment IDs, superframe matches, or artifact hashes;
- deterministic replay is byte-identical;
- provider calls and durable writes remain zero.
