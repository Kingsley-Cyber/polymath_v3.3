# Polymath Extraction Model Routing Report

Date: 2026-07-07

Purpose: handoff document for building a production extraction router that can use the tested cloud models dynamically while forcing all outputs into the Polymath extraction schema.

This report is intended for a strong implementation model. It records the tested providers, model IDs, exact extraction modes, observed response quality, failure modes, and the script architecture needed to integrate them robustly.

No API keys are included here. Use environment variables only.

## Executive Summary

The winning strategy is not to trust any model raw. The production path should be:

```text
document chunk
  -> model-specific prompt/request adapter
  -> raw response capture
  -> JSON envelope/balanced-object extraction
  -> deterministic normalization/compiler
  -> Pydantic ExtractionResponse validation
  -> semantic repair/checks
  -> endpoint completion
  -> routing metrics + persisted audit artifact
```

The four adapter families ready to implement are:

1. LongCat `LongCat-2.0`: strong direct extractor when thinking is disabled; Pydantic-valid, but still needs semantic direction checks.
2. SiliconFlow Tencent `tencent/Hy3-preview`: strong prompt-only extractor; JSON mode unsupported; needs object_kind normalization.
3. SiliconFlow Tencent `tencent/Hy3`: strong prompt-only extractor; JSON mode unsupported; needs predicate normalization for occasional raw verbs.
4. OpenRouter `mistralai/mistral-nemo` and `inclusionai/ling-2.6-flash`: use Nemo only with OpenRouter structured outputs; use Ling as a cheap IR/table miner with tight chunks, not as the primary direct Polymath writer.

If only four runtime slots are desired, use:

1. `LongCat-2.0`
2. `tencent/Hy3-preview`
3. `tencent/Hy3`
4. `mistralai/mistral-nemo`

Keep `inclusionai/ling-2.6-flash` as the cheap auxiliary lane for short/table chunks and compact linguistic IR. It should not replace the four direct lanes.

## Polymath Schema Contract

The target Python contract is `backend/services/ghost_b_schemas.py::ExtractionResponse`.

Top-level JSON object:

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
  "confidence": 0.0,
  "query_aliases": [],
  "definitional_phrase": "string",
  "object_kind": "open string facet"
}
```

Relation fields:

```json
{
  "subject": "string",
  "predicate": "part_of | member_of | located_in | works_for | created_by | owns | affiliated_with | synonym_of | instance_of | example_of | uses | references | implements | depends_on | produces | stores | detects | supports | defines | represents | maps_to | preceded_by | causes | overlaps | during | derived_from | contradicts | excepts | overrides | related_to",
  "object": "string",
  "object_kind": "entity | literal",
  "confidence": 0.0,
  "evidence_phrase": "string",
  "relation_cue": "string"
}
```

Fact fields:

```json
{
  "subject": "string",
  "fact_type": "property | status | timestamp | quantity | threshold | category | tag | rule_condition | rule_action",
  "property_name": "string",
  "value": "string",
  "unit": "string",
  "condition": "string",
  "confidence": 0.0,
  "evidence_phrase": "string"
}
```

Important: Pydantic validation only proves syntactic/schema validity. It does not catch semantic direction bugs such as:

```text
Mira Chen -created_by-> HarborLight Analytics
Rafael Ortiz -created_by-> Aegis-9
```

The integration script must add semantic validators after Pydantic.

## Test Corpus

Two local documents were used:

1. Narrative extraction stress case:
   `C:\Users\Sammb\Downloads\On March 14, 2026, Dr. Mira Chen ar.txt`

   Chunk size: 3,615 chars.

   Content includes people, companies, products, locations, tests, contract/payment conditions, maintenance requirements, hardware failures, thresholds, and outcome metrics.

2. Markdown/table extraction stress case:
   `E:\books\AI_FILM_SCHOOL\film_school_reading_map_for_ai_video_direction.md`

   Chunks tested included the `# 2. Control Modules` section and the smaller `Control Module Concepts to Extract` table.

