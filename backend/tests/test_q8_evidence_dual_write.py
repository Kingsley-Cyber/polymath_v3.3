"""q8 (owner directive 2026-08-04) — one-point-per-child evidence canary.

Regression coverage for the shadow dual-write seam:

* candidate naming lives OUTSIDE the legacy kind vocabulary so no legacy
  alias / readiness / read path can resolve it accidentally;
* route-eligibility vocabulary matches the three proof routes exactly
  (naive -> focused, hrag -> hierarchical, graph -> graph-seed);
* payload indexes stay minimal (runtime-proven filter fields only);
* dual-write is OFF by default and shadow failures never break the legacy
  production write path;
* eligibility flags OR-merge across the worker's sequential single-kind
  upsert_children calls (no flag clobbering);
* doc-scoped and corpus-scoped deletion cascades stay complete.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config import Settings
from services.storage import qdrant_writer

CORPUS_ID = "c6518e7b-1327-4694-85c8-08a81542e425"


# ── vocabulary / naming ──────────────────────────────────────────────────────


def test_evidence_naming_is_outside_legacy_kind_vocabulary():
    name = qdrant_writer._evidence_col_for_corpus(CORPUS_ID)

    assert name == "corpus_c6518e7b_evidence"
    assert "evidence" not in qdrant_writer._VALID_KINDS
    with pytest.raises(ValueError):
        qdrant_writer._col_for_corpus(CORPUS_ID, "evidence")


def test_evidence_naming_requires_corpus_id():
    with pytest.raises(ValueError):
        qdrant_writer._evidence_col_for_corpus("")


def test_kind_to_evidence_flag_vocabulary_matches_owner_proof_routes():
    assert qdrant_writer._KIND_TO_EVIDENCE_FLAG == {
        "naive": "eligible_focused",
        "hrag": "eligible_hierarchical",
        "graph": "eligible_graph_seed",
    }


def test_evidence_payload_indexes_are_the_minimal_runtime_set():
    # Owner directive: only fields proven to be filtered. concepts /
    # entity_ids are funnel_b SHOULD-clause fields and stay unindexed until
    # a runtime audit proves a latency dependency. q9 prune: corpus_id is
    # constant inside a per-corpus collection, so its index had zero
    # selectivity and was removed from the audited minimum set.
    assert qdrant_writer._EVIDENCE_PAYLOAD_INDEXES == (
        "doc_id",
        "parent_id",
        "chunk_type",
        "chunk_kind",
        "record_kind",
        "active",
        "eligible_focused",
        "eligible_hierarchical",
        "eligible_graph_seed",
    )
    assert "corpus_id" not in qdrant_writer._EVIDENCE_PAYLOAD_INDEXES
    assert "concepts" not in qdrant_writer._EVIDENCE_PAYLOAD_INDEXES
    assert "entity_ids" not in qdrant_writer._EVIDENCE_PAYLOAD_INDEXES


def test_dual_write_defaults_on_canonical():
    # Contract revision (owner, 2026-08-08): q8 is the CANONICAL projection —
    # the factory's target storage contract writes it everywhere by default.
    assert Settings.model_fields["QDRANT_EVIDENCE_DUAL_WRITE"].default is True
    assert (
        Settings.model_fields["QDRANT_EVIDENCE_DUAL_WRITE_CORPUS_IDS"].default
        == ""
    )


def test_dual_write_gate_respects_canary_allowlist(monkeypatch):
    monkeypatch.setattr(
        qdrant_writer.settings, "QDRANT_EVIDENCE_DUAL_WRITE", True
    )

    # Flag off -> never active.
    monkeypatch.setattr(
        qdrant_writer.settings, "QDRANT_EVIDENCE_DUAL_WRITE", False
    )
    assert qdrant_writer._evidence_dual_write_active(CORPUS_ID) is False
    monkeypatch.setattr(
        qdrant_writer.settings, "QDRANT_EVIDENCE_DUAL_WRITE", True
    )

    # Allowlist set -> only the canary corpus, everyone else untouched.
    monkeypatch.setattr(
        qdrant_writer.settings,
        "QDRANT_EVIDENCE_DUAL_WRITE_CORPUS_IDS",
        CORPUS_ID,
    )
    assert qdrant_writer._evidence_dual_write_active(CORPUS_ID) is True
    assert (
        qdrant_writer._evidence_dual_write_active("other-corpus-id") is False
    )

    # Empty allowlist -> every corpus (q9 pressure-test mode).
    monkeypatch.setattr(
        qdrant_writer.settings, "QDRANT_EVIDENCE_DUAL_WRITE_CORPUS_IDS", ""
    )
    assert qdrant_writer._evidence_dual_write_active("any-corpus") is True


# ── shadow flag merge ────────────────────────────────────────────────────────


class _FlagStoreClient:
    """Retrieve-backed store: mirrors the flag fields the shadow would see."""

    def __init__(self) -> None:
        self.store: dict[str, dict] = {}
        self.retrieve_calls = 0

    async def retrieve(self, *, ids, with_payload=None, **_kwargs):
        self.retrieve_calls += 1
        return [
            SimpleNamespace(id=pid, payload=dict(self.store[str(pid)]))
            for pid in ids
            if str(pid) in self.store
        ]


def _chunk(chunk_id: str) -> dict:
    return {
        "corpus_id": CORPUS_ID,
        "doc_id": "doc-1",
        "chunk_id": chunk_id,
        "parent_id": "parent-1",
        "source_tier": "tier_a",
        "text": f"body text {chunk_id}",
    }


@pytest.mark.asyncio
async def test_shadow_flags_or_merge_across_sequential_kind_calls(monkeypatch):
    """The worker calls upsert_children once PER kind (naive, hrag, graph).
    A later single-kind call must never clobber flags set by earlier calls.
    """
    monkeypatch.setattr(
        qdrant_writer,
        "ensure_evidence_collection_for_corpus",
        AsyncMock(return_value="corpus_c6518e7b_evidence"),
    )
    upserts: list[list] = []

    async def _capture_upsert(client, *, collection_name, points, point_label):
        upserts.append(list(points))
        # Simulate durable state: later retrieve() sees these flags.
        for point in points:
            client.store[str(point.id)] = {
                flag: point.payload[flag]
                for flag in qdrant_writer._EVIDENCE_FLAG_FIELDS
            }

    monkeypatch.setattr(qdrant_writer, "_upsert_points_batched", _capture_upsert)
    client = _FlagStoreClient()
    chunks = [_chunk("chunk-1"), _chunk("chunk-2")]
    vectors = [[0.1] * 4, [0.2] * 4]
    payloads = [
        {"chunk_id": c["chunk_id"], "chunk_text": c["text"]} for c in chunks
    ]

    # Worker sequence: naive → hrag → graph.
    for kinds in (["naive"], ["hrag"], ["graph"]):
        await qdrant_writer._upsert_evidence_shadow(
            client, CORPUS_ID, chunks, vectors, kinds, payloads, None
        )

    assert len(upserts) == 3
    first = {p.payload["chunk_id"]: p.payload for p in upserts[0]}
    assert first["chunk-1"]["eligible_focused"] is True
    assert first["chunk-1"]["eligible_hierarchical"] is False
    assert first["chunk-1"]["eligible_graph_seed"] is False

    second = {p.payload["chunk_id"]: p.payload for p in upserts[1]}
    assert second["chunk-1"]["eligible_focused"] is True  # NOT clobbered
    assert second["chunk-1"]["eligible_hierarchical"] is True
    assert second["chunk-1"]["eligible_graph_seed"] is False

    final = {p.payload["chunk_id"]: p.payload for p in upserts[2]}
    for payload in final.values():
        assert payload["record_kind"] == "child"
        assert payload["active"] is True
        assert payload["eligible_focused"] is True
        assert payload["eligible_hierarchical"] is True
        assert payload["eligible_graph_seed"] is True

    # Exactly one physical point per chunk in the candidate collection.
    assert {str(p.id) for p in upserts[2]} == {
        str(qdrant_writer._child_point_id("chunk-1")),
        str(qdrant_writer._child_point_id("chunk-2")),
    }


@pytest.mark.asyncio
async def test_shadow_readback_failure_writes_this_calls_flags_only(monkeypatch):
    monkeypatch.setattr(
        qdrant_writer,
        "ensure_evidence_collection_for_corpus",
        AsyncMock(return_value="corpus_c6518e7b_evidence"),
    )
    upserts: list[list] = []

    async def _capture_upsert(client, *, collection_name, points, point_label):
        upserts.append(list(points))

    monkeypatch.setattr(qdrant_writer, "_upsert_points_batched", _capture_upsert)

    class _BrokenRetrieveClient:
        async def retrieve(self, **_kwargs):
            raise TimeoutError("read timed out")

    await qdrant_writer._upsert_evidence_shadow(
        _BrokenRetrieveClient(),
        CORPUS_ID,
        [_chunk("chunk-1")],
        [[0.1] * 4],
        ["hrag"],
        [{"chunk_text": "x"}],
        None,
    )

    assert len(upserts) == 1
    payload = upserts[0][0].payload
    assert payload["eligible_hierarchical"] is True
    assert payload["eligible_focused"] is False
    assert payload["eligible_graph_seed"] is False


# ── upsert_children hook: gating + failure isolation ────────────────────────


def _stub_legacy_path(monkeypatch) -> None:
    """Stub the legacy write path so upsert_children runs without Qdrant."""
    monkeypatch.setattr(
        qdrant_writer, "_assert_collection_owner", AsyncMock()
    )
    monkeypatch.setattr(
        qdrant_writer, "_collection_layout", AsyncMock(return_value=(True, True))
    )


@pytest.mark.asyncio
async def test_upsert_children_skips_shadow_when_dual_write_off(monkeypatch):
    _stub_legacy_path(monkeypatch)
    monkeypatch.setattr(
        qdrant_writer, "_upsert_points_batched", AsyncMock()
    )
    shadow = AsyncMock()
    monkeypatch.setattr(qdrant_writer, "_upsert_evidence_shadow", shadow)
    monkeypatch.setattr(
        qdrant_writer.settings, "QDRANT_EVIDENCE_DUAL_WRITE", False
    )

    await qdrant_writer.upsert_children(
        object(),
        CORPUS_ID,
        [_chunk("chunk-1")],
        [[0.1] * 4],
        ["naive"],
    )

    shadow.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_children_calls_shadow_when_dual_write_on(monkeypatch):
    _stub_legacy_path(monkeypatch)
    legacy_upsert = AsyncMock()
    monkeypatch.setattr(qdrant_writer, "_upsert_points_batched", legacy_upsert)
    shadow = AsyncMock()
    monkeypatch.setattr(qdrant_writer, "_upsert_evidence_shadow", shadow)
    monkeypatch.setattr(
        qdrant_writer.settings, "QDRANT_EVIDENCE_DUAL_WRITE", True
    )
    monkeypatch.setattr(
        qdrant_writer.settings, "QDRANT_EVIDENCE_DUAL_WRITE_CORPUS_IDS", ""
    )

    await qdrant_writer.upsert_children(
        object(),
        CORPUS_ID,
        [_chunk("chunk-1")],
        [[0.1] * 4],
        ["naive"],
    )

    shadow.assert_awaited_once()
    legacy_upsert.assert_awaited_once()  # legacy write still happened


@pytest.mark.asyncio
async def test_canonical_evidence_write_failure_fails_ingest(monkeypatch):
    _stub_legacy_path(monkeypatch)
    legacy_upsert = AsyncMock()
    monkeypatch.setattr(qdrant_writer, "_upsert_points_batched", legacy_upsert)
    monkeypatch.setattr(
        qdrant_writer,
        "ensure_evidence_collection_for_corpus",
        AsyncMock(side_effect=RuntimeError("qdrant unavailable")),
    )
    monkeypatch.setattr(
        qdrant_writer.settings, "QDRANT_EVIDENCE_DUAL_WRITE", True
    )
    monkeypatch.setattr(
        qdrant_writer.settings, "QDRANT_EVIDENCE_DUAL_WRITE_CORPUS_IDS", ""
    )

    # Contract revision (owner, 2026-08-08): q8 is canonical — its write
    # failure FAILS the ingest instead of silently missing points.
    with pytest.raises(RuntimeError, match="qdrant unavailable"):
        await qdrant_writer.upsert_children(
            object(),
            CORPUS_ID,
            [_chunk("chunk-1")],
            [[0.1] * 4],
            ["naive", "hrag", "graph"],
        )


# ── deletion cascade completeness ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_points_by_doc_cascades_to_evidence_collection():
    class _DeleteClient:
        def __init__(self):
            self.deleted: list[tuple[str, object]] = []

        async def collection_exists(self, _name):
            return True

        async def delete(self, *, collection_name, points_selector, **_kwargs):
            self.deleted.append((collection_name, points_selector))
            return SimpleNamespace(operation_id=1)

    client = _DeleteClient()
    results = await qdrant_writer.delete_points_by_doc(
        client, CORPUS_ID, "doc-1"
    )

    assert results["evidence"] is True
    evidence_name = qdrant_writer._evidence_col_for_corpus(CORPUS_ID)
    evidence_deletes = [
        (name, selector)
        for name, selector in client.deleted
        if name == evidence_name
    ]
    assert len(evidence_deletes) == 1
    selector = evidence_deletes[0][1]
    keys = {condition.key for condition in selector.must}
    assert keys == {"corpus_id", "doc_id"}
    # Child-only candidate today: no summary preservation needed, but the
    # selector must not delete future summary records when asked to preserve.
    results_keep = await qdrant_writer.delete_points_by_doc(
        client, CORPUS_ID, "doc-1", preserve_summary_points=True
    )
    assert results_keep["evidence"] is True
    assert client.deleted[-1][1].must_not is not None


@pytest.mark.asyncio
async def test_delete_points_by_doc_tolerates_missing_evidence_collection():
    class _NoEvidenceClient:
        def __init__(self):
            self.deleted = []

        async def collection_exists(self, name):
            return not name.endswith("_evidence")

        async def delete(self, *, collection_name, points_selector, **_kwargs):
            self.deleted.append(collection_name)
            return SimpleNamespace(operation_id=1)

    client = _NoEvidenceClient()
    results = await qdrant_writer.delete_points_by_doc(
        client, CORPUS_ID, "doc-1"
    )

    assert results["evidence"] is False
    assert all(not name.endswith("_evidence") for name in client.deleted)
    assert {results[k] for k in ("naive", "hrag", "graph")} == {True}


@pytest.mark.asyncio
async def test_drop_collections_for_corpus_drops_evidence_collection():
    class _DropClient:
        def __init__(self):
            self.dropped: list[str] = []

        async def collection_exists(self, _name):
            return True

        async def delete_collection(self, *, collection_name):
            self.dropped.append(collection_name)

    client = _DropClient()
    dropped = await qdrant_writer.drop_collections_for_corpus(client, CORPUS_ID)

    assert dropped == 5  # naive + hrag + graph + schemas + evidence
    assert client.dropped[-1] == "corpus_c6518e7b_evidence"
    assert len(client.dropped) == len(set(client.dropped))


# ── ensure_evidence_collection_for_corpus ────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_evidence_collection_creates_minimal_indexes(monkeypatch):
    monkeypatch.setattr(qdrant_writer, "_assert_collection_owner", AsyncMock())
    monkeypatch.setattr(
        qdrant_writer, "ensure_binary_quantization", AsyncMock(return_value=False)
    )
    created_indexes: list[tuple[str, str]] = []

    async def _create_index(client, *, collection_name, field_name, **_kwargs):
        created_indexes.append((collection_name, field_name))

    monkeypatch.setattr(
        qdrant_writer, "_create_payload_index_with_retry", _create_index
    )

    class _CreateClient:
        def __init__(self):
            self.create_kwargs = None

        async def collection_exists(self, _name):
            return False

        async def create_collection(self, **kwargs):
            self.create_kwargs = kwargs

    client = _CreateClient()
    name = await qdrant_writer.ensure_evidence_collection_for_corpus(
        client, CORPUS_ID
    )

    assert name == "corpus_c6518e7b_evidence"
    assert name in qdrant_writer._COLLECTION_EXISTENCE_CACHE
    # New-corpus layout: named dense + named sparse (server-side IDF).
    assert "dense" in client.create_kwargs["vectors_config"]
    assert "sparse" in client.create_kwargs["sparse_vectors_config"]
    assert {field for _name, field in created_indexes} == set(
        qdrant_writer._EVIDENCE_PAYLOAD_INDEXES
    )
    assert all(col == name for col, _field in created_indexes)

    qdrant_writer._COLLECTION_EXISTENCE_CACHE.discard(name)


@pytest.mark.asyncio
async def test_ensure_evidence_collection_is_cached(monkeypatch):
    name = qdrant_writer._evidence_col_for_corpus(CORPUS_ID)
    qdrant_writer._COLLECTION_EXISTENCE_CACHE.add(name)
    try:
        client = object()  # must never be touched on the cached path
        resolved = await qdrant_writer.ensure_evidence_collection_for_corpus(
            client, CORPUS_ID
        )
        assert resolved == name
    finally:
        qdrant_writer._COLLECTION_EXISTENCE_CACHE.discard(name)
