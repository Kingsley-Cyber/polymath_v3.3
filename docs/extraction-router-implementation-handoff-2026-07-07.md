# Extraction Router Implementation Handoff

Date: 2026-07-07

Audience: a stronger implementation model that needs to turn the extraction experiments into a production-ready Polymath model router.

Companion script:

```text
scripts/run_extraction_model_router.py
```

Related summary report:

```text
docs/extraction-model-routing-report-2026-07-07.md
```

No API keys are included in this document. Use environment variables only.

## Goal

Build a dynamic, robust extraction router that can call the four primary tested model lanes, normalize their responses, enforce the Polymath schema, repair deterministic model-specific mistakes, score the result, and persist audit artifacts.

The router must never trust raw model output. A model response is acceptable only after:

```text
provider call
  -> raw response capture
  -> JSON extraction or salvage
  -> deterministic compiler/normalizer
  -> Pydantic ExtractionResponse validation
  -> semantic relation repair
  -> endpoint completion
  -> acceptance scoring
  -> audit persistence
```

## Primary Four Model Lanes

These are the four lanes to implement first.

| Lane | Provider | Model ID | Best Role | Ready Today |
|---|---|---|---|---|
| `longcat_2_direct` | LongCat | `LongCat-2.0` | High-quality direct extraction | Yes, with thinking disabled |
| `hy3_preview_direct` | SiliconFlow | `tencent/Hy3-preview` | Fast direct extraction | Yes, prompt-only |
| `hy3_direct` | SiliconFlow | `tencent/Hy3` | Fast backup direct extraction | Yes, prompt-only |
| `mistral_nemo_schema` | OpenRouter | `mistralai/mistral-nemo` | Strict structured-output fallback | Yes, only with OpenRouter json_schema |

Auxiliary, not one of the four primary lanes:

```text
inclusionai/ling-2.6-flash -> compact IR -> Python compiler -> Polymath
```

Ling is useful for short table/concept chunks, but it should not be a primary direct Polymath writer.

## Polymath Contract

The single source of truth is:

```text
backend/services/ghost_b_schemas.py::ExtractionResponse
```

Top-level object:

```json
{
  "entities": [],
  "relations": [],
  "facts": []
}
```

Entity fields:

```json
{
  "canonical_name": "string",
  "surface_form": "string",
  "entity_type": "Person | Organization | Location | Event | Concept | Method | Product | Software | Document | Standard | Rule | Law | Artifact | TimeReference | other",
  "confidence": 0.91,
  "query_aliases": [],
  "definitional_phrase": "",
  "object_kind": ""
}
```

Relation fields:

```json
{
  "subject": "canonical source entity",
  "predicate": "one Polymath predicate",
  "object": "canonical target entity or literal",
  "object_kind": "entity | literal",
  "confidence": 0.88,
  "evidence_phrase": "short exact phrase",
  "relation_cue": "raw source verb"
}
```

Fact fields:

```json
{
  "subject": "canonical entity",
  "fact_type": "property | status | timestamp | quantity | threshold | category | tag | rule_condition | rule_action",
  "property_name": "snake_case",
  "value": "string",
  "unit": "",
  "condition": "",
  "confidence": 0.86,
  "evidence_phrase": "short exact phrase"
}
```

Pydantic-valid JSON is necessary but not sufficient. Pydantic will not catch semantic direction errors such as:

```text
Mira Chen -created_by-> HarborLight Analytics
Rafael Ortiz -created_by-> Aegis-9
```

The router must enforce relation direction after schema validation.

## Test Corpus

Two local documents were used during the experiments.

Narrative stress document:

```text
C:\Users\Sammb\Downloads\On March 14, 2026, Dr. Mira Chen ar.txt
```

This document stresses people, companies, products, locations, tests, payments, hardware failures, dates, maintenance requirements, thresholds, and outcome metrics.

Markdown/table stress document:

```text
E:\books\AI_FILM_SCHOOL\film_school_reading_map_for_ai_video_direction.md
```

This document stresses markdown sections, film-direction concepts, compact tables, row-wise facts, and table chunking behavior.

## 1. LongCat Direct Lane

Profile name:

```text
longcat_2_direct
```

Provider:

```text
LongCat
```

Base URL:

```text
https://api.longcat.chat/openai/v1
```

Model:

```text
LongCat-2.0
```

API key env var:

```text
LONGCAT_API_KEY
```

Request mode tested:

```text
OpenAI-compatible chat completions
Refined XML-delimited prompt
<json_payload>{...}</json_payload> output envelope
thinking disabled
temperature=0
top_p=0.1
```

