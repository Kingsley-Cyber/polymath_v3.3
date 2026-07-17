# Polymath Retrieval Program — Owner Results Report

Date: 2026-07-17
Scope: fresh 15-document E2E corpus and retrieval-path review branches
Corpus: `2c894530-8d57-4432-a6d4-bc14505a698b`

## Executive decision

The current retrieval wave identified five separate failure mechanisms rather
than one generic retrieval problem:

1. relationship questions lost one side during evidence-seat allocation;
2. the chat arbiter loosened strict retriever refusals on generic word overlap;
3. temporal metadata was captured but not consumed by query planning;
4. semantic digests regressed quality when admitted as chunk competitors; and
5. exact atomic claims were stored but a summary-seat identity join hid them.

The completed fixes remain independently feature-flagged and default OFF.
Relationship allocation and `corpus_scope.v2` meet their acceptance gates.
Temporal routing meets its temporal targets; its final stabilized frozen
regression is reported below. Direct digest activation is rejected. Claim
anchor, four-lane routing, and hydration-waterfall verdicts are reported below
from their separately controlled review branches.

## Acceptance scoreboard

| Family | OFF | ON | Required | Verdict / recommendation |
|---|---:|---:|---:|---|
| Relationship minimum-distinct | historical 50.0%; paired 75.0% | **100.0%** | >=75% | PASS; promotion candidate |
| Relationship direct doc-hit | 100.0% | **100.0%** | >=85% | PASS, no regression |
| Relationship lay doc-hit | 100.0% | **100.0%** | >=75% | PASS, no regression |
| Refusal gate, negative controls | historical 44.4%; paired 66.7% | **100.0%** | 100% | PASS; promotion candidate |
| Refusal gate, direct doc-hit | 88.9% | **88.9%** | >=85% | PASS, no regression |
| Refusal gate, lay doc-hit | 100.0% paired | **91.7%** | >=75% | PASS, within floor |
| Temporal overall doc-hit | 95.83% | **95.83%** | >=90% | PASS |
| Temporal full-anchor coverage | 75.0% | **87.5%** | >=70% | PASS |
| Temporal frozen direct / lay | 100% / 100% | **100% / 100%** | >=85% / >=75% | PASS, no regression |
| Digest Fast Mark doc-hit | 88.9% | **77.8%** | no regression | REJECT |
| Digest Fast direct hits | 2/2 | **1/2** | improve | REJECT |
| Claim-anchor q021 rendered anchors | 0 | **2/2 valid and rendered** | >0 plus no regression | JOIN fixed; activation RED |
| Four-lane six-query bridge diagnostic | 4/6 | **4/6** | 6/6 | REJECT; no routing delta |
| Four-lane frozen direct / lay / relationship | 100% / 100% / 75% | **100% / 100% / 75%** | >=85% / >=75% / >=75% | floors preserved |
| Hydration waterfall bridge quality | 4/6 | **4/6** | no quality loss | preservation PASS; lower-tier coverage incomplete |

The frozen, temporal, relationship, and router comparisons used the immutable
15-document selection hash
`da7b94c152dd5e72d52db1fd80a68f0cc2797d85ed1fd4899f9a8c19874eaf00`.
The digest and claim micro A/Bs instead used their preregistered Mark-corpus
contracts; the claim held-out file hash was
`7c000ca43911684c0991c7bc63455e4595f1ffe3d25a2d82aa551cf5d555a42b`.
Frozen comparisons used preregistration hash
`8f70b1d375120862712fa4a44abad5ca7eb38eb0fbc7d3a3a86e79f4827bc110`
and the same `anthropic/minimax-m2.7` route. Feature flags remain default OFF
in every review branch.

## Fix results

### Relationship evidence allocation

The proven per-side allocator reserves evidence seats for both sides of a
relationship query, caps a protected document at two seats, and spills unused
reservations only after satisfying strong-match coverage. It reuses the shared
relationship/comparison classification instead of adding another detector.

| Relationship query | OFF, expected docs by tier | ON, expected docs by tier |
|---|---:|---:|
| Shoot/edit/emotion | 3 / 3 / 3 | 3 / 3 / 3 |
| Fight/camera direction | 1 / 1 / 1 | **2 / 2 / 2** |
| VFX/story pipeline | 3 / 3 / 3 | 3 / 3 / 3 |
| Movement/machine/figure | 2 / 2 / 2 | 2 / 2 / 2 |

The missing *Directing — Film Techniques and Aesthetics* evidence returned for
the fight/camera question while *Stage Combat Arts* remained present. The
canonical focused and adjacent test set closed 80/80. Substantive Mongo,
Qdrant, and Neo4j fingerprints were unchanged.

The first frozen program baseline was 50.0%. The acceptance comparison is the
controlled same-session 75.0% OFF to 100.0% ON pair; both values are retained
so the report does not erase historical framing or attribute unrelated drift
to this allocator.

