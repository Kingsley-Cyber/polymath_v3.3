"""Versioned, content-neutral semantic-parent eligibility.

The production rule is deliberately independent of corpus identity, section
names, promotional phrases, and evaluation fixtures. Structural eligibility
remains owned by the caller; this module classifies only parent text.
"""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata
from typing import Any

from models.hash_taxonomy import namespace_hash
from models.semantic_parent_eligibility import ParentEligibilityDecisionV2

REGISTRY_PATH = (
    Path(__file__).resolve().parents[2]
    / "registries"
    / "semantic_parent_eligibility.v2.json"
)

_EXPECTED_FIELDS = {
    "registry",
    "version",
    "schema_version",
    "authority",
    "source",
    "changes_require_new_version",
    "unicode_normalization",
    "outer_whitespace_policy",
    "line_whitespace_policy",
    "heading_pattern",
    "heading_only_policy",
    "substantive_line_policy",
    "url_pattern",
    "lexical_nonword_pattern",
    "lexical_regex_mode",
    "substantive_byte_encoding",
    "substantive_byte_min",
    "comparison",
    "reason_precedence",
    "production_bans",
}


class ParentEligibilityRegistryError(ValueError):
    """The immutable eligibility recipe is missing or malformed."""


def _validate_recipe(recipe: dict[str, Any]) -> None:
    if set(recipe) != _EXPECTED_FIELDS:
        raise ParentEligibilityRegistryError("eligibility recipe fields are not exact")
    expected = {
        "registry": "semantic_parent_eligibility",
        "version": "v2",
        "schema_version": "semantic_parent_eligibility.v2",
        "authority": "owner-authorized, senior-approved",
        "changes_require_new_version": True,
        "unicode_normalization": "NFKC",
        "outer_whitespace_policy": "strip",
        "line_whitespace_policy": "strip_and_drop_blank",
        "heading_pattern": r"^\s{0,3}#{1,6}(?:\s+|$)",
        "heading_only_policy": (
            "at_least_one_nonblank_line_and_all_nonblank_lines_match"
        ),
        "substantive_line_policy": "non_heading_lines_in_source_order",
        "url_pattern": r"https?://\S+|www\.\S+",
        "lexical_nonword_pattern": r"[^\w]+",
        "lexical_regex_mode": "unicode",
        "substantive_byte_encoding": "utf-8",
        "substantive_byte_min": 256,
        "comparison": "greater_than_or_equal",
        "reason_precedence": [
            "heading_only",
            "below_substantive_byte_min",
            "eligible",
        ],
        "production_bans": [
            "corpus_ids",
            "parent_ids",
            "section_name_blacklists",
            "promotional_phrase_blacklists",
            "evaluation_keys",
        ],
    }
    for field, value in expected.items():
        if recipe.get(field) != value:
            raise ParentEligibilityRegistryError(
                f"eligibility recipe field {field!r} drifted"
            )
    if not isinstance(recipe.get("source"), str) or not recipe["source"]:
        raise ParentEligibilityRegistryError("eligibility recipe source is missing")
    try:
        re.compile(recipe["heading_pattern"])
        re.compile(recipe["url_pattern"], re.IGNORECASE)
        re.compile(recipe["lexical_nonword_pattern"])
    except re.error as exc:
        raise ParentEligibilityRegistryError(
            "eligibility recipe contains an invalid regular expression"
        ) from exc


@lru_cache(maxsize=1)
def load_parent_eligibility_recipe() -> dict[str, Any]:
    try:
        recipe = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ParentEligibilityRegistryError(
            "cannot load semantic parent eligibility recipe"
        ) from exc
    if not isinstance(recipe, dict):
        raise ParentEligibilityRegistryError("eligibility recipe must be an object")
    _validate_recipe(recipe)
    return recipe


def parent_eligibility_recipe_hash() -> str:
    return namespace_hash("registry", load_parent_eligibility_recipe())


def classify_parent_text_v2(text: str) -> ParentEligibilityDecisionV2:
    """Classify parent text with the frozen v2 recipe."""

    if not isinstance(text, str):
        raise TypeError("parent text must be a string")
    recipe = load_parent_eligibility_recipe()
    normalized = unicodedata.normalize(recipe["unicode_normalization"], text).strip()
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    heading_pattern = re.compile(recipe["heading_pattern"])
    heading_only = bool(lines) and all(heading_pattern.match(line) for line in lines)
    prose = "\n".join(line for line in lines if not heading_pattern.match(line))
    prose = re.sub(recipe["url_pattern"], " ", prose, flags=re.IGNORECASE)
    lexical = re.sub(recipe["lexical_nonword_pattern"], " ", prose).strip()
    substantive_bytes = len(lexical.encode(recipe["substantive_byte_encoding"]))

    if heading_only:
        reason = "heading_only"
    elif substantive_bytes < recipe["substantive_byte_min"]:
        reason = "below_substantive_byte_min"
    else:
        reason = "eligible"
    return ParentEligibilityDecisionV2(
        schema_version="semantic_parent_eligibility.v2",
        eligible=reason == "eligible",
        reason=reason,
        heading_only=heading_only,
        substantive_bytes=substantive_bytes,
        recipe_version="v2",
        recipe_hash=namespace_hash("registry", recipe),
    )
