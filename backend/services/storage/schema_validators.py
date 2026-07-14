"""Warn-first Mongo JSON-schema validators for durable collections (P0.8).

Legacy validators stay additive and permissive by design; envelope-era
collections are strict-but-warn-first because they begin empty:

  - Only identity-spine fields are ``required`` per collection.
  - Known optional fields are type-checked with ``bsonType`` unions that
    include ``"null"`` so legacy rows and existing writers never fail.
  - Legacy top-level ``additionalProperties`` is never ``False`` — unrelated
    parent/document fields stay allowed. New semantic/outbox/manifest rows are
    closed contracts and reject unknown fields in validation diagnostics.
  - Defaults are ``validationAction: "warn"`` + ``validationLevel:
    "moderate"``: violations are logged by mongod, writes always proceed.

Escalation to ``validationAction: "error"`` is an explicit, later decision
(see docs/RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md P0.8); nothing in this
module defaults to it.
"""

from __future__ import annotations

DOCUMENTS_SCHEMA: dict = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["doc_id", "corpus_id"],
        "properties": {
            "doc_id": {"bsonType": "string"},
            "corpus_id": {"bsonType": "string"},
        },
    }
}

PARENT_CHUNKS_SCHEMA: dict = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["parent_id", "doc_id", "corpus_id"],
        "properties": {
            "parent_id": {"bsonType": "string"},
            "doc_id": {"bsonType": "string"},
            "corpus_id": {"bsonType": "string"},
            "summary": {"bsonType": ["string", "null"]},
            "summary_model": {"bsonType": ["string", "null"]},
            "latent_concepts": {
                "bsonType": ["array", "null"],
                "maxItems": 12,
                "items": {
                    "bsonType": "object",
                    "required": ["concept", "evidence_basis"],
                    "additionalProperties": False,
                    "properties": {
                        "concept": {
                            "bsonType": "string",
                            "minLength": 1,
                            "maxLength": 60,
                        },
                        "evidence_basis": {"enum": ["direct", "inferred"]},
                        "aliases": {
                            "bsonType": "array",
                            "maxItems": 3,
                            "items": {"bsonType": "string"},
                        },
                    },
                },
            },
            # T-HOOK-2: temporal capture on the Ghost A summary contract.
            "temporal_class": {
                "bsonType": ["string", "null"],
                "enum": [
                    "evergreen",
                    "slowly_evolving",
                    "versioned",
                    "event",
                    "ephemeral",
                    "unknown",
                    None,
                ]
            },
            "time_expressions": {
                "bsonType": ["array", "null"],
                "maxItems": 12,
                "items": {
                    "bsonType": "object",
                    "required": ["text", "role"],
                    "additionalProperties": False,
                    "dependencies": {
                        "char_start": ["char_end"],
                        "char_end": ["char_start"],
                    },
                    "properties": {
                        "text": {
                            "bsonType": "string",
                            "minLength": 1,
                            "maxLength": 60,
                        },
                        "role": {
                            "enum": [
                                "publication_time",
                                "revision_time",
                                "reference_time",
                                "event_time",
                                "effective_time",
                                "forecast_time",
                                "deadline_time",
                                "media_offset",
                                "unknown",
                            ]
                        },
                        "char_start": {
                            "bsonType": ["int", "long", "null"],
                            "minimum": 0,
                        },
                        "char_end": {
                            "bsonType": ["int", "long", "null"],
                            "minimum": 0,
                        },
                    },
                },
            },
        },
    }
}

GHOST_B_EXTRACTIONS_SCHEMA: dict = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["corpus_id", "doc_id"],
        "properties": {
            "corpus_id": {"bsonType": "string"},
            "doc_id": {"bsonType": "string"},
            "schema_version": {"bsonType": ["string", "null"]},
            "extractor": {"bsonType": ["string", "null"]},
            "entities": {"bsonType": ["array", "null"]},
            "relations": {"bsonType": ["array", "null"]},
        },
    }
}

