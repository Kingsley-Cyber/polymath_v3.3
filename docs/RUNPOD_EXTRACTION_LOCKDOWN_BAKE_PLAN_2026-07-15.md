# RunPod extraction lockdown — bake design

Date: 2026-07-15
Status: preregistered design; **no image built or endpoint changed by this note**

## Outcome and boundary

Bake the certified deterministic child-extraction lane into
`runpod_flash_extractor`, deploy it blue-green, and prove one comparison:
the new RunPod output is equivalent to the pinned-local reference on the same
frozen chunks. RunPod owns extraction compute; the certified API gateway owns
the 15-document test corpus's summaries/digests. The worker remains stateless
and credential-free and cannot write MongoDB, Qdrant, or Neo4j.

The active artifact is strict `LocalExtractionV1`. GLiNER produces controlled
entity mentions; spaCy plus the predicate registry produces sentence,
predicate, negation, modality, condition, and temporal observations; Python
performs selection, identity, normalization, validation, and claim
compilation. The existing `polymath.runpod_gliner_relex.v3` endpoint is a
rollback surface, not the locked reference contract.

T8.5's published frozen verdict is `without_wins`: GLiREL proposals remain
observation-only and CP9 consumes the deterministic spine. Therefore GLiREL
will not be an active relation authority in this image and the locked
`LocalExtractionV1.relations` field remains empty under the current
disposition. Re-enabling GLiREL requires a new owner-ratified evidence class;
it cannot enter through this deployment task.

## Image contents and immutable identity

The image will contain only the extraction-side source closure copied from the
published repository, plus the exact model/runtime assets below:

- `models/local_extraction.py`, `models/hash_taxonomy.py`, the identifier and
  evidence helpers reached by the compiler, and strict registry loading;
- `services/ingestion/semantic_observations.py` and the deterministic
  `LocalExtractionV1`/claim-spine compiler boundary;
- `registries/extraction_vocabularies.v1.json` and
  `registries/predicate_normalization.v1.json`;
- a product GLiNER-mention adapter extracted from the already-certified C2
  selection logic: controlled labels only, confidence-first deterministic
  tie-breaking, exact-offset validation, same-span deduplication, and overlap
  rejection; it will contain no fixture IDs, gold labels, or eval branching;
- existing v3 temporal surface capture only where it is contract-equivalent to
  the deterministic local observation lane; resolution remains backend-side;
- worker request validation, bounded batching/windowing, model provenance,
  and a response envelope carrying `LocalExtractionV1` plus runtime and asset
  hashes.

Of the semantic gateway's permanent 49-file runtime closure, only the files
reachable from this extraction boundary are admissible. Provider routing,
prices, summary schemas, domain/frame/motif generation, paid-job code, Mongo
settings, and API keys are explicitly excluded. A generated import-closure
manifest will name every included repo file and SHA-256; an undeclared import
or missing declared file fails the bake.

Certified pins (from the frozen C2 gate and its published receipt):

| Component | Required identity |
|---|---|
| Python | `3.11.15` exact; a platform that cannot select or attest this patch fails the bake |
| spaCy | `3.8.14` |
| spaCy model | `en_core_web_sm==3.8.0`; wheel SHA-256 `1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85` |
| GLiNER | `0.2.26` |
| GLiNER model | `urchade/gliner_medium-v2.1` revision `40ec419335d09393f298636f471328b722c6da9e` |
| GLiNER config | SHA-256 `a8f3c2ecc57deb70077be6940962aa60e82d861a153a5cd2839b91795968ae7d` |
| GLiNER weights | SHA-256 `922214c0c60f7835bb5c00f52ad1769d38518d5183f85de7bc03893a8403c023` |
| Numerical/runtime closure | `torch==2.12.0`, `transformers==4.57.6`, `tokenizers==0.22.2`, `numpy==2.2.6`, `safetensors==0.7.0`, `sentencepiece==0.2.1`, `huggingface_hub==0.36.2`, `pydantic==2.13.4` |
| Deployment tool | `runpod-flash==1.18.0` |

No compatible ranges are permitted in the locked image. If Flash cannot
resolve one exact pin or cannot expose the built image/template identity, the
bake stops; the pin is not loosened. The receipt must bind: commit, source
closure hash, frozen registry hashes, model hashes, lock/freeze hash, remote
image/template digest or immutable platform artifact ID, and endpoint ID. If
RunPod exposes no immutable image identity, that is a blocker rather than an
inferred hash.

## Wire and compatibility

