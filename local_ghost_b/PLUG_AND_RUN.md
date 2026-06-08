# PLUG_AND_RUN — fine-tuned GLiREL is the default local classifier

**Status (2026-06-07)**: fine-tuned `glirel_ghost_b_v1` is **shipped, validated,
and the production default** in [pipeline_config.py](pipeline_config.py)
(`DEFAULT_CLASSIFIER = "glirel"`). The cascade is still reachable for rollback.

---

## ⚠️ Threshold is load-bearing — read this first

The fine-tune is calibrated at **`threshold = 0.40`**, NOT 0.50.
That's the best-F1 threshold on the held-out RTX test (see `EVAL_REPORT.txt`).
Running at 0.5 silently drops ~25% of the typed predictions.

```bash
# These are equivalent — both work:
export LOCAL_GHOST_B_GLIREL_THRESHOLD=0.40
# or pass at call-time inside run_on_mac.py — auto-defaulted from pipeline_config
```

If you see typed % regress against the numbers below, **check the threshold first**.

---

## Validation deltas vs the cascade & base model

Same 21 Vector DB chunks, same 8 flame chunks, same harness. Headline numbers
that map directly to "is the fine-tune doing what we trained it for":

### Trivial-case demo (Flame is built on Flutter; Alice Chen at Meta AI)

| edge | base @0.5 | fine-tune @0.4 |
|---|---:|---:|
| `Alice Chen --works_for--> Meta AI` (correct) | 0.61 | **0.96** |
| `FineLlama --created_by--> Alice Chen` (correct direction) | not top | **0.81** |
| `Flame --created_by--> Alice Chen` (base **hallucination**) | 0.92 | **SUPPRESSED below 0.4** |

### Vector DB markdown (21 chunks, cue-rich technical content)

| classifier | rels | typed | typed % | # distinct typed preds |
|---|---:|---:|---:|---:|
| cascade | 12 | 4 | 33% | 2 |
| fine-tune **whole-chunk** (bugged) | 84 | 35 | 42% | 6 |
| **fine-tune sentence-windowed** | **54** | **45** | **83%** | **12** |

### Flame tutorial markdown (8 chunks, prose only, no cue verbs)

| classifier | rels | typed | typed % | example |
|---|---:|---:|---:|---|
| cascade | 8 | 0 | 0% | (cascade can't type without cue regex) |
| **fine-tune sentence-windowed** | **17** | **16** | **94%** | `flame --uses--> flutter` cf=0.81 (no "uses" verb in text) |

### Latency (Apple Silicon MPS, GLiREL-large 1.87 GB)

| op | base | fine-tune |
|---|---:|---:|
| cold first-call | 471ms | 470ms |
| warm first-call | 386ms | 254ms |
| sustained (50 chunks) | 4.84 ch/s | 2.12 ch/s (sentence-windowed; does more passes per chunk) |
| 230-chunk file projection | 47s | ~110s |

---

## How to run

```bash
cd /Users/king/polymath_v3.3/local_ghost_b

# Production default (glirel @ 0.4) is auto-picked from pipeline_config:
.venv/bin/python run_on_mac.py --chunks my_chunks.jsonl --out my_rels.jsonl

# Or explicit:
LOCAL_GHOST_B_CLASSIFIER=glirel \
LOCAL_GHOST_B_GLIREL_THRESHOLD=0.40 \
  .venv/bin/python run_on_mac.py --chunks my_chunks.jsonl --out my_rels.jsonl
```

Expected log lines:

```
[classifier] glirel_extract_chunk (env LOCAL_GHOST_B_CLASSIFIER=glirel)
[glirel] loading checkpoint: /Users/king/polymath_v3.3/models/glirel_ghost_b_v1/best
[glirel] 30 labels from .../models/glirel_ghost_b_v1/best/labels.json
[glirel] threshold=0.4
[done] chunks=N relations=M typed=T (P%) rejects=0
[perf] X.XX chunks/sec
```

If you see `WARNING: falling back to zero-shot` — the weights aren't at the
expected path. Loader checks (in order):
`models/glirel_ghost_b_v1/best/` → `models/glirel_ghost_b_v1/` → `local_ghost_b/heads/glirel_ghost_b_v1/`.

---

## Rollback (zero-risk, single env var)

```bash
LOCAL_GHOST_B_CLASSIFIER=existing
```

Cascade weights, `relation_exists_v1` gate, and the cascade dispatch are all
preserved — flipping the var routes the same chunks back through the original
pipeline. Useful while diagnosing or when you specifically need `preceded_by`
patterns (cascade has a hard-coded regex for it, fine-tune is weaker there).

---

## Known flags (none blocking, all mitigated)

### 1. `preceded_by`/`depends_on` undertrained — root cause is data, not architecture
Per `EVAL_REPORT.txt`: `preceded_by` F1=0.25 (support 161), `depends_on` F1=0.35 (support 391).
Cause: v1 training excluded ~60K in-sentence `literal_object` relations that
disproportionately feed these predicates.
- **Short-term**: cascade still fires `preceded_by` deterministically when the regex
  matches. Could orchestrate a union (`extract_chunk` primary + cascade backfill for
  silent + cue-matched edges) but binary-default is fine for v1.
- **Real fix**: v2 retrain with literal-recovery (~2.5 hr on RTX, no Mac change).

### 2. >128-token truncation — FIXED by adopting sentence-windowed glirel_infer
The earlier whole-chunk implementation hit the model's 128-token train-time cap.
The canonical `glirel_infer.py` (now in place) splits chunks into sentences first
via `_SENT_SPLIT` and only feeds units ≤160 tokens to GLiREL. Validated: typed share
on Vector DB went 42% → 83% after the fix; on Flame 47% → 94%.
- If a single sentence still exceeds 128 tokens, the truncation warning fires for
  that one sentence — to handle outliers, set `clf.model.config.max_len = 256`
  after load (DeBERTa supports up to 512; relation scoring is local).

### 3. URL/file-path entities slip through (`pub.dev`, sometimes `github.com`)
Type-plausibility gate allows `Software → located_in → Location`, so a URL host
mistagged as Location gets a typed edge. **Fix upstream in the entity step** —
`tools/chunk_with_gliner.py` already strips most URL fragments; extend
`_URL_HOST` to catch bare hosts like `pub.dev`. Not a model issue.

---

## What you can confirm vs the bundle docs

- `EVAL_REPORT.txt` (in the bundle root) — per-predicate P/R/F1 from RTX
- `history.json` (in the bundle root) — training curve, best_threshold
- `glirel_ghost_b_v1/best/labels.json` — the 30 labels the model embeds (incl. `related_to`, no `no_relation`)
- `glirel_ghost_b_v1/best/glirel_config.json` — architecture config (`fixed_relation_types=True`, `max_len=128`, etc.)
- `reference/glirel_infer.py` — the canonical inference (now adopted as `local_ghost_b/glirel_infer.py`)
- `reference/safety_rules.py` — diff-only; the harness uses its own `safety_rules.py`

---

## When v2 lands

Same drop pattern — push the new bundle, extract to
`models/glirel_ghost_b_v2/best/`, update one constant:

```python
# pipeline_config.py
GLIREL_BUNDLE = "glirel_ghost_b_v2"
```

Or pass `--glirel-bundle glirel_ghost_b_v2` on the CLI. v1 stays archived; A/B
becomes flag-flippable.
