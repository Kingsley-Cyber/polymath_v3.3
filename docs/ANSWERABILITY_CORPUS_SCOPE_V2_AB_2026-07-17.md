# Answerability Corpus-Scope v2 A/B Receipt — 2026-07-17

Status: **ACCEPTANCE GREEN; FEATURE DEFAULT OFF**

Scope: chat-side answerability arbiter only. The retriever's strict
`_evaluate_sufficiency` implementation, retrieval scoring, prompts, frozen eval
spec, and corpus data were not changed.

## Finding from the frozen 51-run artifact

The authoritative baseline is
`e2e-retrieval-results.json` (started `2026-07-16T11:46Z`, 51 executions). Its
negative-control rate was 4/9. All five false answers arrived at the chat
arbiter with the retriever's `raw_answerable=false`; the legacy chat policy then
promoted generic 50% atom overlap:

| Query | Tier(s) wrongly answered | Generic overlap that caused promotion | Legacy status |
|---|---|---|---|
| quantum error-correcting code | graph | `best` / `code` | partial |
| human genomics guide | hybrid, graph | `guide` / `sequence` | partial |
| 2025 US federal tax rate | fast, hybrid | `statutory` / `tax`, then `rate` / `tax` | answerable |

This was an arbiter-layer loosening defect, not a retrieval-sufficiency defect.

## Versioned change

`corpus_scope.v2` derives a conservative, domain-neutral distinctive-term
signature from the query. It can refuse only when all of these are true:

1. `ANSWERABILITY_CORPUS_SCOPE_V2_ENABLED=true`;
2. the request is corpus-scoped and has retrieved evidence;
3. strict retrieval already reported `raw_answerable=false`;
4. the legacy chat arbiter would loosen that to `answerable` or `partial`; and
5. at least two distinctive terms exist and less than 60% are present in the
   final evidence packet (chunk text, summary, heading, and facets).

The feature flag defaults to `false`. The trace records policy version,
signature, matched/missing terms, coverage, and whether the guard fired.

## Tests

Canonical backend image command (isolated from the live stack):

```text
docker run --rm --env-file <redacted-env> -e PYTHONPATH=/app \
  -v <worktree>/backend:/app polymath_v33-backend:latest \
  pytest -q tests/test_answerability_corpus_scope_v2.py \
  tests/test_answerability_honesty.py \
  tests/test_answerability_text_fallback.py \
  tests/test_answerability_gate_loosening.py \
  tests/test_pt10_answerability_bundle.py \
  tests/test_chat_orchestrator_coverage.py \
  tests/test_retrieval_three_tier_validation.py \
  tests/test_retrieval_ranking_policy.py
```

Result: **147 passed**, 14 warnings, `EXIT=0`. The new suite covers the exact
generic-overlap defect, two adjacent different-domain variants, a grounded
positive query, a non-eligible lay query, and the default-OFF path. A pure
replay reproduced the 39 stored direct/lay/negative baseline gate statuses
39/39; v2 changed no positive status and produced 9/9 negative refusals.

## Live A/B

Both arms used the same frozen preregistration hash, same 13-query subset
(direct + lay + negative), all three tiers, and the same synthesis contract:
`opencode-go-anthropic` / `anthropic/minimax-m2.7`. The flag was the only
intentional difference.

| Metric | Frozen 51-run historical | Same-session OFF | Same-session ON | Target | ON |
|---|---:|---:|---:|---:|---|
| Execution closure | 51/51 | 39/39 | 39/39 | 39/39 subset | pass |
| Technical success | 100% | 100% | 100% | 100% | pass |
| Negative refusal | 4/9 (44.4%) | 6/9 (66.7%) | **9/9 (100%)** | 100% | pass |
| Direct doc-hit | 88.9% | 88.9% | **88.9%** | >=85% | pass |
| Lay-language doc-hit | 91.7% | 100% | **91.7%** | >=75% | pass |
| Positive guard applications | n/a | 0 | **0** | 0 | pass |

The historical/same-session difference is provider/retrieval nondeterminism;
both comparisons are retained. The acceptance decision uses the paired
same-session OFF/ON run while the historical 4/9 failure remains the declared
before state.

### Per-query aggregate

Positive entries are doc-hit executions out of three tiers. Negative entries
are refusals out of three tiers.

| Query | Shape | Historical | OFF | ON |
|---|---|---:|---:|---:|
| `direct_facs` | direct | 3/3 | 3/3 | 3/3 |
| `direct_rule_of_six` | direct | 3/3 | 3/3 | 3/3 |
| `direct_laban_machine` | direct | 3/3 | 2/3 | 2/3 |
| `direct_edit_grammar` | direct | 2/3 | 2/3 | 2/3 |
| `direct_anatomy_masses` | direct | 2/3 | 3/3 | 3/3 |
| `direct_elemental_novel` | direct | 3/3 | 3/3 | 3/3 |
| `lay_dynamic_figure` | lay | 3/3 | 3/3 | 3/3 |
| `lay_natural_cut` | lay | 2/3 | 3/3 | 3/3 |
| `lay_safe_fight` | lay | 3/3 | 3/3 | 3/3 |
| `lay_manga_attention` | lay | 3/3 | 3/3 | 2/3 |
| `negative_quantum` | negative | 2/3 | 2/3 | **3/3** |
| `negative_genomics` | negative | 1/3 | 1/3 | **3/3** |
| `negative_tax_law` | negative | 1/3 | 3/3 | **3/3** |

### Negative controls by tier

| Query | Tier | Historical | OFF | ON | v2 guard |
|---|---|---|---|---|---|
| `negative_quantum` | fast | refuse | refuse | refuse | not needed |
| `negative_quantum` | hybrid | refuse | refuse | refuse | not needed |
| `negative_quantum` | graph | answer | answer | **refuse** | applied |
| `negative_genomics` | fast | refuse | refuse | refuse | not needed |
| `negative_genomics` | hybrid | answer | answer | **refuse** | applied |
| `negative_genomics` | graph | answer | answer | **refuse** | applied |
| `negative_tax_law` | fast | answer | refuse | refuse | applied; independently weak in OFF |
| `negative_tax_law` | hybrid | answer | refuse | refuse | not needed |
| `negative_tax_law` | graph | refuse | refuse | refuse | not needed |

Durable live artifacts:

- OFF: `/data/ingest-files/runpod-job-journals/refusal-arbiter-off-20260717.json`
- ON: `/data/ingest-files/runpod-job-journals/refusal-arbiter-on-20260717.json`
- post-run corpus fingerprint:
  `/data/ingest-files/runpod-job-journals/refusal-arbiter-corpus-after-20260717.json`

The subset wrapper intentionally excluded the four relationship queries. The
unmodified full-suite finalizer therefore returned `EXIT=1` only because the
relationship metric had zero executions. Scoped validators returned `EXIT=0`
for both arms and `acceptance=true` for ON. This receipt does **not** relabel the
subset as a full 17-query-suite pass.

## No-corpus-write and restoration receipt

The read-only post-run fingerprint differed from the preceding fingerprint in
only `mongo.ingest_scheduler_state` (scheduler heartbeat); chunks, documents,
lexicon, summaries, Qdrant collections, and Neo4j nodes/relationships were
byte-for-byte unchanged. The live backend was restored to repository revision
`3157ec9`, reported healthy, and both `ANSWERABILITY_CORPUS_SCOPE_V2_ENABLED`
and `RELATIONSHIP_EVIDENCE_PLAN_ENABLED` were verified `False` before releasing
`/tmp/polymath-eval.lock`.
