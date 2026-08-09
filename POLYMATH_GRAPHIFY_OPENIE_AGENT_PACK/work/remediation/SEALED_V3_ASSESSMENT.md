# Sealed-v3 (meeting transcript) — assessment vs owner key, 2026-08-07

Extraction was digest-locked (4c833f05…) before the key existed; owner supplied the key
in-chat (owner's seal-break; extraction provably uncontaminated). Key uses a custom
18-entity/13-predicate schema outside the frozen ontology — assessed for accuracy, not gated.

## Entities: 13/18 exact-name (72%), 15/18 lenient (83%)
FOUND: Jordan Lee, Priya Nair, Daniel Ortiz (person✓), Legal (organization✓), Thursday July 31,
audit log, 1.8.0-rc3, POST /relation-reviews, 74 seconds, CED-119, staging checklist,
Tuesday noon, recommendation service (minted endpoint).
PARTIAL: Project Cedar → 'Cedar'; relation_review → truncated.
MISS: five customer accounts + two internal accounts (counted NPs — suppressed BY POLICY),
disputed graph link (lowercase concept-phrase class).
Type accuracy vs their labels: poor where labels are custom (Thursday July 31 typed software,
CED-119 typed law) — types don't map by design; person/org/time mostly right.

## Relations: 0/13 strict promoted; ~2/13 captured in non-positive lanes
- POST /relation-reviews CREATES relation_review → captured as created_by (review state)
- Priya PUBLISHES_BY Tuesday noon → captured open ("publish" = open-only lemma)
- recommendation service: we promoted consumes(status event) from the same sentence —
  correct fact, not the gold pair (PROPAGATES_WITHIN 74 seconds)
- 1 promoted FP: wednesday owns legal (attachment/direction artifact — logged defect)
- Zero false-claim leakage; 7 qualified assertions correctly contained

## Failure classes (transcript genre = third new task family)
1. **Speaker-turn discourse (dominant)**: imperatives/vocatives ("Priya, publish the
   checklist…", "Daniel, send X to Priya") have no grammatical subject; the speaker header
   (**09:12 — Priya Nair:**) is the implicit agent. No speaker-resolution machinery exists.
   Kills ~5 golds (SENDS_TO, APPROVES, PUBLISHES_BY, RUNS, REVIEWS_BY subjects).
2. **Deadline/temporal predicate family**: LAUNCHES_ON / PUBLISHES_BY / REVIEWS_BY —
   no temporal relation family in the frozen ontology. ~4 golds.
3. **Counted-NP scope entities**: gold wants "five customer accounts" as an entity;
   our junk policy suppresses counted NPs by contract. 2 golds + 2 entities.
4. **Concept-phrase entities** (disputed graph link, training, customer workflow) —
   same contract question as sealed-v2.
5. Transcript noise partially leaked into entities (Wednesday ×3 from headers, 'What',
   'Questions' — utterance-initial capitals): speaker-header/dialogue furniture handling absent.

## Verdict
Entity discovery genuinely decent on a third unseen genre (72-83%). Relation recall ~0 for
structural reasons that are now precisely named: speaker-turn agency, temporal/deadline
relations, and the two standing contract questions (counted scopes, concept phrases).
Containment held (0 leaks); one promoted FP logged.
