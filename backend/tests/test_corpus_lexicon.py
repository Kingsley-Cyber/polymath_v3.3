from __future__ import annotations

import pytest

from services.ingestion import corpus_lexicon
from services.ingestion.extraction_artifacts import (
    adapt_extraction_failure,
    adapt_extraction_result,
)
from services.ingestion.corpus_lexicon import (
    LEXICON_SCHEMA_VERSION,
    build_document_lexicon_sources,
    clean_alias,
    finalize_corpus_lexicon_index,
    materialize_affected_lexicon,
    materialize_entries,
    mine_acronym_pairs,
    mine_entity_text_evidence,
    mine_structural_contexts,
    normalize_identity,
)


class _Cursor:
    def __init__(self, rows):
        self.rows = list(rows)

    async def to_list(self, length=None):
        return list(self.rows if length is None else self.rows[:length])

    def __aiter__(self):
        self._iter = iter(self.rows)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _Collection:
    def __init__(self, rows):
        self.rows = list(rows)

    def find(self, query, projection=None):
        def matches(row, key, expected):
            if isinstance(expected, dict) and "$in" in expected:
                return row.get(key) in expected["$in"]
            return row.get(key) == expected

        rows = [
            row
            for row in self.rows
            if all(matches(row, key, value) for key, value in query.items())
        ]
        if projection:
            rows = [
                {
                    key: value
                    for key, value in row.items()
                    if key in projection and projection[key]
                }
                for row in rows
            ]
        return _Cursor(rows)


class _Db:
    def __init__(self, collections):
        self.collections = collections

    def __getitem__(self, name):
        return self.collections[name]


def _entity(name, *, surface=None, aliases=None, definition="", confidence=0.98):
    return {
        "canonical_name": name,
        "surface_form": surface or name,
        "entity_type": "Concept",
        "object_kind": "standard",
        "confidence": confidence,
        "query_aliases": aliases or [],
        "definitional_phrase": definition,
    }


def _candidate_artifact(engine: str, *, text: str):
    return adapt_extraction_result(
        {
            "schema_version": "polymath.extract.v1",
            "corpus_id": "c1",
            "doc_id": "d1",
            "chunk_id": "chunk-1",
            "text": text,
            "entities": [
                {
                    "canonical_name": "Facial Action Coding System",
                    "surface_form": "Facial Action Coding System",
                    "entity_type": "Concept",
                    "confidence": 0.98,
                },
                {
                    "canonical_name": "Action Unit 12",
                    "surface_form": "Action Unit 12",
                    "entity_type": "Concept",
                    "confidence": 0.95,
                },
            ],
            "relations": [
                {
                    "subject": "Facial Action Coding System",
                    "predicate": "uses",
                    "object": "Action Unit 12",
                    "object_kind": "entity",
                    "confidence": 0.9,
                    "evidence_phrase": text,
                    "relation_cue": "uses",
                    "validation_status": "accepted",
                }
            ],
            "facts": [],
            "model": f"{engine}-model",
            "attempts": 1,
        },
        engine=engine,
        engine_runtime_version=f"{engine}-runtime.1",
        source_wire_contract_version=f"{engine}-wire.1",
        source_contract_hash="sha256:source-contract",
        model_id=f"{engine}-model",
    )


@pytest.mark.asyncio
async def test_all_candidate_engines_share_the_exact_lexicon_projector() -> None:
    text = "Facial Action Coding System (FACS) uses Action Unit 12."
    db = _Db(
        {
            "chunks": _Collection(
                [
                    {
                        "corpus_id": "c1",
                        "doc_id": "d1",
                        "chunk_id": "chunk-1",
                        "parent_id": "parent-1",
                        "text": text,
                        "heading_path": ["Facial Performance"],
                    }
                ]
            ),
            "parent_chunks": _Collection(
                [
                    {
                        "corpus_id": "c1",
                        "doc_id": "d1",
                        "parent_id": "parent-1",
                        "central_claim": (
                            "Facial Action Coding System connects facial movement "
                            "to reproducible action units."
                        ),
                        "main_mechanism": (
                            "Observers code visible muscle actions as numbered units."
                        ),
                        "retrieval_uses": [
                            "Use action units to compare facial performances."
                        ],
                        "quality_score": 0.96,
                        "validation_status": "valid",
                    }
                ]
            ),
        }
    )
    projected = []
    for engine in ("cloud", "local", "legacy_local", "runpod_flash"):
        sources = await build_document_lexicon_sources(
            db,
            corpus_id="c1",
            doc_id="d1",
            candidate_artifacts=[_candidate_artifact(engine, text=text)],
        )
        projected.append(
            [
                {key: value for key, value in row.items() if key != "updated_at"}
                for row in sources
            ]
        )

    assert projected[1:] == [projected[0], projected[0], projected[0]]
    entries = materialize_entries(projected[0], "c1")
    by_key = {row["canonical_key"]: row for row in entries}
    assert by_key["facial action coding system"]["cooccurrence_neighbors"]
    assert any(
        item["method"] == "parent_retrieval_use"
        for item in by_key["facial action coding system"]["contextual_usages"]
    )
    assert any(
        relation["evidence_phrase"] == text
        for relation in by_key["facial action coding system"]["factual_relations"]
    )
    assert by_key["facial action coding system"]["retrieval_gloss"]
    assert by_key["facial action coding system"]["retrieval_eligible"] is True