CORPUS_LEXICON_SCHEMA: dict = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["corpus_id", "lexicon_id"],
        "properties": {
            "corpus_id": {"bsonType": "string"},
            "lexicon_id": {"bsonType": "string"},
            "canonical_key": {"bsonType": ["string", "null"]},
            "retrieval_eligible": {"bsonType": ["bool", "null"]},
        },
    }
}

SUMMARY_TREE_SCHEMA: dict = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["corpus_id", "doc_id", "node_id", "node_type"],
        "properties": {
            "corpus_id": {"bsonType": "string"},
            "doc_id": {"bsonType": "string"},
            "node_id": {"bsonType": "string"},
            "node_type": {"enum": ["rollup", "section", "document"]},
            "child_node_ids": {"bsonType": ["array", "null"]},
            "concepts": {"bsonType": ["array", "null"]},
        },
    }
}


_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"


SEMANTIC_ARTIFACTS_SCHEMA: dict = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": [
            "envelope_version",
            "artifact_type",
            "schema_id",
            "schema_version",
            "schema_hash",
            "artifact_id",
            "artifact_revision_id",
            "artifact_state",
            "knowledge_status",
            "ownership",
            "integrity",
            "provenance",
            "validation",
            "lifecycle",
            "body",
        ],
        "additionalProperties": False,
        "properties": {
            "envelope_version": {"enum": ["polymath.artifact_envelope.v1"]},
            "artifact_type": {"bsonType": "string", "minLength": 1},
            "schema_id": {"bsonType": "string", "minLength": 1},
            "schema_version": {"bsonType": "string", "minLength": 1},
            "schema_hash": {"bsonType": "string", "pattern": _SHA256_PATTERN},
            "artifact_id": {"bsonType": "string", "minLength": 1},
            "artifact_revision_id": {
                "bsonType": "string",
                "pattern": r"^rev:[0-9a-f]{64}$",
            },
            "artifact_state": {
                "enum": [
                    "candidate",
                    "validated",
                    "active",
                    "rejected",
                    "quarantined",
                    "superseded",
                ]
            },
            "knowledge_status": {
                "enum": [
                    "asserted",
                    "entailed",
                    "cross_passage_synthesis",
                    "structural_analogy",
                    "hypothetical",
                    None,
                ]
            },
            "ownership": {
                "bsonType": "object",
                "required": [
                    "corpus_id",
                    "doc_id",
                    "source_version_id",
                    "hierarchy_node_id",
                ],
                "additionalProperties": False,
                "properties": {
                    "corpus_id": {"bsonType": "string", "minLength": 1},
                    "doc_id": {"bsonType": "string", "minLength": 1},
                    "source_version_id": {
                        "bsonType": "string",
                        "minLength": 1,
                    },
                    "hierarchy_node_id": {"bsonType": ["string", "null"]},
                },
            },
            "integrity": {
                "bsonType": "object",
                "required": [
                    "body_hash",
                    "evidence_set_hash",
                    "input_set_hash",
                    "recipe_hash",
                    "registry_set_hash",
                ],
                "additionalProperties": False,
                "properties": {
                    "body_hash": {
                        "bsonType": "string",
                        "pattern": _SHA256_PATTERN,
                    },
                    "evidence_set_hash": {
                        "bsonType": ["string", "null"],
                        "pattern": _SHA256_PATTERN,
                    },
                    "input_set_hash": {
                        "bsonType": "string",
                        "pattern": _SHA256_PATTERN,
                    },
                    "recipe_hash": {
                        "bsonType": "string",
                        "pattern": _SHA256_PATTERN,
                    },
                    "registry_set_hash": {
                        "bsonType": ["string", "null"],
                        "pattern": _SHA256_PATTERN,
                    },
                },
            },
            "provenance": {
                "bsonType": "object",
                "required": [
                    "work_id",
                    "attempt_id",
                    "raw_artifact_ids",
                    "producer_kind",
                    "engine",
                    "model_id",
                    "model_revision",
                    "prompt_id",
                    "prompt_hash",
                    "compiler_version",
                    "parser_version",
                    "rule_pack_version",
                    "run_id",
                ],
                "additionalProperties": False,
                "properties": {
                    "work_id": {"bsonType": "string", "minLength": 1},
                    "attempt_id": {"bsonType": ["string", "null"]},
                    "raw_artifact_ids": {
                        "bsonType": "array",
                        "items": {"bsonType": "string"},
                    },
                    "producer_kind": {
                        "enum": [
                            "python_rule",
                            "spacy",
                            "zero_shot",
                            "provider_llm",
                            "human",
                            "migration",
                        ]
                    },
                    "engine": {"bsonType": "string", "minLength": 1},
                    "model_id": {"bsonType": ["string", "null"]},
                    "model_revision": {"bsonType": ["string", "null"]},
                    "prompt_id": {"bsonType": ["string", "null"]},
                    "prompt_hash": {
                        "bsonType": ["string", "null"],
                        "pattern": _SHA256_PATTERN,
                    },
                    "compiler_version": {
                        "bsonType": "string",
                        "minLength": 1,
                    },
                    "parser_version": {"bsonType": ["string", "null"]},
                    "rule_pack_version": {"bsonType": ["string", "null"]},
                    "run_id": {"bsonType": "string", "minLength": 1},
                },
            },
            "validation": {
                "bsonType": "object",
                "required": [
                    "contract_valid",
                    "evidence_valid",
                    "registry_valid",
                    "policy_valid",
                    "validator_version",
                    "errors",
                    "warnings",
                ],
                "additionalProperties": False,
                "properties": {
                    "contract_valid": {"bsonType": "bool"},
                    "evidence_valid": {"bsonType": "bool"},
                    "registry_valid": {"bsonType": "bool"},
                    "policy_valid": {"bsonType": "bool"},
                    "validator_version": {
                        "bsonType": "string",
                        "minLength": 1,
                    },
                    "errors": {
                        "bsonType": "array",
                        "items": {"bsonType": "string"},
                    },
                    "warnings": {
                        "bsonType": "array",
                        "items": {"bsonType": "string"},
                    },
                },
            },
            "lifecycle": {
                "bsonType": "object",
                "required": [
                    "created_at",
                    "validated_at",
                    "activated_at",
                    "supersedes_revision_id",
                    "superseded_at",
                ],
                "additionalProperties": False,
                "properties": {
                    "created_at": {"bsonType": "date"},
                    "validated_at": {"bsonType": ["date", "null"]},
                    "activated_at": {"bsonType": ["date", "null"]},
                    "supersedes_revision_id": {
                        "bsonType": ["string", "null"],
                    },
                    "superseded_at": {"bsonType": ["date", "null"]},
                },
            },
            "body": {"bsonType": "object"},
        },
    }
}


