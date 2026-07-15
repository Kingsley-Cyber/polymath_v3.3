# T9.3 Sentence-Hybrid v3 Preflight Receipt — 2026-07-15

## 5Ws

- **Who:** Codex executor under the senior ruling at
  `COORDINATION.md:2026-07-15T03:02:04Z`.
- **What:** strict `semantic_parent_packet.sentence_hybrid.v3`, deterministic
  sentence→atomic expansion, frozen stratified selection, and credential-blind
  live preflight.
- **When:** 2026-07-15 UTC, before the senior's separate canary GO.
- **Where:** `markbuildsbrands_transcripts`, noncanonical candidate inputs only;
  protected Mongo/Qdrant/Neo4j stores were census-checked and unchanged.
- **Why:** claims-only v2 deterministically produced empty tool arguments and
  failed faithfulness. V3 restores ordered prose while limiting citations to
  evidence sentences with real local atomic mappings.
- **How:** every evidence sentence is emitted in source order; mapped units have
  an evidence-sentence `claim_id`, unmapped units have no ID and remain outside
  validator scope. Python expands accepted sentence citations to a sorted local
  atomic union, explicitly as expansion rather than model intent.

## Frozen contract

- Packet schema: `semantic_parent_packet.sentence_hybrid.v3`.
- Schema-contract hash:
  `sha256:5c600d3047807541a09be38d01933b6e048f5a3f730de1b5e2cf6c48991f2e40`.
- Provider serialization omits only absent/default optional entity metadata;
  every present entity value and every source sentence remains.
- Per-packet disclosure is `sentence_counts: {mapped, unmapped}`.
- Cap: 26,000 UTF-8 bytes; truncation and sentence dropping are forbidden.
- Context-only units have no `claim_id` field and cannot enter
  `SemanticValidationContext`.
- Expansion rejects context-only/unknown citations, duplicates, empty or
  unstable maps, cross-parent/child ownership, and stale compilation sets.

## Live population receipt

The final live receipt is `/tmp/t9_3_v3_live_preflight_final.log`, true
`EXIT=0`.

| Metric | Result |
|---|---:|
| Eligible parents | 795 |
| Packet-ready / excluded | 793 / 2 |
| Exclusion reason | both `source_child_without_atomic_claim` |
| Source sentences | 30,694 |
| Mapped / context-only | 24,845 / 5,849 |
| Dropped | 0 |
| Packet bytes min / p25 / p50 / p75 | 3,421 / 12,975 / 13,917 / 14,673 |
| Packet bytes p90 / p95 / p99 / max | 15,206 / 15,528 / 16,091 / 25,601 |
| Packets >20KB / >26KB | 3 / 0 |
| Packet-set hash | `sha256:89ace7ede4eab1d00f7f8d062b92d756cc5f7243fe4d0c3d0c7e0fec131b2d43` |

Protected canonical census was exactly unchanged. Provider calls, provider
credential plaintext reads, database writes, canonical writes, and projection
writes were all zero.

## Selection and authority

- Previously purchased unique parents: 81; fresh packet-ready population: 728.
- Selection: 10 unique documents, two per size band, with the largest eligible
  >20KB packet reserved before band fill.
- Final selection hash:
  `sha256:6aed7b1a967c1ad8889a0f058091e7f47691053d25185ff03cac797b3875f595`.
- Selected bytes: 152,090; selected >20KB count: 1.
- Exact selected two-attempt authority and summed per-claim reservation:
  `$0.78260930`.
- Max-any-ten: `$0.83466680`, below the approved `$0.83486975` design bound.
- All 793: `$59.91857894`; fresh 728: `$54.98061844`.
- Cumulative owner umbrella: `$49.45`; current ceiling basis:
  `$2.19883750`; remaining: `$47.25116250`.
- Current ordinal affordable prefix: 626 claims reserving `$47.21649988`;
  `$0.03466262` remains, while claim 627 requires `$0.07667363` and therefore
  cannot be dispatched at that boundary.

Expected spend was not used as authority. The preflight contained no paid
runner and did not authorize execution. The senior subsequently issued a
separate exact canary GO at `COORDINATION.md:2026-07-15T03:39:13Z`.

## Gates and receipt pointers

| Gate | Command | Result |
|---|---|---|
| Host pure | `PYTHONPATH=backend ... pytest test_semantic_digest_claim_inputs.py test_semantic_gateway_mark_sentence_hybrid_preflight.py` | 28/28, `EXIT=0`, `/tmp/t9_3_v3_pure_host_final_v2.log` |
| Backend canonical | `PYTHONPATH=/tmp/t93v3:/app python -m pytest ...` | 27 passed + 1 trained-spaCy skip, `EXIT=0`, `/tmp/t9_3_v3_pure_canonical_final_v2.log` |
| Ingest-worker canonical | same isolated overlay suite | 27 passed + 1 trained-spaCy skip, `EXIT=0`, `/tmp/t9_3_v3_pure_ingest_worker_final_v2.log` |
| Live preflight | `PYTHONPATH=/tmp/t93v3:/app python scripts/semantic_gateway_mark_sentence_hybrid_preflight.py` | population/cost/census green, `EXIT=0`, `/tmp/t9_3_v3_live_preflight_final.log` |
| Black | `python -m black --check` on six changed Python files | `EXIT=0`, `/tmp/t9_3_v3_black_final.log` |
| Backend compile | `PYTHONPYCACHEPREFIX=/tmp/... python -m compileall -q ...` | `EXIT=0`, `/tmp/t9_3_v3_compile_backend_final_v2.log` |
| Ingest-worker compile | `python -m compileall -q ...` | `EXIT=0`, `/tmp/t9_3_v3_compile_ingest_worker_final.log` |
| Diff hygiene | `git diff --check` | `EXIT=0`, `/tmp/t9_3_v3_diff_final.log` |

Disclosed failed attempts: the first live invocation used the non-baked `/app`
tree and failed before import (`EXIT=1`, no DB/call/write). The first complete
shape attempted 28,041 bytes on one parent because absent entity optionals were
serialized as null/default values; it failed closed (`EXIT=1`, zero calls and
writes). The corrected provider serializer preserved present metadata and
recovered the approved 793+2 population. The first backend compile attempt
failed only because the isolated overlay's `__pycache__` was root-owned;
external bytecode cache retry was green. No failed attempt reached a provider.

## Remaining gate

The senior has authorized only the frozen ten-packet canary. Its paid runner
must rederive the exact packet/schema/selection/prompt/repair hashes and
`$0.78260930` authority, prove the reservation guard before claim, remain
noncanonical, and stop at any preregistered bar failure. Phase 2 is not part of
this receipt.