@pytest.mark.asyncio
async def test_candidate_lexicon_projection_fails_closed_on_invalid_scope() -> None:
    text = "Facial Action Coding System uses Action Unit 12."
    db = _Db(
        {
            "chunks": _Collection(
                [
                    {
                        "corpus_id": "c1",
                        "doc_id": "d1",
                        "chunk_id": "chunk-1",
                        "parent_id": "parent-1",
                        "text": text,
                    }
                ]
            ),
            "parent_chunks": _Collection([]),
        }
    )
    artifact = _candidate_artifact("runpod_flash", text=text)
    stale = _candidate_artifact("runpod_flash", text="Different source text.")
    wrong_owner = artifact.model_copy(update={"doc_id": "d2"})
    missing_chunk = artifact.model_copy(update={"chunk_id": "chunk-missing"})
    drifted_provenance = artifact.provenance.model_copy(
        update={"shared_contract_hash": "sha256:drifted"}
    )
    drifted_contract = artifact.model_copy(update={"provenance": drifted_provenance})
    failure = adapt_extraction_failure(
        {
            "corpus_id": "c1",
            "doc_id": "d1",
            "chunk_id": "chunk-1",
            "error_type": "TimeoutError",
            "error_message": "timeout",
        },
        engine="runpod_flash",
        engine_runtime_version="runpod-runtime.1",
        source_wire_contract_version="runpod-wire.1",
        source_contract_hash="sha256:source-contract",
        source_text=text,
        model_id="runpod-model",
    )

    with pytest.raises(ValueError, match="duplicate chunk"):
        await build_document_lexicon_sources(
            db,
            corpus_id="c1",
            doc_id="d1",
            candidate_artifacts=[artifact, artifact],
        )
    with pytest.raises(ValueError, match="ownership escapes"):
        await build_document_lexicon_sources(
            db,
            corpus_id="c1",
            doc_id="d1",
            candidate_artifacts=[wrong_owner],
        )
    with pytest.raises(ValueError, match="contract hash drifted"):
        await build_document_lexicon_sources(
            db,
            corpus_id="c1",
            doc_id="d1",
            candidate_artifacts=[drifted_contract],
        )
    with pytest.raises(ValueError, match="chunk is absent"):
        await build_document_lexicon_sources(
            db,
            corpus_id="c1",
            doc_id="d1",
            candidate_artifacts=[missing_chunk],
        )
    with pytest.raises(ValueError, match="source text is stale"):
        await build_document_lexicon_sources(
            db,
            corpus_id="c1",
            doc_id="d1",
            candidate_artifacts=[stale],
        )
    with pytest.raises(ValueError, match="only candidate"):
        await build_document_lexicon_sources(
            db,
            corpus_id="c1",
            doc_id="d1",
            candidate_artifacts=[failure],
        )


def test_alias_cleaning_preserves_real_acronyms_and_rejects_noise():
    assert clean_alias("FACS", canonical_name="Facial Action Coding System") == (
        "FACS",
        None,
    )
    assert clean_alias("fas", canonical_name="Facial Action Coding System")[1] == (
        "malformed_short_alias"
    )
    assert clean_alias("FACS (1978 and 2002)", canonical_name="FACS")[1] == (
        "edition_or_citation"
    )
    assert clean_alias("page 214", canonical_name="FACS")[1] == "citation_noise"
    assert clean_alias("10+12+25, 6+12", canonical_name="FACS")[1] == ("numeric_code")
    assert (
        clean_alias(
            "Facial Action Coding System 171",
            canonical_name="Facial Action Coding System",
        )[1]
        == "trailing_number_variant"
    )
    assert clean_alias("FACS 84", canonical_name="Facial Action Coding System")[1] == (
        "trailing_number_variant"
    )
    assert (
        clean_alias(
            "facial_action_coding_system_84",
            canonical_name="Facial Action Coding System",
        )[1]
        == "trailing_number_variant"
    )


def test_identity_normalization_reconciles_separator_variants():
    assert normalize_identity("facial_action-coding system") == (
        "facial action coding system"
    )


def test_deterministic_text_mining_keeps_aliases_and_definitions_typed():
    text = (
        "The Facial Action Coding System (FACS) is a taxonomy for observable "
        "facial movement. FACS is also known as facial action coding."
    )

    assert mine_acronym_pairs(text) == [
        {
            "long_form": "Facial Action Coding System",
            "short_form": "FACS",
            "evidence": "The Facial Action Coding System (FACS)",
        }
    ]
    evidence = mine_entity_text_evidence(text, "Facial Action Coding System")

    assert any(
        item["alias"] == "FACS" and item["method"] == "schwartz_hearst_acronym"
        for item in evidence["aliases"]
    )
    assert evidence["definitions"] == [
        {
            "text": "a taxonomy for observable facial movement",
            "method": "explicit_definition_pattern",
        }
    ]


def test_structure_mining_keeps_heading_context_out_of_alias_identity():
    assert mine_structural_contexts(
        ["Chapter 4: Facial Performance", "Introduction", "Lighting for Close-Ups"],
        "Facial Action Coding System",
    ) == [
        {
            "text": "Facial Performance",
            "context_key": "facial performance",
            "method": "heading_path",
        },
        {
            "text": "Lighting for Close-Ups",
            "context_key": "lighting for close ups",
            "method": "heading_path",
        },
    ]