### Answerability `corpus_scope.v2`

All five historical false answers reached chat with
`raw_answerable=false`. The legacy chat arbiter then promoted generic overlap
such as “best/code,” “guide/sequence,” and “rate/tax.” The versioned v2 guard
acts only after strict retrieval refuses and only when the legacy arbiter would
loosen that decision; it requires at least two distinctive query terms and
refuses when evidence coverage is below 60%.

| Negative query | Historical refusal | Paired OFF | ON |
|---|---:|---:|---:|
| Quantum error-correcting code | 2/3 | 2/3 | **3/3** |
| Human genomics guide | 1/3 | 1/3 | **3/3** |
| 2025 US federal tax rate | 1/3 | 3/3 | **3/3** |

The guard applied zero times to the positive direct and lay set. Canonical
tests closed 147/147. The retriever's strict `_evaluate_sufficiency` gate,
prompts, scoring, and frozen specifications were unchanged.

This paired live A/B was the preregistered 13-query direct+lay+negative subset,
39 executions per arm. Relationship questions were intentionally excluded;
the unmodified full-suite finalizer returned EXIT=1 solely because that subset
had zero relationship executions. This report does not relabel it as a full
17-query/51-execution run.

### Temporal query routing

The feature reuses the extraction runtime's qualified temporal-expression
families, admits only exact or boundary-contained temporal surfaces, hydrates
bounded temporal metadata from Mongo, and deterministically prefers temporal
and graph-anchored evidence. It never filters an otherwise nonempty result set
to empty.

| Tier | Historical doc hit / anchor | Same-build OFF | ON |
|---|---:|---:|---:|
| Qdrant only | 37.5% / 0.0% | 87.5% / 62.5% | **87.5% / 75.0%** |
| Qdrant + Mongo | 100% / 62.5% | 100% / 75.0% | **100% / 87.5%** |
| Qdrant + Mongo + graph | 100% / 75.0% | 100% / 87.5% | **100% / 100%** |
| Overall | 79.17% / 45.83% | 95.83% / 75.0% | **95.83% / 87.5%** |

The temporal target and stabilized frozen no-regression gate are green. After
the MLX client patch, the paired 51-execution arms each achieved direct 100%,
lay 100%, relationship 75%, technical 100%, corpus/citation 100%, and no
embedding timeout. The existing negative-refusal metric moved 55.56% OFF to
44.44% ON because `negative_genomics` Graph changed on a non-temporal query
whose temporal detector was inactive; this is outside the temporal admission
path and remains assigned to the independently passing `corpus_scope.v2`
package.

### Semantic-digest activation

The experiment projected 249 immutable, provenance-closed digest points for
38 source documents. Projection was idempotent and existing points were
byte-for-byte unchanged. Quality nevertheless regressed when the points
competed directly with chunks:

| Fast Mark metric | OFF | Digest ON | Delta |
|---|---:|---:|---:|
| Document hit | 88.9% | 77.8% | -11.1 points |
| Mean document recall | 79.6% | 64.8% | -14.8 points |
| Direct hits | 2/2 | 1/2 | -1 |
| Lay hits | 1/1 | 1/1 | 0 |

The digest feature remains dark and should not be activated as a chunk
competitor. Router A tested the more appropriate document-routing role, where
digests nominate documents without occupying evidence seats. That role is
architecturally valid for documents with provenance-matched digest profiles,
but it did not improve the E2E diagnostic because this corpus has none; the
249 projected profiles belong to a different corpus with no lawful durable
identity match. The rejected competitor experiment therefore does not verify
routing benefit.

Exposure stopped at the first valid Fast Mark arm after the measured
regression. A partial Hybrid arm was stopped; Graph and Frozen ON were never
launched. The rejection is justified by the preregistered no-regression gate,
but it is not a claim about unexecuted tiers.

### Claim-anchor join

The original q021 Graph probe found four valid current compilation rows but
rendered zero claim anchors. Read-only identity inspection showed the selected
seat was a parent-summary source and that its durable parent row had exactly
three child IDs with exactly three corresponding compilation rows. The fix
joins sentence-keyed claims through that exact corpus/document/parent mapping
and fails closed on absent, foreign, duplicate, or disagreeing mappings.

Both six-query arms were internally green and left corpus fingerprints
unchanged. ON attached 18 anchors, all 18 passed exact ownership/span/claim/
source-version validation, and 16 reached the rendered prompt. q021 moved from
zero to two attached, valid, and rendered anchors.

The paired promotion invariant nevertheless failed: selected source IDs
changed on q021, q022, and q023; q029 kept the same IDs but its non-anchor
evidence bytes changed. Persisted q021 previews exactly matched the raw SSE
source IDs, ruling out preview compaction as the explanation. No selective
retry was used, so causal no-regression is unproven and activation remains
rejected/default OFF.

