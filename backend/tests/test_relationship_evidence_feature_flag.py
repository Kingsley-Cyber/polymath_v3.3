"""Dark-launch contract for relationship evidence allocation."""

from config import Settings


def test_relationship_evidence_allocation_defaults_off(monkeypatch):
    monkeypatch.delenv("RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED", raising=False)
    resolved = Settings(_env_file=None)
    assert resolved.RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED is False


def test_relationship_evidence_allocation_can_be_enabled_from_settings(monkeypatch):
    monkeypatch.setenv("RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED", "true")
    resolved = Settings(_env_file=None)
    assert resolved.RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED is True