@pytest.mark.asyncio
async def test_document_projection_uses_chunk_acronyms_without_model_aliases():
    corpus_id = "c1"
    doc_id = "d1"
    db = _Db(
        {
            "ghost_b_extractions": _Collection(
                [
                    {
                        "corpus_id": corpus_id,
                        "doc_id": doc_id,
                        "status": "ok",
                        "chunk_id": "chunk-1",
                        "chunk_hash": "hash-1",
                        "entities": [
                            _entity("Facial Action Coding System"),
                            _entity("FACS"),
                        ],
                        "relations": [],
                        "facts": [],
                    }
                ]
            ),
            "chunks": _Collection(
                [
                    {
                        "corpus_id": corpus_id,
                        "doc_id": doc_id,
                        "chunk_id": "chunk-1",
                        "parent_id": "parent-1",
                        "text": "Facial Action Coding System (FACS) is a facial movement taxonomy.",
                        "heading_path": ["Actor Performance", "Facial Motion"],
                    }
                ]
            ),
        }
    )

    sources = await build_document_lexicon_sources(
        db, corpus_id=corpus_id, doc_id=doc_id
    )

    assert len(sources) == 1
    assert set(sources[0]["canonical_keys"]) == {
        "facs",
        "facial action coding system",
    }
    assert any(
        row["method"] == "schwartz_hearst_acronym"
        for row in sources[0]["alias_evidence"]
    )
    assert {row["context_key"] for row in sources[0]["structural_contexts"]} == {
        "actor performance",
        "facial motion",
    }
    assert "actor performance" not in sources[0]["aliases_normalized"]


def test_alphanumeric_initialism_preserves_complete_numeric_suffix():
    entries = materialize_entries(
        [
            {
                "canonical_key": "action unit 12",
                "canonical_keys": ["action unit 12"],
                "canonical_names": [{"value": "Action Unit 12", "count": 2}],
                "aliases": ["AU12"],
                "abbreviations": ["AU12"],
                "identity_links": [
                    {
                        "source": "action unit 12",
                        "target": "au12",
                        "surface": "AU12",
                    }
                ],
                "doc_id": "d1",
                "support_count": 2,
                "mean_confidence": 0.95,
            },
            {
                "canonical_key": "au12",
                "canonical_keys": ["au12"],
                "canonical_names": [{"value": "AU12", "count": 1}],
                "aliases": ["Action Unit 12"],
                "identity_links": [
                    {
                        "source": "au12",
                        "target": "action unit 12",
                        "surface": "Action Unit 12",
                    }
                ],
                "doc_id": "d2",
                "support_count": 1,
                "mean_confidence": 0.95,
            },
        ],
        "c1",
    )

    assert len(entries) == 1
    assert set(entries[0]["member_keys"]) == {"action unit 12", "au12"}


def test_ambiguous_short_code_does_not_transitively_merge_concepts():
    def source(long_key: str, doc_id: str, usage: str):
        return {
            "canonical_key": long_key,
            "canonical_keys": [long_key, "ad"],
            "canonical_names": [{"value": long_key, "count": 2}],
            "aliases": ["AD"],
            "abbreviations": ["AD"],
            "identity_links": [
                {"source": long_key, "target": "ad", "surface": "AD"}
            ],
            "contextual_usages": [
                {
                    "text": usage,
                    "method": "parent_central_claim",
                    "chunk_id": f"chunk-{doc_id}",
                    "parent_id": f"parent-{doc_id}",
                    "doc_id": doc_id,
                }
            ],
            "doc_id": doc_id,
            "support_count": 2,
            "mean_confidence": 0.95,
        }

    entries = materialize_entries(
        [
            source("advertisement", "ads", "An advertisement presents an offer."),
            source(
                "assistant director",
                "film",
                "The assistant director coordinates the set.",
            ),
            source(
                "action descriptors",
                "facs",
                "Action Descriptors annotate non-muscular facial movements.",
            ),
        ],
        "c1",
    )

    by_key = {entry["canonical_key"]: entry for entry in entries}
    assert set(by_key) == {
        "advertisement",
        "assistant director",
        "action descriptors",
    }
    assert all("AD" in entry["abbreviations"] for entry in entries)
    assert all("ambiguous_short_identity" in entry["quality_flags"] for entry in entries)
    assert [
        item["text"] for item in by_key["assistant director"]["contextual_usages"]
    ] == ["The assistant director coordinates the set."]


@pytest.mark.asyncio
async def test_document_projection_does_not_merge_ambiguous_acronym_senses():
    db = _Db(
        {
            "ghost_b_extractions": _Collection(
                [
                    {
                        "corpus_id": "c1",
                        "doc_id": "d1",
                        "status": "ok",
                        "chunk_id": "chunk-film",
                        "entities": [
                            _entity("assistant director", aliases=["AD"]),
                            _entity("ad", surface="AD"),
                        ],
                        "relations": [],
                        "facts": [],
                    },
                    {
                        "corpus_id": "c1",
                        "doc_id": "d1",
                        "status": "ok",
                        "chunk_id": "chunk-facs",
                        "entities": [
                            _entity("action descriptor", aliases=["AD"]),
                            _entity("ad", surface="AD"),
                        ],
                        "relations": [],
                        "facts": [],
                    },
                ]
            ),
            "chunks": _Collection(
                [
                    {
                        "corpus_id": "c1",
                        "doc_id": "d1",
                        "chunk_id": "chunk-film",
                        "parent_id": "parent-film",
                        "text": "The assistant director (AD) coordinates the set.",
                    },
                    {
                        "corpus_id": "c1",
                        "doc_id": "d1",
                        "chunk_id": "chunk-facs",
                        "parent_id": "parent-facs",
                        "text": "An Action Descriptor (AD) records visible behavior.",
                    },
                ]
            ),
            "parent_chunks": _Collection([]),
        }
    )

    sources = await build_document_lexicon_sources(
        db,
        corpus_id="c1",
        doc_id="d1",
    )

    by_key = {source["canonical_key"]: source for source in sources}
    assert "assistant director" in by_key
    assert "action descriptor" in by_key
    assert set(by_key["assistant director"]["canonical_keys"]) == {
        "assistant director"
    }
    assert set(by_key["action descriptor"]["canonical_keys"]) == {
        "action descriptor"
    }
    assert "ambiguous_short_identity" in by_key["assistant director"]["quality_flags"]
    assert "ambiguous_short_identity" in by_key["action descriptor"]["quality_flags"]