## Provider And Model Profiles

### 1. LongCat

Provider: LongCat

Base URL:

```text
https://api.longcat.chat
```

Chat endpoint:

```text
POST /openai/v1/chat/completions
```

Model ID:

```text
LongCat-2.0
```

Auth env var to use:

```text
LONGCAT_API_KEY
```

Required request detail:

```json
{
  "thinking": { "type": "disabled" }
}
```

Why: without disabling thinking, the first LongCat run spent the token budget in reasoning and returned empty `message.content` with `finish_reason: length`.

Best tested mode:

```text
Refined XML-delimited prompt + <json_payload>{...}</json_payload> output envelope + thinking disabled
```

Best result artifact:

```text
C:\Users\Sammb\AppData\Local\Temp\polymath_longcat_probe\longcat2_mira_refined_hybrid_thinking_disabled_summary.json
```

Raw/compiled artifacts:

```text
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

Valuable output examples:

```text
blue lantern.first_field_test_date = 2026-03-14
aegis-9.tamper_detection_time = 12
northstar shipping.additional_payment = 180000
blue lantern.theft_reduction = 40
aegis-9.accuracy_threshold = 95
```

Strengths:

- Best raw structural compliance among non-OpenRouter-schema runs.
- Direct Polymath JSON was parseable and Pydantic-valid.
- Good fact recall on numeric/time/threshold details.
- No endpoint completion needed in the compiled run.

Weaknesses:

- Needs `thinking` disabled.
- Sometimes directionally wrong on `created_by`.
- It emitted `schema_version: polyextract.v2` instead of `polymath.extract.v2`; current Pydantic ignores this because the field is not in the contract.
- It can exceed requested relation caps.

Required script mitigations:

- Always set `thinking: {"type": "disabled"}`.
- Run semantic relation repair after Pydantic:
  - `Person -created_by-> Organization/Product` should usually be reversed.
  - `created_by` direction should be `created thing -> creator`.
  - Use `relation_cue` and `evidence_phrase` to confirm reversal.
- Drop ignored metadata fields before strict validation if the final contract forbids extras.

Recommended role:

```text
Primary direct extractor, but not trusted without semantic repair.
```

### 2. SiliconFlow Tencent Hy3 Preview

Provider: SiliconFlow

Base URL:

```text
https://api.siliconflow.com/v1
```

Chat endpoint:

```text
POST /chat/completions
```

Model ID:

```text
tencent/Hy3-preview
```

Auth env var to use:

```text
SILICONFLOW_API_KEY
```

JSON mode support:

```text
Not supported for this model.
```

Observed JSON mode error:

```json
{
  "code": 20024,
  "message": "Json mode is not supported for this model."
}
```

Best tested mode:

```text
Prompt-only refined XML section delimiters + <json_payload>{...}</json_payload> output envelope
```

Best result artifact:

```text
C:\Users\Sammb\AppData\Local\Temp\polymath_siliconflow_probe\hy3_preview_mira_refined_hybrid_summary.json
```

Raw/compiled artifacts:

```text
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

Valuable output examples:

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

- Fast relative to LongCat and Nemo in these tests.
- Good entity and relation recall.
- Stronger semantic direction than LongCat in the Mira test.
- Prompt-only extraction is usable despite no JSON mode support.

Weaknesses:

- JSON mode is unavailable.
- `object_kind` confusion between entity facet and relation endpoint kind.
- Endpoint completion still needed.
- Facts were partially reduced by downstream parser in this run.

Required script mitigations:

- Never request SiliconFlow `response_format: {"type": "json_object"}` for Hy3/Hy3-preview.
- Use XML only as prompt delimiters; do not make XML part of the ontology.
- Normalize relation `object_kind`:
  - if relation object matches known entity or looks like a named entity/ID, use `"entity"`;
  - otherwise use `"literal"`.
- Add endpoint completion after compiling.

Recommended role:

