# Feasibility Report — Deterministic Local Extraction (GLiNER ×2 + GLiREL + Python) + 100–150-Token Child Chunks

**Date**: 2026-06-09
**Question**: Can Python + GLiNER + GLiREL replace local-SLM and cloud-LLM extraction at near-equal-or-better quality against the existing Ghost B schema, and is moving child chunks to 100–150 tokens sound for a cross-domain corpus?
**Verdict**: **Feasible — and ~90% already built and validated.** Relations and facts are at or near cloud parity on cue-bearing technical prose; numeric facts are *better* than any LLM lane (verbatim-grounded, zero hallucination by construction). Three concrete parity gaps remain (entity precision on generic nouns, `object_kind` coverage, `definitional_phrase`), each with a cheap deterministic mitigation. The 100–150-token child proposal is sound and actually *moves production into the band the local stack was validated on*; it requires one config change in `tier_chunker.py` and costs ~2.8× more Qdrant points at roughly neutral total compute.

---

## 1. What is already built and proven (Phase A, committed locally)

The deterministic lane shipped as 5 commits on local `main` (`0d60567`..`56644e7`):

| stage | implementation | status |
|---|---|---|
| Entity tagging | GLiNER pass-1 (`urchade/gliner_medium-v2.1`, thr 0.45, 14 types) | ✅ validated on real docs |
| Facet (`object_kind`) | GLiNER pass-2, 28-label vocab, deduped per unique entity | ✅ 5/5 on clean contexts; 38% coverage on flame smoke |
| Relations (30 predicates) | GLiREL fine-tuned v1, sentence-windowed, type-gated, thr 0.40 | ✅ 83–94% typed share on bench docs |
| Numeric facts | `enrich.py` rules (quantity/timestamp/threshold/property), conf 1.0 | ✅ 12/12 pass Pydantic, verbatim values |
| Qualitative facts | `enrich.py` rules (status/category/tag/rule_condition/rule_action), conf 0.9 | ✅ all 5 types firing, generic-noun filter applied |
| In-text aliases | Schwartz-Hearst + casing variants | ✅ (e.g. HNSW ↔ "Hierarchical Navigable Small World") |
| Contract | emits cloud's exact `ExtractionResult` shape, `schema_version="polymath.extract.v1"`, `.text` populated | ✅ 0 Pydantic drops on smoke; deterministic (byte-identical re-runs) |

End-to-end smoke (full real flame doc, 8 chunks): 32 entities / 17 relations / 17 facts, 0 validation drops, deterministic, warm ~577 ms/chunk (worst case — see §5).

## 2. Field-by-field parity vs cloud Ghost B

