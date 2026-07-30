# DETERMINISTIC RELATION RECALL SPEC — spaCy + GLiNER + Python to GLiREL-level yield

**Status:** SPEC / awaiting owner GO on R0 (fixture v2) — nothing executed.
**Author + executor:** Claude (ROLE LAW re-engraved 2026-07-30 — Codex retired,
Claude holds the executor seat; discipline unchanged: receipts or NOT RUN).
**Date:** 2026-07-30.
**Anchors:** `CONTINUITY/Extraction_Elite_Roadmap.md` (P4/P5 queued),
checklist anchor **P2.11** (to be created — does not yet exist in
`docs/RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md`; the roadmap's cited P2.10 is
also absent — see §7).

---

## 0. THE ASK, RESTATED PRECISELY

Owner directive: *a good deterministic pipeline for spaCy + GLiNER + Python
that does the same level of extraction the control plane currently gets from
GLiREL. LLM stays in its current role (summaries/cloud), never in extraction.*

"Same level" is **yield**, not behaviour. GLiREL's measured precision on the
frozen gate is **0.273** — imitating it would be a regression. The target is to
match or beat its *quantity* while holding the precision the deterministic lane
already has:

| Metric | GLiREL (measured) | dep-path today (measured) | TARGET |
|---|---|---|---|
| Precision (gate v2) | 0.273 | **1.000** (v1) | **>= 0.80** (hard floor, every rung) |
| Recall (gate v2) | 0.200 | **0.467** (v1) | **>= 0.75** |
| F1 | 0.231 | 0.636 | **>= 0.78** |
| Relations / chunk (corpus) | 1.10 **raw** | 0.04 | **set by R-pre — see below** |
| Rel-bearing chunk share | 35% **raw** | ~4% | derived from the above |
| ms / chunk (Stage C) | 121 | **4.5** | **<= 25** |
| Determinism | n/a | byte-identical | byte-identical (unchanged) |

### The yield target is MEASURED, never inherited (corrected 2026-07-30)

An earlier draft set the finish line at `>= 1.00 rel/chunk` by copying GLiREL's
raw 1.10. **That number is wrong to inherit.** GLiREL's precision is 0.273 — the
majority of what it emits is noise, so its raw count is not a recall ceiling, it
is a recall ceiling *plus* an unknown amount of garbage. Hand-judging the 4
sampled enumeration edges from the chunk_0099 head-to-head: 2 are genuine
(`animation systems uses IK` / `uses retargeting`), 2 have manufactured
predicates (`biomechanics supports …`). ~50%.

So the honest recoverable target is roughly **half of GLiREL's raw yield**, and
the exact figure is not guessable — it is **measured by R-pre** (§2-pre) before
the ladder starts. Provisional planning band: **0.55 – 0.75 rel/chunk floor,
1.0+ stretch**, superseded by R-pre's number the moment it exists.

Every rung is gate-guarded: a rung that drops precision below 0.80 is reverted,
not argued with. **Yield below target with precision intact is a partial win and
is reported as a percentage. Precision broken is not a percentage — it is a
failure.**

### Deployment intent (owner, 2026-07-30)

**Local on-device extraction is the default and the easy path.** RunPod is NOT
retired — it stays available for bursts and large corpora per existing ingestion
doctrine. But the go-to road is local, and it is allowed to be slower. That
reorders two things:
- the local path must work with **no special flags or setup gymnastics** — one
  documented command, verified by `scripts/verify_backend_runtime.sh`;
- **R7 (ASR/transcripts) gains priority** — if local is the default road, the
  transcript corpus cannot stay a dead zone;
- **R8 stops being about rescue and becomes about parity** — pods must not
  silently emit fewer relations than the local path, or the two roads disagree
  about what is true.

---

## 1. WHY THE YIELD IS 0.04/chunk — EIGHT NAMED CAUSES, ALL IN CODE

Read from the live source, not from memory. Each cause cites its line.

### G1 — Coordination is rejected wholesale (LARGEST single cause)
`dep_path_extractor.py:1152-1154` drops any candidate whose signature contains
`cc` or `conj`. This guard was added in P2 to kill 16 false positives and it
worked — but enumeration is the single most common relation-bearing
construction in expository prose. The chunk_0099 head-to-head is entirely this:
GLiREL got 10 edges from `"traditional animation systems ... uses inverse
kinematics, retargeting, ..."`; dep-path got 1, because everything after the
first conjunct is unreachable.

