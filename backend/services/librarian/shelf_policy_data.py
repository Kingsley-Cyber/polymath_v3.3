"""Versioned misuse/counterbalance policy DATA for shelf-role assignment (P1.5).

These sets are DATA, not logic: owner-curated, versioned as a unit under
``POLICY_VERSION``, and deliberately generic (concept families, never
per-corpus or per-document keys). Content-keyed behavior lives ONLY here —
``shelf_engine`` consumes these sets and contains zero content-specific
conditionals. Editing a key set is a policy revision: bump
``POLICY_VERSION`` when the sets change so role assignments stay replayable
against the policy that produced them.

Keys are normalized snake_case concept identifiers. The engine matches them
against card ``value_key`` identities through
``services.ingestion.corpus_lexicon.normalize_identity``, so underscore vs
space vs hyphen variants are equivalent at match time.
"""

from __future__ import annotations

POLICY_VERSION = "shelf_policy.v0"

# Concept families whose presence in the query (or on the direct shelf)
# signals elevated misuse potential — persuasion/manipulation, addiction, and
# gambling-mechanics style families. Generic by design; owner-curated data.
HIGH_MISUSE_KEYS: frozenset[str] = frozenset(
    {
        "persuasion",
        "manipulation",
        "psychological_manipulation",
        "dark_patterns",
        "deceptive_advertising",
        "misinformation",
        "propaganda",
        "coercion",
        "addiction",
        "behavioral_addiction",
        "compulsion_loops",
        "gambling",
        "gambling_mechanics",
        "variable_reward_schedules",
        "loot_boxes",
        "attention_hacking",
        "engagement_maximization",
        "fear_based_marketing",
        "scarcity_tactics",
        "social_proof_exploitation",
    }
)

# Concept families that counterbalance the misuse families above —
# ethics, wellbeing, critical-thinking, and consumer-protection style
# families. Generic by design; owner-curated data.
COUNTERBALANCE_KEYS: frozenset[str] = frozenset(
    {
        "ethics",
        "marketing_ethics",
        "media_ethics",
        "informed_consent",
        "transparency",
        "accountability",
        "consumer_protection",
        "consumer_rights",
        "privacy",
        "autonomy",
        "digital_wellbeing",
        "wellbeing",
        "mental_health",
        "mindfulness",
        "self_regulation",
        "harm_reduction",
        "critical_thinking",
        "media_literacy",
        "skepticism",
        "cognitive_biases",
    }
)