```text
Primary or backup direct extractor for narrative chunks.
```

### 3. SiliconFlow Tencent Hy3

Provider: SiliconFlow

Base URL:

```text
https://api.siliconflow.com/v1
```

Chat endpoint:

```text
POST /chat/completions
```

Model ID:

```text
tencent/Hy3
```

Auth env var to use:

```text
SILICONFLOW_API_KEY
```

JSON mode support:

```text
Not supported for this model.
```

Best tested mode:

```text
Prompt-only refined XML section delimiters + <json_payload>{...}</json_payload> output envelope
```

Best result artifact:

```text
C:\Users\Sammb\AppData\Local\Temp\polymath_siliconflow_probe\hy3_mira_refined_hybrid_summary.json
```

Raw/compiled artifacts:

```text
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

Valuable output examples:

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

- Comparable to Hy3-preview, sometimes better fact preservation after parsing.
- Good schema-ish discipline with only a small number of predicate mistakes.
- Fast enough for a fallback or load-balancing lane.

Weaknesses:

- JSON mode is unavailable.
- Still invents raw predicate labels occasionally.
- Needs deterministic predicate remapping and endpoint completion.

Required script mitigations:

- Same SiliconFlow request adapter as Hy3-preview.
- Predicate remap table must include:
  - `run_by` -> `affiliated_with` or `supports` depending on direction/evidence
  - `opens` -> `detects` or `related_to` depending on subject/object
  - `founded_by`, `designed_by` -> `created_by` with direction repair
  - `supervised_by`, `approved_by` -> `supports` or `related_to`
- Preserve original raw verb in `relation_cue`.

Recommended role:

```text
Backup direct extractor and practical SiliconFlow lane when preview is rate-limited or unavailable.
```

### 4. OpenRouter Mistral Nemo

Provider: OpenRouter

Base URL:

```text
https://openrouter.ai/api/v1
```

Chat endpoint:

```text
POST /chat/completions
```

Model ID:

```text
mistralai/mistral-nemo
```

Auth env var to use:

```text
OPENROUTER_API_KEY
```

Best tested mode:

```text
OpenRouter response_format json_schema strict + provider.require_parameters true
```

Do not use prompt-only Nemo for production. Prompt-only tuned Nemo returned a structurally wrong object where:

```text
entities = ["Dr. Mira Chen", ...]
relations = ["founded_by", "run_by", ...]
facts = objects with predicate/object fields, not Polymath fact fields
```

Best result artifact:

```text
C:\Users\Sammb\AppData\Local\Temp\polymath_openrouter_probe\mistral_nemo_structured_schema_v2_summary.json
```

Raw/API artifacts:

```text
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

Valuable output examples:

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

- With OpenRouter structured outputs, raw schema compliance is strong.
- Predicate enum mistakes disappear under `json_schema` strict mode.
- Good facts.
- Good semantic direction after adding Nemo-specific direction rules.

Weaknesses:

- Prompt-only mode is not production-safe.
- Structured run was conservative on relation recall.
- One relation endpoint, `Port Alder`, was missing from entities despite being an object endpoint.
- A more aggressive relation-checklist run increased recall to 13 relations but introduced edge conflation; do not use that version as default.

Required script mitigations:

- Always set `response_format.type = "json_schema"` and `json_schema.strict = true`.
- Always set provider routing:

```json
{
  "provider": {
    "require_parameters": true
  }
}
```

- Add endpoint completion:
  - if a relation has `object_kind: "entity"` and object is absent from entities, synthesize an entity using casing/source evidence.
- Add semantic direction checks for `created_by`, `works_for`, and `located_in`.
- Keep Nemo-specific direction instructions in the prompt.

Recommended role:

```text
Structured-output fallback lane. Use when OpenRouter structured outputs are available.
```

### 5. OpenRouter InclusionAI Ling 2.6 Flash

Provider: OpenRouter

Base URL:

```text
https://openrouter.ai/api/v1
```

Chat endpoint:

```text
POST /chat/completions
```

Model ID:

```text
inclusionai/ling-2.6-flash
```

Auth env var to use:

```text
OPENROUTER_API_KEY
```

Best tested role:

```text
Cheap compact IR/table extractor, not primary direct Polymath writer.
```

Key stress-test finding:

- Dense 5,964-char markdown table chunk caused `finish_reason: length` and incomplete JSON.
- Smaller 856-char table chunk with XML-delimited prompt stopped normally, parsed JSON, and passed Pydantic.
- JSON-only prompt on the same small chunk ran to `finish_reason: length`.
- XML section delimiters helped; XML should not become ontology.

Dense chunk failure artifact:

```text
C:\Users\Sammb\AppData\Local\Temp\polymath_openrouter_probe\ling_table_ir_control_modules_summary.json
```

Successful small chunk artifact:

```text
C:\Users\Sammb\AppData\Local\Temp\polymath_openrouter_probe\ling_xml_control_concepts_confidence_fixed_summary.json
```

Observed successful small-chunk result:

```text
http_status: 200
elapsed_s: 4.51
finish_reason: stop
prompt_tokens: 711
completion_tokens: 696
total_tokens: 1407
cost: 0.00002799
raw JSON parse: true
Pydantic: true
counts: 6 entities, 4 relations, 4 facts
```

Valuable output examples:

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

Strengths:

- Very cheap.
- Good for extracting lightweight frames/facts from tightly bounded chunks.
- XML-delimited prompts improve completion behavior.
- Useful as a recall/auxiliary lane for table or linguistic-frame extraction.

Weaknesses:

- Not reliable for direct Polymath over broad/dense chunks.
- Big chunks trigger exhaustive enumeration and truncation.
- Earlier direct runs produced many off-schema predicates and too much endpoint completion.
- It can copy placeholder confidence values if examples use `0.0`.
- It can confuse ontology layers if asked to output Polymath plus a separate IR plus table semantics.

Required script mitigations:

- Hard chunk limits:
  - narrative: start around 1,500-2,000 chars;
  - markdown tables: 3-5 rows per call;
  - concept tables: 4 rows per call.
- Use XML for prompt sections only:

```xml
<document>...</document>
<task>...</task>
<allowed_labels>...</allowed_labels>
```

- Output JSON only. Do not require XML output wrappers for Ling; it may ignore them.
- Do not include `0.0` placeholder examples.
- Prefer compact IR first, then compile to Polymath:

```json
{
  "entities": [],
  "frames": [],
  "facts": []
}
```

- Let Python handle relation construction, canonical alias merging, and endpoint completion.

Recommended role:

```text
Cheap auxiliary extractor for short chunks and tables. Do not route critical direct Polymath extraction to Ling unless the chunk is small and validation gates pass.
```

## Rejected Or Experimental Models

These were tested enough to avoid integrating as ready lanes today.

### OpenRouter `meta-llama/llama-3.1-8b-instruct`

Outcome:

```text
Started correct JSON, then repeated until finish_reason: length.
No closed JSON payload.
```

Recommendation:

```text
Do not integrate for this extraction router.
```

### OpenRouter `liquid/lfm-2.5-1.2b-instruct:free`

Outcome:

```text
Returned only {"schema_version":"polymath.extract.v2"}.
No useful extraction.
```

Recommendation:

```text
Do not integrate for this extraction router.
```

### OpenRouter `mistralai/mistral-small-24b-instruct-2501`

Outcome:

```text
Produced useful content but failed wrapper closure on first run.
Balanced JSON salvage worked.
Pydantic passed after salvage.
Semantic relation quality was mixed.
```

Recommendation:

```text
Experimental only. Keep out of first production router unless additional tests prove stable.
```

## Prompting Rules Learned

1. XML is useful as a section delimiter, not as ontology.

Good:

```xml
<document>...</document>
<allowed_labels>...</allowed_labels>
<task>...</task>
```

Risky:

