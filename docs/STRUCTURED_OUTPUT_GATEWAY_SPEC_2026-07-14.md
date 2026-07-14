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

Runtime-verification note (2026-07-14): provider/library metadata is advisory
and cannot grant Tier 1. The versioned route registry is authoritative. All
five configured routes rejected the tiny native-schema probe. Flash Tier 4 is
`structurally_unreliable` for SemanticDigestV1; flash Tier 3 produced partial
acceptance but exhausted its repair budget and therefore has no verified
digest path. LongCat accepted a tiny forced-tool probe, but full-digest
capability remains unverified. These are external runtime limits, not reasons
to weaken validation or relabel a lower tier.

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
Never mark your own proposal as validated. Return only a JSON object. Return
the SemanticDigestV1 object itself at the top level. Do not wrap it under
digest or add other top-level fields." Temperature 0. This universal
`parent-digest.v5` prompt is identical across tiers; its final instructions
satisfy DeepSeek's runtime requirement that a `json_object` prompt contain the
literal word `json` and forbid the observed `{digest: ...}` wrapper without
pasting a schema. The targeted repair instruction repeats the same top-level
shape constraint. The route-level requirement is recorded in
`structured_output_capabilities.v1.json`.

Tier 3 targeted repairs use the separately versioned
`parent-digest-repair.v2` instruction: corrections must be resubmitted through
the same forced `submit_semantic_digest` tool, with all 12 fields directly at
the tool-arguments root and no `parameters` or other wrapper. Its independent
hash is recorded in provenance and participates in the combined prompt hash,
so repair-contract changes always change the cache identity.

### 5.1 Frozen target-corpus remediation text (senior-approved)

Status: **approved verbatim and frozen at 2026-07-14T20:38:14Z**. This prompt
contract responds to
the mark phase-1 evidence that 4/12 packets dead-lettered with nonempty output:
three unsupported motif/frame proposal classes and one structurally invalid
`latent_concepts` value. It does not change `SemanticDigestV1`, the semantic
validator, temperature, provider route, or permission ladder.

Frozen universal system prompt, `parent-digest.v6` (exact text):

> Generate a SemanticDigestV1 from the supplied evidence. Use only claim IDs
> present in the input. Do not invent registry IDs. Use empty arrays when no
> supported result exists. Treat latent concepts and motifs as proposals, not
> facts. Separate source-backed conclusions from proposed interpretations.
> Never mark your own proposal as validated. Every domain, frame, latent-
> concept, or motif proposal must have a non-empty supporting_claim_ids array
> containing only claim IDs present in the input; otherwise omit that
> proposal. Propose a motif only when every frame_id in its frame_sequence
> also appears in frame_proposals, and use at least two frames. Every
> latent_concepts item must contain exactly these five fields: preferred_label
> as a string, definition as a string, assignment_state as candidate,
> corroborated, unresolved, or rejected, supporting_claim_ids as an array of
> input claim IDs, and aliases as an array of strings. Fewer proposals are
> correct when support is uncertain; empty proposal arrays are always lawful.
> Return only a JSON object. Return the SemanticDigestV1 object itself at the
> top level. Do not wrap it under digest or add other top-level fields.

Frozen generic targeted repair instruction,
`parent-digest-repair.v3` (exact text):

> Correct only the validation failures. When a validation error names an
> unsupported proposal or an invalid proposal reference, remove that entire
> optional proposal. Never preserve a failing proposal by inventing,
> substituting, or reassigning claims, frames, registry IDs, aliases,
> definitions, or justification. Preserve valid content. Fewer proposals are
> correct; empty proposal arrays are always lawful. Return every required
> array, using an empty array when no supported result exists. Return the
> SemanticDigestV1 object itself at the top level. Do not wrap it under digest
> or add other top-level fields.

Frozen Tier 3 repair suffix (exact text, appended to repair-v3):

> Resubmit the correction through the SAME forced submit_semantic_digest tool.
> Put all 12 SemanticDigestV1 fields directly at the tool-arguments root. Do
> not nest them under parameters or any other wrapper.

The two versions and hashes change independently in provenance;
their combined prompt identity changes the generation cache key. Accepted
digests from the previously certified route remain contract-valid purchases
and are skipped by parent acceptance, regardless of prompt version.

### 5.2 Paid-pass transport exposure accounting (senior-approved)

A transport-dead job is terminal and is never retried in-phase because the
provider may have processed the timed-out request. Missing provider telemetry
is not rewritten as zero and is not synthesized as actual cost:
`actual_cost_usd` remains null and row `cost_complete` remains false. The
operator may instead book an explicit, versioned
`unpriced_exposure_upper_bound_usd` on a separate ledger line. Budget ceiling
checks sum known actual cost plus these bounds; when every unknown cost has a
lawful bound, phase accounting state is
`complete_with_bounded_exposure` while actual-cost completeness remains false.

The current mark ruling fixes the bound at `$0.06` per transport-dead row
(`bounded_transport_exposure.v1`). This accounting distinction does not
change acceptance: Phase 1C still requires 48/50. Its ordinal-60 ReadTimeout
is a final Phase-1C loss and joins the separately sealed post-Phase-2 tail set;
tail retries use authorization-scoped durable job IDs so the original attempt
ledger remains immutable. Three total Phase-1C ReadTimeouts pause the pass for
a versioned transport-timeout proposal; they do not silently change runtime
parameters.

### 5.3 Canonical census scope v2 (senior-approved)

Canonical drift verdicts protect Polymath-owned assets, not unrelated
applications that share the same database server. The versioned
`canonical_store_census.scope.v2` recipe is hashed under the registered
`scope` namespace. Its current recipe hash is
`sha256:d5a5c1344898d397f1b687b4569fff2613da67d2b8d27a53542d97b7983c8773`.

Verdict-bearing counts are:

- Mongo `semantic_artifacts`;
- all nodes and relationships in Polymath's Neo4j instance;
- Qdrant's exact shared Polymath collections `polymath_children` and
  `polymath_doc_summaries`; and
- per-corpus Qdrant collections matching the frozen naming rule
  `^corpus_[0-9a-f]{8}_(graph|hrag|naive|schemas)$`.

Every other Qdrant collection is an ambient co-tenant observation. Its before
and after count and any per-collection delta remain visible in every receipt,
but it has no RED/GREEN verdict authority. A change in any allowlisted count
is RED. A missing or malformed scope version, recipe hash, protected count,
ambient partition, or total reconciliation is also RED. This is a prospective
scope correction: earlier global-v1 receipts remain immutable and are never
relabeled.

The triggering Phase-1C receipt therefore remains RED because it observed a
one-point `hermes_memories` change. Read-only attribution proved that point was
written by the host-side Hermes/mem0 co-tenant, not the paid backend. A new,
zero-provider v2 postflight referenced the RED receipt and senior ruling,
revalidated the frozen quality/cost ledger, observed no protected drift, and
then wrote the Phase-1C release marker.

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
 "prompt_version": "parent-digest.v5", "prompt_hash": "...",
 "repair_prompt_version": "parent-digest-repair.v2",
 "repair_prompt_hash": "...",
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
