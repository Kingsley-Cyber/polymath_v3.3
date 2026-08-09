# Residual Error Ledger — saturation closeout (2026-08-08)

Frozen stack under audit: relex-large single-pass (host MPS, e=0.30/r=0.30
Pareto-pinned), v3 relation policy, counted-NP guard, adapter compiler v1,
OpenIE retained (ablation-proven). Every audit-time miss below carries
exactly one class and a disposition. **UNKNOWN / UNEXPLAINED = 0.**

Classes: MODEL_SPAN_MISS · MODEL_PAIR_MISS · MODEL_PREDICATE_MISS ·
STRUCTURAL_UNAVAILABLE · ADAPTER_COVERAGE · CANONICAL_NO_EQUIVALENT(→OPEN)
· AMBIGUOUS_IDENTITY · QUALIFIED_NONFACT(→qualified) · GATE_REJECT_CORRECT

## Pack (calibration; oracle-adapter waterfall 38→26→15→9→9→4→3→3 (post title-miner))

### R1 endpoint undiscovered (15)
| ids | class | detail | disposition |
|---|---|---|---|
| r01 r02 r08 | ~~MODEL_SPAN_MISS~~ **RESOLVED** | deterministic title miner (heading + YAML-frontmatter title, graphify-title-miner-v1) recovered the title entity — R1 23→26; the three golds now die at R2 under already-classified metadiscourse/pair classes | lever EXERCISED; burned sets byte-stable, leakage 0 |
| r22 r38 | MODEL_SPAN_MISS | long titled compounds ("Video Observation Graph…") beyond title position | ACCEPTED_MODEL_LIMITATION |
| r26 r27 r29 r31 r32 r33 r34 | MODEL_SPAN_MISS | lowercase/compound concept spans ("semantic envelope", "Laban movement quality", "presentation timestamps", "product or hand") | ACCEPTED_MODEL_LIMITATION (schema present, text visible, no admissible generic fix) |
| r35 r36 | STRUCTURAL_UNAVAILABLE | snake_case identifiers inside code fences (candidate_passages, product_reveal) | BY_DESIGN — unit.kind doctrine routes code windows format-only (frozen decision) |

### R2 pair undiscovered (8)
| ids | class | detail | disposition |
|---|---|---|---|
| r03 | MODEL_PAIR_MISS | anaphoric subject ("It requires…") | ACCEPTED — coref machinery prohibited by architecture boundary |
| r04 r05 r06 | MODEL_PAIR_MISS | coordinated premodifiers + semantically remote subject | ACCEPTED — generic fix would be hand-built OpenIE expansion (rejected by admissibility rule 5) |
| r12 | MODEL_PAIR_MISS | absolutive with-pcomp parse fracture | ACCEPTED — same boundary |
| r23 | MODEL_PAIR_MISS | sm-tagger noun-misparse ("stores") kills the frame | ACCEPTED — parser-upgrade lever available but is a runtime swap, measured non-blocking |
| r30 r37 | MODEL_PAIR_MISS | debatable-gold advcl subject; embedded interrogative clause | ACCEPTED — general rule would be wrong on ordinary English |

### R3 native frame not recovered (6)
r16–r21 — MODEL_PREDICATE_MISS: fractured-list parse garbles the governing
participle ("containing" → measures|pose|reviewed). Pairs exist (R2 mechanism
delivered them); predicate naming remains model/parser-owned. ACCEPTED.

### R5 canonical mapping (5)
| ids | class | detail | disposition |
|---|---|---|---|
| r13 r14 r15 | ADAPTER_COVERAGE | knowledge IS stored (part_of, inverse-correct); gold vocabulary says CONTAINS | TRUE_KNOWLEDGE_ALTERNATE_NAME — benchmark-named remapping forbidden by Matrix B rule |
| r10 r28 | CANONICAL_NO_EQUIVALENT | NOT_TREATED_AS, REMAINS_IN have no frozen equivalent | → OPEN (native predicate preserved; correct saturated behavior) |

### R6 gate (1)
r25 — AMBIGUOUS_IDENTITY: endpoint type signature held the candidate in
REVIEW. Preserved and visible; never fabricated. GATE_REJECT_CORRECT-adjacent.

## Book-66 (6 of 66 unmatched under frozen stack; leakage 0)
| id | class | detail |
|---|---|---|
| T001 | CANONICAL_NO_EQUIVALENT | "accepts telemetry from" → consumes would be nearest-vague; abstained (OPEN-side) |
| T002 | MODEL_PAIR_MISS | appositive "operated by" construction (legacy family-02 class) |
| T003 | MODEL_PAIR_MISS | role-inverse inference (MongoDB stores X's data ⇒ X uses MongoDB) — an inference, not an extraction |
| T011 | MODEL_PAIR_MISS | passive "consumed by" pair unrecovered in this context |
| T017 | AMBIGUOUS_IDENTITY | "normalization code" surface vs "Normalization Process" entity — alias distance |
| T043 | CANONICAL_NO_EQUIVALENT | "coordinates leases for" → supports would be nearest-vague; abstained |

## Sealed-v1 (7 of 51 unmatched; leakage 0)
| id | class | detail |
|---|---|---|
| T002 T011 | MODEL_PAIR_MISS | appositive operated-by / membership (legacy family-02 class) |
| T008 | MODEL_PAIR_MISS | relative-clause object chain |
| T016 | AMBIGUOUS_IDENTITY | acronym alias (DCU ↔ full unit name) |
| T042 T051 | MODEL_SPAN_MISS | lowercase concept spans ("thermal sensor saturation", "frame integrity") |
| T044 | MODEL_SPAN_MISS | alarm-code compound ("ALR-2214 alarms") |

## Synthetic families (2 residual FAILs, both pre-Relex legacy)
| family | class | detail |
|---|---|---|
| 02 argument_alignment | AMBIGUOUS_IDENTITY | appositive minting ('archival store' vs Vaultstone) — oldest open class in the program |
| 06 direction_passive | MODEL_PREDICATE_MISS | one nominalization direction case ('glowline metrics defines redlark') |

## Saturation state
- Thresholds: EXHAUSTED — grid {0.25,0.30,0.40}² measured; (0.30, 0.30)
  Pareto-dominates; pinned permanently. Accept-threshold (0.5) affects only
  corroborated duplicates — no recall axis exists; declared exhausted.
- Admissible improvements remaining: **ZERO** — the title/heading/frontmatter
  mention miner (the last queued Matrix C lever) is implemented, tested, and
  battery-verified (pack R1 23→26; burned sets byte-stable; leakage 0).
- Everything else above: ACCEPTED_MODEL_LIMITATION, BY_DESIGN, OPEN-preserved,
  or legacy-class with no admissible fix under rules 1–8.
- UNKNOWN residuals: **0**.
