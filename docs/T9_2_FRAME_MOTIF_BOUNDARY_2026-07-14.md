# T9.2 Frame-Role + Motif-Matcher Boundary

Status: CONFIRMED by senior rulings 2026-07-14T18:34:02Z and
2026-07-14T18:36:03Z; registry freeze and implementation authorized. New
recipes remain executor-proposed, owner-ratifiable.

## 5Ws

- **What:** convert one T9.1 predicate→superframe rule match and its source
  ClaimRecordV1 into a lossless, role-bound frame candidate, then match ordered
  frame windows against the 12 owner motifs through the approved set-valued
  stage bindings. Emit sequence and role-continuity scores separately.
- **Why:** a frame ID without real argument bindings cannot prove mechanism
  continuity, while a frame-ID sequence without participant threading can
  manufacture thematic false positives. The conservative boundary preserves
  exact evidence and abstains from frame-specific role semantics the owner
  registry does not yet define.
- **Who:** Python is final matching authority. ClaimRecordV1 supplies source
  arguments and qualifiers; the T9.1 rule match supplies the candidate MF;
  callers supply explicit thread keys and sequence order; owner motif and
  stage-binding snapshots define canonical sequences and admissible frames.
- **Where:** local, candidate-only, annotate-only artifacts. No provider,
  Mongo, Neo4j, Qdrant, projection, outbox, or retrieval activation.
- **When:** after T9.1 and before the gated T9.3 parent digest. The output can
  seed later candidate paths but is not the accepted Final Schema layer.

## Authority gap

`superframe_registry.v1.json` supplies stable MF IDs and names but no required
or optional frame-specific role inventory. The Final Schema says such roles
belong in the registry; inventing them in Python would violate the registry
boundary. Proposed v1 therefore binds only lossless relation direction:

| Claim argument | Frame binding | Meaning claimed |
|---|---|---|
| subject | source | the compiled relation's source endpoint |
| object | target | the compiled relation's target endpoint |

These are not causal-agent, instrument, outcome, baseline, or other
frame-specific semantic roles. A future owner-approved role registry can add
those meanings through a new recipe/schema version.

Every binding retains the source claim ID, argument role, filler kind/ref,
span observation ID, exact evidence sentence ID, surface offsets, and an
explicit caller-supplied thread key. Thread keys have no implicit fallback:
the caller may explicitly choose the real filler ref or an authorized CP5
canonical ID. Surface similarity and alias inference are forbidden.

The frozen ClaimArgumentV1 role vocabulary is exactly `subject|object` under
`extra="forbid"`, so v1 binds every currently legal argument and reports
`unbound_argument_count=0` as definitional. A hard contract check pins that
vocabulary. Any future owner-approved participant role must break loudly and
force a FrameInstance v2 that retains/counts it; T9.2 does not pre-invent an
extra argument lane.

## Proposed strict sequence recipe v1

1. Candidate windows contain every canonical motif stage in order and are
   contiguous: no missing stages and no intervening frames.
2. A stage accepts any owner-approved binding row. Dominant/admissible tier is
   retained as a raw component; query-mode strictness remains serving policy.
3. No unregistered substitution is allowed. Motif qualifiers never become
   stages.
4. `sequence_alignment = matched_stage_count / canonical_stage_count`. Under
   strict v1 every emitted sequence-aligned observation is 1.0; this is an
   honest coverage measure, not a second final-ranking score.
5. Callers provide contiguous nonnegative `sequence_index` values. Hash IDs
   never determine order.

The strict zero-tolerance policy is still versioned recipe data. Evidence may
justify a future version with gaps, missing/optional stages, or substitution;
v1 does not guess those tolerances.

## Proposed exact role threading v1

For each adjacent matched pair:

1. an exact prior-target thread key shared with the next source is
   `directional`;
2. otherwise any exact shared thread key is `shared_participant`;
3. otherwise the transition is `disconnected`.

`role_continuity = connected_transition_count / total_transition_count`, kept
separate from sequence alignment. Disposition is threshold-free:

- every transition connected → `confirmed_candidate`;
- some but not every transition connected → `provisional`;
- no transition connected → `rejected`.

All states remain annotate-only. `confirmed_candidate` means the deterministic
matcher confirmed its own recipe, not that the semantic artifact was accepted
or promoted.

M12's `UNDER CONDITION` qualifier is claim-level, not a stage. At least one
matched MF04 claim must retain a nonempty condition. Without one, the complete
sequence observation is retained rejected with `required_condition_missing`.

## Coverage honesty

The matcher contract can interpret all 12 owner motifs. The current T9.1
predicate lane reaches only MF02/MF03/MF04/MF06/MF07/MF09/MF15/MF16, so it
can deterministically realize only M03, M08, M09, and M12 (4/12). No other
motif may be fabricated until another authorized, role-bound frame lane exists.

## Required proofs

- every frame binding round-trips to a real ClaimArgumentV1 and evidence ID;
- the ClaimArgument role vocabulary is exactly `subject|object`, every current
  argument binds, and definitional unbound count remains zero;
- missing/extra thread-key mappings hard-error; no surface fallback;
- unknown claim/rule/frame/motif/stage IDs hard-error;
- approved admissible stage bindings match but retain their tier;
- missing, gapped, reordered, or unapproved frame sequences do not match;
- directional/shared/disconnected transition classification is exact;
- sequence and role scores remain separate and replay-identical;
- M12 without a real claim condition is rejected with evidence;
- no accepted state, provider call, spend, or durable write exists in T9.2.