**This is not a bug — it is a precision/recall trade that was never re-paid.**

### G2 — Cross-sentence pairs are structurally unreachable
`_shortest_dep_path` (line 614) builds an undirected adjacency list where a
sentence root's head is itself, so **no edge ever links two sentence roots**.
BFS therefore returns `[]` for any entity pair spanning a sentence boundary,
and line 1094 (`if not path: continue`) drops it silently. In multi-sentence
chunks (the norm at 188 tok/chunk) the majority of entity pairs are
cross-sentence. GLiREL has no such restriction — it scores any pair in the
window. `coreference_heuristic` exists in-tree and is **unwired** (flagged as a
P3 "wire-or-delete" decision that was never taken).

### G3 — Nominal predication is suppressed instead of re-anchored
`_is_light_verb_construction` (line 895) detects `make use of` / `give rise to`
and **drops the pair**. The eventive noun IS the predicate — `_is_eventive_noun`
(line 887) already identifies it. The same class covers verbless nominals the
extractor never attempts: *"the integration of X with Y"*, *"support for X in
Y"*, *"X's dependency on Y"*. Only `poss` and `appos` have verbless handling
today (lines 407, 762).

### G4 — T4 drops are silent and unmeasured
`resolve_predicate` returns `None` (line 580) whenever the verb lemma is absent
from `predicate_synonyms.yaml`. The adapter counts these only in aggregate
(`noise_or_t4_dropped`, line 232) — **the lemma is never recorded**. Nobody
knows which verbs are being discarded or how much recall they hold. This is the
cheapest un-taken win in the whole system: pure YAML, zero code risk.

### G5 — Verb POS mistagging (already diagnosed, never fixed)
`en_core_web_sm` tags `stores`, `references`, `indexes` as NOUN, so
`_extract_predicate_from_path` (line 719) finds no verb on the path. Roadmap P2
step 4 measured **4 FNs of 15 gold relations ≈ 27% of gate recall** from this
alone. Filed as backlog **PF-2**, never built.

### G6 — ASR/transcript chunks produce zero relations by construction
`_is_low_parse_confidence` (line 959) returns early for the entire chunk when
sentence-final punctuation is under 2% of tokens. The whole video corpus is
below that threshold. Entities still run; relations are **structurally zero**.

### G7 — The per-chunk cap discards by position, not by quality
`spacy_relation_adapter.py:223` breaks at `max_related` (10) over an unsorted
list, so on rich chunks the surviving 10 are whichever entity pairs happened to
come first in offset order. Free fix.

### G8 — The RunPod lane emits NO relations at all
`runpod_local_extraction.py:574` hardcodes `relations=[], facts=[]`. Every
corpus extracted on pods (cyb / ecom / video, ~79k chunks) has zero relations
regardless of how good the Mac lane becomes. **Any recall work that does not
ship into the pinned extraction image only ever helps Mac-lane ingests.**

### Contamination note (blocks measurement, not a cause)
Stored local extractions are mixed-provenance: on the probed corpus only
**27 of 680** stored relations carry dep-path sentinels (cf 0.9/1.0); the other
**653 carry GLiREL-era scores** (cf 0.4–0.97) from a stale sidecar. **No recall
claim may be measured against stored data** until a relations-only
re-extraction lands (§6).

---

## 2-PRE. R-pre — THE COUNTERS ARE THROWN AWAY (do this FIRST, before R0)

**Found 2026-07-30 while auditing the enumeration claim.** The instrumentation
that would size this entire mission already exists and is discarded.

- `dep_path_extractor.py:1154` increments `suppressed_conjunct_crossing`.
- `ghost_b_local.py:624` initializes the counter dict — and **does not include**
  `suppressed_conjunct_crossing`, `suppressed_multi_clause`, or
  `suppressed_exception_boundary`. They are created on the fly by `_inc()`.
- `ghost_b_local.py:818-821` emits **4 counters of the 14+ that exist**:
  `entity_drop`, `relation_drop`, `evidence_drop`, `fact_drop`.

Everything else — every suppression counter, every `qualified_*` counter, all
three P2 structural guards — is computed, incremented, and garbage-collected at
the emit boundary. **The repo law "never drop silently, every suppression
increments a named counter" is honored inside the extractor and defeated on the
way out.** Nobody could have known what the conjunct guard costs, even in
principle. That is why §5's numbers were projections in the first place.