```xml
<json_payload>{...}</json_payload>
```

The wrapper worked for LongCat and Hy3, but Ling often ignored it. The parser should not require wrapper closure if a balanced JSON object can be recovered.

2. Use one semantic contract per call.

Do not ask a weak model to simultaneously understand:

```text
Polymath ontology + compact IR ontology + table ontology + XML output ontology
```

For Ling, choose one:

```text
Ling -> compact IR -> Python compiler -> Polymath
```

3. Never use placeholder examples such as:

```json
{"confidence": 0.0}
```

Some models copy them. Use realistic values like `0.91`, and explicitly say confidence must be `0.72-0.99`.

4. Put raw verbs in `relation_cue`, not `predicate`.

Prompt language should say:

```text
The text verb goes in relation_cue. The predicate must be from the allowed enum.
```

5. Direction rules must be explicit.

Especially:

```text
created_by = created thing -> creator
works_for = person -> organization
located_in = contained thing/location -> containing location
```

## Dynamic Router Script Specification

Build a script or service module that can run all supported extraction lanes with a shared contract.

Suggested file:

```text
backend/services/extraction_model_router.py
```

or, if first implemented as a standalone probe:

```text
scripts/run_extraction_model_router.py
```

### Environment Variables

```text
LONGCAT_API_KEY
SILICONFLOW_API_KEY
OPENROUTER_API_KEY
EXTRACTION_ROUTER_DEFAULT_MODE
EXTRACTION_ROUTER_AUDIT_DIR
```

Never commit secrets.

### Core Data Structures

Use something like:

```python
from dataclasses import dataclass
from typing import Literal

ProviderName = Literal["longcat", "siliconflow", "openrouter"]
ExtractionMode = Literal["prompt_json", "json_schema", "compact_ir"]

@dataclass(frozen=True)
class ModelProfile:
    name: str
    provider: ProviderName
    model_id: str
    mode: ExtractionMode
    base_url: str
    api_key_env: str
    priority: int
    max_chunk_chars: int
    max_output_tokens: int
    supports_json_schema: bool
    requires_thinking_disabled: bool = False
```

Recommended profiles:

```python
MODEL_PROFILES = [
    ModelProfile(
        name="longcat_2_direct",
        provider="longcat",
        model_id="LongCat-2.0",
        mode="prompt_json",
        base_url="https://api.longcat.chat/openai/v1",
        api_key_env="LONGCAT_API_KEY",
        priority=10,
        max_chunk_chars=4500,
        max_output_tokens=4500,
        supports_json_schema=False,
        requires_thinking_disabled=True,
    ),
    ModelProfile(
        name="hy3_preview_direct",
        provider="siliconflow",
        model_id="tencent/Hy3-preview",
        mode="prompt_json",
        base_url="https://api.siliconflow.com/v1",
        api_key_env="SILICONFLOW_API_KEY",
        priority=20,
        max_chunk_chars=4000,
        max_output_tokens=3500,
        supports_json_schema=False,
    ),
    ModelProfile(
        name="hy3_direct",
        provider="siliconflow",
        model_id="tencent/Hy3",
        mode="prompt_json",
        base_url="https://api.siliconflow.com/v1",
        api_key_env="SILICONFLOW_API_KEY",
        priority=30,
        max_chunk_chars=4000,
        max_output_tokens=3500,
        supports_json_schema=False,
    ),
    ModelProfile(
        name="mistral_nemo_schema",
        provider="openrouter",
        model_id="mistralai/mistral-nemo",
        mode="json_schema",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        priority=40,
        max_chunk_chars=4000,
        max_output_tokens=3600,
        supports_json_schema=True,
    ),
    ModelProfile(
        name="ling_flash_ir",
        provider="openrouter",
        model_id="inclusionai/ling-2.6-flash",
        mode="compact_ir",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        priority=90,
        max_chunk_chars=1800,
        max_output_tokens=1600,
        supports_json_schema=False,
    ),
]
```

