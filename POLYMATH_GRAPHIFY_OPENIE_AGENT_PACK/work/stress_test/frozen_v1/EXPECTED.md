# Expected Extraction Results

## Primary score

For the supplied 12-predicate ontology, the reference answer contains:

- **66 positive canonical triples** in the normalization-tolerant reference graph.
- **61 high-confidence/direct triples** before the five most ontology-dependent normalizations are required.
- **5 qualified statements** that should not become ordinary positive production edges.

A healthy production extractor should normally finish in the following range:

| Stage | Expected result |
|---|---:|
| Raw evidence-bound assertion candidates | **72–84** |
| Positive canonical triples after deduplication | **58–72** |
| Strong target | **62–68** |
| Reference positive canonical graph | **66** |
| Qualified negative/modal/attributed statements | **5** |

The range is intentional. Closed-ontology normalization can legitimately differ for a few book-like sentences without indicating a broken extractor.

## Five normalization-sensitive reference triples

These are valid for this test ontology but are less literal than the rest:

1. `Harbor --consumes--> Industrial Sensor Telemetry` from “accepts telemetry”.
2. `Normalization Process --uses--> spaCy` from “normalization code uses spaCy”.
3. `Redis --supports--> Projection Job Coordination` from “coordinates leases for projection jobs”.
4. `August Recovery Event --related_to--> El Paso` because the ontology does not contain `occurred_in`.
5. `Projection Recovery --supports--> Query Service` from “supports continued query service”.

An extractor that omits or represents these differently can still be high quality.

## Statements that must not silently become current positive facts

1. `Harbor --depends_on--> SQLite` — explicitly negated.
2. `Harbor --supports--> ClickHouse` — future/modal.
3. `Qdrant --supports--> Recommendation Feature` — attributed suggestion + future possibility.
4. `Northstar Labs --owns--> Phantom Dataset` — explicitly denied.
5. `Projection Failure Process --causes--> Temporary Search Staleness` — modal (“may”).

These can be retained as qualified assertion objects if the data model supports polarity/modality/attribution.

## Canonicalization expectations

The repeated passive statements in section 7.3 should **not** add new canonical edges:

- `Graph Writer --consumes--> Validated Assertion Batch`
- `Query Service --implements--> Hybrid Retrieval Process`
- `Architecture Guide --defines--> Rebuildability Contract`

Likewise, the final review notes are summaries and should not inflate the canonical count.

## Exclusion expectations

A conservative extractor should not create useful production triples from:

- the illustrative YAML block merely because component names occur together;
- “the system owns the problem”;
- “this approach uses a process”;
- “It uses Redis, and they support the platform” when the pronouns are unresolved;
- generic prose describing what an extractor *should* do.

## Interpreting your count

- **<50 positive canonical triples:** likely recall loss, over-aggressive sentence filtering, missed passive/coordinated relations, or entity typing failures.
- **58–72:** expected production-quality band.
- **62–68:** particularly well aligned with the supplied ontology/reference policy.
- **>80:** inspect for duplicate active/passive edges, review-note duplication, code/config extraction, generic noun relations, or qualified statements promoted to facts.
- **>100:** strong indication that the pipeline is over-materializing nearby entity pairs rather than extracting evidence-bound relations.

Count alone is not sufficient: direction, qualification, provenance, and duplicate control matter more than matching 66 exactly.
