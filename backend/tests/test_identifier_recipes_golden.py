"""P2.5b acceptance: identifier recipes — byte-exact goldens + the lineage
distinctions the checklist demands (duplicate bytes, changed versions,
unrelated same-title files, explicit lineage only)."""

from __future__ import annotations

import pytest

from models.identifier_recipes import (
    RECIPE_VERSION,
    artifact_revision_id,
    claim_id,
    hierarchy_node_id,
    logical_doc_id,
    projection_point_id,
    raw_artifact_id,
    source_version_id,
    work_id,
)

DOC = "doc:46cf4ed433283c75c240b16cd05d22e137f5526b8a81b21d7ffd7f8e60ad9df1"
SRCV = "srcv:4142741b7cd66018b338c2827b072eb091aac706fdea3ba3ad118d37bee8734b"


def test_goldens_byte_exact():
    d = logical_doc_id("corpusA", "isbn:978-3-16-148410-0")
    assert d == DOC
    v = source_version_id(d, "sha256:aaaa")
    assert v == SRCV
    assert hierarchy_node_id(v, "hier.v1", "parent", "ordinal:3") == (
        "hnode:813b549bb27f57e01c57273c321722efb73ef5734fb21dc74e5fbfb65ef01bd6")
    assert claim_id("polymath", "author_assertion", {"ev:2", "ev:1"}, set(),
                    "sig:x", "scope:y") == (
        "claim:711d1177ea1ee1e9a9f6373636b47fc04b671b8c160c04808c70749a74cacefe")
    assert artifact_revision_id("art:1", "sha256:s", "sha256:b") == (
        "rev:26430add3e397fb2470e9d587463da9d723ce1fee0e8165becf9e2e5f25c61af")
    assert work_id("digest", "sha256:in", "sha256:rec") == (
        "work:bfec5dc3c1464d587f95ecd7c901ab876b9d5e4457964bcd8c099665ef22b9f7")
    assert raw_artifact_id(b"exact bytes") == (
        "raw:345907712de103dba4ffb0f29d6c070e54ff3c0a8e9ed9a8ffa73d7d2950d0ae")
    assert projection_point_id("art:1", "source_child", "sha256:prof") == (
        "b4f8fccb-2204-e122-ed52-5c5cf88e8be4")


def test_duplicate_bytes_same_doc_same_version():
    """Same strong key + same content = identical identity everywhere."""
    d1 = logical_doc_id("c1", "isbn:1")
    d2 = logical_doc_id("c1", "isbn:1")
    assert d1 == d2
    assert source_version_id(d1, "sha256:x") == source_version_id(d2, "sha256:x")


def test_changed_version_same_logical_doc():
    """Changed bytes under the same strong key = same doc, NEW version."""
    d = logical_doc_id("c1", "isbn:1")
    v1 = source_version_id(d, "sha256:old")
    v2 = source_version_id(d, "sha256:new")
    assert v1 != v2


def test_unrelated_same_title_files_stay_distinct():
    """Same content hash under DIFFERENT strong keys = different documents."""
    da = logical_doc_id("c1", "isbn:AAA")
    db = logical_doc_id("c1", "isbn:BBB")
    assert da != db
    assert source_version_id(da, "sha256:same") != source_version_id(db, "sha256:same")


def test_no_lineage_inference_without_strong_key():
    with pytest.raises(ValueError):
        logical_doc_id("c1", "")
    with pytest.raises(ValueError):
        logical_doc_id("c1", "   ")


def test_corpus_scopes_logical_identity():
    assert logical_doc_id("c1", "isbn:1") != logical_doc_id("c2", "isbn:1")


def test_claim_id_sorted_set_semantics():
    a = claim_id("ns", "author_assertion", ["ev:1", "ev:2"], ["p:1"], "sig", "sc")
    b = claim_id("ns", "author_assertion", {"ev:2", "ev:1"}, {"p:1"}, "sig", "sc")
    assert a == b


def test_claim_identity_distinguishes_knowledge_status():
    """A text-identical entailment is NOT the author's assertion."""
    a = claim_id("ns", "author_assertion", {"ev:1"}, set(), "sig", "sc")
    b = claim_id("ns", "derived_entailment", {"ev:1"}, set(), "sig", "sc")
    assert a != b


def test_work_id_reused_on_retry_raw_output_distinct():
    w1 = work_id("digest", "sha256:in", "sha256:rec")
    w2 = work_id("digest", "sha256:in", "sha256:rec")
    assert w1 == w2  # deterministic work identity survives retries
    r1 = raw_artifact_id(b"output attempt one")
    r2 = raw_artifact_id(b"output attempt two")
    assert r1 != r2  # stochastic outputs never collide into one identity


def test_projection_point_id_is_valid_deterministic_uuid():
    import uuid

    p1 = projection_point_id("art:1", "role", "sha256:p")
    p2 = projection_point_id("art:1", "role", "sha256:p")
    assert p1 == p2
    uuid.UUID(p1)
    assert projection_point_id("art:1", "OTHER", "sha256:p") != p1


def test_raw_requires_bytes():
    with pytest.raises(TypeError):
        raw_artifact_id("not bytes")  # type: ignore[arg-type]


def test_recipe_version_frozen():
    assert RECIPE_VERSION == "identifier_recipes.v1"
