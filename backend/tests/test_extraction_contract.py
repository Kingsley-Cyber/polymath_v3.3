"""The production extraction contract has one provider and one explicit opt-out."""

from services.ingestion.extraction_contract import (
    CANONICAL_ENGINE,
    ENGINES,
    resolve_extraction_contract,
)


def _resolve(*, corpus_engine=None, global_engine=None):
    return resolve_extraction_contract(
        corpus_engine=corpus_engine,
        global_engine=global_engine,
        models_linked=False,
        summary_model_count=0,
        extraction_model_count=0,
        provider_pool_entries=[],
    )


def test_engine_surface_is_graphify_or_off_only():
    assert ENGINES == ("off", "graphify_cpu")
    assert CANONICAL_ENGINE == "graphify_cpu"


def test_graphify_is_the_default_and_canonical_provider():
    contract = _resolve()
    assert contract.engine == "graphify_cpu"
    assert contract.source == "default"
    assert contract.uses_graphify_cpu
    assert contract.pool_source == "none"
    assert contract.pool_size == 0
    assert not contract.errors


def test_corpus_selection_precedes_global_selection():
    contract = _resolve(corpus_engine="off", global_engine="graphify_cpu")
    assert contract.engine == "off"
    assert contract.source == "corpus"


def test_global_graphify_is_used_when_corpus_value_is_missing():
    contract = _resolve(global_engine="graphify_cpu")
    assert contract.engine == "graphify_cpu"
    assert contract.source == "global"


def test_explicit_off_is_the_only_non_graphify_route():
    contract = _resolve(corpus_engine="off")
    assert contract.engine == "off"
    assert not contract.uses_graphify_cpu
    assert not contract.errors


def test_retired_engines_fail_closed_without_alias_or_fallback():
    for retired in (
        "local",
        "cloud",
        "relex_local",
        "runpod_flash",
        "gliner",
        "glirel",
        "inherit",
        "dual",
        "local_then_cloud",
    ):
        contract = _resolve(corpus_engine=retired)
        assert contract.engine == "graphify_cpu"
        assert contract.errors
        assert "no runtime alias or fallback" in contract.errors[0]


def test_provider_pool_arguments_cannot_activate_an_extractor():
    contract = resolve_extraction_contract(
        corpus_engine="graphify_cpu",
        global_engine="off",
        models_linked=True,
        summary_model_count=2,
        extraction_model_count=3,
        provider_pool_entries=[object(), object()],
    )
    assert contract.engine == "graphify_cpu"
    assert contract.pool_source == "none"
    assert contract.pool_size == 0
    assert not contract.errors
