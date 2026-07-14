# Structured-Output Gateway — owner-delivered spec (2026-07-14, design of record)

Execution split (owner ruling): **RunPod = extraction** (deploy + scale pods,
saturating concurrency, speed-optimized per job, scale-to-zero) ·
**API calls = summaries/digests** through THIS gateway.

Core principle: standardize the APPLICATION CONTRACT; never expect every model
to follow the same prompt equally well. Never "return valid JSON matching this
example". Always:

```
Pydantic model → JSON Schema → constrained decoding → Pydantic validation
→ semantic validation → one targeted repair attempt → accepted digest
```

JSON mode alone guarantees valid JSON, NOT adherence to a specific schema.

## 1. One generative intermediate representation

The LLM never generates Mongo documents, Neo4j nodes, or vector records.
It returns ONE `SemanticDigestV1`; the Python compiler projects it:

```
GLiNER/spaCy/GLiREL output → SemanticDigestV1 → Python compiler
                                                ├── MongoDB projection
                                                ├── Neo4j projection
                                                └── vector projection
```

Digest contains ONLY LLM-owned products: summary, central thesis, underlying
meanings, adjacent-domain proposals, latent-concept proposals, implicit frame
proposals, motif proposals, conditions, exceptions, unresolved interpretations.
Entities, predicates, explicit relations, evidence offsets, and initial claims
remain Python/GLiNER/GLiREL outputs (LocalExtractionV1 / ClaimRecordV1).

## 2. Portable schema subset (cross-provider compatibility)

- Shallow (2–3 nesting levels max)
- Fully required — empty arrays instead of omitted properties
- Closed: `additionalProperties: false` (JSON Schema permits extras by default)
- Enum-driven fixed values; array-of-uniform-records shape
- NO recursion, NO arbitrary dictionaries, NO complex oneOf/allOf/nested unions
- Versioned (`schema_version` literal)
- Avoid nested keyed objects (`domain_profiles.marketing.subdomains.pricing`);
  use arrays of records (`domain_proposals: [{registry_id, role, ...}]`)

## 3. Canonical Pydantic contract (owner-delivered, verbatim)

```python
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


AssignmentState = Literal["candidate", "corroborated", "validated", "unresolved", "rejected"]
SemanticRole = Literal["dominant", "supporting", "adjacent", "exploratory"]
FrameId = Literal["MF01", "MF02", "MF03", "MF04", "MF05", "MF06", "MF07", "MF08",
                  "MF09", "MF10", "MF11", "MF12", "MF13", "MF14", "MF15", "MF16"]


class SupportedStatement(StrictModel):
    text: str
    supporting_claim_ids: list[str] = Field(default_factory=list)


class DomainProposal(StrictModel):
    registry_id: str
    proposed_label: str
    role: SemanticRole
    assignment_state: AssignmentState
    supporting_claim_ids: list[str] = Field(default_factory=list)


class FrameProposal(StrictModel):
    frame_id: FrameId
    role: SemanticRole
    assignment_state: AssignmentState
    supporting_claim_ids: list[str] = Field(default_factory=list)
    explanation: str


class LatentConceptProposal(StrictModel):
    preferred_label: str
    definition: str
    assignment_state: AssignmentState
    supporting_claim_ids: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)


class MotifProposal(StrictModel):
    proposed_label: str
    frame_sequence: list[FrameId] = Field(default_factory=list)
    abstract_sequence: list[str] = Field(default_factory=list)
    supporting_claim_ids: list[str] = Field(default_factory=list)


class SemanticDigestV1(StrictModel):
    schema_version: Literal["semantic_digest.v1"]
    parent_id: str
    summary: str
    central_thesis: str
    underlying_meanings: list[SupportedStatement] = Field(default_factory=list)
    domain_proposals: list[DomainProposal] = Field(default_factory=list)
    frame_proposals: list[FrameProposal] = Field(default_factory=list)
    latent_concepts: list[LatentConceptProposal] = Field(default_factory=list)
    motif_proposals: list[MotifProposal] = Field(default_factory=list)
    conditions: list[SupportedStatement] = Field(default_factory=list)
    exceptions: list[SupportedStatement] = Field(default_factory=list)
    unresolved_interpretations: list[str] = Field(default_factory=list)
```

**SENIOR ERRATUM (2026-07-14, T4.1):** Section 2's fully-required rule is
operative. Every list field shown above is required (`Field()`, no default) in
the executable contract, and callers must provide an explicit empty array when
no supported value exists. This keeps every property in JSON Schema
`required`, as native strict-schema providers require. Ruling recorded in
`COORDINATION.md` at 2026-07-14T13:31:30Z; owner may veto by `OWNER ::` entry.

`SemanticDigestV1.model_json_schema()` (Draft 2020-12) is the single source of
truth for every backend.

## 4. Capability ladder (gateway selects strongest available per model)

