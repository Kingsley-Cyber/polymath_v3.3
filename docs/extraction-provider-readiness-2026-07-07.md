# Extraction Provider Readiness - 2026-07-07

## Scope

Chunk-level live probe for the Polymath Ghost B extraction contract using real
`polymath_v2` corpus chunks.

- Corpus: `polymath_v2`
- Corpus id: `999b5934-272e-4f20-a538-b5d422249a05`
- Private RTX lane: 24 chunks
- Cloud lanes: same 8 core chunks
- Contract gates: JSON parse, Pydantic `ExtractionResponse`, allowed predicate,
  required `evidence_phrase`, endpoint sanity, semantic direction check
- Raw secrets: not written to this report

## Registered Configuration

`polymath_v2` active extraction lane remains private RTX only:

- Engine: `local`
- Pool source: `extraction_models`
- Provider: `vllm-rtx`
- Model: `openai/polymath-extract`
- Base URL: `http://192.168.1.83:8000/v1`
- Controller: `http://192.168.1.83:8085`
- Max concurrent: `60`
- Provider-card mode: native `json_schema`
- Concurrency policy: adaptive 85% VRAM

`polymath_v2` summary lane was updated to SiliconFlow Hy3:

- Provider: `siliconflow`
- Model: `openai/tencent/Hy3`
- Base URL: `https://api.siliconflow.com/v1`
- Three encrypted keys
- Max concurrent: `8` per chip
- `chunk_summarization`: `true`

LongCat and SiliconFlow Hy3 extraction cards were registered in the user-visible
model pool for testing/reuse, but were not attached to the paused production
batch as active extraction lanes.

## Provider Contract Notes

Private RTX/vLLM is the strongest contract lane because it uses provider-native
`json_schema` through the OpenAI-compatible vLLM endpoint.

LongCat and SiliconFlow Hy3 do not currently prove provider-native schema
enforcement in this repo. They are configured as compiler-gated JSON lanes:
JSON-object prompt, deterministic compiler/repair, Pydantic validation,
required evidence, semantic verifier, and retry/stage failure policy. That can
be production-safe only because invalid outputs are rejected before promotion.

## Results

| Provider | Grade | Chunks | Time | Chunks/min | Success | Relations | Facts | `related_to` | Validation rejects |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Private RTX vLLM | CANARY | 24 | 154.93s | 9.29 | 100% | 38 | 24 | 21.1% | 20 |
| LongCat 2.0 | READY | 8 | 15.93s | 30.14 | 100% | 34 | 23 | 26.5% | 0 |
| SiliconFlow Hy3 | READY | 8 | 7.39s | 64.94 | 100% | 16 | 7 | 18.8% | 1 |

## Interpretation

LongCat produced the richest accepted output on the 8-chunk control set:
4.25 relations/chunk and 2.88 facts/chunk, with zero validation rejections.
Its `related_to` rate is acceptable but worth watching at scale.

SiliconFlow Hy3 was the fastest cloud lane in this probe and stayed mostly
clean: 1 validation rejection, no failed chunks, and the lowest `related_to`
ratio among the tested lanes. It is a good cost/speed candidate, especially
for summary and lower-cost enrichment.

Private RTX vLLM was schema-valid at the chunk level, but not production-clean
yet under the current strict prompt. It succeeded on all chunks, but 7 of 24
chunks had validation rejections and one attempt logged a JSON parse failure
before recovery. Throughput was also weaker than expected for the 96GB server.
Treat it as canary until prompt/output budget and vLLM decoding are tuned.

## Production Recommendation

Use this order for the next controlled ingestion test:

1. Primary summary lane: SiliconFlow Hy3.
2. Primary cloud extraction canary: LongCat 2.0 or SiliconFlow Hy3.
3. Private RTX lane: keep enabled as a local/private extraction provider, but
   run a canary/backfill sweep before making it the only production extractor.

Do not treat LongCat or Hy3 as "100% provider-native schema enforced" yet. They
are production-usable only with Polymath's compiler + validation gate enabled.
For true provider-side enforcement, use lanes that pass native `json_schema`
requests, currently private RTX/vLLM in this setup.

## Follow-Up Gates

- Run a 100-chunk RTX canary after reducing output budget or prompt size.
- Add provider readiness status to the Corpus Manager chip: native schema vs
  compiler-gated JSON.
- Record validation rejections per provider in the library graph chip.
- Keep paused production batch paused until the active extraction mix is chosen.
