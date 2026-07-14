"""P2.5b acceptance: byte-exact golden vectors + cross-process replay parity.

Golden constants were computed once (2026-07-14) and FROZEN. If any of these
assertions ever fails, the canonical serializer or a namespace recipe changed
— that is an identity-breaking event and must be a new canonical version, not
an edit to these constants.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys

import pytest

from models.hash_taxonomy import (
    CANONICAL_VERSION,
    HASH_NAMESPACES,
    canonical_json_v1,
    canonicalize,
    namespace_hash,
)

GOLDEN_SERIALIZATIONS = [
    ({"b": 2, "a": {"z": [3, 1, 2], "y": None}, "c": True},
     '{"a":{"y":null,"z":[3,1,2]},"b":2,"c":true}'),
    ({"ids": {"claim:9", "claim:1", "claim:10"}},
     '{"ids":["claim:1","claim:10","claim:9"]}'),
    ({"at": dt.datetime(2026, 7, 14, 3, 5, 6, 7, tzinfo=dt.timezone.utc),
      "d": dt.date(2019, 3, 1)},
     '{"at":"2026-07-14T03:05:06.000007Z","d":"2019-03-01"}'),
    ({"txt": "café → 🧭", "n": 1.5},
     '{"n":1.5,"txt":"café → 🧭"}'),
]

GOLDEN_HASHES = [
    ("input-set", frozenset({"a:2", "a:10", "a:1"}),
     "sha256:c46192afb475cf91f7adcf37f0201ca74138692d3f1600c7abe64f3d733454db"),
    ("motif", {"frame_sequence": ["MF02", "MF07"], "qualifier": None},
     "sha256:cb6d18cb0d910888d923f700f0a60f581d4f3536e8b41787769fb20bcda4d7ce"),
    ("schema", {"title": "X", "type": "object"},
     "sha256:6f78c317b21d5a922744f10672ca7304570b14cf5551e9da8dfad09a84203fc7"),
]


@pytest.mark.parametrize("value,expected", GOLDEN_SERIALIZATIONS)
def test_golden_serialization_byte_exact(value, expected):
    assert canonical_json_v1(value) == expected
    assert canonical_json_v1(value).encode("utf-8") == expected.encode("utf-8")


@pytest.mark.parametrize("namespace,value,expected", GOLDEN_HASHES)
def test_golden_namespace_hashes(namespace, value, expected):
    assert namespace_hash(namespace, value) == expected


def test_set_ordering_is_input_order_independent():
    a = namespace_hash("input-set", {"x:1", "x:2", "x:3"})
    b = namespace_hash("input-set", {"x:3", "x:1", "x:2"})
    c = namespace_hash("input-set", frozenset(["x:2", "x:3", "x:1"]))
    assert a == b == c


def test_list_order_is_preserved_and_significant():
    fwd = namespace_hash("motif", {"frame_sequence": ["MF02", "MF07"], "qualifier": None})
    rev = namespace_hash("motif", {"frame_sequence": ["MF07", "MF02"], "qualifier": None})
    assert fwd != rev


def test_namespaces_are_distinct_for_identical_values():
    value = {"k": "v"}
    hashes = {ns: namespace_hash(ns, value) for ns in HASH_NAMESPACES}
    assert len(set(hashes.values())) == len(HASH_NAMESPACES)


def test_all_fifteen_namespaces_frozen():
    assert len(HASH_NAMESPACES) == 15
    assert set(HASH_NAMESPACES) == {
        "source-content", "normalized-text", "schema", "registry", "recipe",
        "input-set", "body", "evidence-set", "scope", "motif",
        "projection-profile", "work", "raw-output", "logical-artifact",
        "revision",
    }


def test_unknown_namespace_rejected():
    with pytest.raises(KeyError):
        namespace_hash("made-up", {})


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        canonicalize({"at": dt.datetime(2026, 7, 14, 3, 5, 6)})


def test_nonfinite_float_rejected():
    with pytest.raises(ValueError):
        canonicalize({"x": float("nan")})
    with pytest.raises(ValueError):
        canonicalize({"x": float("inf")})


def test_arbitrary_object_rejected_no_str_coercion():
    class Thing:
        def __str__(self):
            return "sneaky"

    with pytest.raises(TypeError):
        canonicalize({"x": Thing()})


def test_non_string_dict_keys_rejected():
    with pytest.raises(TypeError):
        canonicalize({1: "a"})


def test_timezone_conversion_to_utc():
    tz = dt.timezone(dt.timedelta(hours=-5))
    est = dt.datetime(2026, 7, 14, 22, 0, 0, tzinfo=tz)
    assert canonicalize(est) == "2026-07-15T03:00:00Z"


def test_cross_process_replay_parity():
    """The same value must hash identically in a fresh interpreter."""
    ns, value, expected = GOLDEN_HASHES[0]
    code = (
        "import sys; sys.path.insert(0, '/app');\n"
        "from models.hash_taxonomy import namespace_hash\n"
        f"print(namespace_hash({ns!r}, frozenset({sorted(value)!r})))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == expected


def test_canonical_version_constant():
    assert CANONICAL_VERSION == "canonical_json.v1"


def test_plain_json_backcompat_with_domain_hash():
    """For values that are already plain JSON, canonicalize() is a no-op, so
    namespace_hash == domain_hash byte-compatibility holds."""
    from models.semantic_artifacts import domain_hash

    value = {"a": 1, "b": ["x", "y"]}
    assert namespace_hash("body", value) == domain_hash("body", value)


def test_stability_against_json_roundtrip():
    value = {"nested": {"set_free": [1, 2, 3]}, "s": "ü"}
    once = canonical_json_v1(value)
    again = canonical_json_v1(json.loads(once))
    assert once == again