Critical request field:

```json
{
  "thinking": {
    "type": "disabled"
  }
}
```

Why this matters:

```text
Without disabling thinking, the first LongCat run spent the token budget in reasoning and returned empty message.content with finish_reason=length.
```

Best artifact set from testing:

```text
C:\Users\Sammb\AppData\Local\Temp\polymath_longcat_probe\longcat2_mira_refined_hybrid_thinking_disabled_summary.json
C:\Users\Sammb\AppData\Local\Temp\polymath_longcat_probe\longcat2_mira_refined_hybrid_thinking_disabled_raw.txt
C:\Users\Sammb\AppData\Local\Temp\polymath_longcat_probe\longcat2_mira_refined_hybrid_thinking_disabled_compiled.json
```

Observed result:

```text
http_status: 200
elapsed_s: 40.424
finish_reason: stop
prompt_tokens: 1573
completion_tokens: 2272
total_tokens: 3845
raw JSON parse: true
candidate Pydantic: true
compiled Pydantic: true
Polymath parse: true
candidate counts: 16 entities, 13 relations, 8 facts
endpoint_completion_count: 0
```

Valuable responses observed:

```text
blue lantern.first_field_test_date = 2026-03-14
aegis-9.tamper_detection_time = 12
northstar shipping.additional_payment = 180000
blue lantern.theft_reduction = 40
aegis-9.accuracy_threshold = 95
```

Strengths:

```text
Best raw structural compliance among non-OpenRouter-schema runs.
Good direct Polymath JSON.
Good fact recall on dates, measurements, thresholds, and payment details.
No endpoint completion was needed in the best compiled run.
```

Weaknesses:

```text
Needs thinking disabled.
Can make semantic direction mistakes on created_by.
Can emit extra metadata such as schema_version.
Can exceed requested relation caps.
```

Required mitigations:

```text
Always disable thinking.
Strip metadata fields outside the contract.
Run created_by direction repair.
Run works_for and located_in direction checks.
Keep relation_cue as the raw source verb.
Score down semantic repair events.
```

Production role:

```text
Primary direct extractor when the LongCat account has balance.
```

## 2. SiliconFlow Hy3 Preview Direct Lane

Profile name:

```text
hy3_preview_direct
```

Provider:

```text
SiliconFlow
```

Base URL:

```text
https://api.siliconflow.com/v1
```

Model:

```text
tencent/Hy3-preview
```

API key env var:

```text
SILICONFLOW_API_KEY
```

Request mode tested:

```text
OpenAI-compatible chat completions
Prompt-only refined XML section delimiters
<json_payload>{...}</json_payload> output envelope
temperature=0
top_p=0.1
```

Do not send:

```json
{
  "response_format": {
    "type": "json_object"
  }
}
```

Observed JSON mode error:

```json
{
  "code": 20024,
  "message": "Json mode is not supported for this model."
}
```

Best artifact set from testing:

```text
C:\Users\Sammb\AppData\Local\Temp\polymath_siliconflow_probe\hy3_preview_mira_refined_hybrid_summary.json
C:\Users\Sammb\AppData\Local\Temp\polymath_siliconflow_probe\hy3_preview_mira_refined_hybrid_raw.txt
C:\Users\Sammb\AppData\Local\Temp\polymath_siliconflow_probe\hy3_preview_mira_refined_hybrid_compiled.json
```

Observed result:

```text
http_status: 200
elapsed_s: 10.597
finish_reason: stop
prompt_tokens: 1507
completion_tokens: 2280
total_tokens: 3787
raw JSON parse: true
candidate counts: 16 entities, 12 relations, 8 facts
candidate Pydantic: false
compiled Pydantic: true
Polymath parse: true
Polymath counts: 19 entities, 12 relations, 4 facts
endpoint_completion_count: 3
```

Why raw Pydantic failed:

```text
relation.object_kind values included "other" and "Artifact".
Polymath relation.object_kind only allows "entity" or "literal".
```

Valuable responses observed:

```text
harborlight analytics -created_by-> mira chen
aegis-9 -created_by-> rafael ortiz
blue lantern -affiliated_with-> northstar shipping
elise ward -works_for-> port alder authority
aegis-9 -detects-> unauthorized container opening
vanton components -produces-> rubber seal
aegis-9.accuracy_reduction_temperature = 95
blue lantern.theft_reduction_percentage = 40
shieldcap.performance_in_heavy_rain = zero false alerts
```

Strengths:

```text
Fast compared with LongCat and Mistral Nemo in the tests.
Good narrative recall.
Good semantic direction in the Mira document.
Usable despite no provider JSON mode.
```

Weaknesses:

```text
No SiliconFlow JSON mode for this model.
Confuses relation.object_kind with entity/object ontology.
Needs endpoint completion.
Facts may be reduced by downstream parsing if subjects are missing.
```

Required mitigations:

```text
Never request JSON mode for this model.
Use XML only as prompt delimiters, not ontology.
Normalize relation.object_kind to "entity" or "literal".
Complete missing entity endpoints.
Preserve raw object_kind mistakes in audit metrics.
```

Production role:

```text
Primary fast direct extractor or load-balancing lane.
```

## 3. SiliconFlow Hy3 Direct Lane

Profile name:

```text
hy3_direct
```

Provider:

```text
SiliconFlow
```

Base URL:

```text
https://api.siliconflow.com/v1
```

Model:

```text
tencent/Hy3
```

API key env var:

```text
SILICONFLOW_API_KEY
```

Request mode tested:

```text
OpenAI-compatible chat completions
Prompt-only refined XML section delimiters
<json_payload>{...}</json_payload> output envelope
temperature=0
top_p=0.1
```

JSON mode:

```text
Unsupported for this model in the tests.
```

Best artifact set from testing:

```text
C:\Users\Sammb\AppData\Local\Temp\polymath_siliconflow_probe\hy3_mira_refined_hybrid_summary.json
C:\Users\Sammb\AppData\Local\Temp\polymath_siliconflow_probe\hy3_mira_refined_hybrid_raw.txt
C:\Users\Sammb\AppData\Local\Temp\polymath_siliconflow_probe\hy3_mira_refined_hybrid_compiled.json
```

Observed result:

```text
http_status: 200
elapsed_s: 13.109
finish_reason: stop
prompt_tokens: 1553
completion_tokens: 2196
total_tokens: 3749
raw JSON parse: true
candidate counts: 16 entities, 12 relations, 8 facts
candidate Pydantic: false
compiled Pydantic: true
Polymath parse: true
Polymath counts: 17 entities, 12 relations, 8 facts
endpoint_completion_count: 1
```

Why raw Pydantic failed:

```text
Two off-schema predicates:
- run_by
- opens
```

Valuable responses observed:

```text
harborlight analytics -created_by-> mira chen
aegis-9 -created_by-> rafael ortiz
rafael ortiz -works_for-> harborlight analytics
elise ward -works_for-> port alder authority
lena brooks -works_for-> harborlight analytics
harborlight analytics -produces-> aegis-9
aegis-9 -uses-> thermal imaging
northstar shipping.additional_payment = 180000
harborlight analytics.accuracy_report_frequency = monthly
aegis-9.accuracy_temperature_limit = 95
lena brooks.maintenance_checklist = weekly seal inspections, battery checks, firmware version record
```

Strengths:

```text
Comparable to Hy3-preview.
Good fact preservation after parsing.
Only a small number of predicate mistakes in the best run.
Fast enough for backup/load balancing.
```

Weaknesses:

```text
No JSON mode.
Occasional raw predicate invention.
Needs deterministic predicate remapping.
Needs endpoint completion.
```

Required mitigations:

```text
Map run_by -> affiliated_with.
Map opens -> related_to or detects depending on evidence.
Map founded_by/designed_by -> created_by with direction repair.
Map supervised_by/approved_by -> supports or related_to.
Preserve the raw verb in relation_cue.
Score down every remap.
```

Production role:

```text
Backup direct extractor when Hy3-preview is unavailable, rate-limited, or cost-balanced away.
```

## 4. OpenRouter Mistral Nemo Structured Lane

Profile name:

```text
mistral_nemo_schema
```

Provider:

```text
OpenRouter
```

Base URL:

```text
https://openrouter.ai/api/v1
```

Model:

```text
mistralai/mistral-nemo
```

API key env var:

```text
OPENROUTER_API_KEY
```

Request mode tested:

```text
OpenRouter response_format.type=json_schema
json_schema.strict=true
provider.require_parameters=true
temperature=0
top_p=0.1
```

Required request fields:

```json
{
  "response_format": {
    "type": "json_schema",
    "json_schema": {
      "name": "polymath_extraction",
      "strict": true,
      "schema": "<ExtractionResponse JSON Schema>"
    }
  },
  "provider": {
    "require_parameters": true
  }
}
```

Do not use prompt-only Nemo for production.

