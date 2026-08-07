# Deterministic Context Doctrine — audit & enforcement plan

**Guiding rule (owner, 2026-08-07):** never make a later model rediscover information
that deterministic upstream processing already knows. Cheap deterministic context is
computed once, upstream, and reused everywhere downstream.

This file is the audit of that rule against the current codebase — what already
exists, what is partial, what is missing — and how QA/QC and the control plane
enforce it. Statuses verified against code on 2026-08-07 (post legacy purge,
freeze_v6 era).

Legend: ✅ done · 🟡 partial · ❌ missing

| # | Win | Status | Evidence / gap |
|---|---|---|---|
| 1 | **DocumentProfileV1** (canonical title, URL, author, dates, format, language, identifiers, headings, aliases, schema profile as ONE reusable record) | 🟡 | Pieces exist but scattered: docling adapter extracts title/author/dates + `routing_trace` (`docling_adapter.py`); survey computes headings/aliases/definitions/blocks + identifier density (`graphify_survey.py`); census stamps `schema_release`. No single versioned profile record persisted and referenced downstream. |
| 2 | **unit.kind classifier** (prose / definition / metadata / navigation / bibliography / table / code — semantic extraction OFF for furniture) | ❌ | Ratified in GENRE_STEERING (post-v6). Today only coarse signals exist: `furniture_candidate` on survey blocks, `DENSE_STRUCTURE` labeling, metalinguistic guard. Navigation/citation/boilerplate still reach GLiNER2+OpenIE (confirmed by v7/v8 monograph noise). |
| 3 | **Structure propagation** (every unit carries title, heading path, section id, structural owner) | 🟡 | Census windows carry `heading_path` (`graphify_census.py:161-177`); relation units do NOT — eligibility units drop structural context, so downstream compilers can't use section ownership. |
| 4 | **Metadata parser before NLP** (YAML/JSON/front matter/key:value/tables deterministically) | 🟡 | YAML front matter preserved as plain key:value (owner-ratified, `docling_adapter._strip_yaml_frontmatter`); kv structured-data lane exists (`_structured_data_proposals`). Tables and JSON blocks not yet parsed deterministically. |
| 5 | **Generic identifier patterns** (AR-17, INC-4821, RFC-9110 → mentions) | 🟡 | Survey detects identifiers (`graphify_survey.py:210` `_IDENTIFIER_RE`) but only for density/adapter selection — identifier spans are NOT minted as deterministic mentions to union with GLiNER2 output. Direct cause of the sealed-v6 `AR-17` entity miss. |
| 6 | **Alias registry** (articles, punctuation, case variants, explicit acronyms, unique surnames, safe short forms) | 🟡 | Reducer accumulates aliases from survey + mention surfaces (`graphify_reducer.py:225,267`); argument-ladder rungs 2/4/5 consume canonical/alias/short-form (adapter v2). Missing: punctuation/underscore variant generation, explicit acronym establishment ("Ingestion Worker (IW)"). |
| 7 | **GLiNER2 corpus-wide batching** (length+schema buckets across documents) | 🟡 | Census buckets windows by token size ACROSS the documents it is given (`_bucket_key`) and groups by adapter tuple — but the live flow feeds one document per pipeline run, so batching never spans a corpus. Blocked on the factory architecture (below). |
| 8 | **Deterministic metadata mentions UNION with GLiNER mentions** | 🟡 | Structured kv lane creates proposals+endpoints; but deterministic mentions (identifiers, front-matter values, headings-as-titles) are not unioned into the census mention stream itself. |
| 9 | **Summaries reuse extraction; never graph-extract generated summaries** | 🟡 | E2E config sets `chunk_summarization=False`; worker keeps summary and extraction lanes on disjoint resources (`worker.py:372`). No explicit invariant test that generated summary text can never enter the graphify extraction path. |
| 10 | **OpenIE: warm multiprocess workers + exact-result cache; semantic prose only** | ❌ | Today: ONE warm instance behind a global `_INFERENCE_LOCK` (`graphify_openie.py`) — intentionally serialized; no result cache; runs on all eligible units incl. furniture (see #2). This is the core of the ratified factory phase. |
| 11 | **Alignment uses aliases + structural subjects** (definitions/lists/headings as subject context) | 🟡 | Ladder consumes aliases; discourse-subject resolution exists for headings; TERM:definition structural subjects not yet a lane (ratified with #2). |
| 12 | **Predicate compiler ladder: exact canonical surface > synonym > syntax > adapter > OPEN** | ✅ | `graphify_predicate_compiler` + provenanced `predicate_synonyms.yaml`; unknown predicates stored OPEN (`store_unmapped_surface_relation`), never forced. Known defect queued: `operate: runs_on` inversion (line 506). |
| 13 | **Schema: core ontology + corpus adapter → frozen compiled profile; adapters never invent truth** | ✅ | `config/entity_schema.yaml` core+metadata adapter, survey-driven activation, `schema_hash` stamped per run (`graphify_census.py`). |
| 14 | **Temporal/numeric capture (spans/values/units) before full semantic lanes** | 🟡 | Adapter classifies LITERAL args with `literal_type="temporal_or_metric"` (`graphify_argument_adapter.py`); TimeReference entities minted (v6: 3/3). But values/units are not normalized into typed capture records, and no temporal relation family exists (v6 temporal 0/3, numeric 0/9). |
| 15 | **Storage: bulk writes; DocumentProfile stored once and referenced** | 🟡 | Neo4j writer uses UNWIND bulk (13 sites); Mongo upserts batch; artifact shard codec (2026-08-07). Per-relation write batching during extraction and single-profile referencing await #1 + factory writers. |
| 16 | **CorpusProfileV1** (vocabulary, identifier shapes, adapters, acronyms, doc types aggregated BEFORE mass extraction) | ❌ | Nothing aggregates across documents today; adapter selection is per-document. |

## Enforcement — QA/QC and control plane

The rule is only real if a machine checks it. Enforcement lands in three layers:

1. **Stage-report invariants (exists, extend).** Every stage already emits a
   conservation report checked by the pipeline (`conservation=lambda ...` in
   `graphify_pipeline.py`). Each win, as it lands, must add its own report field +
   check: e.g. #2 adds `unit_kind_counts` + `furniture_units_extracted == 0`; #5 adds
   `identifier_mentions_minted`; #10 adds worker-pool telemetry
   (units/sec, queue depth) with the existing hard invariant
   `eligible == openie_successes + explicit_openie_failures` unchanged.
2. **Invariant tests (exists, extend).** `test_graphify_only_runtime_invariant.py`
   is the pattern: source-scanning tests that fail on architectural regression.
   Add: summary-text-never-extracted (#9), no-model-rediscovery checks
   (e.g. a unit reaching OpenIE must already carry `unit.kind` and heading path),
   deterministic-mention union conservation (#8).
3. **Freeze manifests (exists).** Versioned configs (`entity_schema.yaml`,
   `predicate_synonyms.yaml`) and code hashes are pinned per freeze
   (`freeze_v6/FREEZE_MANIFEST.json` + ancestor-aware verifier); every new
   deterministic artifact (DocumentProfileV1 schema, unit.kind rules,
   CorpusProfileV1) must be a hashed, versioned config — never inline constants.

## Sequencing (owner-ratified)

The ❌/🟡 items are NOT to be built ad hoc. Order: finish legacy purge → v7/v8
answers → **factory architecture phase** (CorpusCoordinator, corpus-wide GLiNER
batching, N-process warm triplet-extract farm, spaCy `nlp.pipe` batching, parallel
deterministic compiler, bulk writers, saturation controller, stage telemetry) with
#2/#5/#8 (unit.kind, identifier mentions, deterministic-mention union) riding the
same phase since they gate what work enters the queues. Correctness invariants
carried unchanged: OpenIE bypass impossible, observation loss impossible,
deterministic_only = 0, assertion safety untouched, document semantics isolated,
deterministic output ordering, pinned model releases.
