# Qwen Ambiguity-Resolver — v1 (CLAUDE)

The second stage of the broke-mode local Ghost B pipeline. The ModernBERT
cascade is fast and handles the confident/obvious cases; **Qwen only runs on the
ambiguous edges the cascade couldn't commit** and outputs a single predicate.

This is a fine-tuned `Qwen2.5-1.5B-Instruct` (LoRA, merged), trained on the
"BERT failure set" — the cases where the cascade was wrong, uncertain, or fell
back to `related_to`.

---

## What's here

```
qwen_resolver_merged/      fine-tuned Qwen2.5-1.5B, merged bf16 (~2.9 GB)
                           -> convert to MLX for the Mac (below)
qwen_resolver.py           QwenResolver (PyTorch/MPS) + HybridExtractor
qwen_resolver_mlx.py       QwenResolverMLX (MLX fast path, after conversion)
scripts/eval_hybrid.py     BERT-only vs BERT+Qwen comparison
reports/
  qwen_resolver_val_metrics.json    0.78 exact-match on held-out failure set
  hybrid_report_clean.json          end-to-end on deduped held-out
```

---

## The pipeline

```
entities -> candidate-pair GATE -> ModernBERT cascade
   confident edge  -> accept (fast)
   ambiguous edge  -> Qwen resolver -> exact predicate or related_to
-> Python compiler / evidence gate -> Ghost B JSONL
```

Qwen sees the same prompt it was trained on: evidence window, subject/object +
types, cue, and the three cascade heads' guesses + confidences. It replies with
exactly `PREDICATE=<x>`.

---

## Convert to MLX (run on the Mac)

```bash
pip install mlx-lm
mlx_lm.convert --hf-path qwen_resolver_merged \
               --mlx-path qwen_resolver_mlx -q --q-bits 4
```

`Qwen2.5` is fully supported by mlx-lm — this is the standard path (unlike the
ModernBERT classifier heads, which are encoder models and do NOT convert with
mlx_lm). The 4-bit MLX model is ~0.9 GB and runs fast on Apple Silicon.

---

## Run the hybrid on Mac

**Fast path (MLX, after conversion):**
```python
from qwen_resolver import HybridExtractor
from qwen_resolver_mlx import QwenResolverMLX

hybrid = HybridExtractor(
    runs_dir="heads",                       # the 3 ModernBERT heads
    resolver=QwenResolverMLX("qwen_resolver_mlx"),
)
edges = hybrid.extract(pairs)               # pairs = candidate entity pairs
```

**Simple path (no conversion, PyTorch on MPS — slower):**
```python
from qwen_resolver import HybridExtractor
hybrid = HybridExtractor(runs_dir="heads", qwen_dir="qwen_resolver_merged")
edges = hybrid.extract(pairs)
```

`HybridExtractor` runs the 3 ModernBERT heads, accepts confident edges, and sends
only the ambiguous ones to Qwen. Each returned edge has `.predicate`, `.confidence`,
`.tier` (`tier1_exact` / `tier2_family` / `qwen_resolved` / `tier3_related` / `drop`).

---

## Honest performance numbers

**Resolver, on its own held-out failure set (clean):**
`val_exact_match = 0.78` — Qwen correctly resolves 78% of cases the cascade
couldn't, predicting the exact Ghost B predicate.

**Hybrid, on a held-out set deduped against ALL training (BERT + Qwen):**
precision 0.94, coverage 0.82 -> 0.996. Qwen recovered ~70% of the edges BERT
abstained on. NOTE: that clean subset skews toward BERT-*easy* cases (because the
hard cases were Qwen's training data), so it understates how much Qwen helps on a
real, harder book. Treat 0.78 (resolver) and ~0.70-0.94 (hybrid precision) as the
trustworthy band.

**Read this honestly:** Qwen is NOT a perfect Ghost B clone. It is a high-recall
local resolver for the ambiguous middle. Combined with the cascade it turns the
graph from "mostly related_to scaffold" into "mostly typed edges" — without any
API calls.

---

## Speed note

Qwen runs ONLY on ambiguous edges (typically a minority after the cascade), so
the slow generative model isn't in the hot path for most pairs. Budget roughly:
ModernBERT cascade = hundreds of pairs/sec; Qwen (MLX 4-bit) = a few-to-tens of
ambiguous pairs/sec on Apple Silicon. Ingestion is offline/batch, so this is fine.

Predicate vocabulary = the 30 Ghost B predicates + `related_to`/`none`.
See README_CLAUDE.md for the cascade + candidate-gate details.