### Request Builder

Implement provider-specific request details:

LongCat:

```python
payload["thinking"] = {"type": "disabled"}
```

SiliconFlow Hy3:

```python
# Do not send response_format json_object for tencent/Hy3 or tencent/Hy3-preview.
```

OpenRouter Nemo:

```python
payload["response_format"] = {
    "type": "json_schema",
    "json_schema": {
        "name": "polymath_extraction",
        "strict": True,
        "schema": extraction_response_json_schema(),
    },
}
payload["provider"] = {"require_parameters": True}
```

OpenRouter Ling:

```python
# Use compact prompt only. Prefer no response_format unless model support is verified.
```

### Parser Pipeline

Implement in this order:

1. Capture full provider JSON response to audit file.
2. Extract `choices[0].message.content`.
3. Try `<json_payload>...</json_payload>` extraction.
4. If no closed wrapper, find first balanced JSON object.
5. Try `json.loads`.
6. If needed for legacy probes only, try `ast.literal_eval` for Python-dict-like outputs.
7. Drop metadata keys not in the Polymath contract.
8. Normalize object shapes.
9. Compile IR to Polymath if using Ling.
10. Validate with `ExtractionResponse.model_validate`.
11. Run semantic validators and repairs.
12. Run endpoint completion.
13. Persist final compiled JSON and metrics.

### Deterministic Normalization

Required normalizers:

```text
canonical_name:
- lowercase natural names for prompt-only models, unless product/container IDs need case preservation
- replace underscores with spaces
- remove honorific prefixes for canonical matching: Dr., Captain
- preserve IDs: Aegis-9, NX-441, NX-502

relation.object_kind:
- only "entity" or "literal"
- if value is "Entity", "Artifact", "Person", "Product", "other" in relation.object_kind, infer true endpoint kind

predicate:
- map raw verbs to schema predicates
- preserve raw verb in relation_cue

confidence:
- clamp to 0.0-1.0
- reject or repair obvious placeholders if all scores are 0.0 and prompt did not ask for zero
```

Predicate remap starter table:

```python
PREDICATE_REMAP = {
    "founded": "created_by",
    "founded_by": "created_by",
    "created": "created_by",
    "created_by": "created_by",
    "designed": "created_by",
    "designed_by": "created_by",
    "built": "created_by",
    "run_by": "affiliated_with",
    "ran": "affiliated_with",
    "signed_contract_with": "affiliated_with",
    "contract_with": "affiliated_with",
    "supervised": "supports",
    "supervised_by": "supports",
    "approved": "supports",
    "approved_by": "supports",
    "uses": "uses",
    "used": "uses",
    "detected": "detects",
    "detects": "detects",
    "stored": "stores",
    "stores": "stores",
    "located_at": "located_in",
    "located_in": "located_in",
    "paid": "supports",
    "opened": "related_to",
    "opens": "related_to",
    "blamed": "related_to",
}
```

### Semantic Validators

These checks are mandatory because Pydantic cannot catch them.

Created-by direction:

```python
if rel.predicate == "created_by":
    # Desired: created thing -> creator.
    # If subject is Person and object is Organization/Product/Concept/Artifact,
    # reverse the edge when evidence/cue indicates founded/designed/created.
```

Works-for direction:

```python
if rel.predicate == "works_for":
    # Desired: Person -> Organization.
    # If reversed, swap.
```

Located-in direction:

```python
if rel.predicate == "located_in":
    # Desired: contained location/entity -> containing location.
```

Endpoint completion:

```python
for relation in relations:
    if relation.object_kind == "entity":
        ensure_entity_exists(relation.subject)
        ensure_entity_exists(relation.object)
```

Fact conversion:

```text
Some models express a literal relation that is better as a fact:
- Aegis-9 -detects-> "within twelve seconds" should become fact Aegis-9.tamper_detection_time = 12 seconds.
- Northstar Shipping -supports-> "$180,000" should become payment fact.
```

### Routing Policy