@pytest.mark.asyncio
async def test_document_projection_adds_source_backed_parent_usage_context():
    db = _Db(
        {
            "ghost_b_extractions": _Collection(
                [
                    {
                        "corpus_id": "c1",
                        "doc_id": "d1",
                        "status": "ok",
                        "chunk_id": "chunk-1",
                        "entities": [_entity("Laban Effort Actions")],
                        "relations": [],
                        "facts": [],
                    }
                ]
            ),
            "chunks": _Collection(
                [
                    {
                        "corpus_id": "c1",
                        "doc_id": "d1",
                        "chunk_id": "chunk-1",
                        "parent_id": "parent-1",
                        "text": "Laban Effort Actions describe movement qualities.",
                    }
                ]
            ),
            "parent_chunks": _Collection(
                [
                    {
                        "corpus_id": "c1",
                        "doc_id": "d1",
                        "parent_id": "parent-1",
                        "central_claim": "Movement quality communicates character intent.",
                        "main_mechanism": "Control time, weight, space, and flow.",
                        "retrieval_uses": [
                            "Direct an actor to move slowly and deliberately."
                        ],
                        "quality_score": 0.92,
                        "validation_status": "valid",
                    }
                ]
            ),
        }
    )

    sources = await build_document_lexicon_sources(
        db,
        corpus_id="c1",
        doc_id="d1",
    )

    usage_text = {item["text"] for item in sources[0]["contextual_usages"]}
    assert "Direct an actor to move slowly and deliberately." in usage_text
    entries = materialize_entries(sources, "c1")
    assert "move slowly and deliberately" in entries[0]["embedding_gloss"].lower()


def test_contextual_usage_selection_preserves_document_diversity_and_mechanisms():
    rows = [
        {
            "text": f"This passage describes facial coding table {index}.",
            "method": "parent_central_claim",
            "parent_id": f"manual-{index}",
            "doc_id": "manual",
            "confidence": 1.0,
        }
        for index in range(20)
    ]
    rows.extend(
        [
            {
                "text": (
                    "The extraction completeness checklist maps FACS sources "
                    "to validation files."
                ),
                "method": "parent_main_mechanism",
                "parent_id": "qc-1",
                "doc_id": "extraction-qc",
                "confidence": 1.0,
            },
            {
                "text": (
                    "This source map catalogs Facial Action Coding System "
                    "references for deeper study."
                ),
                "method": "parent_main_mechanism",
                "parent_id": "catalog-1",
                "doc_id": "source-catalog",
                "confidence": 1.0,
            },
            {
                "text": (
                    "Facial Action Coding System concepts map acting craft to "
                    "AI video prompt fields for performance direction."
                ),
                "method": "parent_main_mechanism",
                "parent_id": "bridge-1",
                "doc_id": "ai-video-map",
                "confidence": 0.92,
            },
            {
                "text": "Action Units measure visible facial muscle changes.",
                "method": "parent_main_mechanism",
                "parent_id": "measurement-1",
                "doc_id": "measurement",
                "confidence": 0.95,
            },
        ]
    )

    selected = corpus_lexicon._select_contextual_usages(
        rows,
        cap=3,
        identity_terms=["Facial Action Coding System", "FACS", "facial coding"],
    )

    assert {row["doc_id"] for row in selected} == {
        "manual",
        "ai-video-map",
        "measurement",
    }
    assert selected[0]["doc_id"] == "ai-video-map"
    assert not {"extraction-qc", "source-catalog"}.intersection(
        row["doc_id"] for row in selected
    )
    embedding_gloss = corpus_lexicon._build_embedding_gloss(
        {
            "canonical_name": "Facial Action Coding System",
            "canonical_key": "facial action coding system",
            "contextual_usages": selected,
        }
    )
    assert "AI video prompt fields" in embedding_gloss
    refreshed = corpus_lexicon._refreshed_gloss_fields(
        {
            "canonical_name": "Facial Action Coding System",
            "canonical_key": "facial action coding system",
            "aliases": ["facial coding"],
            "abbreviations": ["FACS"],
            "contextual_usages": rows,
        }
    )
    assert refreshed["contextual_usages"][0]["doc_id"] == "ai-video-map"
    assert "AI video prompt fields" in refreshed["embedding_gloss"]
    assert "facial coding table" not in refreshed["embedding_gloss"]
    assert "facial coding table" in refreshed["retrieval_gloss"]
    assert "extraction completeness checklist" not in refreshed["utility_gloss"]


