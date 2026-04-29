"""
Registers the `integration` marker and auto-skips integration tests unless
the user opts in with `pytest -m integration`.
"""

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: live smoke test against the running docker-compose "
        "stack (Mongo + Qdrant + Neo4j + LLM). Skipped by default; run with "
        "`pytest -m integration` to include.",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    explicit = (config.getoption("-m") or "").strip()
    # User explicitly asked for integration → run them (and only them by marker rules).
    if "integration" in explicit:
        return
    skip = pytest.mark.skip(reason="integration test; run with `-m integration`")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