- **Tier 1 — native strict JSON Schema**: `response_format={"type":"json_schema",
  "json_schema":{"name":"semantic_digest_v1","strict":True,"schema":...}}`.
  LiteLLM standardizes this + `supports_response_schema()` for detection
  (fits the existing LiteLLM stack).
- **Tier 2 — runtime grammar-constrained decoding** (local models): vLLM
  (XGrammar/Guidance), Outlines, llama.cpp server JSON-Schema grammar.
  Mac path: `LiteLLM → llama.cpp server (Metal) → schema-constrained response`.
  CAUTION: MLX-LM lacks a stable first-class JSON-Schema API — test a specific
  MLX-LM/Outlines combo before trusting it as the universal path.
- **Tier 3 — strict function/tool arguments**: force one tool
  `submit_semantic_digest(SemanticDigestV1)`; arguments are the output.
- **Tier 4 — JSON mode + validate + ONE retry** (last resort only; never treat
  JSON mode as schema enforcement).

## 5. Gateway flow (reference implementation shape)

```python
def call_structured(model, packet, constrained_fallback) -> SemanticDigestV1:
    schema = SemanticDigestV1.model_json_schema()
    try:
        if supports_response_schema(model=model):
            result = call_native_schema(model, packet)      # Tier 1
        else:
            raw = constrained_fallback(model, packet, schema)  # Tier 2/3/4
            result = SemanticDigestV1.model_validate_json(raw)
    except (ValidationError, ValueError, TypeError) as exc:
        raise StructuredGenerationError(f"Structural validation failed: {exc}") from exc
    return result
```

System prompt (compact; schema goes through response_format, NEVER pasted into
the prompt): "Generate a SemanticDigestV1 from the supplied evidence. Use only
claim IDs present in the input. Do not invent registry IDs. Use empty arrays
when no supported result exists. Treat latent concepts and motifs as proposals,
not facts. Separate source-backed conclusions from proposed interpretations.
Never mark your own proposal as validated." Temperature 0.

## 6. Semantic validation (structure ≠ meaning; Python enforces after Pydantic)

Every referenced claim ID exists AND belongs to the supplied parent · every
domain registry ID exists or is explicitly `candidate` (unknown registry id
cannot be non-candidate) · every frame ID ∈ MF01–MF16 · every frame proposal
has supporting claims · every latent concept has supporting claims (claim-
grounded mode) · every motif sequence corresponds to proposed/validated frames
and has ≥2 frames · no cross-link points to itself · **no LLM proposal is
marked source-observed or validated**. Returns a list of precise error strings
(location-indexed) — see owner reference `semantic_validate()`.

## 7. Repair policy — targeted, bounded, honest

```
Attempt 1: normal constrained generation
Attempt 2: targeted correction — send {original_output, validation_errors[],
           instruction: "Correct only the validation failures."} under the
           SAME constrained schema
Still invalid: dead-letter queue. NEVER write to the canonical graph.
```

Safe deterministic fixes (e.g. dedupe array entries) happen in Python.
Semantic corrections (e.g. replacing an unsupported frame) are NEVER silently
invented.

## 8. Scope discipline

The digest call may produce several semantic products in one call, but NEVER:
all entities, all relation triples, all atomic claims, Neo4j Cypher, MongoDB
records, Qdrant payloads, embeddings, or corpus-wide cross-book analogies.
Those belong to other stages. Large schemas raise output length, failure
surface, and grammar-compilation cost.

## 9. Determinism & provenance (recorded with EVERY generation)

```json
{"model_id": "exact-model-version", "runtime": "llama.cpp|vllm|mlx|provider",
 "runtime_version": "...", "tokenizer_id": "...", "chat_template_hash": "...",
 "schema_version": "semantic_digest.v1", "schema_hash": "...",
 "prompt_version": "parent-digest.v3", "prompt_hash": "...",
 "temperature": 0, "input_hash": "...", "output_hash": "..."}
```

Cache key = input_hash + model_id + schema_hash + prompt_hash +
runtime_version. Temperature 0 reduces variability but does NOT guarantee
bit-identical output across runtimes/quantizations/hardware — repeatability
comes from fixed contracts, validation, versioning, and caching, never from
assuming model determinism.

## 10. Final flow of record

```
One Pydantic SemanticDigest schema
→ LiteLLM capability detection
→ native strict schema when supported
→ llama.cpp/vLLM/Outlines/XGrammar constrained fallback
→ Pydantic structural validation
→ Python semantic validation
→ one constrained repair attempt
→ assignment compiler
→ MongoDB / Neo4j / vector projections
```

## Sequencing note (senior-dev ruling)

Tonight's re-batch (REBATCH_RUNBOOK Phase B) rides the CURRENT deployed Ghost A
summary path (interim-v1 latent capture) — it does NOT wait on this gateway.
The gateway + SemanticDigestV1 is the S11/P2.5c build that replaces Ghost A's
semantic role at the measured RetrievalSummary→SemanticDigest cutover already
ruled in P2.5b. Registry-independent, Codex-executable now.
