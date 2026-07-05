"""Deterministic extraction-contract resolution.

One pure function owns the answer to "which extraction workflow runs for this
doc" — the per-corpus engine wins, "inherit" falls back to the global Settings
engine, and enablement is VALIDATED against what is actually configured so a
doc fails fast at contract time with one clear error instead of thousands of
silent chunk failures.

Born from the 2026-07-05 collapse (§13 ground-truth correction in
CONTINUITY/POLYMATH_ARCHITECTURE.md): global engine said "cloud", the corpus
silently borrowed 3 duplicate Qwen2.5-7B summary chips that could not hold the
JSONL contract, the enabled sidecars idled, and 110/113 docs finished
graph-dead while every screen looked green.

Owner semantics (the two-toggle model): local enabled -> "local", cloud
enabled -> "cloud", both -> "dual" (even/odd chunk split across both engines),
neither -> "off" (vectors-only, explicit). "local_then_cloud" survives as a
resilience value (local primary, cloud rescue). "inherit" exists only for
pre-migration configs; the lifespan migration stamps every corpus explicit.

Dependency-free on purpose: takes primitives, returns a frozen dataclass —
runnable standalone by tests/test_extraction_contract.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ENGINES = ("off", "local", "cloud", "dual", "local_then_cloud")

# Engines that REQUIRE a non-empty cloud pool to honor the contract.
_CLOUD_REQUIRED = ("cloud", "dual")
# Engines where cloud is optional rescue capacity.
_CLOUD_OPTIONAL = ("local_then_cloud",)
_LOCAL_ENGINES = ("local", "dual", "local_then_cloud")


@dataclass(frozen=True)
class ExtractionContract:
    engine: str  # one of ENGINES — what the worker MUST run
    source: str  # "corpus" | "global" | "default"
    pool_source: str  # "extraction_models" | "summary_models" | "none"
    pool_size: int  # resolved cloud chips count
    endpoint_urls: tuple[str, ...]  # enabled sidecar URLs (unprobed)
    errors: tuple[str, ...]  # fatal — fail the doc BEFORE extraction starts
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def uses_cloud(self) -> bool:
        return self.engine in _CLOUD_REQUIRED or self.engine in _CLOUD_OPTIONAL

    @property
    def uses_local(self) -> bool:
        return self.engine in _LOCAL_ENGINES


def resolve_extraction_contract(
    *,
    corpus_engine: str | None,
    global_engine: str | None,
    models_linked: bool | None,
    summary_model_count: int,
    extraction_model_count: int,
    enabled_endpoint_urls: list[str] | tuple[str, ...] | None,
) -> ExtractionContract:
    """Resolve the contract deterministically. Never raises; violations land
    in .errors (caller fails the doc fast) and .warnings (caller logs)."""
    warnings: list[str] = []
    errors: list[str] = []

    raw = (corpus_engine or "").strip().lower()
    if raw and raw != "inherit":
        if raw in ENGINES:
            engine, source = raw, "corpus"
        else:
            engine, source = "local", "default"
            warnings.append(
                f"unknown corpus extraction_engine={raw!r} — defaulting to "
                f"'local' (never silently cloud)"
            )
    else:
        g = (global_engine or "").strip().lower()
        if g in ENGINES:
            engine, source = g, "global"
        else:
            engine, source = "local", "default"
            if g:
                warnings.append(
                    f"unknown global extraction engine={g!r} — defaulting to 'local'"
                )

    # Cloud pool resolution mirrors the worker rule EXACTLY:
    # linked -> summary pool; unlinked -> extraction pool. Never silently fall
    # back to Summary when the user explicitly split the pools.
    linked = models_linked is True
    if linked:
        pool_size = max(0, int(summary_model_count))
        pool_source = "summary_models" if pool_size else "none"
    else:
        pool_size = max(0, int(extraction_model_count))
        pool_source = "extraction_models" if pool_size else "none"

    urls = tuple(u.strip().rstrip("/") for u in (enabled_endpoint_urls or []) if u and u.strip())

    if engine in _CLOUD_REQUIRED and pool_size == 0:
        errors.append(
            f"engine={engine!r} requires a cloud model pool but the resolved "
            f"pool ({'summary_models' if linked else 'extraction_models'}) is "
            f"EMPTY — {'enable Reuse Summary pool with a non-empty Summary pool' if not linked else 'configure Summary models'} "
            f"or switch the corpus to 'local'"
        )
    if engine in _CLOUD_OPTIONAL and pool_size == 0:
        warnings.append(
            "local_then_cloud has no cloud pool — the cloud rescue lane is "
            "unavailable; docs fail hard if the local engine fails"
        )
    if engine in _LOCAL_ENGINES and not urls:
        warnings.append(
            "no enabled sidecar endpoints in Settings — local extraction "
            "falls back to env-wired defaults (the configured floor)"
        )

    return ExtractionContract(
        engine=engine,
        source=source,
        pool_source=pool_source if engine != "off" else "none",
        pool_size=pool_size,
        endpoint_urls=urls,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
