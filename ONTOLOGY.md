# Polymath Lightweight Ontology

Machine source of truth: `backend/services/ontology.yaml`

The `.yaml` file is intentionally strict JSON. YAML 1.2 accepts JSON syntax, and
the backend can load it with Python's standard `json` module without adding a
new parser dependency.

## What This Governs

- Ghost B entity types
- Ghost B relation predicates
- Predicate glosses shown to the extraction LLM
- Predicate discrimination tests
- Canonical relation direction
- Relation aliases and inverse/passive direction repair
- Entity-type domain/range validation
- Object-kind compatibility hints for graph write repair
- Predicate governance scope
- Relation family grouping used by Mission Control

## Current Entity Types

- `Person`
- `Organization`
- `Location`
- `Event`
- `Concept`
- `Method`
- `Product`
- `Document`
- `Rule`
- `Law`
- `Artifact`
- `TimeReference`

Fallback sentinel: `other`

## Current Relation Families

- Structural: `part_of`, `member_of`, `parameter_of`
- Operational: `uses`, `calls`, `implements`, `depends_on`, `produces`,
  `stores`, `extracts`, `detects`, `classifies`, `runs_on`, `trained_on`,
  `supports`
- Analytical: `measures`, `follows_distribution`, `tests`, `applied_to`
- Referential: `references`, `derived_from`, `represents`, `maps_to`,
  `defined_in`, `illustrated_in`, `equivalent_to`
- Interpretive: `embodies`, `symbolizes`, `frames_as`
- Causal: `causes`, `preceded_by`, `influences`, `motivates`, `reinforces`,
  `undermines`
- Psychosocial: `struggles_with`
- Strategic: `conceals`, `leverages`
- Conflict: `contradicts`, `excepts`, `overrides`
- Provenance/Affiliation/Spatial: `created_by`, `works_for`, `located_in`
- WeakAssociation: `related_to`

Fallback sentinel: `related_to`

## Predicate Governance Scopes

Every relation in `ontology.yaml` has a machine-readable `governance_scope`:

- `core`: original/base ontology predicate. These are stable graph predicates
  used across product, technical, temporal, provenance, conflict, and fallback
  extraction.
- `related_to_repair`: predicate added because current Neo4j `related_to`
  samples repeatedly showed enough textual evidence to support this narrower
  relation.
- `ontology_expansion`: predicate added for future corpus coverage where the
  current corpus mix is expected to include literature, self-growth, power, and
  social-dynamics books. These predicates require explicit textual evidence and
  should not be used to interpret vague association.

Current `related_to_repair` predicates:

- `measures`
- `defined_in`
- `follows_distribution`
- `tests`
- `applied_to`
- `illustrated_in`
- `parameter_of`
- `equivalent_to`

Current `ontology_expansion` predicates:

- `embodies`
- `symbolizes`
- `influences`
- `motivates`
- `struggles_with`
- `reinforces`
- `undermines`
- `frames_as`
- `conceals`
- `leverages`

## Core Predicate Tests

`uses` means the source operationally consumes the target. If removing the
target breaks the source, prefer `depends_on`. If the target is the runtime
substrate, prefer `runs_on`. If the source invokes an API/function/service,
prefer `calls`.

`depends_on` means the source requires the target to function. Do not use it for
generic background technology unless the text states a hard prerequisite.

`runs_on` means the source executes on the target as a platform, runtime, device,
or substrate.

`supports` means the source enables a capability. It does not mean the source
consumes the target.

`produces`, `stores`, and `extracts` are separated by data flow:

- `produces`: creates or outputs something new
- `stores`: persists something in a storage/container
- `extracts`: pulls something from source data

`references`, `derived_from`, `represents`, and `maps_to` are separated by
referential role:

- `references`: cites or mentions
- `derived_from`: based on or adapted from
- `represents`: models or encodes
- `maps_to`: converts/transforms into

`measures`, `tests`, `applied_to`, and `follows_distribution` cover academic,
statistical, scientific, and evaluation-heavy books without becoming stats-only:

- `measures`: quantifies, observes, scores, estimates, or evaluates a target
- `tests`: checks whether a condition, assumption, hypothesis, or constraint holds
- `applied_to`: uses a method/model/framework on a target data, case, domain,
  problem, or situation
- `follows_distribution`: follows a named distribution, law, pattern, curve, or
  expected form

`defined_in`, `illustrated_in`, `parameter_of`, and `equivalent_to` preserve
textbook structure:

- `defined_in`: formal definition/specification appears in a document, equation,
  figure, section, standard, or named container
- `illustrated_in`: concept/model/result is shown in a figure, table, diagram,
  example, or artifact
- `parameter_of`: variable/threshold/setting configures a model/system/process
- `equivalent_to`: aliases, alternate names, or mathematically/conceptually
  interchangeable forms

`embodies`, `symbolizes`, `influences`, `motivates`, `struggles_with`,
`reinforces`, `undermines`, `frames_as`, `conceals`, and `leverages` cover
self-growth, power, emotional, literary, and social-dynamics books:

- `embodies`: concrete actor/object/event personifies a theme, trait, archetype,
  law, or pattern
- `symbolizes`: source stands for a deeper theme, emotional force, value, or
  social pattern
- `influences`: soft shaping pressure where direct causality is too strong
- `motivates`: desire, fear, wound, incentive, or value drives action/behavior
- `struggles_with`: internal, interpersonal, or social conflict
- `reinforces` / `undermines`: strengthens or weakens a pattern/system/identity
- `frames_as`: presents a target through an interpretive lens
- `conceals`: hides or masks intent, weakness, evidence, knowledge, or feeling
- `leverages`: strategically uses a resource, constraint, relationship, weakness,
  or situation as advantage

`related_to` must remain in the ontology. It is the honest graph label for
co-occurrence, "see also" links, vague similarity/comparison, low predicate
confidence, and interpretive claims without explicit evidence. Chasing 0%
`related_to` would make the graph look cleaner while making it less truthful:
weak associations would be over-promoted into specific causal, operational, or
interpretive claims the source text did not prove.

## Edit Rules

When adding a new predicate, update `backend/services/ontology.yaml` with:

- `name`
- `family`
- `gloss`
- `definition`
- `canonical_direction`
- `inverse`
- `governance_scope` (`core`, `related_to_repair`, or `ontology_expansion`)
- `subject_types`
- `object_types`
- `discrimination_tests`
- at least one positive and negative example

Then run:

```powershell
pytest backend/tests/test_universal_schema.py backend/tests/test_ontology_contract.py
```
