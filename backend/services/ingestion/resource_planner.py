"""Environment-aware ingestion resource planning.

The planner keeps the user-facing extraction contract separate from how the
Mac process allocates CPU/RAM/Metal while that contract runs. It is intentionally
small and deterministic enough to unit-test: detection is isolated, and callers
may inject a SystemResources snapshot.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import logging
import os
from pathlib import Path
import resource
import sys
from typing import Any, Literal

logger = logging.getLogger(__name__)

ExtractionBackend = Literal["off", "local_cpu"]
EmbeddingBackend = Literal["local_metal", "cpu", "remote", "disabled"]
StorageMode = Literal["local_disk", "mounted_volume", "network_share"]


@dataclass(frozen=True)
class SystemResources:
    cpu_cores: int
    ram_total_mb: int | None = None
    ram_available_mb: int | None = None
    cgroup_limit_mb: int | None = None
    process_rss_mb: int | None = None
    metal_available: bool = False
    detection_notes: tuple[str, ...] = ()

    @property
    def effective_ram_mb(self) -> int | None:
        values = [
            value
            for value in (self.ram_total_mb, self.cgroup_limit_mb)
            if value and value > 0
        ]
        return min(values) if values else self.ram_total_mb


@dataclass(frozen=True)
class ResourceProfile:
    name: str
    extraction_backend: ExtractionBackend
    extraction_lanes: tuple[str, ...]
    embedding_backend: EmbeddingBackend
    storage_mode: StorageMode
    cpu_cores: int
    ram_total_mb: int | None
    ram_available_mb: int | None
    cgroup_limit_mb: int | None
    process_rss_mb: int | None
    ram_cap_mb: int
    rss_soft_limit_mb: int
    metal_available: bool
    extraction_max_concurrent: int
    extraction_active_docs: int
    model_phase_docs: int
    embedding_batch_size: int
    qdrant_write_concurrency: int
    neo4j_write_concurrency: int
    backpressure_enabled: bool = True
    recommended_ingest_profile: str = "mac_queryable_first"
    warnings: tuple[str, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def rss_pressure(self) -> float | None:
        if not self.process_rss_mb or self.rss_soft_limit_mb <= 0:
            return None
        return self.process_rss_mb / self.rss_soft_limit_mb

    def to_log_dict(self) -> dict[str, Any]:
        data = asdict(self)
        pressure = self.rss_pressure
        data["rss_pressure"] = round(pressure, 3) if pressure is not None else None
        return data


def _read_int(path: str) -> int | None:
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text or text == "max":
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return value if value > 0 else None


def _cgroup_memory_limit_mb() -> int | None:
    candidates = (
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    )
    # Linux reports enormous sentinel values when there is no cgroup cap.
    sentinel_floor = 1 << 50
    values = [
        value
        for value in (_read_int(path) for path in candidates)
        if value and value < sentinel_floor
    ]
    return int(min(values) / (1024 * 1024)) if values else None


def _ru_maxrss_to_mb(usage: int | float, *, platform: str | None = None) -> int:
    """Normalize getrusage max RSS units for the current operating system."""

    divisor = 1024 * 1024 if (platform or sys.platform) == "darwin" else 1024
    return int(max(0, usage) / divisor)


def _process_rss_mb() -> int | None:
    try:
        import psutil  # type: ignore

        return int(psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024))
    except Exception:
        pass
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # macOS reports bytes, Linux reports KiB.
        return _ru_maxrss_to_mb(usage)
    except Exception:
        return None


def current_process_rss_mb() -> int | None:
    return _process_rss_mb()


def memory_soft_limit_mb(settings: Any | None = None) -> tuple[int, int]:
    if settings is None:
        from config import get_settings

        settings = get_settings()
    requested = int(getattr(settings, "INGEST_BACKEND_RAM_TARGET_MB", 16_384))
    cgroup_limit = _cgroup_memory_limit_mb()
    ram_cap = max(512, min(requested, cgroup_limit or requested))
    ratio = float(getattr(settings, "INGEST_RSS_SOFT_LIMIT_RATIO", 0.85))
    ratio = min(0.95, max(0.50, ratio))
    return ram_cap, max(256, int(ram_cap * ratio))


def throttle_concurrency_for_rss(
    requested_concurrency: int,
    *,
    settings: Any | None = None,
) -> tuple[int, dict[str, Any]]:
    ram_cap, soft_limit = memory_soft_limit_mb(settings)
    rss = current_process_rss_mb()
    requested = max(1, int(requested_concurrency or 1))
    if rss is None or rss < soft_limit:
        return requested, {
            "rss_mb": rss,
            "ram_cap_mb": ram_cap,
            "rss_soft_limit_mb": soft_limit,
            "throttled": False,
        }
    reduced = max(1, min(requested, requested // 2))
    return reduced, {
        "rss_mb": rss,
        "ram_cap_mb": ram_cap,
        "rss_soft_limit_mb": soft_limit,
        "throttled": reduced < requested,
    }


def _metal_available() -> tuple[bool, str | None]:
    try:
        import torch  # type: ignore

        return bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()), None
    except Exception as exc:
        return False, f"torch_mps_unavailable:{type(exc).__name__}"


def detect_system_resources() -> SystemResources:
    notes: list[str] = []
    cpu_cores = max(1, os.cpu_count() or 1)
    ram_total_mb: int | None = None
    ram_available_mb: int | None = None
    try:
        import psutil  # type: ignore

        vm = psutil.virtual_memory()
        ram_total_mb = int(vm.total / (1024 * 1024))
        ram_available_mb = int(vm.available / (1024 * 1024))
    except Exception as exc:
        notes.append(f"psutil_unavailable:{type(exc).__name__}")
        try:
            page_size = os.sysconf("SC_PAGE_SIZE")
            pages = os.sysconf("SC_PHYS_PAGES")
            ram_total_mb = int((page_size * pages) / (1024 * 1024))
        except Exception:
            ram_total_mb = None

    metal, metal_note = _metal_available()
    if metal_note:
        notes.append(metal_note)

    return SystemResources(
        cpu_cores=cpu_cores,
        ram_total_mb=ram_total_mb,
        ram_available_mb=ram_available_mb,
        cgroup_limit_mb=_cgroup_memory_limit_mb(),
        process_rss_mb=_process_rss_mb(),
        metal_available=metal,
        detection_notes=tuple(notes),
    )


def classify_storage_mode(source_location: str | None) -> StorageMode:
    raw = (source_location or "").strip()
    lower = raw.lower()
    if lower.startswith(("smb://", "nfs://", "afp://")):
        return "network_share"
    if raw.startswith("/Volumes/") or raw.startswith("/mnt/"):
        return "mounted_volume"
    return "local_disk"


def classify_embedding_backend(
    *,
    config: Any,
    resources: SystemResources,
) -> EmbeddingBackend:
    mode = str(getattr(config, "embed_mode", "local") or "local").lower()
    aliases = {"local_st": "local", "modal_tei": "modal", "siliconflow": "api"}
    mode = aliases.get(mode, mode)
    if mode in {"off", "disabled", "none"}:
        return "disabled"
    if mode in {"api", "modal", "remote"}:
        return "remote"
    return "local_metal" if resources.metal_available else "cpu"


def classify_extraction_backend(
    *,
    extraction_engine: str | None,
    extraction_pool: list[Any] | tuple[Any, ...] | None,
) -> tuple[ExtractionBackend, tuple[str, ...]]:
    engine = str(extraction_engine or "graphify_cpu").lower()
    if engine == "graphify_cpu":
        return "local_cpu", ("local_cpu",)
    if engine == "off":
        return "off", ("off",)
    raise ValueError(f"unsupported extraction engine: {engine}")


def plan_ingestion_resources(
    *,
    config: Any,
    extraction_engine: str | None,
    extraction_pool: list[Any] | tuple[Any, ...] | None,
    source_location: str | None = None,
    settings: Any | None = None,
    resources: SystemResources | None = None,
) -> ResourceProfile:
    if settings is None:
        from config import get_settings

        settings = get_settings()
    resources = resources or detect_system_resources()

    extraction_backend, extraction_lanes = classify_extraction_backend(
        extraction_engine=extraction_engine,
        extraction_pool=extraction_pool,
    )
    embedding_backend = classify_embedding_backend(config=config, resources=resources)
    storage_mode = classify_storage_mode(source_location)
    max_concurrent = max(1, int(getattr(settings, "EXTRACTION_MAX_CONCURRENT", 8)))

    requested_ram_cap = int(getattr(settings, "INGEST_BACKEND_RAM_TARGET_MB", 16_384))
    effective_ram = resources.effective_ram_mb or requested_ram_cap
    ram_cap_mb = max(512, min(requested_ram_cap, effective_ram))
    ratio = float(getattr(settings, "INGEST_RSS_SOFT_LIMIT_RATIO", 0.85))
    ratio = min(0.95, max(0.50, ratio))
    rss_soft_limit_mb = max(256, int(ram_cap_mb * ratio))
    rss_high = bool(resources.process_rss_mb and resources.process_rss_mb >= rss_soft_limit_mb)

    warnings: list[str] = []
    notes: list[str] = list(resources.detection_notes)
    if rss_high:
        warnings.append(
            f"process RSS {resources.process_rss_mb}MB exceeds soft limit {rss_soft_limit_mb}MB"
        )
    extraction_active_docs = max(
        1,
        int(getattr(settings, "EXTRACTION_MAX_ACTIVE_DOCS", 1)),
    )
    model_phase_docs = max(
        1,
        int(getattr(settings, "INGEST_MAX_MODEL_PHASE_DOCS", 1)),
    )

    configured_embed_batch = max(1, int(getattr(settings, "EMBED_BATCH_SIZE", 32)))
    if embedding_backend in {"local_metal", "cpu"} and rss_high:
        embedding_batch_size = min(configured_embed_batch, 16)
    else:
        embedding_batch_size = configured_embed_batch
    recommended_profile = "mac_queryable_first"
    name_parts = [extraction_backend, embedding_backend, storage_mode]
    return ResourceProfile(
        name="+".join(name_parts),
        extraction_backend=extraction_backend,
        extraction_lanes=extraction_lanes,
        embedding_backend=embedding_backend,
        storage_mode=storage_mode,
        cpu_cores=resources.cpu_cores,
        ram_total_mb=resources.ram_total_mb,
        ram_available_mb=resources.ram_available_mb,
        cgroup_limit_mb=resources.cgroup_limit_mb,
        process_rss_mb=resources.process_rss_mb,
        ram_cap_mb=ram_cap_mb,
        rss_soft_limit_mb=rss_soft_limit_mb,
        metal_available=resources.metal_available,
        extraction_max_concurrent=max_concurrent,
        extraction_active_docs=extraction_active_docs,
        model_phase_docs=model_phase_docs,
        embedding_batch_size=embedding_batch_size,
        qdrant_write_concurrency=max(
            1,
            int(getattr(settings, "QDRANT_INGEST_WRITE_CONCURRENCY", 2)),
        ),
        neo4j_write_concurrency=max(
            1,
            int(getattr(settings, "NEO4J_INGEST_WRITE_CONCURRENCY", 1)),
        ),
        recommended_ingest_profile=recommended_profile,
        warnings=tuple(warnings),
        notes=tuple(notes),
    )


def log_resource_profile(
    profile: ResourceProfile,
    *,
    prefix: str = "phase=resource_profile",
    extra: dict[str, Any] | None = None,
) -> None:
    payload = profile.to_log_dict()
    if extra:
        payload.update(extra)
    logger.info("%s %s", prefix, payload)
    for warning in profile.warnings:
        logger.warning("%s warning=%s", prefix, warning)
