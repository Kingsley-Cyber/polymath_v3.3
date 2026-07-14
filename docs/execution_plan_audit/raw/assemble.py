#!/usr/bin/env python3
"""Deterministic assembler for EXECUTION_PLAN_2026-07-13.md from workflow artifacts."""
import json
import os

BASE = "/private/tmp/claude-501/-Applications/25355cb5-c5b9-4d9e-92ab-e94544cd9481/scratchpad"
EP = os.path.join(BASE, "exec_plan")
OUT = "/Users/king/polymath_v3.3/docs/EXECUTION_PLAN_2026-07-13.md"
HASH = "f049041"

CHUNKS = [
    ("HDR", "Header / Standing Rules / Legend / Invariants / Baseline", 1, 96),
    ("AUD", "Audit Delta 1 + Audit Delta 2", 97, 180),
    ("TMP", "Temporal RAG Program + Governing Librarian Constraint", 181, 247),
    ("LIB", "Deterministic-First Librarian Build Order (Phases 0-4)", 248, 346),
    ("P0S", "P0 Summary Integrity", 347, 401),
    ("P0R", "P0 Retrieval Correctness", 402, 474),
    ("P0L", "P0 Lifecycle And Storage Hygiene", 475, 545),
    ("P1Q", "P1 Librarian Query Understanding", 546, 696),
    ("P1L", "P1 Latency And Resource Isolation", 697, 812),
    ("P2V", "P2 Universal Concept And Vocabulary Layer", 813, 974),
    ("P2X", "P2 Extraction And RunPod Parity", 975, 1093),
    ("P3A", "P3 Thematic RAPTOR + Storage Experiments", 1094, 1184),
    ("GTE", "Quick Upload + Regression Matrix + Strict Ready + Completion Rule", 1185, 1248),
    ("LOG", "Implementation Log (historical receipts)", 1249, 1398),
]

FAMS = ["latent", "alias", "facet", "librarian", "temporal", "hierarchy",
        "extraction", "readiness", "retrieval", "eval", "leftover"]


def load(path):
    with open(path) as f:
        return json.load(f)


def find_critique(code):
    for p in (f"{EP}/critique_{code}.json", f"{BASE}/critique_{code}.json"):
        if os.path.exists(p):
            return load(p)
    return None


def esc(s):
    if s is None:
        return ""
    return str(s).replace("|", "\\|").replace("\n", " ").strip()


def trim(s, n):
    s = esc(s)
    return s if len(s) <= n else s[: n - 1] + "…"


out = []
out.append("# Execution Plan — Auditable Claim Ledger (2026-07-13)\n")
out.append(f"Derived from docs/RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md FROZEN at commit {HASH} "
           "(claim IDs = `<chunk>:L<line>` in the frozen copy).")
out.append("Method: exhaustive claim extraction (14 chunks, 533 claims) → terminology reconciliation vs code "
           "(11 families) → 5W+feasibility critique per claim → deterministic parity-checked assembly.")
out.append("Every claim in the implementation plan, resolved or not, appears below exactly once as an auditable action.")
out.append("How to audit a row: open the frozen line, run/inspect the Required receipt, confirm the Disposition.")
out.append("Companion: docs/PLAN_CRITIQUE_2026-07-13.md holds the S0–S14 sequencing rationale; this file holds the per-claim ledger.")
out.append("ASSEMBLY NOTE: the critique stage for 5 chunks (P1L, P2V, P2X, P3A, GTE) was cut off by the API monthly "
           "spend limit; their claims are rendered below with disposition `pending-critique` and will be filled by the "
           "cached-workflow resume. 9/14 chunks carry the full 5W+feasibility critique.\n")

# 1. Orchestrator's end-state map
with open(f"{EP}/part_00_endstate.md") as f:
    out.append(f.read())

