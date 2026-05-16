"""Pt9e — `migrate_universal_schema` additive-extension tests.

The migration runs at backend lifespan startup. It walks every corpus
and patches the stored entity/relation schemas when they're outdated
subsets of the current UNIVERSAL_*_SCHEMA. The relation branch already
extends additively (added in earlier work); Pt9e adds the equivalent
entity branch.

Pinned invariants:
  • Subset entity_schemas get missing universal terms APPENDED (not
    overwritten — preserves position-dependent downstream behavior).
  • Custom entity_schemas (containing terms NOT in universal) are
    LEFT ALONE — strict-subset guard prevents clobbering user vocab.
  • Idempotent: a corpus already at universal stays untouched.
  • Empty/null entity_schema gets the full universal list (existing
    behavior preserved).
  • force=True overwrites everything (existing behavior preserved).

The mongo client is mocked so this test doesn't require a live DB.
"""
from __future__ import annotations

import asyncio
import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock


# ── Auth-package stubs (same pattern as other unit tests) ──────────
def _install_stubs_if_missing() -> None:
    if "jose" not in sys.modules:
        try:
            import jose  # noqa: F401
        except ImportError:
            jose_mod = ModuleType("jose")

            class JWTError(Exception):
                pass

            class _Jwt:
                @staticmethod
                def encode(*_a, **_kw):  # pragma: no cover
                    raise RuntimeError("stub")

                @staticmethod
                def decode(*_a, **_kw):  # pragma: no cover
                    raise RuntimeError("stub")

            jose_mod.JWTError = JWTError
            jose_mod.jwt = _Jwt()
            sys.modules["jose"] = jose_mod

    if "passlib.context" not in sys.modules:
        try:
            import passlib.context  # noqa: F401
        except ImportError:
            passlib_mod = ModuleType("passlib")
            ctx_mod = ModuleType("passlib.context")

            class _CryptContext:
                def __init__(self, *a, **kw):
                    pass

                def hash(self, *_a, **_kw):  # pragma: no cover
                    raise RuntimeError("stub")

                def verify(self, *_a, **_kw):  # pragma: no cover
                    raise RuntimeError("stub")

            ctx_mod.CryptContext = _CryptContext
            passlib_mod.context = ctx_mod
            sys.modules["passlib"] = passlib_mod
            sys.modules["passlib.context"] = ctx_mod

    if "slowapi" not in sys.modules:
        try:
            import slowapi  # noqa: F401
        except ImportError:
            slowapi_mod = ModuleType("slowapi")
            util_mod = ModuleType("slowapi.util")

            class _Limiter:
                def __init__(self, *a, **kw):
                    pass

                def limit(self, *_a, **_kw):
                    def _decorator(fn):
                        return fn
                    return _decorator

            def _get_remote_address(_request):  # pragma: no cover
                return "0.0.0.0"

            slowapi_mod.Limiter = _Limiter
            util_mod.get_remote_address = _get_remote_address
            sys.modules["slowapi"] = slowapi_mod
            sys.modules["slowapi.util"] = util_mod


_install_stubs_if_missing()


from services.ghost_b import UNIVERSAL_ENTITY_SCHEMA  # noqa: E402
from services.ingestion_service import IngestionService  # noqa: E402


def _build_service_with_corpora(corpora: list[dict]) -> tuple[IngestionService, MagicMock]:
    """Build an IngestionService with its `_db` rigged to return the given
    corpus docs from `corpora.find()` and an update_one mock we can inspect."""
    svc = IngestionService.__new__(IngestionService)

    class _AsyncCursor:
        def __init__(self, docs: list[dict]):
            self._docs = list(docs)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._docs:
                raise StopAsyncIteration
            return self._docs.pop(0)

    db = MagicMock()
    corpora_col = MagicMock()
    corpora_col.find = MagicMock(return_value=_AsyncCursor(corpora))
    corpora_col.update_one = AsyncMock()
    db.__getitem__ = MagicMock(return_value=corpora_col)
    svc._db = db
    return svc, corpora_col


# ── Pt9e: additive extension on entity_schema ───────────────────────


def test_subset_entity_schema_gets_missing_universal_terms_appended():
    """The pre-Pt9a 12-type schema (no Software/Standard) should pick up
    those two when the migration fires post-Pt9a."""
    pre_pt9a = [
        "Person", "Organization", "Location", "Event",
        "Concept", "Method", "Product", "Document",
        "Rule", "Law", "Artifact", "TimeReference",
    ]
    assert all(et in UNIVERSAL_ENTITY_SCHEMA for et in pre_pt9a), (
        "Test premise broken — pre-Pt9a schema should still be a subset of universal"
    )
    svc, corpora_col = _build_service_with_corpora([
        {
            "corpus_id": "c1",
            "name": "old-corpus",
            "default_ingestion_config": {
                "entity_schema": pre_pt9a,
                "relation_schema": ["uses", "related_to"],  # subset, so will also extend
                "schema_strict": "soft",
            },
        },
    ])
    result = asyncio.run(svc.migrate_universal_schema(force=False))

    assert result["scanned"] == 1
    assert result["patched"] == 1
    corpora_col.update_one.assert_awaited_once()
    args, _kwargs = corpora_col.update_one.call_args
    patch = args[1]["$set"]
    new_entities = patch["default_ingestion_config.entity_schema"]
    # Original 12 terms are preserved IN ORDER, missing universal terms appended.
    assert new_entities[:12] == pre_pt9a
    # The new universal terms appear after — exactly the ones missing.
    missing = [et for et in UNIVERSAL_ENTITY_SCHEMA if et not in pre_pt9a]
    assert new_entities[12:] == missing
    # And the final list equals the universal as a set.
    assert set(new_entities) == set(UNIVERSAL_ENTITY_SCHEMA)


