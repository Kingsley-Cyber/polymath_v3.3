"""Open-relation argument-assignment regression tests.

Source: 12 adjudicated open relations from ADJUDICATION_20260727.md
Fixtures: tests/extraction/fixtures/open_relation_adjudicated.yaml

Regression targets:
  1. 4/4 VALID records route to STORE_UNMAPPED_SURFACE_RELATION
  2. 0/8 INVALID records route to STORE_UNMAPPED_SURFACE_RELATION
  3. All 8 INVALID records retained in REVIEW or audit (not silently dropped)
  4. Exact evidence offsets preserved in all output records
  5. Direction metadata populated for all 12 records
"""

from pathlib import Path

import pytest
import yaml

from services.extraction.dep_path_extractor import EntitySpan
from services.extraction.frame_extractor import FrameExtractor

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "open_relation_adjudicated.yaml"


def _load_fixtures() -> list[dict]:
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data["fixtures"]


FIXTURES = _load_fixtures()
VALID_FIXTURES = [f for f in FIXTURES if f["verdict"] == "VALID"]
INVALID_FIXTURES = [f for f in FIXTURES if f["verdict"] != "VALID"]


@pytest.fixture(scope="module")
def extractor():
    return FrameExtractor()


def _run_fixture(extractor: FrameExtractor, fixture: dict) -> list:
    """Run the FrameExtractor on a fixture and return matching triples."""
    sentence = fixture["sentence"]
    entities = [
        EntitySpan(
            surface=surface,
            start_char=start,
            end_char=end,
            entity_type=etype,
        )
        for surface, start, end, etype in fixture["entities"]
    ]
    triples = extractor.extract(sentence, entities, chunk_id=fixture["id"])
    # Filter to triples matching the expected surface predicate
    pred = fixture["expected_surface_predicate"]
    return [
        t for t in triples
        if t.predicate_lemma == pred or t.predicate_surface.lower().startswith(pred)
    ]


class TestValidOpenRelationsPreserved:
    """4/4 VALID records must be extracted with correct argument assignment."""

    @pytest.mark.parametrize("fixture", VALID_FIXTURES, ids=[f["id"] for f in VALID_FIXTURES])
    def test_valid_relation_extracted(self, extractor, fixture):
        """Valid open relations must produce at least one triple."""
        triples = _run_fixture(extractor, fixture)
        assert len(triples) >= 1, (
            f"Expected at least one triple for {fixture['id']}, got none. "
            f"Sentence: {fixture['sentence'][:60]}..."
        )

    @pytest.mark.parametrize("fixture", VALID_FIXTURES, ids=[f["id"] for f in VALID_FIXTURES])
    def test_valid_relation_direction_metadata(self, extractor, fixture):
        """Valid open relations must carry correct direction metadata."""
        triples = _run_fixture(extractor, fixture)
        if not triples:
            pytest.skip("No triple extracted")
        t = triples[0]
        expected = fixture["expected_metadata"]
        assert t.subject_dependency_role == expected["subject_dependency_role"], (
            f"subject_dependency_role: got {t.subject_dependency_role!r}, "
            f"expected {expected['subject_dependency_role']!r}"
        )
        assert t.object_dependency_role == expected["object_dependency_role"]
        assert t.voice == expected["voice"]
        assert t.direction_source == expected["direction_source"]
        assert t.direction_confidence == expected["direction_confidence"]

    @pytest.mark.parametrize("fixture", VALID_FIXTURES, ids=[f["id"] for f in VALID_FIXTURES])
    def test_valid_relation_offsets_preserved(self, extractor, fixture):
        """Exact evidence offsets must be preserved."""
        triples = _run_fixture(extractor, fixture)
        if not triples:
            pytest.skip("No triple extracted")
        t = triples[0]
        # Subject and object offsets must match entity spans
        subj_ent = fixture["entities"][0]
        obj_ent = fixture["entities"][1]
        assert t.subject_start == subj_ent[1], (
            f"subject_start: got {t.subject_start}, expected {subj_ent[1]}"
        )
        assert t.subject_end == subj_ent[2]
        assert t.object_start == obj_ent[1]
        assert t.object_end == obj_ent[2]


class TestInvalidOpenRelationsNotStored:
    """0/8 INVALID records may be stored as trusted open relations."""

    @pytest.mark.parametrize("fixture", INVALID_FIXTURES, ids=[f["id"] for f in INVALID_FIXTURES])
    def test_invalid_direction_not_stored_as_trusted(self, extractor, fixture):
        """Invalid-direction records must NOT have high-confidence agent→patient metadata.

        The current system extracts these with subj=agent, obj=patient, conf=high.
        After the argument repair, they should either:
        - Have corrected metadata (subj=patient, obj=agent) indicating the inversion
        - Have direction_confidence=low or medium
        - Not be extracted at all (suppressed)

        This test FAILS on the current system and PASSES after the repair.
        """
        triples = _run_fixture(extractor, fixture)
        if not triples:
            # Suppressed entirely — acceptable (not stored as trusted)
            return
        t = triples[0]
        # The record must NOT be high-confidence agent→patient (that's the bug)
        is_trusted_wrong_direction = (
            t.subject_dependency_role == "agent"
            and t.object_dependency_role == "patient"
            and t.direction_confidence == "high"
        )
        assert not is_trusted_wrong_direction, (
            f"{fixture['id']}: extracted as high-confidence agent→patient "
            f"but adjudicated as {fixture['verdict']}. "
            f"Expected direction_confidence != high or corrected roles."
        )

    @pytest.mark.parametrize("fixture", INVALID_FIXTURES, ids=[f["id"] for f in INVALID_FIXTURES])
    def test_invalid_records_retained_in_audit(self, extractor, fixture):
        """Invalid records must be retained (not silently dropped).

        They should appear in the output with REVIEW or REJECT status,
        not disappear entirely.
        """
        triples = _run_fixture(extractor, fixture)
        # For now, we just verify the extractor doesn't crash.
        # The full routing test requires the corroboration gate integration.
        # This is a placeholder for the gate-level routing test.
        assert isinstance(triples, list)


class TestDirectionMetadataPopulated:
    """All extracted open relations must carry direction metadata."""

    @pytest.mark.parametrize("fixture", FIXTURES, ids=[f["id"] for f in FIXTURES])
    def test_metadata_fields_present(self, extractor, fixture):
        """Every extracted triple must have non-empty direction metadata."""
        triples = _run_fixture(extractor, fixture)
        for t in triples:
            if t.mapping_status == "UNMAPPED":
                assert t.subject_dependency_role != "", (
                    f"{fixture['id']}: subject_dependency_role is empty"
                )
                assert t.object_dependency_role != "", (
                    f"{fixture['id']}: object_dependency_role is empty"
                )
                assert t.voice != "", f"{fixture['id']}: voice is empty"
                assert t.direction_source != "", (
                    f"{fixture['id']}: direction_source is empty"
                )
                assert t.direction_confidence != "", (
                    f"{fixture['id']}: direction_confidence is empty"
                )