**R-pre deliverable:**
1. Initialize all suppression/qualified keys in the `counters_per` dict.
2. Emit the full counter map on `ExtractionResult` (one nested object, not 14
   top-level fields) so it persists to Mongo and is queryable per corpus.
3. Run over a >= 5,000-chunk sample spanning books + papers + transcripts.
4. **Report the real distribution**: how many candidate pairs die at each guard,
   per chunk and corpus-wide.

**This converts the shakiest assumption in the plan into a number.** R3's true
ceiling is `suppressed_conjunct_crossing` per chunk x the share that survives
the non-distributive guards x ~0.5 (the genuine-relation rate estimated above).
**§0's yield target is set from this measurement.** R-pre is cheap, it is a
prerequisite for honest sizing, and it makes every later rung auditable.

---

## 2-PRE-RESULTS. R-pre MEASURED 2026-07-30 — THE PREMISE WAS WRONG

**Run:** 5,500 chunks, deterministic stratified sample (books 2,700 / papers
1,000 / ASR 1,800) across 5 live corpora. Host venv `local_ghost_b/.venv`,
spaCy 3.8.14, en_core_web_sm, darwin arm64. Two passes (types as-stored and
normalized). Receipts: `backend/scripts/rpre_{export_sample,measure}.py`,
report JSON in the R-pre commit. **MEASURED, not projected.**

### Finding 1 — the 0.04 rel/chunk baseline was FALSE. The real rate is 0.5476.

| | claimed in §0 | MEASURED |
|---|---|---|
| relations / chunk | 0.04 | **0.5476** |
| rel-bearing chunk share | ~4% | **19.3%** |
| ms / chunk (Stage C) | 4.5 | **13.5** |

The 0.04 came from dividing the *dep-path-sentinel subset* of one corpus's
STORED relations by its chunk count. It measured storage, not the extractor.
Run live, the extractor is **13.7x more productive than the number this entire
plan was built on.**

### Finding 2 — GLiREL parity is ALREADY EXCEEDED, today, with no rungs built

GLiREL raw 1.10 rel/chunk at P 0.273 → genuine ≈ **0.30/chunk**.
Dep-path today: **0.5476/chunk at gate P 1.000.**

**The deterministic lane already produces ~1.8x GLiREL's genuine yield.** §0's
"floor 0.55 / stretch 1.0" target is met at 0.5476 before R1 is written. The
recall emergency this spec was written to solve does not exist in the extractor.

### Finding 3 — the real problem is G8, and it is 98.6% of the corpus

Stored relations per corpus, live count:

| corpus | chunks | chunks w/ relations | relations | rel/chunk | provider |
|---|---|---|---|---|---|
| ecommerce_meta | 161,108 | **0** | **0** | 0.0000 | runpod_local_extraction |
| video_gen_schools | 78,891 | **0** | **0** | 0.0000 | runpod_local_extraction |
| authentic_library_v2 | 60,137 | **0** | **0** | 0.0000 | runpod_local_extraction |
| cybersecurity_study | 39,885 | **0** | **0** | 0.0000 | runpod_local_extraction |
| markbuilds_transcripts | 17,825 | **0** | **0** | 0.0000 | runpod_flash / runpod_local_extraction |
| cpcs_local | 617 | 216 | 680 | 1.1021 | *(local lane)* |

**357,846 of 362,759 chunks — 98.6% — hold zero relations, and every one of
them was extracted by the RunPod lane** whose `relations=[]` hardcode is G8.
The single locally-extracted corpus is the only one with relations at all.

The graph is empty because the output was never written, **not** because the
extractor is quiet. R8 was ranked last in §3 and sized as "parity". It is
neither last nor parity — **it is essentially the whole mission.**

### Finding 4 — the suppression ranking was wrong: multi-clause dominates

| guard | total | per chunk |
|---|---|---|
| `suppressed_multi_clause` | 21,658 | **3.94** |
| `suppressed_conjunct_crossing` | 8,196 | **1.49** |
| `suppressed_expletive` | 2,830 | 0.51 |
| `skipped_verbless` | 1,885 | 0.34 |
| `suppressed_contrast` | 1,549 | 0.28 |
| `suppressed_agentless_passive` | 1,179 | 0.21 |
| `skipped_low_parse_confidence` | 918 | 0.17 |
| `suppressed_light_verb` | 636 | 0.12 |
| `suppressed_exception_boundary` | 102 | 0.02 |