def test_trailing_code_identity_merge_requires_observed_specific_base():
    def source(key: str, doc_id: str):
        return {
            "canonical_key": key,
            "canonical_keys": [key],
            "canonical_names": [{"value": key, "count": 1}],
            "doc_id": doc_id,
            "support_count": 1,
            "mean_confidence": 0.95,
        }

    entries = materialize_entries(
        [
            source("customer lifetime value model", "d1"),
            source("customer lifetime value model 84", "d2"),
            source("action unit", "d1"),
            source("action unit 12", "d2"),
        ],
        "c1",
    )

    customer = next(
        entry
        for entry in entries
        if entry["canonical_key"] == "customer lifetime value model"
    )
    assert set(customer["member_keys"]) == {
        "customer lifetime value model",
        "customer lifetime value model 84",
    }
    assert "reconciled_trailing_identity_code" in customer["quality_flags"]
    assert {entry["canonical_key"] for entry in entries} >= {
        "action unit",
        "action unit 12",
    }


@pytest.mark.asyncio
async def test_document_projection_merges_alias_identities_but_not_components():
    corpus_id = "c1"
    doc_id = "d1"
    rows = [
        {
            "corpus_id": corpus_id,
            "doc_id": doc_id,
            "status": "ok",
            "chunk_id": "chunk-1",
            "chunk_hash": "hash-1",
            "entities": [
                _entity(
                    "facial_action_coding_system",
                    surface="Facial Action Coding System",
                    aliases=["FACS", "action unit 12"],
                    definition="a scientific system for coding visible facial movement",
                ),
                _entity("action unit 12", surface="AU12"),
            ],
            "relations": [
                {
                    "subject": "action unit 12",
                    "predicate": "part_of",
                    "object": "facial_action_coding_system",
                    "object_kind": "entity",
                    "confidence": 0.97,
                    "evidence_phrase": "Action Unit 12 is part of FACS.",
                }
            ],
            "facts": [],
        },
        {
            "corpus_id": corpus_id,
            "doc_id": doc_id,
            "status": "ok",
            "chunk_id": "chunk-2",
            "chunk_hash": "hash-2",
            "entities": [
                _entity(
                    "facs",
                    surface="FACS",
                    aliases=["Facial Action Coding System", "FACS 1978"],
                    definition="a system that describes expressions with Action Units",
                ),
                _entity("facs measurement", aliases=["FACS"]),
            ],
            "relations": [],
            "facts": [],
        },
    ]
    db = _Db(
        {
            "ghost_b_extractions": _Collection(rows),
            "chunks": _Collection(
                [
                    {
                        "corpus_id": corpus_id,
                        "doc_id": doc_id,
                        "chunk_id": "chunk-1",
                        "parent_id": "parent-1",
                    },
                    {
                        "corpus_id": corpus_id,
                        "doc_id": doc_id,
                        "chunk_id": "chunk-2",
                        "parent_id": "parent-2",
                    },
                ]
            ),
        }
    )

    sources = await build_document_lexicon_sources(
        db, corpus_id=corpus_id, doc_id=doc_id
    )

    assert len(sources) == 3
    facs = next(row for row in sources if "facs" in row["aliases_normalized"])
    au12 = next(row for row in sources if row["canonical_key"] == "action unit 12")
    measurement = next(
        row for row in sources if row["canonical_key"] == "facs measurement"
    )
    assert set(facs["canonical_keys"]) == {
        "facs",
        "facial action coding system",
    }
    assert "facs" in facs["abbreviations_normalized"]
    assert "facs 1978" not in facs["aliases_normalized"]
    assert "action unit 12" not in facs["aliases_normalized"]
    assert "rejected_alias:relation_conflict" in facs["quality_flags"]
    assert au12["canonical_key"] == "action unit 12"
    assert "facs" not in measurement["aliases_normalized"]
    assert "rejected_alias:abbreviation_scope_mismatch" in measurement["quality_flags"]
    assert facs["components"][0]["target_key"] == "action unit 12"
    assert facs["source_parent_ids"] == ["parent-1", "parent-2"]