Prompt-only tuned Nemo returned a structurally wrong object:

```text
entities = ["Dr. Mira Chen", ...]
relations = ["founded_by", "run_by", ...]
facts = objects with predicate/object fields instead of Polymath fact fields
```

Best artifact set from testing:

```text
C:\Users\Sammb\AppData\Local\Temp\polymath_openrouter_probe\mistral_nemo_structured_schema_v2_summary.json
C:\Users\Sammb\AppData\Local\Temp\polymath_openrouter_probe\mistral_nemo_structured_schema_v2_raw.txt
C:\Users\Sammb\AppData\Local\Temp\polymath_openrouter_probe\mistral_nemo_structured_schema_v2_api.json
```

Observed result:

```text
http_status: 200
elapsed_s: 38.32
finish_reason: stop
prompt_tokens: 1261
completion_tokens: 2929
total_tokens: 4190
estimated upstream cost: 0.0006285
raw JSON parse: true
Pydantic: true
counts: 17 entities, 9 relations, 9 facts
bad_relation_count: 0
wrong_created_by_direction_count: 0
missing_entity_endpoint_count: 1
```

Valuable responses observed:

```text
HarborLight Analytics -created_by(founded by)-> Mira Chen
Elise Ward -works_for(works for)-> Port Alder Authority
Blue Lantern -affiliated_with(run by)-> HarborLight Analytics
HarborLight Analytics -affiliated_with(contract with)-> Northstar Shipping
Aegis-9 -uses(used)-> thermal imaging
Aegis-9 -detects(detects)-> unauthorized opening
Aegis-9 -stores(stored)-> heat patterns, timestamps, and container IDs
Aegis-9.detection time = twelve seconds
Aegis-9.mounting distance = two meters
Northstar Shipping.payment amount = $180,000
Aegis-9.high-temperature accuracy limit = 95 degrees Fahrenheit
Blue Lantern.theft reduction = 40 percent
```

Strengths:

```text
Strong schema compliance when OpenRouter strict structured outputs are used.
Predicate enum mistakes disappear under provider-side json_schema.
Good fact extraction.
Good semantic direction after adding direct relation instructions.
```

Weaknesses:

```text
Prompt-only mode is not safe.
Structured-output mode is more conservative on relation recall.
One relation endpoint was missing from entities in the best run.
An aggressive relation-checklist prompt increased recall but introduced edge conflation.
```

Required mitigations:

```text
Always use response_format json_schema strict.
Always set provider.require_parameters=true.
Generate schema from ExtractionResponse or keep an exact synced schema.
Run endpoint completion even after Pydantic passes.
Keep semantic direction checks enabled.
Do not use the aggressive checklist prompt as the default.
```

Production role:

```text
Schema-safe fallback lane and validation benchmark. Prefer it when structural reliability matters more than maximum relation recall.
```

## Auxiliary Ling Lane

Profile name:

```text
ling_flash_ir
```

Provider:

```text
OpenRouter
```

Model:

```text
inclusionai/ling-2.6-flash
```

Best role:

```text
Cheap compact IR/table extractor.
```

Do not use it as a primary direct Polymath writer over dense chunks.

Key stress-test result:

```text
Dense 5,964-character markdown table chunk -> finish_reason=length and incomplete JSON.
Small 856-character table chunk with XML-delimited prompt -> finish_reason=stop, parsed JSON, Pydantic passed.
JSON-only prompt on the small chunk -> finish_reason=length.
```

Successful artifact:

```text
C:\Users\Sammb\AppData\Local\Temp\polymath_openrouter_probe\ling_xml_control_concepts_confidence_fixed_summary.json
```

Valuable responses observed:

```text
ads -defines(concepts to extract)-> hook
ugc -defines(concepts to extract)-> creator_persona
fight -defines(concepts to extract)-> objective
anime -defines(concepts to extract)-> style_era
ads.prompt_use = Show problem in first second, product proof by second four.
ugc.prompt_use = Phone camera, natural hesitation, real desk clutter.
fight.prompt_use = Kick lands, opponent recoils, camera holds readable full body.
anime.prompt_use = 90s OVA cel shading with 2-frame impact flash.
```

Use only with hard chunk limits:

```text
narrative: 1500-2000 chars
markdown tables: 3-5 rows per call
concept tables: 4 rows per call
```

## Router Script Already Added

The scaffold script is:

```text
scripts/run_extraction_model_router.py
```

It currently implements:

```text
model profiles for the four primary lanes plus Ling auxiliary
provider-specific request payloads
LongCat thinking disabled hook
SiliconFlow prompt-only behavior
OpenRouter json_schema strict behavior
OpenRouter provider.require_parameters=true
direct Polymath prompt
Ling compact IR prompt
balanced JSON salvage
<json_payload> extraction
contract stripping
predicate remapping
relation.object_kind repair
confidence clamping
created_by / works_for / located_in semantic repair
endpoint completion
Pydantic ExtractionResponse validation
acceptance scoring
per-call audit artifacts
dry-run mode
```

Example dry run:

```powershell
python scripts/run_extraction_model_router.py `
  --input "C:\Users\Sammb\Downloads\On March 14, 2026, Dr. Mira Chen ar.txt" `
  --profiles longcat_2_direct hy3_preview_direct hy3_direct mistral_nemo_schema `
  --audit-dir .codex-logs\extraction-router `
  --dry-run
```

Example live run:

```powershell
$env:LONGCAT_API_KEY = "<key>"
$env:SILICONFLOW_API_KEY = "<key>"
$env:OPENROUTER_API_KEY = "<key>"

python scripts/run_extraction_model_router.py `
  --input "C:\Users\Sammb\Downloads\On March 14, 2026, Dr. Mira Chen ar.txt" `
  --profiles longcat_2_direct hy3_preview_direct hy3_direct mistral_nemo_schema `
  --audit-dir .codex-logs\extraction-router
```

The script writes:

```text
{timestamp}_{profile}_{chunk_id}_request.json
{timestamp}_{profile}_{chunk_id}_provider_response.json
{timestamp}_{profile}_{chunk_id}_raw_content.txt
{timestamp}_{profile}_{chunk_id}_candidate.json
{timestamp}_{profile}_{chunk_id}_compiled.json
{timestamp}_{profile}_{chunk_id}_metrics.json
{timestamp}_router_summary.json
```

## Acceptance Policy

The first production router should auto-accept only when:

```text
compiled Pydantic valid
finish_reason != length
acceptance_score >= 70
no unresolved entity endpoints
no hard semantic validator failure
```

Current scaffold score:

```text
start at 100
-50 if raw JSON parse fails
-20 if balanced JSON salvage is needed
-15 if candidate Pydantic fails before compiler repair
-60 if compiled Pydantic fails
-10 per predicate remap, capped
-8 per object_kind repair, capped
-8 per endpoint completion, capped
-25 per semantic repair, capped
-50 if finish_reason=length
```

The exact score can be tuned, but the final production script must persist all components of the score.

## Integration Instructions For The Strong Model

1. Read `backend/services/ghost_b.py` and `backend/services/ghost_b_schemas.py`.
2. Treat `ExtractionResponse` as the schema source of truth.
3. Use `scripts/run_extraction_model_router.py` as the first implementation scaffold.
4. Decide whether to keep it as a probe script or move the reusable pieces into:

```text
backend/services/extraction_model_router.py
```

5. Wire the production path through model pool entries or extraction-specific pool config.
6. Preserve per-provider payload hooks exactly:

```text
LongCat: thinking disabled
SiliconFlow Hy3/Hy3-preview: no response_format json_object
OpenRouter Nemo: response_format json_schema strict + provider.require_parameters=true
Ling: compact IR only, short chunks only
```

7. Keep all deterministic repairs outside the prompt.
8. Add tests that replay saved raw outputs and assert the compiled object validates.
9. Add live smoke tests gated by env vars so CI does not require paid keys.
10. Never commit provider secrets or raw request headers.

## Tests To Add Next

Use saved raw outputs or new audit artifacts and assert:

```text
LongCat:
- thinking-disabled output parses
- created_by person->product/org is reversed
- endpoint completion remains low

Hy3-preview:
- object_kind "Artifact"/"other" repairs to entity/literal
- endpoint completion adds missing endpoints
- compiled Pydantic passes

Hy3:
- run_by remaps to affiliated_with
- opens remaps to related_to/detects by evidence
- relation_cue preserves raw verb

Mistral Nemo:
- response_format payload includes strict json_schema
- provider.require_parameters=true is set
- prompt-only malformed shape is rejected
- structured response validates
- endpoint completion runs even after Pydantic passes

Ling:
- dense table chunks are split before calling
- finish_reason=length is rejected
- compact IR compiles into Polymath
```

## Final Implementation Principle

This is not just prompt engineering. The production system should be a deterministic compiler around probabilistic extractors.

The model can decide candidate values. Python must decide whether the result is legal, directionally sane, graph-safe, and worth accepting.