§1 named G1 (coordination) "the LARGEST single cause". It is second.
**`suppressed_multi_clause` discards 2.6x more** — 3.94 candidates per chunk.
That guard was added in P2 to kill 12 of 16 FPs and has never been revisited.
It is now the largest single recall lever and deserves its own rung.

**R3's ceiling, MEASURED:** 1.49 conjunct-crossings/chunk → ~0.75/chunk at the
0.5 genuine rate. The "6.5x GLiREL" figure is retired.

### Finding 5 — ASR is not a dead zone in extraction

ASR: **0.4928 rel/chunk, 18.3% bearing** — 75% of the book rate, not zero.
`skipped_low_parse_confidence` fires only 0.17/chunk. R7's premise ("the
transcript corpus is a dead zone") is wrong at the extractor; transcripts are
dead in *storage*, for the same G8 reason as everything else.

Per genre: book **0.6537** · asr **0.4928** · paper **0.3600**.

### Finding 6 — the entity_type casing hazard is real but minor

`ontology.yaml` is Title Case; RunPod corpora store UPPERCASE (`CONCEPT` vs
`Concept`), and `allowed_pairs` is an exact tuple match. Normalizing recovers
**84 relations (2,928 → 3,012, +2.9%)**. Real, cheap to fix, not a headline —
and it becomes load-bearing the moment R8 makes pod-extracted relations real.

### Consequence — the ladder is re-ordered by evidence

1. **R8 is promoted to FIRST.** It is 98.6% of the missing graph. Every other
   rung optimizes an extractor whose output is already discarded.
2. **New rung R9: revisit `suppressed_multi_clause`** (3.94/chunk, the largest
   lever) — same discipline as R3: no blanket removal, gate-guarded.
3. R3 keeps its place but sized at ~0.75/chunk, not the retired 6.5x.
4. R7 demoted — ASR extracts fine; its problem is G8.
5. R0 still required, but as an **acceptance gate for precision**, not to
   diagnose a recall emergency that the measurement did not find.
6. §0's yield target is **met today**. The mission's success metric must move
   from "raise rel/chunk" to **"relations reaching durable storage per chunk,
   corpus-wide"** — currently 0.0000 on 98.6% of the corpus.

---

## 2. R0 — THE FIXTURE BLOCKS EVERYTHING (owner decision required)

`spacy_relation_gate_v1` holds **15 asserted relations across 11 samples** — at
the declared `thin_evidence_minimums` floor. One additional true positive moves
recall by **6.7 points**. It cannot resolve seven rungs, and it contains no ASR
sample at all, so G6 is unmeasurable against it.

The gate spec is `immutable_after_first_decisive_inference; changes require a
new version and a new pre-run hash`. So this is not an edit — it is a new
version, and fixture scope is an owner decision.

**R0 deliverable — `spacy_relation_gate_v2`:**

| Property | Requirement |
|---|---|
| Asserted relations | >= 120 |
| Distinct samples | >= 40 |
| Predicate types | >= 12 |
| Domain span | books + papers + **>= 8 real ASR transcript chunks** from the video corpus |
| Sampling | random over live chunk ids, seed recorded in the spec |
| Labeling | hand-labeled asserted-only, same derivation rule as v1 |
| Preregistration | pass thresholds + hashes committed **before** the first scoring run |
| v1 | retained and run every rung as a **regression canary** — P must stay 1.000 on v1 |

Without R0, every number below is noise. **R0 is the gate on the whole ladder.**

### Labeling accelerant: GLiREL as a SUGGESTER, never a labeler

Hand-labeling 120 relations is the most expensive single task in this mission.
Retired GLiREL is a bad extractor (P 0.273) but a usable **candidate generator**,
and it fires on exactly the constructions dep-path misses:

- Run retired GLiREL **offline** over the R0 sample (host sidecar :8084). Its
  output never touches production, never enters the pipeline, and is never a
  label.
- **Agreement set** (GLiREL and dep-path both fire on the same pair): near-certain
  relations — fastest rows to adjudicate.
- **GLiREL-only set** (predominantly enumerations): precisely the rows that
  decide R3's real ceiling — highest information per minute of human review.
- **Dep-path-only set**: precision-check rows.
- A human rules on **every** row. An unreviewed GLiREL suggestion is not gold and
  never enters the fixture. Record the suggester's involvement in the fixture
  provenance so the gate's independence is auditable.

