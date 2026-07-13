"""Warn-first Mongo JSON-schema validators for durable collections (P0.8).

Additive and permissive by design:

  - Only identity-spine fields are ``required`` per collection.
  - Known optional fields are type-checked with ``bsonType`` unions that
    include ``"null"`` so legacy rows and existing writers never fail.
  - ``additionalProperties`` is never set to ``False`` — unknown fields
    stay allowed.
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
            "latent_concepts": {"bsonType": ["array", "null"]},
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

#: Collection name -> validator document, in apply order.
VALIDATORS: dict = {
    "documents": DOCUMENTS_SCHEMA,
    "parent_chunks": PARENT_CHUNKS_SCHEMA,
    "ghost_b_extractions": GHOST_B_EXTRACTIONS_SCHEMA,
    "corpus_lexicon": CORPUS_LEXICON_SCHEMA,
    "summary_tree": SUMMARY_TREE_SCHEMA,
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
