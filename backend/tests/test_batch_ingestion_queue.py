from __future__ import annotations

from types import SimpleNamespace

from services.ingestion import batch_queue


class _DiskUsage:
    total = 500 * 1024**3
    used = 100 * 1024**3
    free = 400 * 1024**3


def test_safe_filename_strips_paths_and_weird_chars():
    assert batch_queue._safe_filename("/tmp/bad<>name?.md") == "bad_name_.md"
    assert batch_queue._safe_filename("   ") == "upload"


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


def test_item_status_preserves_vector_ready_before_graph_completion():
    ws = SimpleNamespace(vector_ready=True, qdrant_written=True, graph_status="graph_extracting")
    assert batch_queue.item_status_from_write_state(ws) == "vector_ready"


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