---

## 3. THE RECALL LADDER — R1..R8, ORDERED BY YIELD PER UNIT OF RISK

Standing rule for every rung: **one rung per commit**; rerun gate v2 + v1 canary
+ the portable invariants; record P/R/F1 before and after; a rung that pushes
P below 0.80 on v2 or below 1.000 on v1 is reverted in the same session, not
tuned in place.

---

### R1 — T4 lemma telemetry, then mined YAML expansion *(config only, near-zero risk)*
**Closes G4.** Highest yield per unit of risk in the entire plan.

1. Instrument: in `resolve_predicate`, on the T4 path (line 580), record
   `(lemma, signature, subj_type, obj_type)` into a bounded module-level
   `Counter` (cap ~50k keys, LRU-evict, never unbounded). Emit it per batch
   alongside the existing rejection counters.
2. Harvest: run over >= 50k real chunks spanning all four corpora. Rank
   unmapped lemmas by frequency x clean-signature share (only count drops whose
   signature is single-clause and passes the structural guards — those are
   recoverable, the rest are noise).
3. Extend: batch the top-N into `predicate_synonyms.yaml` as T1/T2 rules where
   the type context is unambiguous, T3 only where the lemma is monosemous.
   **Every addition must target an existing Predicate Literal value** — the
   config loader fails loud otherwise (line 213), which is the desired guard.
4. Any lemma that has no honest home in the 30-value Literal goes on an
   **owner-decision list** for predicate extension. Do NOT widen the Literal
   unilaterally — `_VALID_PREDICATES` and the wire Literal must stay
   exact-equal (roadmap standing law).

`backend/scripts/mine_dependency_patterns.py` already exists for step 2 and
needs a `--t4-drops` mode pointed at the new counter.

**Acceptance:** T4 drop rate falls >= 40% on the harvest sample; gate v2 recall
rises; precision unchanged (config-only additions cannot create structural FPs,
only mis-typed ones — which the `allowed_pairs` gate catches).

---

### R2 — AttributeRuler POS-override lexicon *(backlog PF-2, small closed set)*
**Closes G5.** Add an `AttributeRuler` component to the shared `nlp` singleton
(`appos_enrichment.get_shared_nlp`) forcing `POS=VERB` for a **closed, versioned
lexicon** of domain verbs mistagged by `en_core_web_sm` — seed set: `stores`,
`references`, `indexes`, `maps`, `implements`, `supports`, `caches`, `wraps`.

Constraints:
- Lexicon lives in config (new `config/pos_overrides.yaml`), loaded fail-loud,
  hashed into the gate receipt — a POS lexicon change is a model-contract change.
- Only fires when the token is in a `nsubj … dobj` frame; a blanket override
  breaks noun readings ("the store", "the reference").
- Re-run the sm-vs-md A/B from P2 step 4 *after* the override — the two models
  make opposite errors, and the override may make md unnecessary (md/trf switch
  remains owner decision #1).

**Acceptance:** the 4 known gate-v1 POS FNs convert to TPs with P still 1.000 on
v1; measure delta on v2.

---

### R3 — Coordination distribution *(largest structural win)*
**Closes G1.** Do **not** remove the conjunct guard. Add a distribution pass that
runs *after* a head edge has already survived the entire resolver + allowed_pairs
gate, and can only replicate an already-validated predicate — it can never name
a new one.

**Algorithm.** For each emitted triple `(S, P, O)` with signature `sig`:
1. Collect `conj` siblings of the object head token `O_tok` (direct `conj`
   children, plus `conj` chains, bounded depth 4).
2. For each sibling `C` that is the head of a GLiNER entity span:
   - require `C.head is O_tok` or `C` is in `O_tok`'s conj chain;
   - require `C.pos_ == O_tok.pos_` (coarse POS agreement);
   - require `pair_allowed(P, type(S), type(C))`;
   - emit `(S, P, C)` with `confidence = 0.85`, `dep_signature = f"{sig}+conj"`,
     inheriting polarity / modality / assertion_mode / sentence from the head edge.
3. Symmetrically for `conj` siblings of the **subject** head (`"X and Y use Z"`).

**Non-distributive guards (mandatory — this is where the FPs live):**
- Suppress when the coordination is governed by a symmetric/reciprocal
  preposition — `between`, `among`, `amongst`, `across` (`"the tradeoff between
  A and B"` does not distribute).
- Suppress when the head edge's predicate is itself symmetric (`overlaps`,
  `synonym_of`, `contradicts`, `related_to`) — distribution over a symmetric
  predicate manufactures a false clique.