Recommended runtime selection:

1. If OpenRouter structured outputs are available and the chunk is normal narrative:
   - use `mistral_nemo_schema` as a schema-safe fallback, not first-choice recall model.
2. If LongCat account has balance:
   - use `longcat_2_direct` for high-quality direct extraction, then semantic repair.
3. If SiliconFlow account has balance:
   - use `hy3_preview_direct`, fallback to `hy3_direct`.
4. If chunk is a small table/concept list or cheap recall is desired:
   - use `ling_flash_ir`, compile IR to Polymath.
5. If a provider returns malformed/incomplete JSON:
   - retry once with smaller chunk and lower caps.
6. If Pydantic fails after compiler repair:
   - route to next model, keep failed artifact for audit.

Suggested confidence scoring per run:

```text
score = 100
score -= 20 if raw_json_parse failed and salvage was required
score -= 25 if Pydantic failed before compiler
score -= 10 * relation_remap_count
score -= 8 * endpoint_completion_count
score -= 25 if semantic direction repair occurred
score -= 50 if finish_reason == "length"
score += 10 if provider-side json_schema strict was used
```

Only accept automatically if:

```text
final Pydantic valid
finish_reason != length
score >= 70
no unresolved entity endpoints
no semantic validator hard failures
```

### Audit Output

Each call should save:

```text
audit_dir/
  {timestamp}_{profile}_{chunk_id}_request.json
  {timestamp}_{profile}_{chunk_id}_provider_response.json
  {timestamp}_{profile}_{chunk_id}_raw_content.txt
  {timestamp}_{profile}_{chunk_id}_candidate.json
  {timestamp}_{profile}_{chunk_id}_compiled.json
  {timestamp}_{profile}_{chunk_id}_metrics.json
```

Metrics should include:

```json
{
  "profile": "hy3_preview_direct",
  "provider": "siliconflow",
  "model": "tencent/Hy3-preview",
  "chunk_chars": 3615,
  "elapsed_s": 10.597,
  "finish_reason": "stop",
  "raw_json_parse": true,
  "candidate_pydantic": false,
  "compiled_pydantic": true,
  "entity_count": 16,
  "relation_count": 12,
  "fact_count": 8,
  "predicate_remap_count": 0,
  "object_kind_repair_count": 3,
  "endpoint_completion_count": 3,
  "semantic_repair_count": 0,
  "acceptance_score": 82
}
```

## Provider Request Examples

### LongCat

```python
payload = {
    "model": "LongCat-2.0",
    "messages": messages,
    "temperature": 0,
    "top_p": 0.1,
    "max_tokens": 4500,
    "stream": False,
    "thinking": {"type": "disabled"},
}
```

### SiliconFlow Hy3

```python
payload = {
    "model": "tencent/Hy3-preview",
    "messages": messages,
    "temperature": 0,
    "top_p": 0.1,
    "max_tokens": 3500,
    "stream": False,
}
```

Do not add:

```python
response_format={"type": "json_object"}
```

### OpenRouter Nemo

```python
payload = {
    "model": "mistralai/mistral-nemo",
    "messages": messages,
    "temperature": 0,
    "top_p": 0.1,
    "max_tokens": 3600,
    "stream": False,
    "response_format": {
        "type": "json_schema",
        "json_schema": {
            "name": "polymath_extraction",
            "strict": True,
            "schema": extraction_response_json_schema(),
        },
    },
    "provider": {"require_parameters": True},
}
```

### OpenRouter Ling

```python
payload = {
    "model": "inclusionai/ling-2.6-flash",
    "messages": messages,
    "temperature": 0,
    "top_p": 0.1,
    "max_tokens": 1600,
    "stream": False,
}
```

## Recommended Default Prompts

### Direct Polymath Prompt Skeleton

Use for LongCat and Hy3 family. For Nemo, keep the same semantic instructions but rely on `response_format`.

