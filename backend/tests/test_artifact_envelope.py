from __future__ import annotations

import datetime as dt
import subprocess
import sys

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from models.artifact_envelope import (
    ArtifactLifecycle,
    ArtifactOwnership,
    ArtifactProvenance,
    ArtifactValidation,
    body_hash_for_body,
    make_artifact_envelope,
    schema_hash_for_body,
)
from models.hash_taxonomy import namespace_hash


class DemoBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    thesis: str
    claim_ids: tuple[str, ...]


def _hash(namespace: str, value) -> str:
    return namespace_hash(namespace, value)


def _parts(*, run_id: str = "run-1", warning: str | None = None):
    provenance = ArtifactProvenance(
        work_id="work:demo",
        attempt_id=None,
        raw_artifact_ids=("raw:one",),
        producer_kind="python_rule",
        engine="python",
        model_id=None,
        model_revision=None,
        prompt_id=None,
        prompt_hash=None,
        compiler_version="demo.compiler.v1",
        parser_version=None,
        rule_pack_version="demo.rules.v1",
        run_id=run_id,
    )
    validation = ArtifactValidation(
        contract_valid=True,
        evidence_valid=True,
        registry_valid=True,
        policy_valid=True,
        validator_version="demo.validator.v1",
        errors=(),
        warnings=(warning,) if warning else (),
    )
    lifecycle = ArtifactLifecycle(
        created_at=dt.datetime(2026, 7, 14, 12, 0, tzinfo=dt.timezone.utc),
        validated_at=None,
        activated_at=None,
        supersedes_revision_id=None,
        superseded_at=None,
    )
    return provenance, validation, lifecycle


def _envelope(
    *,
    body: DemoBody | None = None,
    artifact_type: str = "semantic_digest",
    knowledge_status: str | None = "cross_passage_synthesis",
    run_id: str = "run-1",
    warning: str | None = None,
):
    provenance, validation, lifecycle = _parts(run_id=run_id, warning=warning)
    return make_artifact_envelope(
        artifact_type=artifact_type,
        schema_id="polymath.demo_body",
        schema_version="1.0.0",
        artifact_id="digest:demo",
        artifact_state="candidate",
        knowledge_status=knowledge_status,
        ownership=ArtifactOwnership(
            corpus_id="corpus-1",
            doc_id="doc-1",
            source_version_id="srcv:1",
            hierarchy_node_id="hnode:1",
        ),
        input_set_hash=_hash("input-set", {"claim:1", "claim:2"}),
        recipe_hash=_hash("recipe", {"name": "demo", "version": "1"}),
        evidence_set_hash=_hash("evidence-set", {"evidence:1"}),
        registry_set_hash=None,
        provenance=provenance,
        validation=validation,
        lifecycle=lifecycle,
        body=body
        or DemoBody(thesis="A grounded thesis.", claim_ids=("claim:1",)),
    )


def _reconstruct(envelope, **changes):
    fields = {
        name: getattr(envelope, name)
        for name in envelope.__class__.model_fields
    }
    fields.update(changes)
    return envelope.__class__(**fields)


def test_factory_binds_typed_body_schema_body_and_revision_hashes():
    envelope = _envelope()

    assert envelope.envelope_version == "polymath.artifact_envelope.v1"
    assert envelope.schema_hash == schema_hash_for_body(envelope.body)
    assert envelope.integrity.body_hash == body_hash_for_body(envelope.body)
    assert envelope.artifact_revision_id.startswith("rev:")
    assert envelope.body.thesis == "A grounded thesis."


def test_envelope_identity_goldens_are_byte_exact():
    envelope = _envelope()
    assert envelope.schema_hash == (
        "sha256:564b8642fa6d349dc48fe4a99dea18f204506350d02466f66cbefbb21e9d8cdb"
    )
    assert envelope.integrity.body_hash == (
        "sha256:fdef564138fd586d8b344dd99a321dbd549f76c49f440971d66818023f1054d1"
    )
    assert envelope.artifact_revision_id == (
        "rev:78029574cafe3dc22af7163ad860829bda4d3bef3133fbfbd451ddb1bc6b25d7"
    )