There is a second quality caveat: exact citation structure does not certify
claim semantics. The two q021 `claim_text` values are malformed/untyped
compiler propositions even though their cited sentences are exact and
query-overlapping. Claim compiler quality therefore remains a separate
activation dependency.

### Four-lane Tier-0 routing

The review branch preregistered six bridge questions before implementation.
The set is bound to the same immutable 15-document selection and requires a
directing, storytelling, or editing book in the top three while forbidding
the surface-token camera/lens book from rank one.

The implementation added lexical BM25 over document title/summary/headings,
semantic document-summary plus authorized digest vectors, child-hit rollup
without parent-summary embedding, and associative matching through the T9.1
resolvers. Fusion used per-lane reservations, threshold spillover, and
divergent-profile demotion; the optional cached decomposition call remained a
separate default-OFF setting in the committed branch.

| Bridge result | OFF | ON |
|---|---:|---:|
| technical success | 6/6 | 6/6 |
| attribution complete | 6/6 | 6/6 |
| passed | 4/6 | 4/6 |
| camera-motion/story | camera book rank 1; fail | byte-identical ranking; fail |
| character-motion | no expected directing/animation title in top 3; fail | byte-identical ranking; fail |

The RED is architectural, not an embedder outage. The E2E corpus has 15 active
documents and zero succeeded digest jobs. The Mark corpus has 250 succeeded
jobs and 249 applied, provenance-closed projections, but exact intersections
between the two corpora are zero for `source_key`, content SHA-256, normalized
filename, and `doc_id`. Reassigning those profiles would violate corpus and
durable-identity boundaries. Consequently the associative lane correctly
abstained, the other three lanes produced no bridge-quality delta, and the
router remains default OFF.

The full frozen characterization completed 51/51 technically in each arm.
Direct and lay document hit stayed 100%, relationship minimum-distinct stayed
75%, and corpus/citation precision stayed 100%. The independently unresolved
negative-refusal rate moved 44.44% OFF to 33.33% ON; it is not a router
acceptance metric, but the deterioration is retained as an additional reason
not to promote the combined router/decomposition bundle.

### Budget hydration waterfall

The deterministic policy spends the context budget in order: full selected
text, then parent-summary hydration for the next evidence tier, then skip.
Every ranked parent records an explicit hydration decision and every packet
item records its hydration level. The policy itself makes no LLM call.

The exact integrated image built with `EXIT=0`, and 62 combined
router/waterfall tests passed. Router and decomposition stayed OFF in both
arms after the router's RED verdict, isolating the waterfall. Both immutable
six-query bridge arms completed technically, scored 4/6, and returned the
same ordered top-three documents on all six questions. Each upstream runner
returned `EXIT=1` because the already-failing bridge suite requires 6/6; the
dedicated OFF/ON comparison returned `EXIT=0`.

The direct packet probe was deterministic on all six repeated flagged calls:
6/6 hashes repeated, all packets stayed under the 4,000-token/query ceiling,
and the wrapper returned `EXIT=0`. Across the six questions it assembled 37
packet items using 9,966 of 24,000 available tokens. The 17 ranked-parent
decisions were all `full`, with zero `summary` and zero `skip`. Thus
full-text preservation is live-proven and the lower tiers are unit-test-proven
only. The flag remains default OFF until a preregistered high-pressure
diagnostic exercises both summary and skip.

The packet probe incurred no synthesis calls. The quality A/B made 12 small
MiniMax calls; because chat-lane invoice telemetry is still the queued P7
gap, the conservative two-attempt upper bound is $0.61390, not an invoice.
The substantive store comparison returned `EXIT=0`; only the scheduler
heartbeat advanced. Canonical runtime and all review flags were restored
before the eval lock was released.

## Reliability result: MLX embedder

The sustained-load failures were not caused by the local client timeout, which
was already 30 seconds. QueryPlanV2 had an outer five-second deadline that
cancelled the client. The review patch aligns the outer deadline at 30 seconds,
keeps the pooled client at 30 seconds, retries a local timeout exactly once,
uses 120-second keepalive expiry, and adds a real-inference pre-batch probe that
aborts before scoring when degraded.

| Check | Result |
|---|---:|
| Focused + adjacent tests | 24 passed |
| Canonical deployed tests | 15 passed |
| Sustained calls | 100/100 |
| Failures | 0 |
| Concurrency | 3 |
| Wall time | 2.839 s |
| Throughput | 2,113.748 requests/min |
| Vector dimension | 1,024 |
| True exit | 0 |

No RunPod embedding route or cross-backend parity canary was added; the owner
hold on that structural decision remains in force.

## Latency baseline

These are observed client and retrieval-stage timings from the completed
paired relationship OFF/ON artifacts. They are not synthetic projections.
The reported p95 is nearest-rank over only 17 queries per tier and therefore
equals the sample maximum in every row; treat it as a small-sample diagnostic,
not an SLA percentile.

