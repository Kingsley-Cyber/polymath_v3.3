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

Owner semantics: "local" now means a local/private OpenAI-compatible LLM
provider (for example the LAN RTX/vLLM server) using the same strict Ghost B
contract as cloud providers. The old GLiNER/GLiREL sidecar path is reachable
only through "legacy_local" or transitional mixed modes. "inherit" exists only
for pre-migration configs; the lifespan migration stamps every corpus explicit.

Dependency-free on purpose: takes primitives, returns a frozen dataclass —
runnable standalone by tests/test_extraction_contract.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from services.provider_payload import INTERNAL_MODEL_FLAGS, provider_payload_extras

ENGINES = (
    "off",
    "local",
    "cloud",
    "legacy_local",
    "dual",
    "local_then_cloud",
    "local_then_enrich",
)

# Engines that REQUIRE a non-empty provider-card LLM pool to honor the
# contract. "local" is a private/local provider-card LLM, not the legacy
# sidecar.
_PROVIDER_REQUIRED = ("local", "cloud", "dual")
# Engines where provider-card LLM capacity is optional: rescue
# (local_then_cloud) or §13-H quality-gated enrichment (local_then_enrich) —
# the legacy sidecar still functions alone, degraded.
_PROVIDER_OPTIONAL = ("local_then_cloud", "local_then_enrich")
_LEGACY_LOCAL_ENGINES = (
    "legacy_local",
    "dual",
    "local_then_cloud",
    "local_then_enrich",
)

_PRIVATE_NET_MARKERS = (
    "localhost",
    "127.0.0.1",
    "192.168.",
    "10.",
    "172.16.",
    "172.17.",
    "172.18.",
    "172.19.",
    "172.20.",
    "172.21.",
    "172.22.",
    "172.23.",
    "172.24.",
    "172.25.",
    "172.26.",
    "172.27.",
    "172.28.",
    "172.29.",
    "172.30.",
    "172.31.",
)


@dataclass(frozen=True)
class ExtractionContract:
    engine: str  # one of ENGINES — what the worker MUST run
    source: str  # "corpus" | "global" | "default"
    pool_source: str  # "extraction_models" | "summary_models" | "none"
    pool_size: int  # resolved provider-card LLM chips count
    endpoint_urls: tuple[str, ...]  # enabled sidecar URLs (unprobed)
    errors: tuple[str, ...]  # fatal — fail the doc BEFORE extraction starts
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def uses_cloud(self) -> bool:
        # Backward-compatible name used by older callers/UI: this now means
        # "uses a provider-card LLM pool", including private RTX/vLLM.
        return self.uses_provider_llm

    @property
    def uses_provider_llm(self) -> bool:
        return self.engine in _PROVIDER_REQUIRED or self.engine in _PROVIDER_OPTIONAL

    @property
    def uses_legacy_local(self) -> bool:
        return self.engine in _LEGACY_LOCAL_ENGINES

    @property
    def uses_local(self) -> bool:
        # Backward-compatible name for legacy sidecar callers.
        return self.uses_legacy_local


def _entry_dict(entry: object) -> dict:
    if hasattr(entry, "model_dump"):
        return entry.model_dump()  # type: ignore[no-any-return, attr-defined]
    if isinstance(entry, dict):
        return dict(entry)
    return dict(entry or {})  # type: ignore[arg-type]


def _extra(entry: dict) -> dict:
    value = entry.get("extra_params") or {}
    return value if isinstance(value, dict) else {}


def _entry_uses_private_provider(entry: object) -> bool:
    data = _entry_dict(entry)
    extra = _extra(data)
    provider = (
        str(data.get("provider_preset") or data.get("provider") or "").strip().lower()
    )
    model = str(data.get("model") or data.get("model_name") or "").lower()
    base_url = str(data.get("base_url") or data.get("api_base") or "").lower()
    lifecycle = str(
        data.get("lifecycle_base_url") or extra.get("lifecycle_base_url") or ""
    ).lower()
    if provider in {
        "local_private_vllm",
        "private_vllm",
        "vllm",
        "vllm-rtx",
        "rtx",
    }:
        return True
    if bool(extra.get("managed_vllm")):
        return True
    if str(extra.get("resource_class") or "").strip().lower() in {
        "rtx",
        "remote_vllm",
        "local_private_vllm",
    }:
        return True
    if any(
        token in value
        for token in ("vllm", "polymath-extract")
        for value in (provider, model, base_url, lifecycle)
    ):
        return True
    return any(marker in base_url for marker in _PRIVATE_NET_MARKERS)


def resolve_extraction_contract(
    *,
    corpus_engine: str | None,
    global_engine: str | None,
    models_linked: bool | None,
    summary_model_count: int,
    extraction_model_count: int,
    enabled_endpoint_urls: list[str] | tuple[str, ...] | None,
    provider_pool_entries: list[object] | tuple[object, ...] | None = None,
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
                f"'local' private/provider LLM (never silently legacy sidecar)"
            )
    else:
        g = (global_engine or "").strip().lower()
        if g in ENGINES:
            engine, source = g, "global"
        else:
            engine, source = "local", "default"
            if g:
                warnings.append(
                    f"unknown global extraction engine={g!r} — defaulting to "
                    "'local' private/provider LLM"
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

    urls = tuple(
        u.strip().rstrip("/") for u in (enabled_endpoint_urls or []) if u and u.strip()
    )

    if engine in _PROVIDER_REQUIRED and pool_size == 0:
        if engine == "local":
            errors.append(
                "engine='local' now means a local/private provider-card LLM "
                "endpoint, but the resolved provider pool is EMPTY — configure "
                "a private RTX/vLLM extraction chip or switch explicitly to "
                "'legacy_local' for the deprecated GLiNER/GLiREL sidecar"
            )
        else:
            errors.append(
                f"engine={engine!r} requires a provider-card LLM pool but the "
                f"resolved pool ({'summary_models' if linked else 'extraction_models'}) is "
                f"EMPTY — {'enable Reuse Summary pool with a non-empty Summary pool' if not linked else 'configure Summary models'} "
                f"or switch the corpus to 'legacy_local'"
            )
    if (
        engine == "local"
        and pool_size > 0
        and provider_pool_entries is not None
        and not any(
            _entry_uses_private_provider(entry) for entry in provider_pool_entries
        )
    ):
        errors.append(
            "engine='local' requires at least one local_private_vllm / vLLM / "
            "managed RTX provider chip; cloud/API-only chips must use "
            "engine='cloud'"
        )
    if engine in _PROVIDER_OPTIONAL and pool_size == 0:
        if engine == "local_then_enrich":
            warnings.append(
                "local_then_enrich has no cloud pool — the RTX/cloud "
                "enrichment lane is unavailable; docs keep the local-only "
                "skeleton (coverage/fact gaps stay unfilled)"
            )
        else:
            warnings.append(
                "local_then_cloud has no cloud pool — the cloud rescue lane is "
                "unavailable; docs fail hard if the local engine fails"
            )
    if engine in _LEGACY_LOCAL_ENGINES and not urls:
        warnings.append(
            "no enabled sidecar endpoints in Settings — legacy local "
            "extraction falls back to env-wired defaults (the configured floor)"
        )

    return ExtractionContract(
        engine=engine,
        source=source,
        pool_source=pool_source
        if (
            engine != "off"
            and (engine in _PROVIDER_REQUIRED or engine in _PROVIDER_OPTIONAL)
        )
        else "none",
        pool_size=pool_size,
        endpoint_urls=urls,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
