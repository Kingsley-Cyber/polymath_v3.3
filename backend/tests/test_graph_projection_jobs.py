"""Portable invariants for graph_projection_jobs control plane."""

from __future__ import annotations

from services.graph.projection_jobs import (
    CAPABILITY_ELIGIBLE,
    TERMINAL,
    classify_extraction_orphan,
    graph_job_id,
    input_artifact_hash_for_doc,
)


def test_graph_job_id_deterministic():
    a = graph_job_id(
        corpus_id="c1",
        corpus_generation="g1",
        document_id="d1",
        projection_lane="relation_assertions",
        input_artifact_hash="h1",
        ontology_release="ont1",
    )
    b = graph_job_id(
        corpus_id="c1",
        corpus_generation="g1",
        document_id="d1",
        projection_lane="relation_assertions",
        input_artifact_hash="h1",
        ontology_release="ont1",
    )
    assert a == b
    assert a.startswith("gproj_")


def test_graph_job_id_changes_with_lane_or_hash():
    base = dict(
        corpus_id="c1",
        corpus_generation="g1",
        document_id="d1",
        projection_lane="structural",
        input_artifact_hash="h1",
        ontology_release="ont1",
    )
    a = graph_job_id(**base)
    b = graph_job_id(**{**base, "projection_lane": "entity_mentions"})
    c = graph_job_id(**{**base, "input_artifact_hash": "h2"})
    assert a != b != c


def test_input_artifact_hash_stable_under_reorder():
    rows = [
        {"chunk_id": "b", "entities": [1], "relations": [], "facts": []},
        {"chunk_id": "a", "entities": [], "relations": [1], "facts": []},
    ]
    rev = list(reversed(rows))
    assert input_artifact_hash_for_doc(rows) == input_artifact_hash_for_doc(rev)


def test_orphan_recoverable_when_ghost_exists():
    klass, action = classify_extraction_orphan(
        job={"chunk_id": "c1", "attempts": 1},
        document={"status": "ready"},
        ghost_row={"chunk_id": "c1", "entities": []},
        corpus_generation="g1",
        job_generation="g1",
    )
    assert klass == "recoverable_projection"
    assert action == "requeue_projection_only"


def test_orphan_deleted_document():
    klass, action = classify_extraction_orphan(
        job={},
        document=None,
        ghost_row=None,
        corpus_generation="g1",
        job_generation=None,
    )
    assert klass == "deleted_document"
    assert action == "abandon_source_deleted"


def test_orphan_superseded_generation():
    klass, action = classify_extraction_orphan(
        job={},
        document={"status": "ready"},
        ghost_row=None,
        corpus_generation="g2",
        job_generation="g1",
    )
    assert klass == "superseded_generation"
    assert action == "abandon_superseded"


def test_orphan_missing_extraction_default():
    klass, action = classify_extraction_orphan(
        job={"attempts": 0, "engine": "graphify_cpu"},
        document={"status": "ready"},
        ghost_row=None,
        corpus_generation="g1",
        job_generation="g1",
    )
    assert klass == "missing_extraction"
    assert action == "enqueue_bounded_reextraction"


def test_orphan_hash_contract_is_not_legacy():
    klass, action = classify_extraction_orphan(
        job={
            "attempts": 0,
            "extraction_contract_hash": "2e240684457f214e3fb9198716afca9fcf7e455a571e09547d0d2591a124b73e",
        },
        document={"status": "ready"},
        ghost_row=None,
        corpus_generation="g1",
        job_generation="g1",
    )
    assert klass == "missing_extraction"
    assert action == "enqueue_bounded_reextraction"


def test_orphan_dead_letter_after_many_attempts():
    klass, action = classify_extraction_orphan(
        job={"attempts": 9, "engine": "graphify_cpu"},
        document={"status": "ready"},
        ghost_row=None,
        corpus_generation="g1",
        job_generation="g1",
    )
    assert klass == "corrupt_or_ambiguous"
    assert action == "dead_letter"


def test_noop_not_capability_eligible():
    assert "NOOP_NO_ELIGIBLE_ARTIFACTS" in TERMINAL
    assert "NOOP_NO_ELIGIBLE_ARTIFACTS" not in CAPABILITY_ELIGIBLE
    assert CAPABILITY_ELIGIBLE == frozenset({"CERTIFIED"})


def test_plan_idempotent_same_job_id():
    """In-memory fake Mongo: planning twice must not create duplicate job ids."""

    import asyncio

    class FakeCol:
        def __init__(self):
            self.rows: dict[str, dict] = {}

        async def create_index(self, *a, **k):
            return None

        async def find_one(self, q):
            if "graph_job_id" in q:
                return self.rows.get(q["graph_job_id"])
            if "corpus_id" in q and "doc_id" in q:
                return {"corpus_id": q["corpus_id"], "doc_id": q["doc_id"], "status": "ready"}
            if "corpus_id" in q:
                return {"corpus_id": q["corpus_id"], "updated_at": "t1"}
            return None

        async def insert_one(self, doc):
            self.rows[doc["graph_job_id"]] = dict(doc)
            return None

        async def update_one(self, q, upd):
            row = self.rows.get(q["graph_job_id"])
            if row and "$set" in upd:
                row.update(upd["$set"])
            return None

        def find(self, q):
            class C:
                async def to_list(self, n):
                    return [
                        {
                            "chunk_id": "c1",
                            "entities": [{"name": "A"}],
                            "relations": [{"subject": "A", "object": "B"}],
                            "facts": [],
                            "local_extraction": {},
                        }
                    ]

            return C()

        async def count_documents(self, q):
            return 1

    class FakeDB(dict):
        def __getitem__(self, key):
            if key not in self:
                self[key] = FakeCol()
            return dict.__getitem__(self, key)

    from services.graph.projection_jobs import plan_projection_jobs_for_document

    async def _run():
        db = FakeDB()
        jobs = FakeCol()
        db["graph_projection_jobs"] = jobs
        db["corpora"] = FakeCol()
        db["documents"] = FakeCol()
        db["ghost_b_extractions"] = FakeCol()
        db["chunks"] = FakeCol()

        r1 = await plan_projection_jobs_for_document(
            db, corpus_id="c1", document_id="d1"
        )
        r2 = await plan_projection_jobs_for_document(
            db, corpus_id="c1", document_id="d1"
        )
        ids1 = {j["graph_job_id"] for j in r1["jobs"]}
        ids2 = {j["graph_job_id"] for j in r2["jobs"]}
        assert ids1 == ids2
        assert len(jobs.rows) == len(ids1)
        assert all(not j.get("upsert") for j in r2["jobs"])

    asyncio.run(_run())
