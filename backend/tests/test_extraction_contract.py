"""Pure tests for the deterministic extraction-contract resolver.

Runnable standalone: python3 tests/test_extraction_contract.py (loads by file
path so the services package __init__ never imports — same pattern as
test_prefilter.py). Non-zero exit on failure.
"""
import importlib.util
import os
import sys

_spec = importlib.util.spec_from_file_location(
    "extraction_contract",
    os.path.join(
        os.path.dirname(__file__), "..", "services", "ingestion", "extraction_contract.py"
    ),
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod  # dataclasses resolve ClassVar via sys.modules
_spec.loader.exec_module(_mod)
resolve = _mod.resolve_extraction_contract


def _c(**kw):
    base = dict(
        corpus_engine=None,
        global_engine="local",
        models_linked=True,
        summary_model_count=0,
        extraction_model_count=0,
        enabled_endpoint_urls=["http://mac:8084"],
    )
    base.update(kw)
    return resolve(**base)


def test_corpus_engine_wins_over_global():
    c = _c(corpus_engine="cloud", global_engine="local", summary_model_count=2)
    assert c.engine == "cloud" and c.source == "corpus"
    assert not c.errors


def test_inherit_falls_back_to_global():
    c = _c(corpus_engine="inherit", global_engine="dual", summary_model_count=1)
    assert c.engine == "dual" and c.source == "global"


def test_missing_everything_defaults_local_never_cloud():
    c = _c(corpus_engine=None, global_engine=None)
    assert c.engine == "local" and c.source == "default"
    assert not c.errors


def test_unknown_corpus_engine_defaults_local_with_warning():
    c = _c(corpus_engine="turbo")
    assert c.engine == "local" and c.source == "default"
    assert any("unknown corpus" in w for w in c.warnings)


def test_the_qwen_collapse_is_now_a_fast_failure():
    # 2026-07-05: engine=cloud + linked summary pool present -> ran and died
    # per-chunk. With an EMPTY pool the contract must fail the doc up front.
    c = _c(corpus_engine="cloud", summary_model_count=0, extraction_model_count=0)
    assert c.errors and "EMPTY" in c.errors[0]


def test_cloud_with_linked_summary_pool_resolves_summary():
    c = _c(corpus_engine="cloud", models_linked=True, summary_model_count=3)
    assert c.pool_source == "summary_models" and c.pool_size == 3
    assert not c.errors


def test_unlinked_uses_extraction_pool():
    c = _c(
        corpus_engine="cloud",
        models_linked=False,
        summary_model_count=3,
        extraction_model_count=1,
    )
    assert c.pool_source == "extraction_models" and c.pool_size == 1


def test_unlinked_but_empty_extraction_pool_fails_fast():
    c = _c(
        corpus_engine="cloud",
        models_linked=False,
        summary_model_count=2,
        extraction_model_count=0,
    )
    assert c.pool_source == "none" and c.pool_size == 0
    assert c.errors and "extraction_models" in c.errors[0]


def test_missing_link_flag_does_not_borrow_summary_pool():
    c = _c(
        corpus_engine="cloud",
        models_linked=None,
        summary_model_count=2,
        extraction_model_count=0,
    )
    assert c.pool_source == "none" and c.pool_size == 0
    assert c.errors and "extraction_models" in c.errors[0]


def test_dual_requires_pool_too():
    c = _c(corpus_engine="dual", summary_model_count=0)
    assert c.errors


def test_local_then_cloud_empty_pool_is_warning_not_error():
    c = _c(corpus_engine="local_then_cloud", summary_model_count=0)
    assert not c.errors
    assert any("rescue lane is unavailable" in w for w in c.warnings)


def test_off_is_a_valid_explicit_state():
    c = _c(corpus_engine="off")
    assert c.engine == "off" and not c.errors and c.pool_source == "none"


def test_local_without_endpoints_warns_env_floor():
    c = _c(corpus_engine="local", enabled_endpoint_urls=[])
    assert not c.errors
    assert any("env-wired defaults" in w for w in c.warnings)


def test_uses_cloud_uses_local_flags():
    assert _c(corpus_engine="dual", summary_model_count=1).uses_cloud
    assert _c(corpus_engine="dual", summary_model_count=1).uses_local
    assert not _c(corpus_engine="local").uses_cloud
    assert not _c(corpus_engine="cloud", summary_model_count=1).uses_local


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
