from __future__ import annotations

from services.ops_drills import kill_seam


def test_kill_seam_is_inert_without_env(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(kill_seam.KILL_ENV, raising=False)
    fired = []
    monkeypatch.setattr(kill_seam, "_exit", lambda code: fired.append(code))
    kill_seam.ops_kill_point("ENTITY_CENSUS_COMPLETE:after_compute")
    assert fired == []


def test_kill_seam_fires_once_then_marker_disarms(monkeypatch, tmp_path) -> None:
    point = "ENTITY_CENSUS_COMPLETE:after_artifact"
    monkeypatch.setenv(kill_seam.KILL_ENV, point)
    monkeypatch.setenv(kill_seam.MARKER_ENV, str(tmp_path))
    fired = []
    monkeypatch.setattr(kill_seam, "_exit", lambda code: fired.append(code))
    kill_seam.ops_kill_point(point)
    assert fired == [kill_seam.EXIT_CODE]
    kill_seam.ops_kill_point(point)
    assert fired == [kill_seam.EXIT_CODE]


def test_kill_seam_ignores_other_points(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(kill_seam.KILL_ENV, "store:qdrant:after_write")
    monkeypatch.setenv(kill_seam.MARKER_ENV, str(tmp_path))
    fired = []
    monkeypatch.setattr(kill_seam, "_exit", lambda code: fired.append(code))
    kill_seam.ops_kill_point("store:neo4j:after_write")
    assert fired == []