### Flag OFF — p50 / p95 seconds

| Tier | Client | Retrieval | Embed | Vector | Hydrate | Graph | Rerank | Plan/vocabulary |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Qdrant | 6.451 / 26.730 | 5.055 / 13.576 | 0 / 5.007 | 0.503 / 1.478 | 0 / 0.001 | n/a | 3.912 / 8.962 | 0.292 / 6.602 |
| + Mongo | 21.763 / 38.165 | 10.487 / 22.047 | 0 / 0.145 | 0.615 / 1.611 | 0.010 / 0.052 | n/a | 8.915 / 20.096 | 0.994 / 2.466 |
| + Graph | 32.223 / 41.512 | 14.652 / 34.370 | 0 / 0.001 | 0.832 / 2.026 | 0.014 / 0.056 | 1.226 / 12.001 | 10.049 / 20.038 | 1.051 / 2.189 |

### Relationship allocation ON — p50 / p95 seconds

| Tier | Client | Retrieval | Graph | Rerank |
|---|---:|---:|---:|---:|
| Qdrant | 17.584 / 29.281 | 11.820 / 24.051 | n/a | 7.483 / 20.004 |
| + Mongo | 20.751 / 40.084 | 16.227 / 22.407 | n/a | 12.556 / 20.035 |
| + Graph | 35.063 / 47.437 | 24.632 / 34.140 | 2.666 / 12.006 | 17.827 / 20.089 |

The quality gain has a measurable retrieval/rerank cost, especially in the
Fast tier. Promotion should therefore include a latency budget and retain the
default-OFF rollback.

The durable machine-readable baseline is
`docs/baselines/RETRIEVAL_OPTIMIZATION_RECORDED_BASELINE_2026-07-17.json`.
It includes p50/p95/max for every exposed stage, trace source-tier counts,
returned-payload field-presence counts, and the index-execution contract for
all 102 paired executions.
Both arms exercised `naive`, `hrag`, and `polymath_doc_summaries` on 51/51
executions and the graph collection on the 17 Graph-tier executions. Retrieval
trace diagnostics counted 119 `tier_a`, 95 `tier_a+lexical`, and two
`graph_mode_a` entries OFF, versus 121 `tier_a` and 100 `tier_a+lexical` ON.
These diagnostic counts are not SSE source counts: OFF diagnostics sum to 216
while OFF returned-payload rows sum to 204.

Across returned payload rows, OFF/ON nonempty field-presence counts were
204/233 for the
core identity, score, text, heading, provenance, kind, and tier fields;
165/187 for `summary`; 140/159 for `corpus_name`; and 104/128 for `domain`.
They prove availability in returned payloads, not that retrieval ranked or
filtered on each field. Consumption verdicts below come from code paths and
retrieval traces; store-population counts come from the separate census.

## Metadata utilization matrix

`USED` means the current default retrieval path consumes the field.
`FLAGGED` means a tested default-OFF package consumes it. `STORED` means
capture exists but current retrieval does not use it for admission or rank.

