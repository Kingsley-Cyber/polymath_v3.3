# GLiNER-Relex vs the in-repo frame extractor — head to head

**MEASURED 2026-07-31** · host `Darwin 25.4.0` (Apple), `local_ghost_b/.venv`
python3.11 + `gliner` @ GitHub main · model
`knowledgator/gliner-relex-large-v1.0` (0.5B, DeBERTa-v3-large, Apache 2.0)
· harness `scratchpad/bench_relex.py`

Same 30 chunks, same blind gold (`RECALL_GOLD_V2_30CHUNKS_2026-07-31.json`),
**same matcher function copied verbatim** from `relation_stage_trace.py` so the
comparison cannot drift.

## Verdict

GLiNER-Relex triples the frame extractor's *ceiling* and does it end-to-end.

| pipeline | rel/chunk | recall (pair) | recall (strict) | recall (+predicate) |
|---|---|---|---|---|
| frame extractor — **end to end** | 0.033 | **0.000** | 0.000 | 0.000 |
| frame extractor — generator *ceiling* | 8.27 | 0.229 | 0.229 | n/a |
| **GLiNER-Relex @0.3/0.3 — end to end** | 17.2 | **0.694** | **0.667** | **0.417** |

0.229 was a ceiling nothing downstream could beat. 0.694 is an actual output.

### Threshold sweep (MEASURED, 30 chunks)

| entity thr | relation thr | ent/chunk | rel/chunk | pair | strict | +predicate | wall |
|---|---|---|---|---|---|---|---|
| 0.30 | 0.30 | 22.5 | 17.2 | **0.694** | 0.667 | 0.417 | 10.4 s |
| 0.30 | 0.40 | 22.5 | 11.3 | 0.444 | 0.417 | 0.278 | 11.3 s |
| 0.35 | 0.50 | 21.0 | 7.3 | 0.333 | 0.306 | 0.194 | 13.1 s |
| 0.50 | 0.50 | 17.0 | 6.1 | 0.333 | 0.306 | 0.194 | 10.5 s |
| 0.35 | 0.70 | 21.0 | 3.5 | 0.250 | 0.222 | 0.139 | 11.5 s |

Recall is dominated by the **relation** threshold, not the entity threshold.
The model card's recommended 0.7–0.9 relation threshold is tuned for precision
and costs most of the recall on this corpus.

## The pronoun problem, measured

Owner objection: *"i need llm like near extractions. i and pronouns are not it."*

| | pronoun rate |
|---|---|
| current GLiNER v1 entity output | ~13.4% |
| **GLiNER-Relex entities @0.3/0.3** | **1.0%** (7 / 675) |
| **GLiNER-Relex relation endpoints** | **2.1%** (11 / 516) |

A 13× reduction, before any filtering. The residual 2.1% is exactly what the
existing hard-rule tier in `entity_quality.judge_relation_anchor` removes —
`(I) -works for-> (Al)`, `(we) -depends on-> (world)`.

## Why this model and not the alternatives

Published benchmarks from the GLiNER-Relex paper (arXiv 2605.10108), micro-F1:

| model | CoNLL04 | DocRED | FewRel | CrossRE | avg |
|---|---|---|---|---|---|
| **GLiNER-Relex** | 40.4 | 31.3 | 12.5 | 18.1 | **25.6** |
| GPT-5-mini | 42.4 | 18.6 | 15.0 | 12.4 | 22.1 |
| GLiNER2 | 32.9 | 11.7 | 20.8 | 6.0 | 17.8 |
| GLiREL (gold spans) | 4.5 | 2.4 | 24.0 | 1.4 | 8.1 |

An encoder model beating a small LLM on end-to-end relation extraction. That is
the owner's stated requirement — LLM-grade output with the LLM staying out of
extraction — met by a deterministic 0.5B local model.

**GLiREL** is already installed and working in `local_ghost_b/.venv` (v1.2.1,
`jackboyla/glirel-large-v0`). It is not the answer: it classifies relations
between *given* gold entity spans, so end-to-end it inherits every entity error
and scores 8.1. Its FewRel number (24.0, best in table) is a gold-span result.

**ReLiK** (SapienzaNLP, ACL 2024) is the other credible non-LLM option. Not
benchmarked here — its relation models are trained on NYT/CoNLL schemas and
would need retargeting to `config/ontology.yaml`. Worth a second look only if
GLiNER-Relex precision proves unfixable.

## What is NOT measured — read before deciding

- **Precision is unmeasured.** 17.2 relations/chunk at 0.3/0.3 will contain
  junk. Visible in the raw output: `(Web) -synonym of-> (Web)` (self-loop),
  duplicate near-identical pairs, and bibliographies producing `located in`
  chains. This number needs the same hand-judged gate the frame model got
  (gate v2 protocol) before any rollout.
- The 36-relation gold is small. Treat 0.694 as "clearly much better", not as a
  precise figure.
- Bibliographic authorship (`Gaynor, S. (2013) Gone Home` → `created_by`) is
  missed by GLiNER-Relex too — the same blind spot as the frame model.

## Fit with what is already built

The existing filter stack is not wasted; it becomes the precision layer for a
much better generator. Every guard that measured useful still applies:
`frame_self_loop`, `frame_bibliographic_appositive`, `_is_structural_artifact`,
`pair_allowed` (ontology), and the hard-rule entity tier. The architecture
becomes **GLiNER-Relex generates → existing gates filter** — which is the
hybrid I recommended earlier for the wrong reasons and can now justify with
numbers.

## Cost of deployment

- MEASURED: 0.35–0.44 s/chunk, batch_size 1, host CPU/MPS.
- PROJECTED: 306,311 chunks ≈ 34 h single-threaded at that rate. Batching
  (`batch_size=8` is supported) and the RunPod GPU lane should cut this
  substantially. Not measured — do not quote the projection as a result.
- Model is 0.5B / F32. Larger than current GLiNER medium; check the RunPod
  deterministic image size budget before baking.

## Blocking dependency

`gliner` on PyPI (0.2.26 installed, 0.2.28 latest) does **not** ship the relex
classes. `UniEncoderSpanRelexGLiNER` and `.inference()` exist only on GitHub
main. Installed here isolated via
`pip install --target ./glmain --no-deps git+https://github.com/urchade/GLiNER.git`
so the glirel-pinned venv was not touched. **A production lane must pin a
commit SHA, not `main`** — the determinism contract requires it.

## Reproduce

```bash
cd <scratchpad>
PYTHONPATH=./glmain local_ghost_b/.venv/bin/python bench_relex.py 0.3 0.3
```
