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

## Production Repair Validation - 2026-07-09

The active `polymath_v2` contract is no longer RTX-only. The live corpus uses
the cloud/provider execution path with seven independent extraction lanes:

- Managed RTX vLLM: `openai/polymath-extract`, configured `59`, observed
  adaptive limit `58`, native `json_schema`.
- SiliconFlow: three `openai/tencent/Hy3` lanes at `8` each.
- LongCat: three `openai/LongCat-2.0` lanes with configured operator ceilings
  of `45`. Runtime canary control starts each lane at `2` and increases only
  from recent accepted-attempt and rate-limit telemetry.

The bounded repair recovered all 57 genuinely extractable failed chunks and
reclassified 7 bibliography chunks as terminal structural skips. The corpus
ended with `0` Ghost B error rows. No API keys were written to artifacts or
logs.

### Executor And Contract Corrections

- RTX output is bounded against its 8,192-token serving context without
  treating context overflow as evidence that JSON Schema is unsupported.
- Provider prose, Markdown fences, and XML JSON envelopes are compiled by a
  deterministic balanced-object parser before Pydantic and semantic gates.
- Successful and skipped artifacts are stamped with the live corpus contract,
  not a document's retired provider snapshot.
- Retry rows are reconciled against successful artifacts before claims; this
  closed 35 false retries without provider calls.
- Non-extractable structural chunks persist a terminal skipped artifact so a
  later planner cannot recreate them as failures.
- Bounded repair document groups now execute concurrently while existing
  global and per-provider request semaphores remain authoritative.

### Live Throughput

| Repair slice | Result | Elapsed |
|---|---:|---:|
| Serial document executor | 25/25 recovered | 265.33s |
| Bounded concurrent executor | 23/25 recovered first pass | 32.82s |
| Final routed retry | 2/2 recovered | 10.99s |

The like-for-like 25-chunk slice improved by 8.1x after removing document-level
serialization. The final concurrent slice used 18 document slots; provider and
global semaphores still controlled actual network concurrency.

### Latest 50 Accepted Artifacts

| Provider | Chunks | Entities | Relations | Facts | `related_to` | Missing relation evidence |
|---|---:|---:|---:|---:|---:|---:|
| RTX vLLM | 9 | 30 | 23 | 12 | 1 | 0 |
| SiliconFlow Hy3 | 24 | 99 | 28 | 24 | 5 | 0 |
| LongCat 2.0 | 17 | 84 | 57 | 36 | 15 | 0 |

LongCat completed 17/17 sampled chunks without a 429. Its output was the
richest, but its `related_to` ratio remains higher than RTX and should continue
to be measured before lifting the canary cap. Compiler acceptance does not
replace semantic evaluation; the evidence, ontology, endpoint, and promotion
gates remain mandatory for every provider.

At repair completion, the 30-minute health window contained 20 accepted
LongCat attempts and zero rate limits, so the adaptive policy advanced all
three LongCat lanes from effective concurrency `2` to `4`. The configured `45`
per-key ceiling remains available but intentionally ungranted until the larger
canary thresholds are met.
