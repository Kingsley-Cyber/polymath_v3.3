# Prior experiments — what was tried and final findings

**Purpose**: prevent re-doing experiments. These are settled. Don't reopen unless you have new evidence.

## GLiREL fine-tuned vs base — settled

| variant | typed % on Vector DB (21 chunks) | typed % on Flame (8 chunks) |
|---|---|---|
| BERT cascade (whole-chunk regex compiler) | 33 % | 0 % |
| GLiREL base model (`jackboyla/glirel-large-v0`) zero-shot | 11 % | 47 % |
| GLiREL fine-tuned v1 (sentence-windowed) | **83 %** | **94 %** |
| GLiREL v2 (literal-recovery retrain) | regressed; v1 stays | — |

**Settled**: v1 (`models/glirel_ghost_b_v1/best/`) is the production GLiREL. Sentence-windowed inference is required (the `glirel_infer.py` we shipped does this). Do NOT swap models.

## SLM model bake-off — settled, all dropped from the design

Three SLM candidates were tested for Pass-2 enrichment via the GGUF + grammar sidecar:

| model | facts test | object_kind test | latency warm |
|---|---|---|---|
| LFM2-1.2B-Extract MLX-4bit | off-vocab fact_types (production-ready / index / etc.); valid JSON but unusable | 80% accuracy on simple cases | ~2.4 s |
| LFM2-1.2B-Extract dwq6-mlx | identical to 4bit output (model identity-bound, not quant) | identical | ~2.4 s |
| Qwen 2.5-1.5B-Instruct GGUF Q4_K_M | 3/3 valid fact_types (via grammar); 1/3 perfect, 2/3 partial; "production-ready" → status correctly | game_engine / vector_database correct on first try | ~2.4 s |
| LFM2.5-1.2B-Instruct GGUF Q4_K_M | similar quality to Qwen, MORE hallucinations (e.g., "8 GB" RAM when text says "4 GB"; "2021" release year invented) | game_engine correct | ~3.4 s |

**Settled**:
- All SLM models tested have similar 1.5B-class hallucination rates
- Grammar constraint fixes JSON validity + fact_type vocabulary; doesn't fix content faithfulness
- Hallucinated `value` strings would corrupt Neo4j data — adapter Pydantic can't catch them
- User concluded: SLM is unfit for deterministic ingestion → fully local deterministic stack (no SLM) is the path forward

**Do NOT** re-test SLM models. The decision is locked.

## SLM enforcement attempts — settled

Attempted in one session, then reverted:
- `maxItems: 4` in JSON schema → cut off correct deprecation fact (#6 in LFM2's 6-fact output)
- `max_tokens: 300` (down from 400) → caused JSON truncation
- Substring filter on `value` / `condition` → caught some hallucinations but rejected technically-correct paraphrases
- Tighter prompt with "value MUST be verbatim substring" → model ignored the rule

**Settled**: even with enforcement, SLM is unfit. User explicitly reverted these changes ("commit to the point where qwen was the main model"). Don't re-introduce them.

## Example regurgitation on 1.5B models — settled

The redesigned v3 prompt with a worked example (spaCy/GIL facts, Qdrant/vector_database facet) caused BOTH Qwen 2.5 1.5B Instruct AND LFM2-1.2B-Extract to emit byte-identical regurgitated output (subjects = spaCy/GIL even when the chunk was about Qdrant).

**Settled**: small models at greedy decode treat in-prompt examples as the answer template AND content. Either remove examples or use placeholders. Either way: the SLM path is no longer in the design, so this isn't relevant for Phase A. But: **for future prompt work elsewhere, use placeholders, not concrete-name examples.**

## Chunker tightening — settled

Tools chunker (`tools/chunk_with_gliner.py`) was extended to strip:
- Fenced code blocks (` ``` ... ``` `)
- Markdown links (keep text, drop URL)
- Bare URLs
- Citation patterns (`et al.`, `&amp;`, `(2024)`)
- URL host blocklist (`*.com`, `*.io`, etc.) for entities
- Version strings, file extensions, all-punct
- Generic-noun Person mistags (`researchers`, `authors`, etc.)

**Settled**: keep these strips. They cut junk entity rate dramatically.

## Tokenizer alignment — settled

GLiNER chunker uses `text.split()`. GLiREL inference uses the regex tokenizer `\w+(?:[-_]\w+)*|\S` (from the canonical `glirel_infer.py`). The two tokenizers MUST match between training and inference.

**Settled**: canonical `glirel_infer.py` uses the regex tokenizer (`_TOK`); this is the production one. Do NOT change.

## Things measured (for reference, don't re-measure)

| measurement | value | source |
|---|---|---|
| GLiNER on Apple Silicon (M1 Max) | ~50-100 ms/chunk | batch_pipeline runs |
| GLiREL extract_chunk warm | 206 ms/chunk | Phase 4 baseline |
| GLiREL extract_chunk cold | 471 ms | first-call after model load |
| Qwen GGUF 1.5B Q4_K_M cold first facts call | ~14 s (Metal init dominates) | sidecar smoke |
| Qwen GGUF 1.5B Q4_K_M warm facts call | ~2.4 s for 3 facts | sidecar smoke |
| Embedder Qwen3-0.6B mxfp8 on Apple Silicon | not directly measured this session; estimate ~50-200 chunks/sec batched | industry typical |
| Avg tokens/chunk on real docs (Qwen tokenizer) | 188 tokens | measured on 95 real chunks from Vector_DB + Prompting paper |
| `/Volumes/Flash Drive/merged/` total size | 338 MB across 523 .md files | measured |
| Estimated total chunks for the directory | ~450,000 | computed from total bytes / chunk size |
