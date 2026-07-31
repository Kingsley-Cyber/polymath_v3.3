"""The reconciler must OWN organ repair, not just observe it.

Detection (coverage checkpoint) and declaration (organ contract) do not make
repair happen. This asserts the lane that does: gap -> planned job -> executor
-> receipt with a number.

Portable: fakes for Mongo, no live stack.
"""

from __future__ import annotations

import pytest

from services.control_plane.extraction_organs import LANE_LOCAL, LANE_POD
from services.ingestion.organ_repair_jobs import (
    ORGAN_REPAIR_COLLECTION, STATUS_BLOCKED, STATUS_QUEUED,
    organ_repair_job_id, plan_organ_repair_jobs,
)


class _FakeColl:
    def __init__(self):
        self.docs: list[dict] = []

    async def update_one(self, flt, update, upsert=False, array_filters=None):
        for d in self.docs:
            if all(d.get(k) == v for k, v in flt.items()):
                d.update(update.get("$set") or {})
                return type("R", (), {"modified_count": 1})()
        if upsert:
            doc = dict(update.get("$setOnInsert") or {})
            doc.update(update.get("$set") or {})
            doc.update(flt)
            self.docs.append(doc)
        return type("R", (), {"modified_count": 0})()


class _FakeDB:
    def __init__(self):
        self.colls: dict[str, _FakeColl] = {}

    def __getitem__(self, name):
        return self.colls.setdefault(name, _FakeColl())


class TestPlanning:
    @pytest.mark.asyncio
    async def test_gap_becomes_a_durable_job(self):
        db = _FakeDB()
        out = await plan_organ_repair_jobs(
            db, corpus_id="c1",
            organ_gaps={"relations": {"status": "DEAD_ORGAN", "value": 0.0}},
            apply=True,
        )
        assert out["planned"] == 1
        jobs = db[ORGAN_REPAIR_COLLECTION].docs
        assert len(jobs) == 1
        assert jobs[0]["organ"] == "relations"
        assert jobs[0]["status"] == STATUS_QUEUED

    @pytest.mark.asyncio
    async def test_job_carries_the_declared_repair_route_not_an_invented_one(self):
        """The route must come from the contract, so it cannot drift."""
        from services.control_plane.extraction_organs import ORGAN_BY_NAME

        db = _FakeDB()
        await plan_organ_repair_jobs(
            db, corpus_id="c1",
            organ_gaps={"facts": {"status": "DEAD_ORGAN"}}, apply=True,
        )
        job = db[ORGAN_REPAIR_COLLECTION].docs[0]
        assert job["repair_route"] == ORGAN_BY_NAME["facts"].repair_route

    @pytest.mark.asyncio
    async def test_job_records_why_it_exists(self):
        db = _FakeDB()
        gap = {"status": "DEAD_ORGAN", "value": 0.0, "chunks": 161108}
        await plan_organ_repair_jobs(
            db, corpus_id="c1", organ_gaps={"relations": gap}, apply=True,
        )
        assert db[ORGAN_REPAIR_COLLECTION].docs[0]["gap"] == gap

    @pytest.mark.asyncio
    async def test_replanning_is_idempotent(self):
        db = _FakeDB()
        for _ in range(3):
            await plan_organ_repair_jobs(
                db, corpus_id="c1",
                organ_gaps={"relations": {"status": "DEAD_ORGAN"}}, apply=True,
            )
        assert len(db[ORGAN_REPAIR_COLLECTION].docs) == 1

    @pytest.mark.asyncio
    async def test_model_pass_repairs_are_flagged_for_the_scheduler(self):
        """Facets cost GPU time; relations/facts do not. That must be visible."""
        db = _FakeDB()
        out = await plan_organ_repair_jobs(
            db, corpus_id="c1",
            organ_gaps={"facets": {"status": "DEAD_ORGAN"},
                        "relations": {"status": "DEAD_ORGAN"}},
            apply=True,
        )
        assert out["needs_model_pass"] == ["facets"]

    @pytest.mark.asyncio
    async def test_organ_without_executor_is_blocked_with_a_manual_route(self):
        """Never silently drop an organ we cannot yet automate."""
        db = _FakeDB()
        out = await plan_organ_repair_jobs(
            db, corpus_id="c1",
            organ_gaps={"claims": {"status": "DEAD_ORGAN"}}, apply=True,
        )
        assert "claims" in out["skipped"]
        assert "manual route" in out["skipped"]["claims"]


class TestJobIdentity:
    def test_one_job_per_corpus_and_organ(self):
        a = organ_repair_job_id(corpus_id="c1", organ="relations")
        b = organ_repair_job_id(corpus_id="c1", organ="relations")
        c = organ_repair_job_id(corpus_id="c1", organ="facts")
        d = organ_repair_job_id(corpus_id="c2", organ="relations")
        assert a == b
        assert a != c and a != d


class TestLaneAwareness:
    """A gap the lane cannot close must never become a repair job."""

    def test_pod_lane_facet_gap_is_not_actionable(self):
        from services.control_plane.extraction_organs import (
            organs_expected_for_lane,
        )
        assert "facets" not in organs_expected_for_lane(LANE_POD)

    def test_local_lane_claim_gap_is_not_actionable(self):
        from services.control_plane.extraction_organs import (
            organs_expected_for_lane,
        )
        assert "claims" not in organs_expected_for_lane(LANE_LOCAL)


def test_reconciler_plans_organ_repair_before_extraction():
    """Order matters: an organ gap is fixed by recomputing that organ, NOT by
    re-extracting whole documents. Planning extraction first would re-run 161k
    chunks to fix a lane that was simply never wired."""
    from pathlib import Path
    src = (
        Path(__file__).resolve().parents[1]
        / "services" / "control_plane" / "reconciler.py"
    ).read_text()
    organ_at = src.index("STAGE_ORGAN_REPAIR] = receipt")
    extraction_at = src.index("STAGE_EXTRACTION] = receipt")
    assert organ_at < extraction_at, (
        "organ repair must be planned before the extraction lane"
    )


def test_reconciler_imports_the_organ_lane():
    from pathlib import Path
    src = (
        Path(__file__).resolve().parents[1]
        / "services" / "control_plane" / "reconciler.py"
    ).read_text()
    assert "plan_organ_repair_jobs" in src
    assert "_detect_organ_gaps" in src