| Field | Stored where | Current consumption | Verdict / activation |
|---|---|---|---|
| `corpus_id` | Mongo documents/chunks/summaries; Qdrant payloads; Neo4j corpus-qualified document/chunk identities | mandatory corpus filter, hydration boundary, graph isolation, citation membership | USED in every tier |
| `heading_path` | Mongo chunks/parents; Qdrant payload | citation/structure and current MMR final-selection diversity text; lexical router also consumes heading terms when flagged | USED in final selection; FLAGGED for router admission |
| `chunk_kind` | Mongo chunks; Qdrant payload | kind-aware table/code/metadata retrieval and hydration policy | USED in vector/hydration |
| `chunk_type` / `projection_role` | Qdrant document-summary and semantic-digest payloads | excludes dark `semantic_digest` points from the legacy lane; selects authorized digest signals only in flagged router | USED as safety filter; FLAGGED for router |
| summaries | 6,757 Mongo parents and 669 tree rows; 15 Qdrant document-summary points; 6,757 parent-summary vectors expose `retrieval_text` rather than `summary` | Funnel-A summaries, tree expansion, context assembly | USED in vector + Mongo |
| `retrieval_text` | 6,757 Mongo parents and 6,757 naive/HRAG summary points; absent from all 669 tree rows | summary search and parent hydration | USED in vector + Mongo; tree gap |
| `temporal_class` | 6,757 Mongo parents and their naive/HRAG summary projections; absent from child/graph vectors and tree rows | no default query-side use; temporal v1 uses tie-break/refinement | FLAGGED by temporal v1 |
| `time_expressions` | 760 Mongo parents and their naive/HRAG summary projections; 5,069 Ghost-B rows have captures | no default query-side use; temporal v1 exact/boundary match and graph preference | FLAGGED by temporal v1 |
| biblio title | Mongo document/catalog/summary metadata; selected Qdrant payload | document anchoring, source labels/citations; flagged lexical router admission | USED; router expands admission use |
| biblio author / date | Mongo document fields exist on 15/15 rows but are empty; absent from all four inspected Qdrant payload families | unavailable to this corpus's retrieval, rank, and citations | STORED-EMPTY; not consumed |
| document/chunk durable IDs and source identity/hashes | all 15 Mongo documents have source identity/provenance; document/chunk IDs are in every relevant Mongo/Qdrant family | default corpus isolation, exact-source dedupe, hydration, and citation membership | USED by default |
| `source_version_id` | all 18,790 Ghost-B rows; absent from the inspected Qdrant payloads | exact claim ownership is consumed only by the default-OFF claim-anchor family | STORED-UNCONSUMED by default; FLAGGED for claim validation |
| entities / mentions | Ghost-B and Mongo extraction records; Neo4j entity/mention edges | graph expansion and graph-tier evidence | USED in Graph tier |
| predicates / claims | 2,997 local predicates and 152,803 compiled claims in Mongo; zero local relations/facts, zero projected relation predicates, and no E2E predicate relationship type in Neo4j | claims are not attached by default; the claim flag activates a bounded join | STORED-UNCONSUMED by default; relation substrate absent |
| domains / superframes / motifs | 5,957 parents carry domain; registries contain 16 domains, 16 superframes, and 12 motifs; this corpus has no digest-derived ontology profiles | not used for default admission/rank; router associative lane correctly abstained | STORED-UNCONSUMED; router could not activate without profiles |
| digest concepts / central thesis | zero E2E `SemanticDigestV1` jobs, cache rows, compilations, or semantic artifacts | no lawful routing signal exists for this corpus | NOT STORED for E2E; 249 Mark projections cannot be reused |

## Typed-schema utilization

| Contract | Instances / store | Retrieval consumption | Verdict |
|---|---|---|---|
| `LocalExtractionV1` | 18,790 E2E rows; 120,430 entities, 2,997 predicates, zero relations, 10,488 temporal captures | entity projections feed the entity/mention graph; raw predicates and raw temporal captures are not queried (temporal v1 reads parent `time_expressions`) | entity projection USED; raw predicates/captures STORED-UNCONSUMED |
| `ClaimRecordV1` / atomic claims | 18,790 E2E compilation rows with 152,803 claims and 147 links; separately, 84,586 nested claims in 3,493 Mark rows | not attached by default; the claim family joins only to already-selected chunks | STORED; join mechanism fixed, activation rejected |
| `SemanticDigestV1` | zero E2E jobs/cache/compilations/artifacts; separately, 250 succeeded Mark jobs and 249 projected points | no E2E route exists; direct Mark competition regressed | ABSENT for E2E; Mark direct activation rejected |
| Predicate registry | 17 canonical predicate types | normalization contract exists, but the current E2E corpus has zero extracted relations and no predicate graph edges after identity repair | STORED CONTRACT; no current E2E retrieval substrate |
| Domain/superframe/motif assignments | registries: 16 domains, 16 superframes, 12 motifs, 8 superframe rules; 5,957 E2E parents have domain | not part of default admission/rank; E2E lacks digest-derived document profiles | STORED-UNCONSUMED by default |
| Entity/mention contracts | 120,430 extracted entities; 91,654 projected entity IDs; Neo4j has 33,302 linked global entities and 86,593 `MENTIONS` edges | graph anchoring and expansion | USED in Graph tier |
| Summary contracts | 7,031 parents, of which 6,757 have summary/retrieval text; 669 summary-tree rows; 15 document-summary vectors | summary search, parent hydration, and evidence assembly; summary-tree rows lack heading/retrieval/temporal fields | USED, with tree-projection gaps |

### Exact E2E store census

| Store / family | Exact population | Material utilization finding |
|---|---:|---|
| Mongo documents | 15 | title, provenance, profile, and source identity 15/15; author/date 0/15 nonempty |
| Mongo chunks / parents | 19,981 / 7,031 | heading and kind complete; 6,757 parent summaries; 760 parents with 1,651 time expressions |
| Ghost-B extraction / claim rows | 18,790 / 18,790 | entities and claims populated; relations/facts empty |
| Summary tree | 669 | summaries present; heading, retrieval text, and temporal fields absent |
| Qdrant naive / HRAG | 25,547 / 25,547 | 18,790 child plus 6,757 summary points; temporal metadata exists only on the summary subset |
| Qdrant graph / document summaries | 18,790 / 15 | graph vectors are entity-bearing child points; document summaries carry only corpus/doc/title/summary |
| Neo4j, current | 18,790 Chunk; 15 Document | 18,790 `HAS_CHUNK` and 86,593 `MENTIONS`; no current relation/predicate edge family |
| Typed registries | 16 domains; 16 superframes; 12 motifs; 17 predicates; 8 rules | registry definitions exist independently of E2E assignment/activation coverage |

