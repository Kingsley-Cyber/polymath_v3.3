# glirel_ghost_b_v1 — fine-tuned GLiREL classifier slot

This directory is the plug-and-play target for the fine-tuned GLiREL classifier.

## Status

**EMPTY** — fill this with a trained checkpoint to enable the classifier.

When this directory contains a valid GLiREL checkpoint, `run_on_mac.py
--classifier glirel` uses it. When empty, the classifier falls back to a
zero-shot GLiREL model (warns loudly) so the wiring stays testable.

## Expected files (drop in from RTX training run)

```
glirel_ghost_b_v1/
  model.safetensors            # fine-tuned weights
  config.json                  # model config
  tokenizer.json               # tokenizer (must match training)
  tokenizer_config.json
  special_tokens_map.json
  spm.model                    # if sentencepiece-based
  label_descriptions.json      # SHIPS WITH BUNDLE; do not overwrite from RTX
```

`label_descriptions.json` is **independent of training** — it defines the
ontology GLiREL classifies against. The file is checked into source so the
ontology is reviewable. Training should NOT regenerate it.

## How the loader decides which model to use

1. If `model.safetensors` exists in this dir → load fine-tuned checkpoint.
2. Else → load `knowledgator/glirel-large-v0` from HuggingFace (zero-shot
   fallback) and log a `[glirel] WARNING: using zero-shot fallback` line.

Either way, the same `label_descriptions.json` defines the candidate labels.

## Producing the checkpoint

Training happens on RTX (CUDA required for fine-tuning at sensible speed).
See `scripts/train_glirel_ghost_b.py` (not yet written). After training,
bundle the output dir and ship to this slot via the existing flash-bundle
mechanism (`nc`/`python` listener → rsync into this dir).

## Version pinning

Bump the dir name (`glirel_ghost_b_v2/`, etc.) when retraining; do not
overwrite v1 in place. `run_on_mac.py` reads from
`heads/glirel_ghost_b_v1/` by name — point it at a new version with
`--glirel-bundle glirel_ghost_b_v2`.
