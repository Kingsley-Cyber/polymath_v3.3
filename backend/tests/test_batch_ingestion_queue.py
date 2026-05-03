from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

import pytest

from services.ingestion import batch_queue
from services.ingestion.file_intake import IntakeValidationError, normalize_upload_filename


class _DiskUsage:
    total = 500 * 1024**3
    used = 100 * 1024**3
    free = 400 * 1024**3


def test_safe_filename_strips_paths_and_weird_chars():
    assert batch_queue._safe_filename("/tmp/bad<>name?.md") == "bad_name_.md"
    assert batch_queue._safe_filename("   ") == "upload"


def test_no_extension_markdown_filename_is_normalized_before_queueing():
    intake = normalize_upload_filename("Comprehensive Handbook", "text/markdown")
    assert intake.filename == "Comprehensive Handbook.md"
    assert intake.normalized is True


def test_unknown_no_extension_file_is_rejected_before_parser_work():
    with pytest.raises(IntakeValidationError):
        normalize_upload_filename("mystery_upload", "application/octet-stream")


def test_resource_profile_lowers_active_docs_under_low_ram(tmp_path, monkeypatch):
    monkeypatch.setattr(batch_queue, "_sys_memory", lambda: (8.0, 3.2))
    monkeypatch.setattr(batch_queue.os, "cpu_count", lambda: 32)
    monkeypatch.setattr(batch_queue.shutil, "disk_usage", lambda _path: _DiskUsage())

    class _Torch:
        class cuda:
            @staticmethod
            def is_available():
                return False

    monkeypatch.setitem(__import__("sys").modules, "torch", _Torch)

    profile = batch_queue.detect_resource_profile(str(tmp_path))

    assert profile["max_active_docs"] == 1
    assert profile["recommended_parse_concurrency"] == 1
    assert profile["recommended_vector_concurrency"] == 1
    assert profile["recommended_graph_concurrency"] == 1


def test_resource_profile_sets_known_gpu_batch_sizes(tmp_path, monkeypatch):
    monkeypatch.setattr(batch_queue, "_sys_memory", lambda: (64.0, 48.0))
    monkeypatch.setattr(batch_queue.os, "cpu_count", lambda: 24)
    monkeypatch.setattr(batch_queue.shutil, "disk_usage", lambda _path: _DiskUsage())

    class _Props:
        total_memory = 24 * 1024**3

    class _Cuda:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def device_count():
            return 2

        @staticmethod
        def get_device_properties(_idx):
            return _Props()

        @staticmethod
        def memory_allocated(_idx):
            return 0

        @staticmethod
        def memory_reserved(_idx):
            return 0

        @staticmethod
        def get_device_name(idx):
            return ["NVIDIA GeForce RTX 4070", "NVIDIA GeForce RTX 3090"][idx]

    class _Torch:
        cuda = _Cuda

    monkeypatch.setitem(__import__("sys").modules, "torch", _Torch)

    profile = batch_queue.detect_resource_profile(str(tmp_path))

    assert profile["gpu_count"] == 2
    assert profile["recommended_local_worker_batch_sizes"] == {
        "cuda:0": 8,
        "cuda:1": 16,
    }
    assert profile["recommended_graph_concurrency"] == 2


def test_item_status_shows_graph_extracting_during_graph_phase():
    ws = SimpleNamespace(vector_ready=True, qdrant_written=True, graph_status="graph_extracting")
    assert batch_queue.item_status_from_write_state(ws) == "graph_extracting"


def test_item_status_maps_partial_and_backfill_honestly():
    assert (
        batch_queue.item_status_from_write_state(
            SimpleNamespace(vector_ready=True, qdrant_written=True, graph_status="graph_partial")
        )
        == "graph_partial"
    )
    assert (
        batch_queue.item_status_from_write_state(
            SimpleNamespace(vector_ready=True, qdrant_written=True, graph_status="needs_backfill")
        )
        == "needs_backfill"
    )


def test_terminal_batch_status_reports_partial_errors():
    assert (
        batch_queue._terminal_batch_status(
            total_files=4, failed=2, needs_backfill=2, graph_partial=0
        )
        == "completed_with_errors"
    )
    assert (
        batch_queue._terminal_batch_status(
            total_files=2, failed=2, needs_backfill=0, graph_partial=0
        )
        == "failed"
    )


def test_batch_count_fields_counts_vector_ready_independently():
    fields = batch_queue._batch_count_fields(
        Counter({"needs_backfill": 2, "failed": 2}),
        vector_ready=2,
    )
    assert fields["vector_ready_count"] == 2
    assert fields["needs_backfill_count"] == 2
    assert fields["failed_count"] == 2