| field | cloud LLM | local deterministic | parity verdict |
|---|---|---|---|
| `entity.canonical_name/surface_form/entity_type` | LLM judgment, 15 types | GLiNER zero-shot, 14 real types + junk-strips | **Near parity.** Gap: GLiNER tags generic nouns (`engine` cf 0.49, `game`, `world`) the LLM would skip. Fix in §4.1 |
| `entity.confidence` | LLM self-report | GLiNER softmax | Local is *more honest* (calibrated-ish vs LLM's invented numbers) |
| `entity.query_aliases` | in-text + semantic synonyms | in-text only (S-H + casing) | **Accepted gap** (locked): ~5–10% query-recall loss; embedder absorbs at query time |
| `entity.definitional_phrase` | 1-sentence definition | empty | **Gap with cheap fix** — §4.3 |
| `entity.object_kind` | ~all entities (per Pt9b notes) | 38% on smoke (conservative match), rest falls to downstream taxonomy via `result.text` | **Gap with options** — §4.2 |
| `relation.predicate` (30) | LLM, ~21% related_to pre-Pt-8b | GLiREL ft: 6–17% related_to on bench (83–94% typed) | **At/near parity on cue-bearing text**; weaker on implicit no-cue relations |
| `relation.evidence_phrase` | LLM-quoted (evidence gate exists because it fabricates) | actual sentence the relation was scored in | **Local better** — evidence is real by construction |
| `fact.*` numeric (4 types) | paraphrased values | verbatim substrings, conf 1.0 | **Local better** (the SLM lane hallucinated "8 GB" for "4 GB"; rules can't) |
| `fact.*` qualitative (5 types) | LLM recall high | rules: precision-first, est. 60–75% of LLM recall | **Lower recall, higher groundedness** — acceptable per A.4 acceptance |
| determinism | nondeterministic even at temp 0 | bit-for-bit on same machine | **Local better** (idempotent re-ingest, debuggable) |
| hallucination risk | nonzero (hence evidence gate) | structurally zero (span-grounded) | **Local better** |
| marginal cost | $ thousands at corpus scale (§6) | $0 | **Local better** |

**Honest bottom line on "same quality or better":** better on faithfulness, determinism, evidence, numeric facts, cost; equal-ish on typed relations and core entities; worse on implicit relations, qualitative-fact recall, semantic aliases, and (until mitigated) facet coverage + definitional phrases.

## 3. Where the predicate floor sits (cross-domain caution)

Prior finding (memory + experiments): pure-BERT on clean entities floors at ~50% `related_to`; GLiREL fine-tuned v1 measured **83–94% typed** on the two bench domains. The 523-file merged corpus is cross-domain; domains far from the fine-tune distribution may drift back toward the floor. **Monitor**: per-doc typed share from Phase-14 counters; if a domain collapses toward ~50%, that's the floor signature (and 100% typed = junk-entity signature). Remedy would be a domain-expanded fine-tune v3 using the v1 recipe — *not* the v2 literal-recovery recipe, which regressed (micro-F1 0.426 vs 0.443).

## 4. Parity punch list (to close the three real gaps)

1. **Entity precision pass** (~1 hr): confidence floor (drop < ~0.50 unless multi-word/proper-noun), extend `_GENERIC_PERSON`-style blocklist to generic Concept/Software nouns (`engine`, `system`, `way`...). Evidence: flame smoke kept `engine` at cf 0.49.
2. **Facet coverage** (~2–4 hr): (a) use the *parent* chunk (1200 tok) as facet context instead of the child's first 1000 chars; (b) when GLiNER pass-2 misses, fall back to the definitional "X is a/an Y" sentence already detected by the category rule; (c) only as last resort let `graph_backfill` taxonomy fill (already wired via `result.text`). Do **not** blanket-fallback to `entity_type` — it would shadow taxonomy refinement.
3. **`definitional_phrase`** (~1 hr): populate from the category-cue sentence (`X is a Y ...`) — the rule already finds it; copy the sentence (≤200 chars) onto the entity. Closes a cloud-parity gap deterministically.
4. *(Optional, later)* cheap pronoun→nearest-prior-entity resolver to recover coref-blocked facts ("It needs 4 GB").

## 5. Chunk-size analysis: 350 → 100–150 token children

**Current production** (`tier_chunker.py`): child target **350** tok (min 128 / max 512), parent target 1200 (min 500 / max 2000, overlap 200), `sentence_merge` strategy, cl100k tokenizer.
**Proposed**: child target ~128 tok (min ~64 / max ~192–256), parents unchanged.

Stage-by-stage impact:

| stage | effect of 100–150-tok children | net |
|---|---|---|
| Retrieval (cross-domain) | higher topic purity per vector → better precision on heterogeneous corpora; recall preserved by existing small-to-big (retriever dedupes by `parent_id` + hydrates parent text) | **better — your stated motivation is architecturally supported** |
| GLiNER pass-1 | encoder cost ≈ linear in tokens → total compute ~constant; **alignment bonus: the local stack was validated on ~100–190-token chunks** (400-char bench chunker; 188-tok measured avg), so production moves *into* the validated band | neutral→better |
| GLiREL | sentence-windowed (≤160-tok sentences) → chunk size barely matters; fewer entities/chunk = pair caps (16/24) bind less; fewer 128-tok internal truncations | neutral→slightly better |
| Facet pass | per unique entity per doc — unaffected (use parent as context per §4.2a) | neutral |
| Facts/aliases | per-sentence rules — unaffected | neutral |
| Embedder | total tokens constant, ~2.8× more (smaller) calls, batched; all well under the 960-tok safe max | ~neutral |
| Qdrant | ~2.8× more points (~570–680k vs ~243k for the 85M-token corpus) — more storage + HNSW build time, well within local Docker capacity | acceptable cost |
| Neo4j | entities/relations dedup at doc/graph level — unchanged | neutral |
| Ghost A (cloud summaries) | operates on parents — unchanged | neutral |

**Caveats**: keep sentence integrity (never mid-sentence splits — `sentence_merge` already guarantees this); child min must drop below target (128→~64) or short sections will all collapse to min; expect per-chunk Python/dispatch overhead × ~2.8 more chunks ≈ +1–3 hr on a full-corpus run.

**One design lever to remember**: extraction quality wants bigger windows, retrieval wants smaller. If relation recall ever feels thin at 128-tok children, run extraction per *parent* and attribute to children — the contract supports it (`ExtractionTask` is just id+text). Not needed now because GLiREL is sentence-windowed anyway.

## 6. Throughput + cost at full-corpus scale (85M tokens, 523 files)

| | local deterministic | cloud LLM |
|---|---|---|
| chunks @ ~125-tok children | ~570–680k | same |
| per-chunk warm | ~330 ms estimated at scale (577 ms measured on an 8-chunk doc where the facet pass can't amortize; cold load ~20 s once per process) | ~1–3 s API latency, parallelizable with money |
| full corpus | **~2–3 days** single M1 Max (Metal serializes; GLiNER+GLiREL+embedder share the GPU) | hours if heavily parallel |
| marginal cost | **$0** (electricity) | order $2–9k (≈1.5B prompt-tokens incl. per-chunk schema overhead + ~270M output; Haiku-class → Sonnet-class; prompt caching could cut input several-fold) |
| RAM | ~2.5–3 GB (GLiNER 0.5 + GLiREL 1.9 + overhead) — fits the 24-of-32 GB envelope with the embedder | n/a |
| re-ingest idempotency | deterministic → identical MERGE | nondeterministic |

## 7. Risks & monitors

1. **OOD domains → predicate floor** (§3). Monitor per-doc typed share; flag docs < ~60% typed.
2. **Junk entities on noisy files** — 100% typed share or entity spikes = junk signature; the chunker noise-strips (code fences, URLs, citations) are the main defense; extend blocklist per §4.1.
3. **Facet coverage stays low** (38% smoke) until §4.2 lands — downstream taxonomy fills some but cloud parity needs the punch list.
4. **Deployment runtime (the one blocking decision)**: the backend worker container is Linux Docker (exited 8 days); Docker cannot use MPS. The in-process design requires the **worker to run natively on macOS** (the `local_ghost_b/.venv` now has the full stack incl. pydantic) — or wrap `ghost_b_local` as a sidecar like embedder/docling. Native is the locked Phase A design and needs zero extra code. Full Neo4j/Qdrant e2e smoke is gated on this choice (see `A6_SMOKE_AND_DEPLOYMENT.md`).
5. **GLiREL weights provenance**: keeper is v1 (`models/glirel_ghost_b_v1/best/`, 1.87 GB). Don't swap; don't re-run v2.

## 8. Recommendation

Proceed. Order of operations:
1. Decide worker runtime (native recommended) → run the gated e2e smoke (extraction→Neo4j→Qdrant) on flame.
2. Land the chunker change: `child_target_tokens 350→128`, `child_min 128→64`, `child_max 512→192`; re-run smoke + a 2–3-file retrieval A/B (350 vs 128 children) to confirm the cross-domain precision win on *your* queries.
3. Land the parity punch list (§4: ~4–6 hr total) — this is what takes quality from "near cloud" to credibly "same or better" on your schema.
4. Batch-ingest a 10–20-file cross-domain sample; check typed-share distribution before committing to the 2–3-day full run.

*Evidence sources: Phase A smoke (`/tmp/test_ghost_b_local.py` on `flame_chunks.jsonl`), `04_PRIOR_EXPERIMENTS.md` measurements, `A1_FINDINGS.md` contract map, `tier_chunker.py` + retriever inspection 2026-06-09.*