def test_corpus_materialization_keeps_association_types_distinct():
    sources = [
        {
            "canonical_key": "facial action coding system",
            "canonical_keys": ["facial action coding system"],
            "canonical_names": [{"value": "Facial Action Coding System", "count": 2}],
            "aliases": ["FACS"],
            "aliases_normalized": ["facs"],
            "abbreviations": ["FACS"],
            "abbreviations_normalized": ["facs"],
            "identity_links": [
                {
                    "source": "facial action coding system",
                    "target": "facs",
                    "surface": "FACS",
                }
            ],
            "definitions": [
                {
                    "text": "a system for coding facial expressions using Action Units",
                    "chunk_id": "c1",
                    "confidence": 0.98,
                }
            ],
            "components": [
                {
                    "target_key": "action unit 12",
                    "target": "Action Unit 12",
                    "chunk_id": "c1",
                    "confidence": 0.9,
                }
            ],
            "component_of": [],
            "relations": [
                {
                    "predicate": "uses",
                    "direction": "outgoing",
                    "target_key": "action units",
                    "target": "Action Units",
                    "chunk_id": "c1",
                    "confidence": 0.9,
                }
            ],
            "application_contexts": [
                {
                    "predicate": "used_for",
                    "direction": "outgoing",
                    "target_key": "facial performance",
                    "target": "facial performance",
                    "chunk_id": "c1",
                    "confidence": 0.9,
                }
            ],
            "cooccurrence_counts": {"actor performance": 2},
            "entity_types": ["Concept"],
            "object_kinds": ["standard"],
            "entity_ids": ["entity:facial-action-coding-system"],
            "doc_id": "d1",
            "source_chunk_ids": ["c1"],
            "source_chunk_count": 1,
            "source_parent_ids": ["p1"],
            "source_parent_count": 1,
            "source_hashes": ["h1"],
            "support_count": 2,
            "mean_confidence": 0.98,
            "quality_flags": [],
        },
        {
            "canonical_key": "facs",
            "canonical_keys": ["facs"],
            "canonical_names": [{"value": "FACS", "count": 4}],
            "aliases": ["Facial Action Coding System"],
            "aliases_normalized": ["facial action coding system"],
            "abbreviations": [],
            "abbreviations_normalized": [],
            "identity_links": [
                {
                    "source": "facs",
                    "target": "facial action coding system",
                    "surface": "Facial Action Coding System",
                }
            ],
            "definitions": [],
            "components": [],
            "component_of": [],
            "relations": [],
            "application_contexts": [],
            "cooccurrence_counts": {},
            "entity_types": ["Standard"],
            "object_kinds": ["standard"],
            "entity_ids": ["entity:facs"],
            "doc_id": "d2",
            "source_chunk_ids": ["c2"],
            "source_chunk_count": 1,
            "source_parent_ids": ["p2"],
            "source_parent_count": 1,
            "source_hashes": ["h2"],
            "support_count": 4,
            "mean_confidence": 0.95,
            "quality_flags": [],
        },
    ]

    entries = materialize_entries(sources, "c1")

    assert len(entries) == 1
    entry = entries[0]
    assert entry["schema_version"] == LEXICON_SCHEMA_VERSION
    assert entry["canonical_name"] == "Facial Action Coding System"
    assert entry["abbreviations"] == ["FACS"]
    assert entry["components"][0]["target"] == "Action Unit 12"
    assert entry["application_contexts"][0]["predicate"] == "used_for"
    assert entry["cooccurrence_neighbors"][0]["factual"] is False
    assert entry["semantic_neighbors"] == []
    assert entry["source_document_support"] == [
        {
            "doc_id": "d2",
            "support_count": 4,
            "source_chunk_count": 1,
            "source_parent_count": 1,
        },
        {
            "doc_id": "d1",
            "support_count": 2,
            "source_chunk_count": 1,
            "source_parent_count": 1,
        },
    ]
    assert "system for coding facial expressions" in entry["embedding_gloss"]
    assert "actor performance" not in entry["embedding_gloss"]
    assert "Source definition:" in entry["retrieval_gloss"]


def test_unsupported_short_codes_remain_auditable_but_not_retrievable():
    entries = materialize_entries(
        [
            {
                "canonical_key": "o1",
                "canonical_keys": ["o1"],
                "canonical_names": [{"value": "o1", "count": 1}],
                "aliases": [],
                "aliases_normalized": [],
                "abbreviations": [],
                "abbreviations_normalized": [],
                "identity_links": [],
                "definitions": [],
                "components": [],
                "component_of": [],
                "relations": [],
                "application_contexts": [],
                "cooccurrence_counts": {},
                "entity_types": ["Product"],
                "object_kinds": [],
                "entity_ids": ["entity:o1"],
                "doc_id": "d1",
                "source_chunk_ids": ["c1"],
                "source_chunk_count": 1,
                "source_parent_ids": ["p1"],
                "source_parent_count": 1,
                "source_hashes": ["h1"],
                "support_count": 1,
                "mean_confidence": 0.9,
                "quality_flags": [],
            }
        ],
        "c1",
    )

    assert entries[0]["retrieval_eligible"] is False
    assert "low_information_identity" in entries[0]["quality_flags"]


def test_unanchored_one_off_phrases_stay_auditable_but_do_not_crowd_ann():
    entries = materialize_entries(
        [
            {
                "canonical_key": "direct the actors attention",
                "canonical_keys": ["direct the actors attention"],
                "canonical_names": [
                    {"value": "direct the actors attention", "count": 1}
                ],
                "aliases": [],
                "aliases_normalized": [],
                "abbreviations": [],
                "abbreviations_normalized": [],
                "identity_links": [],
                "definitions": [],
                "components": [],
                "component_of": [],
                "relations": [],
                "application_contexts": [],
                "cooccurrence_counts": {},
                "entity_types": ["Concept"],
                "object_kinds": [],
                "entity_ids": ["entity:direct-the-actors-attention"],
                "doc_id": "d1",
                "source_chunk_ids": ["c1"],
                "source_chunk_count": 1,
                "source_parent_ids": ["p1"],
                "source_parent_count": 1,
                "source_hashes": ["h1"],
                "support_count": 1,
                "mean_confidence": 0.9,
                "quality_flags": [],
            }
        ],
        "c1",
    )

    assert entries[0]["retrieval_eligible"] is False
    assert "unanchored_phrase_identity" in entries[0]["quality_flags"]


@pytest.mark.asyncio
async def test_incremental_materialization_writes_before_deleting_stale_identity(
    monkeypatch,
):
    events: list[tuple[str, object]] = []

    class MutationCollection:
        def find(self, query, projection=None):
            return _Cursor(
                [
                    {"lexicon_id": "same"},
                    {"lexicon_id": "stale"},
                ]
            )

        async def bulk_write(self, operations, ordered=False):
            events.append(("write", len(operations)))

        async def delete_many(self, query):
            events.append(("delete", query))

    async def source_closure(*args, **kwargs):
        return ([{"canonical_key": "concept"}], {"concept"})

    monkeypatch.setattr(corpus_lexicon, "ensure_lexicon_indexes", lambda db: _noop())
    monkeypatch.setattr(corpus_lexicon, "_source_identity_closure", source_closure)
    monkeypatch.setattr(
        corpus_lexicon,
        "materialize_entries",
        lambda rows, corpus_id: [
            {
                "corpus_id": corpus_id,
                "lexicon_id": "same",
                "canonical_key": "concept",
            }
        ],
    )
    db = _Db({"corpus_lexicon": MutationCollection()})

    result = await materialize_affected_lexicon(
        db,
        corpus_id="c1",
        affected_keys=["concept"],
    )

    assert [event[0] for event in events] == ["write", "delete"]
    assert events[1][1] == {
        "corpus_id": "c1",
        "lexicon_id": {"$in": ["stale"]},
    }
    assert result["stale_lexicon_ids"] == ["stale"]