PROJECTION_MANIFESTS_SCHEMA: dict = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": [
            "schema_version",
            "store",
            "family",
            "representation_role",
            "source_schema_hashes",
            "payload_schema_hash",
            "recipe_version",
        ],
        "additionalProperties": False,
        "properties": {
            "schema_version": {"enum": ["projection_manifest.v1"]},
            "store": {"enum": ["qdrant", "neo4j"]},
            "family": {"bsonType": "string", "minLength": 1},
            "representation_role": {"bsonType": "string", "minLength": 1},
            "source_schema_hashes": {
                "bsonType": "object",
                "additionalProperties": {
                    "bsonType": "string",
                    "pattern": _SHA256_PATTERN,
                },
            },
            "payload_schema_hash": {
                "bsonType": "string",
                "pattern": _SHA256_PATTERN,
            },
            "embedding_profile": {
                "bsonType": ["object", "null"],
                "required": [
                    "model_id",
                    "dims",
                    "quantization",
                    "instruction_version",
                    "document_side_instruction",
                ],
                "additionalProperties": False,
                "properties": {
                    "model_id": {"bsonType": "string"},
                    "dims": {"bsonType": ["int", "long"]},
                    "quantization": {
                        "enum": ["float32", "float16", "mxfp8", "binary"]
                    },
                    "instruction_version": {"bsonType": "string"},
                    "document_side_instruction": {"enum": ["raw"]},
                },
            },
            "search_compat": {
                "bsonType": ["object", "null"],
                "required": [
                    "oversampling",
                    "rescore_with_full_vectors",
                    "distance",
                ],
                "additionalProperties": False,
                "properties": {
                    "oversampling": {"bsonType": ["double", "int", "long"]},
                    "rescore_with_full_vectors": {"bsonType": "bool"},
                    "distance": {"enum": ["cosine", "dot", "euclid"]},
                },
            },
            "recipe_version": {"bsonType": "string", "minLength": 1},
            "rollback_predecessor": {"bsonType": ["string", "null"]},
        },
    }
}


