"""Fail-closed extraction contract for qualified extraction paths."""

from __future__ import annotations

from dataclasses import dataclass, field

# Re-export: five lazy call sites (batches summary canary, ghost_a x2,
# ghost_b x2) import this helper from the contract module. The re-export
# was lost in the freeze-era rewrite; its home is services.provider_payload.
from services.provider_payload import (  # noqa: F401
    ingestion_provider_payload_extras,
)

ENGINES = ("off", "graphify_cpu", "ghost_b_llm")
CANONICAL_ENGINE = "graphify_cpu"


@dataclass(frozen=True)
class ExtractionContract:
    engine: str
    source: str
    pool_source: str = "none"
    pool_size: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def uses_graphify_cpu(self) -> bool:
        return self.engine == CANONICAL_ENGINE


def resolve_extraction_contract(
    *,
    corpus_engine: str | None,
    global_engine: str | None,
    models_linked: bool | None,
    summary_model_count: int,
    extraction_model_count: int,
    provider_pool_entries: list[object] | tuple[object, ...] | None = None,
) -> ExtractionContract:
    """Resolve a registered engine or the explicit vectors-only opt-out.

    Legacy arguments stay in the function signature until API callers finish
    shedding provider-pool fields. Engine selection is corpus, then global,
    then Graphify CPU by default. Retired names fail closed without aliases.
    """
    del models_linked, summary_model_count, extraction_model_count, provider_pool_entries
    corpus_value = str(corpus_engine or "").strip().lower()
    global_value = str(global_engine or "").strip().lower()
    if corpus_value:
        value, source = corpus_value, "corpus"
    elif global_value:
        value, source = global_value, "global"
    else:
        value, source = CANONICAL_ENGINE, "default"
    if value in ENGINES:
        return ExtractionContract(engine=value, source=source)
    return ExtractionContract(
        engine=CANONICAL_ENGINE,
        source=source,
        errors=(
            f"retired or unknown extraction_engine={value!r}; valid values are "
            f"{', '.join(ENGINES)} and no runtime alias or fallback is permitted",
        ),
    )