@pytest.mark.asyncio
async def test_full_materialization_is_bounded_and_deletes_stale_only_after_validation(
    monkeypatch,
):
    events: list[tuple[str, object]] = []
    generated_rows: list[dict] = []
    closure_calls: list[list[str]] = []

    class DeleteResult:
        deleted_count = 2

    class SourceCollection:
        async def count_documents(self, query):
            return 3

        def aggregate(self, pipeline, **kwargs):
            return _Cursor([{"_id": "alpha"}, {"_id": "beta"}, {"_id": "gamma"}])

    class EntryCollection:
        async def bulk_write(self, operations, ordered=False):
            events.append(("write", len(operations)))

        def find(self, query, projection=None):
            return _Cursor(generated_rows)

        async def delete_many(self, query):
            events.append(("delete", query))
            return DeleteResult()

    class UpdateCollection:
        async def update_many(self, query, update):
            return None

        async def update_one(self, query, update):
            return None

    async def source_closure(*args, seed_keys, **kwargs):
        seeds = list(seed_keys)
        closure_calls.append(seeds)
        return (
            [
                {
                    "canonical_key": key,
                    "canonical_keys": [key],
                }
                for key in seeds
            ],
            set(seeds),
        )

    def materialize(rows, corpus_id):
        entries = [
            {
                "corpus_id": corpus_id,
                "lexicon_id": f"lex-{row['canonical_key']}",
                "canonical_key": row["canonical_key"],
                "member_keys": [row["canonical_key"]],
            }
            for row in rows
        ]
        generated_rows.extend(entries)
        return entries

    monkeypatch.setattr(corpus_lexicon, "ensure_lexicon_indexes", lambda db: _noop())
    monkeypatch.setattr(corpus_lexicon, "_source_identity_closure", source_closure)
    monkeypatch.setattr(corpus_lexicon, "materialize_entries", materialize)
    monkeypatch.setattr(
        corpus_lexicon,
        "_lexicon_document_counts",
        lambda db, corpus_id: _value({"processed": 3, "total": 3}),
    )
    db = _Db(
        {
            "corpus_lexicon_sources": SourceCollection(),
            "corpus_lexicon": EntryCollection(),
            "documents": UpdateCollection(),
            "corpora": UpdateCollection(),
        }
    )

    result = await corpus_lexicon.materialize_corpus_lexicon(
        db,
        corpus_id="c1",
        materialization_id="run-1",
        key_batch_size=2,
    )

    assert closure_calls == [["alpha", "beta"], ["gamma"]]
    assert [event[0] for event in events] == ["write", "write", "delete"]
    assert result["materialization_batches"] == 2
    assert result["lexicon_entries"] == 3
    assert result["stale_entries_deleted"] == 2


@pytest.mark.asyncio
async def test_full_materialization_resumes_generation_and_does_not_skip_frontier_key(
    monkeypatch,
):
    generated_rows = [
        {
            "canonical_key": "alpha",
            "member_keys": ["alpha"],
            "materialization_id": "run-resume",
        }
    ]
    closure_calls: list[list[str]] = []

    class DeleteResult:
        deleted_count = 0

    class SourceCollection:
        async def count_documents(self, query):
            return 2

        def aggregate(self, pipeline, **kwargs):
            return _Cursor([{"_id": "alpha"}, {"_id": "beta"}])

    class EntryCollection:
        async def bulk_write(self, operations, ordered=False):
            return None

        def find(self, query, projection=None):
            return _Cursor(generated_rows)

        async def delete_many(self, query):
            return DeleteResult()

    class UpdateCollection:
        async def update_many(self, query, update):
            return None

        async def update_one(self, query, update):
            return None

    async def source_closure(*args, seed_keys, **kwargs):
        seeds = list(seed_keys)
        closure_calls.append(seeds)
        # ``beta`` may already have appeared on an earlier frontier, but only
        # this row proves it was actually loaded and materialized.
        return ([{"canonical_key": "beta", "canonical_keys": ["beta"]}], {"beta"})

    def materialize(rows, corpus_id):
        entries = [
            {
                "corpus_id": corpus_id,
                "lexicon_id": "lex-beta",
                "canonical_key": "beta",
                "member_keys": ["beta"],
            }
        ]
        generated_rows.extend(entries)
        return entries

    monkeypatch.setattr(corpus_lexicon, "ensure_lexicon_indexes", lambda db: _noop())
    monkeypatch.setattr(corpus_lexicon, "_source_identity_closure", source_closure)
    monkeypatch.setattr(corpus_lexicon, "materialize_entries", materialize)
    monkeypatch.setattr(
        corpus_lexicon,
        "_lexicon_document_counts",
        lambda db, corpus_id: _value({"processed": 2, "total": 2}),
    )
    db = _Db(
        {
            "corpus_lexicon_sources": SourceCollection(),
            "corpus_lexicon": EntryCollection(),
            "documents": UpdateCollection(),
            "corpora": UpdateCollection(),
        }
    )

    result = await corpus_lexicon.materialize_corpus_lexicon(
        db,
        corpus_id="c1",
        materialization_id="run-resume",
        key_batch_size=1,
    )

    assert closure_calls == [["beta"]]
    assert result["lexicon_entries"] == 2