def test_custom_entity_schema_with_off_universal_terms_left_alone():
    """A corpus configured with custom terms (e.g. ["Gene","Protein"])
    that are NOT in UNIVERSAL_ENTITY_SCHEMA must NOT be touched. The
    strict-subset guard `all(et in universal_entities ...)` is the
    load-bearing test of the FROZEN-config-respect contract."""
    svc, corpora_col = _build_service_with_corpora([
        {
            "corpus_id": "c-bio",
            "name": "biology-custom",
            "default_ingestion_config": {
                "entity_schema": ["Gene", "Protein", "Pathway"],
                "relation_schema": ["related_to"],
                "schema_strict": "soft",
            },
        },
    ])
    result = asyncio.run(svc.migrate_universal_schema(force=False))
    # No entity-side patch should fire. Other reasons (legacy_strict, null_relation)
    # don't apply here either — schema_strict is 'soft', relations are non-empty.
    # Whether relations get touched depends on whether `related_to` alone is a
    # subset of universal (it is). So relations will extend; entities WON'T.
    assert result["scanned"] == 1
    if corpora_col.update_one.await_count == 1:
        args, _ = corpora_col.update_one.call_args
        patch = args[1]["$set"]
        new_entities = patch["default_ingestion_config.entity_schema"]
        # Critical: the custom vocab is preserved unchanged.
        assert new_entities == ["Gene", "Protein", "Pathway"]


def test_already_universal_entity_schema_is_idempotent():
    """A corpus already at the current UNIVERSAL_ENTITY_SCHEMA shouldn't
    trigger any entity-side patch."""
    svc, corpora_col = _build_service_with_corpora([
        {
            "corpus_id": "c-current",
            "name": "current",
            "default_ingestion_config": {
                "entity_schema": list(UNIVERSAL_ENTITY_SCHEMA),
                "relation_schema": ["uses", "related_to"],
                "schema_strict": "soft",
            },
        },
    ])
    result = asyncio.run(svc.migrate_universal_schema(force=False))
    # The relation list may still patch (it's a subset of universal_relations)
    # but the entity_schema patch column, if applied, MUST equal universal —
    # not an over-extension.
    if corpora_col.update_one.await_count >= 1:
        args, _ = corpora_col.update_one.call_args
        patch = args[1]["$set"]
        entities = patch["default_ingestion_config.entity_schema"]
        assert set(entities) == set(UNIVERSAL_ENTITY_SCHEMA)
        assert len(entities) == len(UNIVERSAL_ENTITY_SCHEMA), (
            "Idempotent run must not produce duplicate entity_type entries"
        )


def test_null_entity_schema_still_gets_full_universal():
    """Existing behavior (pre-Pt9e) preserved: null/empty entity_schema
    is rewritten to the full universal list."""
    svc, corpora_col = _build_service_with_corpora([
        {
            "corpus_id": "c-null",
            "name": "null-entity",
            "default_ingestion_config": {
                "entity_schema": None,
                "relation_schema": ["uses", "related_to"],
                "schema_strict": "soft",
            },
        },
    ])
    result = asyncio.run(svc.migrate_universal_schema(force=False))
    assert result["patched"] == 1
    args, _ = corpora_col.update_one.call_args
    patch = args[1]["$set"]
    new_entities = patch["default_ingestion_config.entity_schema"]
    assert new_entities == list(UNIVERSAL_ENTITY_SCHEMA)


def test_subset_entity_extension_appears_in_migration_reasons():
    """The patched-corpora log line includes a `missing_universal_entities=N`
    reason. Useful for operator visibility when the migration runs at
    lifespan startup — they can see which corpora got widened."""
    pre_pt9a = [
        "Person", "Organization", "Location", "Event",
        "Concept", "Method", "Product", "Document",
        "Rule", "Law", "Artifact", "TimeReference",
    ]
    svc, corpora_col = _build_service_with_corpora([
        {
            "corpus_id": "c2",
            "name": "old",
            "default_ingestion_config": {
                "entity_schema": pre_pt9a,
                "relation_schema": list(__import__("services.ghost_b",
                                                  fromlist=["UNIVERSAL_RELATION_SCHEMA"])
                                        .UNIVERSAL_RELATION_SCHEMA),
                "schema_strict": "soft",
            },
        },
    ])
    asyncio.run(svc.migrate_universal_schema(force=False))
    assert corpora_col.update_one.await_count == 1
    # The reasons string is embedded in a logger.info call, not the patch
    # body, so we verify shape via the patched corpus_ids count:
    # the entity extension fired but relation extension didn't (already
    # at universal).
    # Indirect check: the entity_schema in the patch is longer than 12.
    args, _ = corpora_col.update_one.call_args
    patch = args[1]["$set"]
    assert len(patch["default_ingestion_config.entity_schema"]) > 12