def test_validation_lifecycle_and_provider_changes_do_not_change_body_identity():
    baseline = _envelope(run_id="run-1")
    revalidated = _envelope(run_id="run-2", warning="policy reviewed")

    assert revalidated.schema_hash == baseline.schema_hash
    assert revalidated.integrity.body_hash == baseline.integrity.body_hash
    assert revalidated.artifact_revision_id == baseline.artifact_revision_id
    assert revalidated.provenance.run_id != baseline.provenance.run_id
    assert revalidated.validation.warnings != baseline.validation.warnings


def test_changed_body_requires_a_new_revision():
    baseline = _envelope()
    changed = _envelope(
        body=DemoBody(thesis="A corrected thesis.", claim_ids=("claim:1",))
    )

    assert changed.schema_hash == baseline.schema_hash
    assert changed.integrity.body_hash != baseline.integrity.body_hash
    assert changed.artifact_revision_id != baseline.artifact_revision_id


def test_body_hash_and_revision_mismatches_fail_closed():
    envelope = _envelope()
    wrong_integrity = envelope.integrity.model_copy(
        update={"body_hash": "sha256:" + "0" * 64}
    )

    with pytest.raises(ValidationError, match="body_hash does not match"):
        _reconstruct(envelope, integrity=wrong_integrity)

    with pytest.raises(ValidationError, match="artifact_revision_id does not match"):
        _reconstruct(envelope, artifact_revision_id="rev:not-the-revision")


def test_schema_hash_mismatch_fails_closed():
    envelope = _envelope()
    with pytest.raises(ValidationError, match="schema_hash does not match"):
        _reconstruct(envelope, schema_hash="sha256:" + "f" * 64)


def test_knowledge_bearing_artifacts_require_explicit_status():
    with pytest.raises(ValidationError, match="requires an explicit knowledge_status"):
        _envelope(knowledge_status=None)

    registry = _envelope(
        artifact_type="domain_registry",
        knowledge_status=None,
    )
    assert registry.knowledge_status is None


def test_bare_dict_body_is_rejected_even_when_shape_matches():
    with pytest.raises(TypeError, match="typed Pydantic model"):
        make_artifact_envelope(  # type: ignore[arg-type]
            artifact_type="semantic_digest",
            schema_id="polymath.demo_body",
            schema_version="1.0.0",
            artifact_id="digest:demo",
            artifact_state="candidate",
            knowledge_status="cross_passage_synthesis",
            ownership=ArtifactOwnership(
                corpus_id="corpus-1",
                doc_id="doc-1",
                source_version_id="srcv:1",
                hierarchy_node_id=None,
            ),
            input_set_hash=_hash("input-set", {"claim:1"}),
            recipe_hash=_hash("recipe", {"name": "demo"}),
            evidence_set_hash=None,
            registry_set_hash=None,
            provenance=_parts()[0],
            validation=_parts()[1],
            lifecycle=_parts()[2],
            body={"thesis": "untyped", "claim_ids": []},
        )


def test_hash_fields_and_lifecycle_are_strict():
    envelope = _envelope()
    with pytest.raises(ValidationError, match="canonical sha256"):
        _reconstruct(envelope, schema_hash="SHA256:BAD")

    with pytest.raises(ValidationError):
        ArtifactLifecycle(
            created_at=dt.datetime(2026, 7, 14, 12, 0),
            validated_at=None,
            activated_at=None,
            supersedes_revision_id=None,
            superseded_at=None,
        )


def test_envelope_is_frozen_and_forbids_extra_fields():
    envelope = _envelope()
    with pytest.raises(ValidationError):
        envelope.artifact_state = "active"  # type: ignore[misc]

    fields = {
        name: getattr(envelope, name)
        for name in envelope.__class__.model_fields
    }
    fields["surprise"] = True
    with pytest.raises(ValidationError):
        envelope.__class__(**fields)


def test_schema_and_body_hash_replay_in_fresh_process():
    envelope = _envelope()
    code = """
from pydantic import BaseModel, ConfigDict
from models.artifact_envelope import schema_hash_for_body, body_hash_for_body
class DemoBody(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    thesis: str
    claim_ids: tuple[str, ...]
body = DemoBody(thesis='A grounded thesis.', claim_ids=('claim:1',))
print(schema_hash_for_body(body))
print(body_hash_for_body(body))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == [
        envelope.schema_hash,
        envelope.integrity.body_hash,
    ]