@pytest.mark.asyncio
async def test_full_materialization_keeps_stale_projection_when_coverage_is_missing(
    monkeypatch,
):
    deleted = False

    class SourceCollection:
        async def count_documents(self, query):
            return 2

        def aggregate(self, pipeline, **kwargs):
            return _Cursor([{"_id": "alpha"}, {"_id": "beta"}])

    class EntryCollection:
        async def bulk_write(self, operations, ordered=False):
            return None

        def find(self, query, projection=None):
            return _Cursor(
                [
                    {
                        "canonical_key": "alpha",
                        "member_keys": ["alpha"],
                        "materialization_id": "run-2",
                    }
                ]
            )

        async def delete_many(self, query):
            nonlocal deleted
            deleted = True

    async def source_closure(*args, seed_keys, **kwargs):
        return ([{"canonical_key": "alpha", "canonical_keys": ["alpha"]}], {"alpha"})

    monkeypatch.setattr(corpus_lexicon, "ensure_lexicon_indexes", lambda db: _noop())
    monkeypatch.setattr(corpus_lexicon, "_source_identity_closure", source_closure)
    monkeypatch.setattr(
        corpus_lexicon,
        "materialize_entries",
        lambda rows, corpus_id: [
            {
                "corpus_id": corpus_id,
                "lexicon_id": "lex-alpha",
                "canonical_key": "alpha",
                "member_keys": ["alpha"],
            }
        ],
    )
    db = _Db(
        {
            "corpus_lexicon_sources": SourceCollection(),
            "corpus_lexicon": EntryCollection(),
        }
    )

    with pytest.raises(RuntimeError, match="missing_source_keys=1"):
        await corpus_lexicon.materialize_corpus_lexicon(
            db,
            corpus_id="c1",
            materialization_id="run-2",
            key_batch_size=2,
        )

    assert deleted is False


@pytest.mark.asyncio
async def test_index_finalization_refuses_missing_qdrant_identity(monkeypatch):
    updates: list[tuple[str, dict]] = []

    class UpdateCollection:
        async def update_one(self, query, update):
            updates.append(("corpora", update))

        async def update_many(self, query, update):
            updates.append(("documents", update))

    async def snapshot(*args, **kwargs):
        return {"lex-a", "lex-b"}, "version-1", 2

    async def qdrant_ids(*args, **kwargs):
        return ["lex-a"]

    async def forbidden_delete(*args, **kwargs):
        raise AssertionError("Stale deletion must wait for complete parity")

    monkeypatch.setattr(corpus_lexicon, "_lexicon_snapshot_from_db", snapshot)
    monkeypatch.setattr("services.storage.qdrant_writer.list_lexicon_ids", qdrant_ids)
    monkeypatch.setattr(
        "services.storage.qdrant_writer.delete_lexicon_entries",
        forbidden_delete,
    )
    db = _Db(
        {
            "corpora": UpdateCollection(),
            "documents": UpdateCollection(),
        }
    )

    with pytest.raises(RuntimeError, match="1 Mongo-eligible IDs"):
        await finalize_corpus_lexicon_index(
            db,
            object(),
            corpus_id="c1",
        )

    assert [kind for kind, _update in updates] == ["corpora"]
    assert updates[0][1]["$set"]["lexicon_state"] == "lexicon_indexing"
    assert updates[0][1]["$set"]["lexicon_index_missing_count"] == 1


@pytest.mark.asyncio
async def test_bulk_vector_index_defers_and_restores_qdrant_optimizer():
    class OptimizerConfig:
        indexing_threshold = 12_345

    class Config:
        optimizer_config = OptimizerConfig()

    class CollectionInfo:
        config = Config()

    class CorporaCollection:
        def __init__(self):
            self.state = {}

        async def find_one(self, query, projection=None):
            return dict(self.state)

        async def update_one(self, query, update):
            self.state.update(update.get("$set") or {})
            for key in (update.get("$unset") or {}):
                self.state.pop(key, None)

    class Qdrant:
        def __init__(self):
            self.get_calls = 0
            self.thresholds = []

        async def get_collection(self, name):
            self.get_calls += 1
            return CollectionInfo()

        async def update_collection(self, name, *, optimizers_config):
            self.thresholds.append(optimizers_config.indexing_threshold)
            return True

    corpora = CorporaCollection()
    qdrant = Qdrant()
    db = _Db({"corpora": corpora})

    first = await corpus_lexicon._defer_lexicon_qdrant_optimization(
        db,
        qdrant,
        corpus_id="c1",
    )
    second = await corpus_lexicon._defer_lexicon_qdrant_optimization(
        db,
        qdrant,
        corpus_id="c1",
    )
    restored = await corpus_lexicon._restore_lexicon_qdrant_optimization(
        db,
        qdrant,
        corpus_id="c1",
    )

    assert first == second == restored == 12_345
    assert qdrant.get_calls == 1
    assert qdrant.thresholds == [0, 0, 12_345]
    assert "lexicon_qdrant_optimizer_deferred" not in corpora.state
    assert "lexicon_qdrant_optimizer_restore_threshold" not in corpora.state


async def _noop():
    return None


async def _value(value):
    return value