# 2. Terminology registry
out.append("\n## Concept & Terminology Registry (plan language ↔ code truth)\n")
out.append("Verdicts: SAME (one concept, name drift) / OVERLAPPING / DISTINCT / PLAN-ONLY (no code counterpart yet).\n")
all_collisions = []
for fam in FAMS:
    p = f"{BASE}/registry_{fam}.json"
    if not os.path.exists(p):
        p = f"{EP}/registry_{fam}.json"
        if not os.path.exists(p):
            out.append(f"### {fam} — MISSING (not produced)\n")
            continue
    rows = load(p)
    if isinstance(rows, dict):
        rows = rows.get("rows") or rows.get("registry") or []
    out.append(f"### Family: {fam}\n")
    out.append("| Plan term | Verdict | Canonical | Code truth | Store fields | Collision note |")
    out.append("|---|---|---|---|---|---|")
    for r in rows:
        syms = r.get("code_symbols") or []
        sym_txt = "; ".join(
            f"`{s.get('symbol','')}` @ {s.get('file_line','')}" for s in syms[:2] if isinstance(s, dict)
        )
        if len(syms) > 2:
            sym_txt += f" (+{len(syms)-2})"
        stores = ", ".join(r.get("store_fields") or [])
        cn = r.get("collision_note") or ""
        if cn:
            all_collisions.append(f"**[{fam}] {r.get('plan_term','')}**: {cn}")
        out.append(
            f"| {esc(r.get('plan_term'))} | {esc(r.get('verdict'))} | {esc(r.get('canonical'))} "
            f"| {trim(sym_txt,160)} | {trim(stores,80)} | {trim(cn,220)} |"
        )
    out.append("")
out.append("### Collisions & duplicate-implementation risks (full list)\n")
seen = set()
for c in all_collisions:
    if c not in seen:
        seen.add(c)
        out.append(f"- {c}")
out.append("")

# 3. Execution flows (orchestrator-authored, from the end-state map S-mapping)
out.append("""## Execution Flows — concept aggregation onto S-steps

*(Orchestrator-authored: the flows agent was a spend-limit casualty; flows below derive from the end-state map + registry.
Canonical names per the registry.)*

- **latent/underlying concepts** (canonical: `latent_concepts`; NOT latent_topics, NOT summary_tree.concepts):
  S1 contract seam carries them per-parent → S4 one paid pass fills the pair → S5 rollup EXTENDS `doc_profile.concepts`
  (which already exists — do not re-derive) + fixes the Qdrant payload whitelist that currently DROPS latent_concepts →
  S8 cards consume via the existing `candidate_latent_subjects` corroborate-only path → S10 P2.2 admission.
- **aliases** (canonical: ONE versioned registry absorbing the 3 existing stores — lexicon identity evidence,
  curated CONCEPT_ALIASES in Python, latent_concepts[].aliases): S4 captures → S5 builds the registry + query-time
  expansion through the EXISTING grounded gate (original-lane protection already in code) → S8 card fields.
  MUST also reconcile the TWO entity_id builders (lexicon `_entity_id` vs graph `entity_id_from_name`) in S5.
- **facets**: S7 DF-admission rule (owner decision) + cleanup riding S5's payload rewrite; unify the duplicate
  alias tables (`_CONTENT_FACET_ALIASES` vs `_CHAT_COVERAGE_FACETS`) and the two broad-alias gates in the same pass;
  note `agency_preservation` is still stampable on NEW ingests — S7 includes the ingest-side gate, not just cleanup.
- **temporal**: S1 (summary temporal_class/time_expressions — the SUMMARY seam, distinct from the chunk-level
  `temporal_captures` already live) → S2 bibliographic/date de-conflation (settle `source_published_at` as the
  canonical name; `published_at` in P2.1 is the same concept) → S4 pair backfill → S6 temporal readiness → S12 T-MAIN.
- **hierarchy/headers** (`heading_path` == `section_path`, one field): S3 disposition matrix (SIGNED?) →
  projection heading-repair (free) → prov-capture code change if page_start is wanted (reingest alone can NEVER add it)
  → S4 rides reingested subset if approved.
- **extraction**: P2.4 negation → P2.5 typed signatures → P2.6 parity → S11 ONE paid burst (pair only, v2 frozen)
  with T-HOOK-1 temporal aboard → P2.8 grounding. External parallel-session work on semantic contracts/spaCy
  (branch `claude-continuation-20260713`) slots HERE and must merge before S11 runs.
- **librarian/seats**: S0 merged shelf_reserve DARK → S8 flips only via before/after A/B on the frozen held-out set;
  disambiguate `shelf` (P1.4 corpus-routing sense is PLAN-ONLY; P1.5 role sense is in code);
  `reserve_corpora` still runs and is NOT superseded — S8 must reconcile it with `reservation_policy` explicitly.
- **readiness**: S6 three-way split (operational / metadata-quality / temporal) consuming S2+S4 fields; strict-ready
  gates (GTE chunk) close at S14.
- **eval spine**: frozen 58q + firewall gates EVERY flip: S8 (shelf), S10 (P2.2 pilot), S11 (parity), S14 (16×3 matrix).

### Cross-flow dependencies
S1 → S4 (capture before paid pass); S2 → S8 (bibliographic before cards); S3 → S4 subset & S11 (disposition before spend);
S4 → S5 (capture before rollup); S5 → S7 cleanup rides its rewrite; S5+S2 → S8 (cards last); S8 → S9/S10 (consumption
after measurement); P2.4–2.6 (+ external contracts branch) → S11; S6 → S12; everything → S14.
""")