The earlier ingest-complete census reported 19,639 `HAS_CHUNK`, 90,934
`MENTIONS`, and 2,211 `SUPPORTS_FACT` relationships touching E2E. Those were
not valid E2E predicate coverage: the pre-composite identity model had
overwritten same-content document/chunk ownership and created exactly 849
cross-corpus `HAS_CHUNK`, 4,341 cross-corpus `MENTIONS`, and 2,211
cross-corpus `SUPPORTS_FACT` edges into protected corpus
`fd460347-61cc-4358-87fc-4b2a80533f0a`. Commit `a8c25dc` made derived
identities corpus-qualified and rebuilt separate E2E instances. The July 17
read-only census is the post-repair topology; the difference is repair
lineage, not a counting-method redefinition.

The committed machine-readable census is
`docs/baselines/E2E_SCHEMA_METADATA_CENSUS_2026-07-17.json`
(committed-file SHA-256
`bae72c68446ad4f4a7c86c343bc8bd064907b4cf67a5658cded47d39a2bed4ab`).
Its pretty runner payload had SHA-256
`a06e71d5f6df6fd5e059a14137cb5c1e3fad991a19e52021d2250247eed911ce`;
minification changed formatting only.

## Five production findings and engraved invariants

| # | Production finding | Fix | Permanent invariant | Commit |
|---:|---|---|---|---:|
| 1 | Punctuation-only extractor noise normalized to an empty label and aborted a book. | Exclude and count only normalization-empty, non-alphanumeric mention noise. | Invalid mention noise is refused at mention granularity; legitimate empty canonical values still fail closed. | `47fee92` |
| 2 | Exact-source dedupe treated an incomplete/queryable document as a successful duplicate. | Skip only when the matched durable document has `write_state.verified=true`; otherwise resume it. | One completeness truth; every skip names its verified match. | `0c3d123` |
| 3 | A resume path reconstructed parents with `summaries=None`, erasing 174 valid summaries. | Carry every validated typed summary field before parent upsert. | A resume may never write less information than the durable store already holds. | `d7ae48e` |
| 4 | A 1,000-entity Neo4j aggregate transaction exceeded the 716.8 MiB cap. | Bound graph write/refresh/delete families to receipted 100-row transactions. | Every graph transaction family has an explicit bound; a partial graph is never success. | `74d2317` |
| 5 | Global mutable `corpus_id` ownership on Document, Chunk, Fact, and summary-tree IDs let a same-source E2E ingest steal protected instances. | Use composite `(corpus_id, content_id)` identity and qualified relation provenance; keep Entity deliberately global. | Derived artifacts are per-corpus instances; ontology entities are the only intentional cross-corpus join. | `a8c25dc` |

The owner later stopped the ecom modernization at its safe boundary. Per the
standing directive, three manifest documents are tombstoned, twelve remain
active, sealed backups remain intact, and no reingest is authorized. Restore,
reingest, or leave-as-is remains an owner decision and is not represented as
closed by this report.

## Retrieval findings and invariants

| Finding | Evidence | Correct invariant | Disposition |
|---|---|---|---|
| Winner-takes-all relationship seats | one relationship query returned only one expected document in all tiers | reserve K-per-side seats before spillover; preserve strong matches and per-doc cap | relationship allocator passes |
| Chat arbiter overrode strict refusal | all five false answers had `raw_answerable=false` before generic overlap promotion | a downstream arbiter may tighten strict retrieval, never loosen it outside a versioned, auditable rule | `corpus_scope.v2` passes |
| Temporal capture was inert | 760 summaries had expressions, yet historical Qdrant-only doc-hit was 37.5% | capture is not “implemented” until query detection and rank/hydration consume it | temporal v1 temporal gate passes |
| Digests occupied the wrong retrieval layer | 249 clean points caused -11.1 doc-hit points and a direct-hit regression | document semantics may nominate identity-matched documents; they do not displace sentence/chunk evidence seats | direct activation rejected; E2E router had no matched profiles and no delta |
| Claims were hidden by identity shape | valid compilation rows existed, selected summary key did not join to child sentence records | resolve every evidence attachment through durable document/parent/child identity and validate exact ownership | join fixed; activation rejected on invariance and semantic-quality gates |

Operational invariant: an evaluation must pass the embedder's real-inference
health probe before scoring starts. The probe may abort before a run; the
system must not silently degrade midway and interpret infrastructure failure
as retrieval quality.

## E2E ingestion timing and cost

