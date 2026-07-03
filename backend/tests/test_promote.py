"""B2 promote() asserting tests — pure, deterministic, idempotent, additive.

    docker exec -i polymath_v33-backend-1 python /app/tests/test_promote.py
"""

from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services.ingestion.promote import promote, promoted_index_fields  # noqa: E402

ROW = {
    "schema_version": "polymath.extract.v1",
    "corpus_id": "k", "doc_id": "d", "chunk_id": "c",
    "entities": [
        {"canonical_name": "TensorFlow", "query_aliases": ["tf", "tensor flow"],
         "canonical_family": "machine_learning", "domain_type": "AIModel"},
        {"canonical_name": "Python", "query_aliases": []},
    ],
    "relations": [
        {"subject": "tensorflow", "predicate": "uses", "object": "python",
         "relation_family": "Operational"},
    ],
    "facts": [{"subject": "tensorflow", "fact_type": "quantity",
               "property_name": "params", "value": "10"}],
}


def test_projection_shape_and_values():
    out = promote(ROW)
    assert out["concepts"] == ["python", "tensor flow", "tensorflow", "tf"]
    assert out["entity_ids"] == ["entity:python", "entity:tensorflow"]
    assert out["entity_families"] == ["machine_learning"]
    assert out["entity_domains"] == ["aimodel"]
    assert out["relation_predicates"] == ["uses"]
    assert out["relation_families"] == ["operational"]
    assert out["fact_types"] == ["quantity"]
    assert out["has_relations"] is True
    assert out["promote_version"] == "polymath.promote.v1"
    assert out["extract_schema_version"] == "polymath.extract.v1"


def test_deterministic_and_idempotent():
    assert promote(ROW) == promote(ROW)
    assert promote(dict(ROW)) == promote(ROW)   # same content twice = same delta


def test_never_touches_identity_keys():
    out = promote(ROW)
    for k in ("corpus_id", "doc_id", "chunk_id", "parent_id"):
        assert k not in out                      # additive-only; cannot clobber identity


def test_custom_entity_id_fn_wins():
    out = promote(ROW, entity_id_fn=lambda n: f"entity:X_{n.replace(' ', '')}")
    assert all(e.startswith("entity:X_") for e in out["entity_ids"])


def test_empty_extraction_yields_empty_but_stamped():
    out = promote({"entities": [], "relations": [], "facts": []})
    assert out["concepts"] == [] and out["has_relations"] is False
    assert out["promote_version"] == "polymath.promote.v1"


def test_index_ships_with_fields():
    fields = dict(promoted_index_fields())
    for f in ("concepts", "entity_ids", "relation_families", "fact_types"):
        assert fields[f] == "keyword"
    assert fields["has_relations"] == "bool"


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
