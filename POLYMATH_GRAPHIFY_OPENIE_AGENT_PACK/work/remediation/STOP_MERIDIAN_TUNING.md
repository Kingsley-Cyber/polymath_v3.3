# STOP ORDER — OWNER DIRECTIVE (2026-08-06 23:15 MDT)

**To the agent running remediation cycles in this directory (cycle_00 → cycle_02 observed):**

The owner has ordered a halt to all score→edit→rescore iteration against the Meridian set
(`/Users/king/Downloads/graphrag_accuracy_test 2/`). Effective immediately:

1. **Do not run the Meridian scorer again.** Three scored exposures (22:29, 22:55, 23:00, 23:05) have
   already converted it from an independent evaluation into a development set. Every additional
   cycle repeats the exact contamination pattern documented in `/Users/king/polymath_v3.3/audit/FORENSIC_AUDIT_REPORT.md`
   (16 correction cycles burned the technical-book-66 set earlier today).
2. You MAY continue fixing general defect classes (projection slugging, claim-noun attribution scope,
   argument alignment, eligibility generation) — but verify them ONLY against synthetic regression
   fixtures you author for the general class, plus the already-burned book-66 development set.
3. Qualification claims require a NEW, sealed, never-inspected fixture+key, scored exactly once via
   the canonical worker path, after code + scorer + policy + environment freeze. Neither book-66 nor
   Meridian can ever serve that role again.
4. Log what you changed and stop. The forensic audit is complete; read it before further work:
   `audit/FORENSIC_AUDIT_REPORT.md`, `audit/timeline.md`, `audit/scorer_integrity.md`, `audit/change_inventory.json`.

— Independent forensic auditor, on owner instruction.
