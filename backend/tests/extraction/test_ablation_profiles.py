"""Regression tests for ablation profile configuration.

Validates:
- Profiles A–F are cumulative (each adds exactly one feature)
- Profile A is the true baseline (all features off)
- Profile F equals production (all features on)
- Feature object is immutable (frozen dataclass)
- resolve_profile handles None, valid, and invalid inputs
- Only declared feature fields differ between adjacent profiles
"""

from __future__ import annotations

import sys
from dataclasses import fields
from pathlib import Path

import pytest

# Ensure backend/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.extraction.ablation import (
    ABLATION_PROFILES,
    PRODUCTION_FEATURES,
    ExtractionFeatures,
    resolve_profile,
)


class TestAblationProfilesAreCumulative:
    """Each profile adds exactly one feature over its predecessor."""

    def test_ablation_profiles_are_cumulative(self) -> None:
        assert not ABLATION_PROFILES["A"].head_token_join
        assert ABLATION_PROFILES["B"].head_token_join
        assert ABLATION_PROFILES["C"].open_relation_lane
        assert ABLATION_PROFILES["D"].credit_patterns
        assert ABLATION_PROFILES["E"].verb_prep_frames
        assert ABLATION_PROFILES["F"].typed_cross_sentence_links

    def test_profile_a_is_all_off(self) -> None:
        a = ABLATION_PROFILES["A"]
        assert not a.head_token_join
        assert not a.open_relation_lane
        assert not a.credit_patterns
        assert not a.verb_prep_frames
        assert not a.typed_cross_sentence_links

    def test_profile_f_is_all_on(self) -> None:
        f = ABLATION_PROFILES["F"]
        assert f.head_token_join
        assert f.open_relation_lane
        assert f.credit_patterns
        assert f.verb_prep_frames
        assert f.typed_cross_sentence_links

    def test_production_equals_f(self) -> None:
        assert PRODUCTION_FEATURES is ABLATION_PROFILES["F"]

    def test_adjacent_profiles_differ_by_exactly_one_field(self) -> None:
        """Only one feature flips between adjacent profiles."""
        order = ["A", "B", "C", "D", "E", "F"]
        field_names = [f.name for f in fields(ExtractionFeatures)]

        for i in range(1, len(order)):
            prev = ABLATION_PROFILES[order[i - 1]]
            curr = ABLATION_PROFILES[order[i]]
            diffs = [
                name for name in field_names
                if getattr(prev, name) != getattr(curr, name)
            ]
            assert len(diffs) == 1, (
                f"Profiles {order[i-1]}→{order[i]} differ by {diffs}, "
                f"expected exactly one field"
            )

    def test_features_are_monotonically_enabled(self) -> None:
        """Once a feature is enabled, it stays enabled in all later profiles."""
        order = ["A", "B", "C", "D", "E", "F"]
        field_names = [f.name for f in fields(ExtractionFeatures)]

        for name in field_names:
            seen_on = False
            for profile_name in order:
                val = getattr(ABLATION_PROFILES[profile_name], name)
                if seen_on:
                    assert val, (
                        f"Feature {name} turned off after being on "
                        f"(profile {profile_name})"
                    )
                if val:
                    seen_on = True


class TestExtractionFeaturesImmutability:
    """The feature object must be frozen (immutable)."""

    def test_frozen_dataclass_rejects_mutation(self) -> None:
        features = ABLATION_PROFILES["A"]
        with pytest.raises(AttributeError):
            features.head_token_join = True  # type: ignore[misc]

    def test_frozen_dataclass_rejects_new_field(self) -> None:
        features = ABLATION_PROFILES["A"]
        with pytest.raises(AttributeError):
            features.new_field = True  # type: ignore[attr-defined]


class TestResolveProfile:
    """resolve_profile handles None, valid, and invalid inputs."""

    def test_none_returns_production(self) -> None:
        assert resolve_profile(None) is PRODUCTION_FEATURES

    def test_valid_profiles_case_insensitive(self) -> None:
        for name in ["a", "B", "c", "D", "e", "F"]:
            result = resolve_profile(name)
            assert result is ABLATION_PROFILES[name.upper()]

    def test_invalid_profile_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown ablation profile"):
            resolve_profile("Z")

    def test_invalid_profile_lists_valid_options(self) -> None:
        with pytest.raises(ValueError, match="A, B, C, D, E, F"):
            resolve_profile("X")


class TestProfileFeatureOrder:
    """The cumulative order matches the declared ablation sequence."""

    def test_feature_activation_order(self) -> None:
        """B=head_token, C=open_relation, D=credit, E=verb_prep, F=typed_links."""
        expected_activation = {
            "B": "head_token_join",
            "C": "open_relation_lane",
            "D": "credit_patterns",
            "E": "verb_prep_frames",
            "F": "typed_cross_sentence_links",
        }
        order = ["A", "B", "C", "D", "E", "F"]
        field_names = [f.name for f in fields(ExtractionFeatures)]

        for i in range(1, len(order)):
            prev = ABLATION_PROFILES[order[i - 1]]
            curr = ABLATION_PROFILES[order[i]]
            activated = [
                name for name in field_names
                if getattr(curr, name) and not getattr(prev, name)
            ]
            assert activated == [expected_activation[order[i]]], (
                f"Profile {order[i]} should activate "
                f"{expected_activation[order[i]]}, got {activated}"
            )