PROJECTION_OUTBOX_SCHEMA: dict = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": [
            "schema_version",
            "outbox_id",
            "artifact_revision_id",
            "manifest_id",
            "op",
            "state",
            "attempt_count",
            "max_attempts",
        ],
        "additionalProperties": False,
        "properties": {
            "schema_version": {"enum": ["projection_outbox.v1"]},
            "outbox_id": {
                "bsonType": "string",
                "pattern": r"^outbox:[0-9a-f]{64}$",
            },
            "artifact_revision_id": {
                "bsonType": "string",
                "pattern": r"^rev:[0-9a-f]{64}$",
            },
            "manifest_id": {
                "bsonType": "string",
                "pattern": r"^projm:[0-9a-f]{64}$",
            },
            "op": {"enum": ["upsert", "delete"]},
            "state": {
                "enum": ["pending", "in_flight", "applied", "failed", "dead"]
            },
            "attempt_count": {
                "bsonType": ["int", "long"],
                "minimum": 0,
            },
            "max_attempts": {
                "bsonType": ["int", "long"],
                "minimum": 1,
            },
            "last_error": {"bsonType": ["string", "null"]},
        },
    }
}

#: Collection name -> validator document, in apply order.
VALIDATORS: dict = {
    "documents": DOCUMENTS_SCHEMA,
    "parent_chunks": PARENT_CHUNKS_SCHEMA,
    "ghost_b_extractions": GHOST_B_EXTRACTIONS_SCHEMA,
    "corpus_lexicon": CORPUS_LEXICON_SCHEMA,
    "summary_tree": SUMMARY_TREE_SCHEMA,
    "semantic_artifacts": SEMANTIC_ARTIFACTS_SCHEMA,
    "projection_manifests": PROJECTION_MANIFESTS_SCHEMA,
    "projection_outbox": PROJECTION_OUTBOX_SCHEMA,
}

_VALID_ACTIONS = ("warn", "error")


async def apply_validators(db, *, action: str = "warn") -> dict:
    """Attach the P0.8 validators to their collections via ``collMod``.

    ``db`` must expose awaitable ``command(document)`` and
    ``create_collection(name, **kwargs)`` (Motor's ``AsyncIOMotorDatabase``
    or any async adapter). If ``collMod`` fails (e.g. the collection does
    not exist yet), falls back to creating the collection with the
    validator attached.

    Returns ``{collection: {"status": "applied"|"created"|"failed",
    "action": action}}`` (``"failed"`` entries also carry ``"error"``).
    Never raises for a single collection failure.
    """
    if action not in _VALID_ACTIONS:
        raise ValueError(
            f"action must be one of {_VALID_ACTIONS!r}, got {action!r}"
        )

    results: dict = {}
    for collection, schema in VALIDATORS.items():
        try:
            await db.command(
                {
                    "collMod": collection,
                    "validator": schema,
                    "validationLevel": "moderate",
                    "validationAction": action,
                }
            )
            results[collection] = {"status": "applied", "action": action}
        except Exception:
            try:
                await db.create_collection(
                    collection,
                    validator=schema,
                    validationLevel="moderate",
                    validationAction=action,
                )
                results[collection] = {"status": "created", "action": action}
            except Exception as exc:
                results[collection] = {
                    "status": "failed",
                    "action": action,
                    "error": str(exc),
                }
    return results