- Suppress when any conjunct carries `neg` or a contrastive `cc` (`but`, `yet`,
  `rather than`, `as opposed to`) — `"uses A but not B"`.
- Suppress when the conjunct list length exceeds a configured cap (default 6) —
  long lists are usually inventories, not shared-predicate enumerations, and one
  bad head edge would multiply into six.
- Every suppression increments a named counter (`suppressed_nondistributive_*`).
  Never drop silently — repo law.

**Determinism:** distribution order is the conjunct's token index. Byte-identical
reruns must be re-proven over 100 real chunks (roadmap P3 step 5 method).

**Acceptance:** on gate v2, recall rises >= 15 points, precision >= 0.80,
v1 canary precision still 1.000. Still expected to be the biggest single mover.

**Sizing correction (2026-07-30).** The "6.5x GLiREL advantage" that motivated
this rung is a **raw** count from an engine at P 0.273 — and it rests on a
single chunk (chunk_0099, n=1). Hand-judging its 4 sampled edges put the genuine
rate near 50%. So R3's honest ceiling is roughly **half** the naive reading, and
its true size is not a matter of opinion: it is
`suppressed_conjunct_crossing` per chunk (from **R-pre**) x non-distributive
survival rate x genuine rate. **Do not quote the 6.5x figure again once R-pre
has produced the real number.**

---

### R4 — Nominal predicate lane *(reuses the whole existing resolver)*
**Closes G3.** When the dep path's only content node is an eventive noun
(`_is_eventive_noun`, already implemented), resolve the predicate from the
**noun lemma** through the same T1–T4 cascade instead of suppressing the pair.

Two new config tables in `predicate_synonyms.yaml` (new sections, fail-loud
validated exactly like the existing ones):
- `nominal_predicates:` noun-lemma -> Predicate Literal value
  (`use`->`uses`, `integration`->`overlaps`, `support`->`supports`,
  `dependency`->`depends_on`, `implementation`->`implements`,
  `derivation`->`derived_from`, ...). Seed it from the R1 harvest, not from
  imagination — the harvest already ranks real corpus nouns.
- `nominal_argument_roles:` preposition -> `(subject|object)` binding per
  predicate, because `of` flips: *"the use of X"* binds X as object, *"the
  dependency of X"* binds X as subject.

Retire `_is_light_verb_construction` from a suppressor to a **router**: light
verb + eventive noun -> route to this lane; light verb + concrete noun -> the
existing resolver, unchanged. Confidence 0.9 (heuristic tier, matching `poss`).

**Acceptance:** `suppressed_light_verb` count converts to emitted-or-explicitly-
dropped with a named reason; gate v2 recall rises; precision holds.

---

### R5 — Rank-then-cap *(trivial, fold into whichever rung ships first)*
**Closes G7.** In `spacy_relation_adapter.extract_chunks`, sort edges by
`(-confidence, subject_start, object_start, predicate)` — a total order, so
determinism is preserved — **before** applying `max_related`. Raise the
deterministic-lane cap to 24 (matching the table-facts precedent
`TABLE_MAX_FACTS_PER_CHUNK=24`) and make it configurable. Log how many edges
were truncated per chunk (no silent caps — repo law).

---

### R6 — Adjacent-sentence subject carry-over *(highest FP risk — qualified by default)*
**Closes G2 partially.** No coreference model, no LLM. Strictly bounded:

Fire only when **all** hold:
- sentences are **immediately adjacent** and in the same paragraph/section_path;
- sentence N+1's `nsubj` is a `PRON` (`it`, `they`, `this`, `these`) or a
  definite NP whose head lemma equals the head lemma of an entity in sentence N;
- there is **exactly one** candidate antecedent entity in sentence N of a
  compatible type (zero or >=2 candidates -> drop, never guess);
- no intervening entity of the same type between antecedent and pronoun.

Emitted with `confidence = 0.8`. **Default `assertion_mode` for carried-over
subjects is a qualified claim, NOT a graph edge** — it reaches the ClaimRecord
path only. Promotion to `is_graph_edge` requires a separate owner GO after gate
v2 shows precision holds on the carry-over subset **scored in isolation**.

