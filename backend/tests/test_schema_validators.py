"""P0.8 schema-validator tests: constants are permissive/structurally sound
and apply_validators drives collMod (with create fallback) against a fake db.
Never touches a live store."""

import pytest

from services.storage import schema_validators as sv

# The identity spine per collection — the ONLY fields a validator may require.
IDENTITY_SPINE = {
    "documents": {"doc_id", "corpus_id"},
    "parent_chunks": {"parent_id", "doc_id", "corpus_id"},
    "ghost_b_extractions": {"corpus_id", "doc_id"},
    "corpus_lexicon": {"corpus_id", "lexicon_id"},
    "summary_tree": {"corpus_id", "doc_id", "node_id", "node_type"},
}


# ---------------------------------------------------------------------------
# Structural soundness of the five $jsonSchema constants
# ---------------------------------------------------------------------------


def test_validators_cover_exactly_the_five_durable_collections():
    assert set(sv.VALIDATORS) == set(IDENTITY_SPINE)


@pytest.mark.parametrize("collection", sorted(IDENTITY_SPINE))
def test_schema_shape_and_required_is_subset_of_identity_spine(collection):
    schema = sv.VALIDATORS[collection]
    assert set(schema) == {"$jsonSchema"}
    body = schema["$jsonSchema"]
    assert body["bsonType"] == "object"
    required = set(body["required"])
    assert required <= IDENTITY_SPINE[collection], (
        f"{collection} requires non-identity fields: "
        f"{required - IDENTITY_SPINE[collection]}"
    )
    # Every required field must also be described in properties.
    assert required <= set(body["properties"])


@pytest.mark.parametrize("collection", sorted(IDENTITY_SPINE))
def test_additional_properties_stays_allowed(collection):
    body = sv.VALIDATORS[collection]["$jsonSchema"]
    assert body.get("additionalProperties") is not False


@pytest.mark.parametrize("collection", sorted(IDENTITY_SPINE))
def test_optional_type_checks_tolerate_null(collection):
    """Non-required type-checked fields must accept null (permissive unions)."""
    body = sv.VALIDATORS[collection]["$jsonSchema"]
    required = set(body["required"])
    for field, spec in body["properties"].items():
        if field in required:
            continue
        bson_type = spec.get("bsonType")
        assert isinstance(bson_type, list) and "null" in bson_type, (
            f"{collection}.{field} optional type-check must union with null"
        )


def test_summary_tree_node_type_enum():
    props = sv.SUMMARY_TREE_SCHEMA["$jsonSchema"]["properties"]
    assert set(props["node_type"]["enum"]) == {"rollup", "section", "document"}


# ---------------------------------------------------------------------------
# apply_validators against a fake db (no live stores)
# ---------------------------------------------------------------------------


class FakeDB:
    """Records collMod commands; raises for collections in `missing`."""

    def __init__(self, missing=(), create_fails=()):
        self.commands = []
        self.created = []
        self.missing = set(missing)
        self.create_fails = set(create_fails)

    async def command(self, document):
        self.commands.append(document)
        if document.get("collMod") in self.missing:
            raise RuntimeError("ns does not exist")
        return {"ok": 1}

    async def create_collection(self, name, **kwargs):
        if name in self.create_fails:
            raise RuntimeError("create refused")
        self.created.append((name, kwargs))
        return None


@pytest.mark.asyncio
async def test_apply_issues_collmod_per_collection_with_warn_default():
    db = FakeDB()
    results = await sv.apply_validators(db)

    assert len(db.commands) == len(sv.VALIDATORS)
    for cmd in db.commands:
        collection = cmd["collMod"]
        assert cmd["validator"] == sv.VALIDATORS[collection]
        assert cmd["validationAction"] == "warn"
        assert cmd["validationLevel"] == "moderate"
    assert results == {
        c: {"status": "applied", "action": "warn"} for c in sv.VALIDATORS
    }
    assert db.created == []


@pytest.mark.asyncio
async def test_apply_falls_back_to_create_collection_when_collmod_fails():
    db = FakeDB(missing={"summary_tree"})
    results = await sv.apply_validators(db)

    assert results["summary_tree"] == {"status": "created", "action": "warn"}
    (name, kwargs), = db.created
    assert name == "summary_tree"
    assert kwargs["validator"] == sv.SUMMARY_TREE_SCHEMA
    assert kwargs["validationAction"] == "warn"
    assert kwargs["validationLevel"] == "moderate"
    # Every other collection still applied normally.
    for collection in sv.VALIDATORS:
        if collection != "summary_tree":
            assert results[collection]["status"] == "applied"


@pytest.mark.asyncio
async def test_apply_reports_failed_when_both_paths_fail_without_raising():
    db = FakeDB(missing={"documents"}, create_fails={"documents"})
    results = await sv.apply_validators(db)

    assert results["documents"]["status"] == "failed"
    assert results["documents"]["action"] == "warn"
    # One failure never aborts the rest.
    assert all(
        results[c]["status"] == "applied"
        for c in sv.VALIDATORS
        if c != "documents"
    )


@pytest.mark.asyncio
async def test_apply_honors_explicit_action():
    db = FakeDB()
    results = await sv.apply_validators(db, action="error")

    assert all(cmd["validationAction"] == "error" for cmd in db.commands)
    assert all(r == {"status": "applied", "action": "error"} for r in results.values())


@pytest.mark.asyncio
async def test_apply_rejects_unknown_action():
    db = FakeDB()
    with pytest.raises(ValueError):
        await sv.apply_validators(db, action="enforce")
    assert db.commands == []