| Component | Measured result |
|---|---:|
| RunPod extraction requests | 595 successful / 0 failed |
| RunPod account split | primary 302 / secondary 293 |
| RunPod worker-seconds | 2,101.845 s |
| Active extraction wall sum | 519.750 s |
| Active / steady-tail throughput | 68.687 / 83.342 requests/min |
| Configured green fleet | 20 workers across two accounts |
| Estimated RunPod extraction cost | $0.97736 (rate model, not invoice) |
| API summary/digest calls | 3,390 |
| Summary/digest input tokens | 9,994,172 |
| Summary/digest output tokens | 5,487,305 |
| Conservative summary/digest ledger cost | $3.48517 |
| Combined conservative E2E ingest arithmetic | **$4.46253** |
| Combined authority | $35.00 |

The extraction burst's first-wave delay was 5.878 s p50 / 16.428 s p95;
overall delay was 3.909 s p50 / 13.590 s p95. The 12,461-second
first-to-last pipeline span includes serial pipeline phases and is not the
active extraction wall time.

The preregistered no-write estimate was approximately 709 requests; durable
resolution closed on 595 actual submitted/terminal requests. The steady
83.342 requests/min measure is 393 terminal-tail jobs divided by 282.931
summed tail seconds and excludes inter-document graph/embed gaps; the broader
active rate is 68.687 requests/min.

The RunPod component is explicitly `estimated_only`: execution seconds at
$0.00031/s with a 1.5× overhead multiplier. The summary ledger remained open
at census time. Its $3.48517 conservative amount comprises $2.93563 reported
usage plus $0.54954 charged conservatively for 543 calls missing provider
usage. Therefore $4.46253 is bounded/accounted arithmetic, not a closed cloud
invoice.

Partial local-phase telemetry mixes fresh and resume paths and excludes three
sealed-rebuild documents: Neo4j covers 12 books and reports 2,806 s total
(128.625 s p50, 597.826 s p95); embedding and Qdrant each cover only 10 books,
at 1,013 s total (51.485 s p50, 309.081 s p95) and 909.970 s total
(66.935 s p50, 287.471 s p95), respectively.

The summary/digest lane made 3,390 provider calls with mean 14.264 s, p50
9.582 s, p95 32.635 s, and 48,355.063 aggregate provider-call seconds. Those
call-seconds overlap under concurrency and are not serial wall time.

## Retrieval-evaluation cost accounting

The `$4.46253` above is ingestion-only. It is not labeled total program cost.
The `/api/chat` SSE contract does not expose provider usage or cost, so P7
remains a real accounting gap.

| Evaluation lane | Accounted amount | Classification |
|---|---:|---|
| Router frozen ON, 51 executions | <=$2.6090478 | conservative two-attempt ceilings: $1.790523 + $0.8185248 |
| Waterfall bridge OFF/ON, 12 executions | <=$0.61390 | conservative two-attempt ceiling |
| MLX health/soak and direct packet probes | no provider calls | local compute/electricity not priced |
| Relationship, refusal, temporal, digest, claim, Router OFF/bridge, and other synthesis arms | unavailable | P7 usage telemetry absent; **not zero** |

The ingestion amount plus the two disclosed eval ceilings is a
**partial bounded/accounted subtotal of $7.68548**. It is not a total because
the remaining synthesis arms are unmetered and provider-invoice
reconciliation is unavailable. This report therefore marks total program cost
**UNKNOWN**, rather than manufacturing precision from missing telemetry.

## Full-corpus projection and quota ask

Three different planning rows must not be conflated:

| Scope and method | Requests / work | Fleet result | Status |
|---|---:|---:|---|
| current 15-book run, measured batch-32 burst-rate extrapolation | 595 actual requests / 2,101.845 worker-s | 58 workers for 3:00 | **INFERRED from measured burst scaling** |
| current 15-book run, batch-64 projection model | 302 projected requests / 939.717 worker-s | 6 workers for 3:00 | **INFERRED; batch-size/model change** |
| 500-book planning scenario, batch-64 projection model | 10,067 projected requests / 31,324.921 worker-s | 100 workers yields 5.49 min | **INFERRED; quota scenario, not a 3:00 result** |

The recommended quota request is **100 workers total: 50 per each of two
accounts**, retaining batch size 64 and the same immutable image. For the
500-book scenario this projects $14.566 extraction cost and 5.49 minutes of
active wall; it does not claim to meet a three-minute target.

The projection fit is only R²=0.8184 and assigns the fitted work to
3.111644 seconds of fixed/request overhead with a zero per-task coefficient.
That makes request count the dominant modeled driver and limits confidence in
the wall-time/quota extrapolation.

The 58-worker and 6-worker current-corpus estimates use different methods and
batch sizes, so their gap is model sensitivity rather than a measured
improvement. The 58-worker result assumes the observed batch-32 active rate
scales linearly; the 6-worker result assumes the inferred batch-64 request and
worker envelope. Neither is a capacity guarantee. The corresponding
12-worker perfect-utilization lower bound for the measured 595-request run is
not operationally realistic. A batch-128 projection appears cheaper/faster
but exceeds the currently validated adapter cap of 64; it is an assumption
requiring a memory canary, not a deployable recommendation.