```text
You are a deterministic Polymath extraction engine.
Extract only claims explicitly supported by the document.
Return exactly one JSON object matching the Polymath contract.

XML tags in the prompt are delimiters only. They are not ontology.
The only ontology is the Polymath entity_type, predicate, and fact_type lists.

The raw text verb belongs in relation_cue.
The predicate must be one of the allowed schema predicates.

Direction rules:
- created_by = created thing -> creator
- works_for = person -> organization
- located_in = contained thing/location -> containing location

Do not use raw predicates like founded, designed, run_by, paid, supervised, approved_by.
Map them to schema predicates and preserve the raw cue in relation_cue.
```

User message:

```xml
<document>
...
</document>

Return only:
<json_payload>{...valid JSON object...}</json_payload>
```

### Ling Compact Prompt Skeleton

```text
You are a deterministic information extraction transducer.
XML tags in the prompt are delimiters only.
Output exactly one RFC8259 JSON object. No markdown. No XML in output.

Use one contract only:
{
  "entities": [],
  "frames": [],
  "facts": []
}

For tables:
- each row is one local extraction unit
- do not enumerate outside the selected rows
- confidence must be 0.72-0.99
- max entities <= 14
- max frames <= 12
- max facts <= 4
```

## Implementation Checklist

1. Add model profile config for LongCat, Hy3-preview, Hy3, Nemo, and Ling.
2. Add OpenAI-compatible request adapter with provider-specific payload hooks.
3. Generate JSON Schema from `ExtractionResponse` or maintain a strict hand-authored provider schema.
4. Implement JSON extraction:
   - envelope extraction;
   - balanced JSON salvage;
   - metadata stripping.
5. Implement compiler/normalizer:
   - canonical names;
   - predicate remaps;
   - relation `object_kind`;
   - confidence clamping;
   - Ling IR to Polymath.
6. Implement semantic validators:
   - `created_by`;
   - `works_for`;
   - `located_in`;
   - literal relation to fact conversion.
7. Implement endpoint completion.
8. Add acceptance scoring and fallback routing.
9. Save audit artifacts per call.
10. Add tests using the two documents listed above.

## Acceptance Tests To Build

Use the Mira document and assert:

```text
Must include entities:
- Mira Chen
- HarborLight Analytics
- Blue Lantern
- Aegis-9
- Rafael Ortiz
- Port Alder Authority
- Elise Ward
- Northstar Shipping
- ShieldCap

Must include or derive relations:
- HarborLight Analytics created_by Mira Chen
- Aegis-9 created_by Rafael Ortiz
- Rafael Ortiz works_for HarborLight Analytics
- Elise Ward works_for Port Alder Authority
- Blue Lantern affiliated_with HarborLight Analytics
- HarborLight Analytics affiliated_with Northstar Shipping
- Aegis-9 uses thermal imaging
- Aegis-9 uses vibration analysis
- Aegis-9 detects unauthorized opening
- Aegis-9 stores heat patterns/timestamps/container IDs

Must include facts:
- Aegis-9 detection time = twelve seconds / 12 seconds
- mounting distance < two meters
- face capture disabled
- payment amount = 180000 / $180,000
- high-temperature threshold = 95 degrees Fahrenheit
- theft reduction = 40 percent
```

Use the film-school table chunk and assert for Ling:

```text
Small 3-5 row table chunks must parse and validate.
Large table chunks should be pre-split before model call.
No run with finish_reason == length can be auto-accepted.
```

## Final Recommendation

Build the router as a deterministic compiler with model adapters, not as a prompt collection.

The model roster should start with:

```text
LongCat-2.0
tencent/Hy3-preview
tencent/Hy3
mistralai/mistral-nemo
```

Add Ling as:

```text
inclusionai/ling-2.6-flash -> compact IR -> Python compiler
```

The crucial production insight from the tests is that structured or Pydantic-valid JSON is not enough. The router must enforce semantic relation direction, endpoint completeness, predicate vocabulary, and model-specific failure handling before anything reaches the graph.