This is the decision `coreference_heuristic` was left open for. If R6 fails its
isolated precision check, **delete that module** rather than leaving it unwired
(the P3 wire-or-delete decision, finally taken).

---

### R7 — ASR re-segmentation lane *(needs its own fixture — that is why R0 demands ASR samples)*
**Closes G6.** Replace the blanket skip in `_is_low_parse_confidence` with a
routed lane:
- punctuation ratio >= threshold -> current path, unchanged;
- below threshold -> **do not skip**. Re-segment deterministically: rule-based
  sentencizer on discourse markers (`so`, `and then`, `but`, `now`, `okay`,
  `right`) plus a hard max-token window (default 40, configurable), re-parse the
  windows, then run extraction with:
  - confidence sentinel **0.8**,
  - the single-clause guard **mandatory** (no exceptions),
  - R3 coordination distribution **disabled** (segmentation noise x distribution
    = multiplied error),
  - a distinct `dep_signature` prefix (`asr:`) so ASR-derived edges are
    filterable downstream and separately auditable.

**Acceptance:** measured on the ASR subset of gate v2 **in isolation**, P >= 0.80.
Below that -> the lane ships disabled behind a flag and the video corpus keeps
entities-only. Do not average ASR into the overall gate number to hide a weak
subset.

---

### R8 — RunPod lane parity *(pods stay — the two roads must agree)*
**Closes G8.** RunPod is **not retired** (owner, 2026-07-30). Local on-device is
the default and easy road; pods remain the burst lane for large corpora under
existing ingestion doctrine. The requirement is therefore **parity, not rescue**:
the same text must yield the same relations on either road, or the corpus
contains two incompatible notions of what is true and no graph query can be
trusted across it.

Today a pod-extracted chunk gets **zero** relations while the same chunk on the
Mac gets the lane rate. That is not a speed difference — it is a **correctness
divergence**, and it silently biases any graph built from a mixed-provenance
corpus. Priority is therefore not "pods are behind"; it is "the fleet disagrees
with itself."

The deterministic stack is spaCy `en_core_web_sm` + PyYAML + pure Python — small
and CPU-only, so it fits the extraction image.

1. Extract the relation lane behind a stable import boundary so the same code
   runs in-container and in the pod image (no fork, no copy — a divergent copy
   guarantees drift).
2. Replace the `relations=[]` hardcode (`runpod_local_extraction.py:574`) with a
   real call, gated on a **new wire contract version** (`local_extraction_v2`) so
   old endpoints keep the old behaviour and nothing changes under a running batch.
3. **New image digest — never retag** (`local_extraction_v1` digest pin is repo
   law). Bake `en_core_web_sm` + `config/*.yaml` into the image; hash both into
   the provider card so a config drift between Mac and pod is detectable.
4. **Cross-runtime determinism proof:** same 100 chunks through the Mac lane and
   the pod lane -> byte-identical relations. Different spaCy or model versions
   silently produce different parses; the hash check must be an assertion, not a
   comment.
5. **1-slice canary on the new endpoint before it joins contract routes** —
   ingestion doctrine law 7, non-negotiable.

---

## 4. WHAT IS EXPLICITLY OUT OF SCOPE

- **No LLM/SLM anywhere in extraction.** Locked. LLM keeps its current role
  (cloud summaries). Not revisited by this spec.
- **No GLiREL revival.** It stays retired; it remains only as the comparison
  baseline already recorded in the gate.
- **No new predicates in the Literal** without owner ratification (R1 step 4
  produces the list; it does not act on it).
- **No ontology edits** beyond owner-ratified ones already recorded 2026-07-29.
- **No spaCy model switch** (sm -> md/trf) without the A/B and owner decision #1.

---

## 5. EXPECTED YIELD (PROJECTED — not measured; label preserved)

**Revised 2026-07-30 — halved where the estimate was anchored on GLiREL raw counts.**

| Rung | Mechanism | Projected rel/chunk delta | Precision risk |
|---|---|---|---|
| R1 | more verbs resolve | +0.15 – 0.35 | very low (type-gated) |
| R2 | 4 known FNs + class | +0.05 – 0.10 | very low (closed lexicon) |
| R3 | enumeration distribution | **+0.20 – 0.35** *(was +0.40–0.60; halved for GLiREL's ~50% genuine rate — supersede with R-pre's number)* | medium (guarded) |
| R4 | nominal predication | +0.10 – 0.20 | low–medium |
| R5 | fewer truncations | +0.05 | none |
| R6 | cross-sentence | +0.05 – 0.15 (claims first) | **high — qualified by default** |
| R7 | ASR corpus unlocked | video corpus 0 -> nonzero | medium (isolated gate) |
| R8 | pod parity | pods 0 -> local lane rate | none (same code) |