The new additive wire stamp will be `polymath.runpod_local_extraction.v1`.
Each task supplies exact `document_id`, `child_id`, source-version identity,
text, and the frozen registry/model identities; no corpus or fixture-specific
labels are accepted. Each result validates as strict `LocalExtractionV1`,
round-trips every emitted span against the request text, closes all internal
references, and returns the observed runtime/model/source hashes.

The backend adapter will accept the new contract only on the blue-green test
route. Existing v2/v3 handling stays intact until cutover and provides
rollback. No dual write targets an existing corpus.

## Blue-green topology

The standing endpoints `m2ric3stpsh11d` and `pitae1qruu59ne` are read-only
blue surfaces during this work. Deployment creates new green endpoint IDs
from actual Flash responses; IDs are never guessed or hardcoded. Green starts
at zero minimum workers, one inference at a time per worker, and the existing
bounded execution timeout. A synthetic canary runs directly against green
before the backend can route to it. Failure means delete/park green and retain
blue; never redeploy in place.

RunPod keys remain encrypted in Mongo settings and are read only by the
existing credential boundary. Commands and receipts may show account/endpoint
names, job IDs, timings, and costs, but never tokens or request headers.

## Preregistered bake/deploy gates

Every command uses a true exit-code wrapper and records command, output tail,
and `EXIT`.

1. **B0 — source/pin closure:** generated import closure has no undeclared or
   missing file; frozen registry/model hashes and exact package pins match;
   secret scan and compile are green; focused extraction/claim tests pass.
2. **B1 — deterministic local reference:** run the frozen same-chunk set twice
   in the certified local venv; normalized outputs are byte-identical and all
   exact-offset/reference/schema checks pass.
3. **B2 — image identity:** build with exact pins; capture immutable image or
   platform artifact identity, dependency freeze, source/model/registry hash
   manifest, non-root/read-only capability where Flash exposes it, and zero
   database/provider credentials.
4. **B3 — green deploy:** re-read `COORDINATION.md`; create green beside blue,
   never in place; record actual endpoint/template IDs and prove blue IDs are
   unchanged.
5. **B4 — synthetic canary:** one bounded request returns the new wire stamp,
   strict `LocalExtractionV1`, correct controlled labels, exact spans,
   negation/modality/predicate output, and temporal phrases including an
   event-period form; malformed contract, out-of-registry label, and bad
   source identity fail closed.
6. **B5 — single same-chunk equivalence comparison:** compare only
   `pinned_local` versus `runpod_green` on the preregistered frozen set. Exact
   equality is required for schema, IDs, text/spans, controlled types,
   canonical labels, predicate normalization, negation, modality, sentence
   references, unresolved spans, empty relation disposition, and ordering.
   GLiNER confidence floats may differ only within a preregistered absolute
   `1e-5` device tolerance; every threshold-side selection must still be
   identical. Any semantic mismatch fails the gate.
7. **B6 — retry safety:** replay the identical request/batch identity after a
   forced client retry; output artifact hashes are identical, no duplicate
   durable artifact is created, and the worker performs zero direct writes.
8. **B7 — cutover readiness receipt:** publish all prior evidence and rollback
   command. Cutover occurs only after senior verification; the old endpoints
   remain available until the fresh-corpus E2E passes.

The earlier 5,000-chunk production gate remains a P2.7 scale gate, but the
later owner/senior finish-line order places no work ahead of the 15-document
E2E beyond bake, blue-green canary, and the small same-chunk comparison. It is
therefore not silently inserted before the owner-requested E2E; any ruling to
restore it to that position will be recorded before launch.

## Frozen comparison and E2E handoff

Before inference, publish the small comparison fixture manifest and hashes.
It will use the nine existing semantic-extraction gold chunks plus bounded
general synthetic cases for long-window overlap, negation/modality, and
temporal capture. Targets test contracts and parity only; gold answers never
branch inference.

After B7, a separate preregistered receipt deterministically selects 15 of the
75 non-AppleDouble files in
`/Users/king/Desktop/hermes agent/ECOMMERCE/pdf` across byte-size and topic
bands. The E2E creates a new corpus discovered from API responses and performs
chunk → green RunPod extraction → instructed embeddings → graph → certified
API summaries. It writes nothing to existing corpora. Retrieval targets,
three tiers, lay-language and relationship questions, negatives, summary
spend ceiling, and stop rules are published before that run.

## Stop conditions

Stop on any pin/hash mismatch, non-equivalent same-chunk artifact, endpoint
mutation, unknown image identity, credential exposure, worker-side durable
write, retry non-idempotence, unexpected provider call, write to an existing
corpus, or exceeded published spend ceiling. Gates are not weakened and
failed outputs are retained as receipts.