Cost-reduction options:

1. **INFERRED local MLX summaries:** provider spend for the measured summary
   lane would fall from $3.48517 to $0. Quality, throughput, electricity, and
   Metal contention are unmeasured, so this needs a frozen retrieval canary.
2. **INFERRED deterministic eligibility floor:** a 200-character parent floor
   retains 4,076 and skips 2,681 of 6,757 summarized parents (39.6774% fewer).
   A proportional model projects 2,045 calls, $2.102346 summary cost, and
   $1.382824 savings; quality impact is unmeasured.
3. **VERIFIED historical pricing capture:** the official DeepSeek pricing
   page inspected on 2026-07-16 published no off-peak tier, so scheduling
   alone had $0 verified savings at that capture. Recheck before relying on
   a later discount.
4. Cache the optional bridge-decomposition call by normalized query and keep
   it separately default OFF; use document routing before reranking only
   after the missing identity-matched profiles exist.
5. Use the deterministic hydration waterfall to prevent lower-ranked full
   text from consuming context after a live high-pressure diagnostic covers
   summary/skip, and implement P7 before any large synthesis evaluation.

## Promotion recommendations

| Flag / branch | Recommendation | Reason |
|---|---|---|
| MLX stability | merge operational patch | 100/100 soak, fail-before-score probe |
| relationship allocation | promote dark, then owner-controlled enablement | quality gate green; monitor latency |
| `corpus_scope.v2` | promote dark, then owner-controlled enablement | 9/9 refusal and positive floors preserved |
| temporal routing v1 | merge dark only; activation requires integrated `corpus_scope.v2` run | temporal targets pass, but the isolated ON arm's unrelated negative rate was 44.44% and the combined production flag stack is untested |
| semantic-digest direct activation | do not promote/enable | measured regression |
| claim-anchor join | keep default OFF | join works and 18/18 anchors validate, but source-invariance gate is RED and claim text quality is weak |
| four-lane router | do not promote/enable | bridge gate remained 4/6 with no OFF→ON delta; target corpus has no identity-matched digest profiles |
| hydration waterfall | keep default OFF | preservation comparator is green, but live A/B exercised only `full`, not summary/skip |

## Review branch and receipt pointers

| Family | Review branch | Receipt commit |
|---|---|---:|
| E2E finding 1, mention-noise guard | `claude-continuation-20260713` (historical foundation) | `47fee92` |
| E2E finding 2, verified dedupe | `claude-continuation-20260713` (historical foundation) | `0c3d123` |
| E2E finding 3, summary-preserving resume | `claude-continuation-20260713` (historical foundation) | `d7ae48e` |
| E2E finding 4, bounded graph transactions | `claude-continuation-20260713` (historical foundation) | `74d2317` |
| E2E finding 5, composite identity | `claude-continuation-20260713` (historical foundation) | `a8c25dc` |
| MLX stability | `codex/mlx-embedder-stability-20260717` | `12eb204` (`c74acb9` code) |
| relationship allocation | `codex/evidence-allocation-audit-20260716` | `3157ec9` |
| `corpus_scope.v2` refusal | `codex/refusal-arbiter-20260716` | `3e0acc5` (`3363d5d` code) |
| temporal routing regression | `codex/temporal-regression-20260717` | `1d82cc4` |
| semantic activation | `codex/semantic-activation-20260716` | `899c930` |
| claim-anchor join | `codex/claim-anchor-join-20260717` | `883ce24` |
| four-lane router | `codex/router-tier0-20260717` | `5e42592` |
| hydration waterfall, static | `codex/hydration-waterfall-20260717` | `37fe0c1` |
| hydration waterfall, integrated A/B | `codex/router-waterfall-integration-20260717` | `f009626` |
| consolidated owner report | `codex/owner-results-report-20260717` | this publication commit |

## Verification labels

- **VERIFIED:** numbers explicitly reported from immutable eval artifacts,
  true-exit receipts, store fingerprints, or completed tests.
- **INFERRED:** every 500-book request, worker-second, wall-time, fleet, and
  dollar figure is a batch-64 model projection. The 58-worker estimate is a
  separate batch-32 rate extrapolation for replaying the measured
  15-book/595-request workload in three minutes; the 6-worker estimate is the
  batch-64 model's current-corpus result. Digest routing remains a
  mechanism-level inference for corpora that actually have identity-matched
  profiles; the E2E router diagnostic did not verify a benefit.
- **ASSUMED:** batch size 128 performance is unverified and excluded from the
  recommendation. No other acceptance metric in this report is assumed.

Publication includes the exact schema census, recorded timing baseline,
branch/commit pointers, and a consolidated COORDINATION receipt. Every feature
remains default OFF on its review branch; no review branch was merged to the
shared branch by this report.
