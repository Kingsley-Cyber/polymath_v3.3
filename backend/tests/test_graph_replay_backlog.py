from scripts.polymath_graph_replay_backlog import (
    _graph_gap_reason,
    _row_matches_repair_filters,
)


def test_graph_gap_reason_detects_missing_neo4j_flag():
    assert (
        _graph_gap_reason({"write_state": {"qdrant_written": True, "neo4j_written": False}})
        == "neo4j_missing"
    )


def test_graph_gap_reason_detects_verify_proven_graph_mismatch():
    row = {
        "write_state": {
            "qdrant_written": True,
            "neo4j_written": True,
            "verified": False,
            "verify_errors": ["neo4j: HAS_CHUNK count=0 but expected=459"],
        }
    }

    assert _graph_gap_reason(row) == "neo4j_verify_mismatch"


def test_graph_gap_reason_ignores_non_graph_verify_errors():
    row = {
        "write_state": {
            "qdrant_written": True,
            "neo4j_written": True,
            "verified": False,
            "verify_errors": ["mismatch: expected=459 child vectors but found 145"],
        }
    }

    assert _graph_gap_reason(row) is None


def test_flush_only_requires_staged_rows_and_no_failures():
    assert _row_matches_repair_filters(
        {
            "graph_gap_reason": "neo4j_verify_mismatch",
            "staged_extractions": 3,
            "failure_rows": 0,
        },
        flush_only=True,
    )
    assert not _row_matches_repair_filters(
        {
            "graph_gap_reason": "neo4j_verify_mismatch",
            "staged_extractions": 0,
            "failure_rows": 0,
        },
        flush_only=True,
    )
    assert not _row_matches_repair_filters(
        {
            "graph_gap_reason": "neo4j_verify_mismatch",
            "staged_extractions": 3,
            "failure_rows": 1,
        },
        flush_only=True,
    )
    assert not _row_matches_repair_filters(
        {
            "graph_gap_reason": "neo4j_verify_mismatch",
            "staged_extractions": 3,
            "failure_rows": 0,
            "ghost_b_failure_count": 1,
        },
        flush_only=True,
    )


def test_repair_reason_filter_is_exact():
    assert _row_matches_repair_filters(
        {"graph_gap_reason": "neo4j_verify_mismatch"},
        reasons={"neo4j_verify_mismatch"},
    )
    assert not _row_matches_repair_filters(
        {"graph_gap_reason": "neo4j_missing"},
        reasons={"neo4j_verify_mismatch"},
    )