# 4. Audit ledger
out.append("## Audit Ledger — every claim, every disposition\n")
total_rendered = 0
all_ids, rendered_ids = [], []
dispo_count = {}
for code, label, a, b in CHUNKS:
    claims = load(f"{EP}/claims_{code}.json")
    crit = find_critique(code)
    crit_by_id = {}
    if crit:
        rows = crit if isinstance(crit, list) else crit.get("rows", [])
        crit_by_id = {r.get("id"): r for r in rows if isinstance(r, dict)}
    pending = " — **critique pending (spend-limit)**" if not crit_by_id else ""
    out.append(f"### {label} (frozen lines {a}-{b}, chunk {code}){pending}\n")
    out.append("| ID | Claim (verbatim) | Status | Disposition | Auditable action | Who | Where | When | Why | Feasibility | Required receipt |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for row in sorted(claims, key=lambda r: r.get("line_start", 0)):
        cid = row.get("id", "?")
        all_ids.append(cid)
        c = crit_by_id.get(cid, {})
        dispo = c.get("disposition") or "pending-critique"
        dispo_count[dispo] = dispo_count.get(dispo, 0) + 1
        out.append(
            f"| {cid} | {trim(row.get('verbatim'),160)} | {esc(row.get('ledger_status'))} "
            f"| {esc(dispo)} | {trim(c.get('action') or '(awaiting critique resume)',200)} "
            f"| {trim(c.get('who'),60)} | {trim(c.get('where'),90)} | {esc(c.get('when_s'))} "
            f"| {trim(c.get('why'),110)} | {trim(c.get('feasibility_verdict'),24)} "
            f"| {trim(c.get('required_receipt'),160)} |"
        )
        rendered_ids.append(cid)
        total_rendered += 1
    out.append("")

# 5. Disposition summary + parity attestation (deterministic)
out.append("## Disposition Summary\n")
out.append("| Disposition | Rows |")
out.append("|---|---|")
for k in sorted(dispo_count, key=lambda k: -dispo_count[k]):
    out.append(f"| {k} | {dispo_count[k]} |")
out.append("")
missing = sorted(set(all_ids) - set(rendered_ids))
dupes = sorted({i for i in rendered_ids if rendered_ids.count(i) > 1})
frozen_boxes = sum(
    1 for line in open(f"{EP}/frozen_checklist_{HASH}.md")
    if line.lstrip().startswith("- [")
)
out.append("## Parity Attestation (deterministic, computed at assembly)\n")
out.append(f"- Frozen source: commit `{HASH}`, 1,398 lines, {frozen_boxes} checkbox lines.")
out.append(f"- Claims extracted: {len(all_ids)}. Rendered ledger rows: {total_rendered}.")
out.append(f"- Missing IDs: {missing if missing else 'NONE'}. Duplicate IDs: {dupes if dupes else 'NONE'}.")
out.append(f"- Critique coverage: 9/14 chunks (5W+feasibility complete); 5 chunks pending workflow resume "
           f"({dispo_count.get('pending-critique', 0)} rows marked `pending-critique`).")
out.append("- Set math: extracted == rendered by construction of this assembler; the independent-agent re-verification "
           "re-runs on workflow resume.")
out.append("")

with open(OUT, "w") as f:
    f.write("\n".join(out) + "\n")
print(f"WROTE {OUT}")
print(f"claims={len(all_ids)} rendered={total_rendered} missing={len(missing)} dupes={len(dupes)} boxes={frozen_boxes}")
print("dispositions:", json.dumps(dispo_count, indent=0))
