# PROGRESS — executor cursor (Codex updates after EVERY task)

- Mission: CODEX_MISSION.md (CP2 → CP12)
- Track: A (core spine)
- Current task: A4/T9.3 sentence-hybrid v3 canary execution. The strict v3
  model/builder/validator, deterministic sentence→atomic expansion, frozen
  selector, and credential-blind live preflight are green. The senior issued
  exact selected-ten GO at 2026-07-15T03:39:13Z for selection `6aed7b1a...`
  and hard authority `$0.78260930`; no call has yet been made under that GO.
- Next task: implement and seal the v3-specific paid runner against the exact
  preregistered hashes/authority, then execute only the ten authorized packets.
  Stop on any seal or preregistered bar failure; Phase 2 remains separately
  gated by canary receipt, three-digest sample, and the owner window.
- Parked: T9.3 Phase 2, the owner sample window, and the five-parent bounded
  tail. Phase 2 is sealed because B4 failed; the prior 727-packet standing GO
  and `$31.1917106` authority are void. Any future paid lane requires the
  reservation control, corrected two-attempt authority, approved v3, and a
  fresh senior GO. The tail remains after a future corpus-wide certified
  acceptance of at least 95%.
  T9.4's pinned deployment, actual PoC-pair engine comparison, 100/500/5,000
  gates, live readiness wiring, full lexicon-projector parity, and production
  stamp remain open under the senior's production boundary. Ecom reingest/
  extraction remains separately owner-gated.
- Owner decisions received: owner selected Lane B (fix-then-buy) after the
  zero-spend sample exposed 8/66 bare headings and whole-parent evidence. Owner
  also authorized a later full E2E on ~15 deterministic files from
  `/Users/king/Desktop/hermes agent/ECOMMERCE/pdf` into a fresh test corpus
  after modular completion + RunPod blue-green parity; this is not approval to
  reingest or mutate the existing ecommerce corpus. Predicate normalization v1
  and ClaimRecordV1 field sets remain owner-ratifiable.
- Last completed subgate: T9.3 ordered-unit v3 zero-provider preflight green.
  The live population is 793 ready + the same two no-claim-child exclusions,
  with all 30,694 sentences present (24,845 mapped / 5,849 context-only), zero
  drops, max 25,601 bytes, and exactly three packets >20KB. Packet/schema/
  selection hashes are `89ace7ed...` / `5c600d30...` / `6aed7b1a...`.
  Selected authority is `$0.78260930`; max-any-ten is `$0.83466680`; the
  remaining cumulative umbrella is `$47.25116250`. Host tests are 28/28;
  backend and ingest-worker canonical overlays are each 27 passed + one
  expected trained-spaCy skip; Black/compile/diff and live census are green.
  Earlier B4 execution/diagnosis remains closed **failed**. The
  senior's frozen population hash was
  `sha256:00960dbeb9d1704421a79ea1abd3b71112e316c66143b2cfe507c709c624bf04`
  and selection hash
  `sha256:55ab1e846c40ef2e3a233a01f3333758b9660451b3237241f1976e271d9f203f`.
  Final durable state is 4 accepted / 5 structural DLQ / 1 unclaimed, 15
  calls, `$0.45429295` cost, and a `$0.02433870` hard-ceiling overage caused by
  `ceiling_guard_missing_reservation`; within-authority acceptance was 3/8 and
  strict faithfulness 2/4. All ten failed attempts were zero-byte empty tool
  arguments. Protected canonical stores are exactly unchanged. The shared
  two-attempt reservation and authority formula passes 57/57 host and 57/57
  canonical tests plus Black/compile/diff. Read-only v3 measurement covers
  793+2 parents and finds sentence→atomic mapping coverage 80.944158%; the
  optional-ID ordered-unit shape is p50 13,930 bytes, max 25,613 bytes, with
  maximum-any-ten authority `$0.83486975` under the corrected envelope.
- Last update: 2026-07-15T03:39:13Z (executor)