Projected total **0.55 – 1.05 rel/chunk at P >= 0.80** — down from the earlier
1.0–1.5, which had inherited GLiREL's noise as if it were signal. Speed stays
well inside budget (R3/R4 are pure-Python walks over an already-parsed Doc).

**Every number in this table is PROJECTED from code reading. R-pre replaces the
R3 row with a measurement; the gate decides the rest. If the measured total
lands at 0.7, that is reported as 0.7 — not rounded toward the plan.**

---

## 6. RE-EXTRACTION (after the ladder, not during)

Stored relations are contaminated (653/680 GLiREL-era on the probed corpus). Once
the ladder lands and gate v2 is green:
- relations-only re-extraction per corpus (entities/claims/vectors untouched —
  do not re-embed, do not re-summarize; the summary provider is the cost center);
- one corpus first, with the 20-edge hand spot-check (>= 0.80) before the next;
- honour the rebuild-freeze rule: batch **all** adopted capture-contract changes
  into one pass — if R8 changes the wire contract, the re-extraction waits for it
  rather than running twice.

---

## 7. LEDGER DEBT FOUND WHILE WRITING THIS

- Roadmap cites checklist anchor **P2.10**; `grep` shows **no P2.10 in the
  checklist**. The extraction work has been running without its item-level
  anchor, against the north-star rule. This spec needs anchor **P2.11** created,
  and P2.10 reconstructed or formally retired.
- `GHOST_B_RELATION_ENGINE` is documented as a switch but is **dead**:
  `ghost_b_local.py:732` hardcodes `"spacy"`. The roadmap's P4 rollback plan
  ("set `=glirel`, recreate") **does not work as written**. Either restore the
  env read or delete the rollback claim — a rollback that silently no-ops is
  worse than no rollback.
- Roadmap P4 (atomic flip) is marked `queued` but the flip is already live in
  code. The roadmap's status table is stale versus the tree.

---

## 8. SEQUENCING

```
R-pre (plumb counters, MEASURE the real guard cost)  <- FIRST, no GO needed
   │   sets §0's yield target and R3's true ceiling
   ▼
R0 (fixture v2, GLiREL as suggester)  ── owner GO on scope ──┐
                                                             ├─> R1 -> R2 -> R3 -> R4 (+R5)
                                                             │     each: one commit, gate v2 + v1 canary
                                                             ├─> R7  (local is the default road ->
                                                             │        transcripts cannot stay dead)
                                                             ├─> R6  (claims-only; graph promotion = own GO)
                                                             └─> R8  (parity; new digest + 1-slice canary)
                                                                      │
                                                                      └─> §6 re-extraction (owner GO)
```

R-pre runs first and needs no decision — it only adds instrumentation and reads
it. R1–R5 are the core ladder once R0 exists. R7 is promoted above R6 because
local on-device is now the default road and the transcript corpus is on it.

---

## 9. REPORTING CONTRACT (owner, 2026-07-30) — TWO VERDICTS, NOTHING ELSE

The final report opens with **exactly one** of these. No preamble, no summary
essay above it, no hedging between them.

**VERDICT A — FAILED**
> Only when the implementation does not work **and there is no reasonable path
> to making it work.** State the blocking reason in <= 5 lines. "Hard" or
> "needs more iterations" is not this verdict — that is a percentage.

**VERDICT B — WORKED AT N%**
> `N = achieved rel/chunk ÷ target rel/chunk` (target from R-pre), capped at 100,
> rounded to whole percent, **and only valid if precision held >= 0.80 on gate v2
> and 1.000 on the v1 canary.**
>
> **Precision broken is never a percentage — it is VERDICT A.** A high yield at
> 0.5 precision is not a partial win; it is the GLiREL failure re-created.

Under the verdict line, one table and nothing more:

| Rung | Landed? | P (v2) | R (v2) | rel/chunk delta | Receipt |
|---|---|---|---|---|---|

Then, at most: rungs that hit the STOP list and what decision each awaits.

**Do not narrate the journey. Do not explain what was hard. The verdict, the
table, the open decisions.**
