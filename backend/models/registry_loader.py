"""Versioned owner-registry loader + resolver (P2.5b registries job).

Loads the owner-delivered registry snapshots from backend/registries/,
validates their internal shape AND cross-registry references, and exposes a
read-only resolver. Registry files are versioned immutable data: any change
must be a NEW version file — the frozen snapshot hashes in the golden tests
make silent edits tamper-evident.

Owner rules enforced here:
- affinity priors may reference only registered domains/superframes and can
  never force/forbid an assignment (this module only *serves* them);
- stage->superframe bindings: every stage has >=1 admissible entry and
  EXACTLY one dominant (set-valued, owner-approved 2026-07-14);
- unknown ids are hard errors, never silent defaults.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from models.hash_taxonomy import namespace_hash

REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registries"

FILES = {
    "domain": "domain_registry.v1.json",
    "superframe": "superframe_registry.v1.json",
    "affinity": "domain_superframe_affinity.v1.json",
    "motif": "motif_registry.v1.json",
    "vocab": "extraction_vocabularies.v1.json",
    "latent_policy": "latent_concept_policy.v1.json",
    "binding": "motif_stage_superframe_binding.v1.json",
    "embedding_instruction": "embedding_instruction_registry.v1.json",
    "predicate_normalization": "predicate_normalization.v1.json",
    "domain_resolution": "domain_resolution_policy.v1.json",
    "superframe_rule": "superframe_rule_registry.v1.json",
}


class RegistryError(ValueError):
    """A registry file is malformed, inconsistent, or referenced id is unknown."""


_PROPOSED_AUTHORITY = "executor-proposed, owner-ratifiable"


def _validate_proposed_v1_registry(
    registry: dict[str, Any],
    *,
    registry_id: str,
) -> None:
    if registry.get("registry") != registry_id:
        raise RegistryError(f"{registry_id} registry has the wrong identity")
    if registry.get("version") != "v1":
        raise RegistryError(f"{registry_id} registry version must be v1")
    if registry.get("authority") != _PROPOSED_AUTHORITY:
        raise RegistryError(f"{registry_id} authority mark is missing")
    if registry.get("owner_ratification_required") is not True:
        raise RegistryError(f"{registry_id} must remain owner-ratifiable")
    if registry.get("changes_require_new_version") is not True:
        raise RegistryError(f"{registry_id} must require monotonic versions")


def _validate_domain_resolution_policy(registry: dict[str, Any]) -> None:
    expected_fields = {
        "registry",
        "version",
        "authority",
        "owner_ratification_required",
        "source",
        "changes_require_new_version",
        "policy_note",
        "normalizer",
        "match_policies",
        "predicate_policy",
        "merge_policy",
        "score_components",
        "scalar_score",
        "cardinality_cap",
        "unknown_policy",
        "affinity_quarantine",
    }
    if set(registry) != expected_fields:
        raise RegistryError("domain resolution policy fields are not exact")
    _validate_proposed_v1_registry(
        registry,
        registry_id="domain_resolution_policy",
    )

    normalizer = registry["normalizer"]
    if set(normalizer) != {
        "normalizer_id",
        "implementation",
        "algorithm",
        "graph_entity_id_divergence",
        "reconciliation_owner",
    }:
        raise RegistryError("domain resolution normalizer fields are not exact")
    if normalizer["normalizer_id"] != "corpus_lexicon.normalize_identity.v1":
        raise RegistryError("domain resolution must reuse the CP5 identity keyspace")
    if (
        normalizer["implementation"]
        != "services.ingestion.corpus_lexicon.normalize_identity"
    ):
        raise RegistryError("domain resolution normalizer implementation drifted")
    if normalizer["algorithm"] != (
        "NFKC; lowercase; underscores and hyphens to spaces; replace ASCII "
        "non-alphanumeric characters with spaces; collapse whitespace"
    ):
        raise RegistryError("domain resolution normalizer algorithm drifted")
    if not all(
        marker in normalizer["graph_entity_id_divergence"]
        for marker in ("canonicalize_entity_name", "NFKD", "alias map")
    ):
        raise RegistryError("graph entity-id normalizer divergence must be surfaced")
    if normalizer["reconciliation_owner"] != "CP5 versioned alias registry":
        raise RegistryError("CP5 alias registry reconciliation ownership drifted")

    policies = registry["match_policies"]
    expected_policies = [
        {
            "signal_kind": "claim_concept",
            "match": "exact_normalized_domain_name_or_member",
            "assignment_role": "dominant",
            "evidence_authority": "claim_local",
        },
        {
            "signal_kind": "section_heading",
            "match": "exact_normalized_domain_name_or_member",
            "assignment_role": "supporting",
            "evidence_authority": "inherited_context",
        },
    ]
    if policies != expected_policies:
        raise RegistryError("domain exact-match policies drifted")
    if registry["predicate_policy"] != {
        "domain_bearing": False,
        "reason": (
            "PredicateType belongs to the independent mechanism axis and "
            "cannot assign a domain"
        ),
    }:
        raise RegistryError("PredicateType must remain non-domain-bearing")
    if registry["merge_policy"] != {
        "key": "domain_id",
        "evidence": "union_sorted_unique",
        "same_domain_role_precedence": ["dominant", "supporting"],
    }:
        raise RegistryError("domain merge policy drifted")
    if registry["score_components"] != [
        "exact_claim_concept_matches",
        "exact_heading_matches",
        "claim_evidence_ref_count",
        "context_evidence_ref_count",
    ]:
        raise RegistryError("domain score components drifted")
    if registry["scalar_score"] is not None:
        raise RegistryError("domain resolution cannot invent a scalar score")
    if registry["cardinality_cap"] is not None:
        raise RegistryError("domain resolution cannot impose a cardinality cap")

    unknown = registry["unknown_policy"]
    if unknown != {
        "assignment_state": "unresolved",
        "reason": "no_exact_domain_registry_match",
        "evidence_destination": "CP5_alias_registry_evidence",
        "act_on_evidence": False,
        "receipt": "count_per_run_and_top_normalized_terms",
    }:
        raise RegistryError("unresolved domain evidence policy drifted")
    quarantine = registry["affinity_quarantine"]
    if quarantine != {
        "source_registry": "domain_superframe_affinity.v1",
        "serve_only": True,
        "may_assign_domain": False,
        "may_assign_or_forbid_superframe": False,
        "excluded_from_artifact_identity": True,
        "excluded_from_acceptance": True,
    }:
        raise RegistryError("domain affinity quarantine drifted")


def _validate_superframe_rules(
    registry: dict[str, Any],
    *,
    controlled_predicates: list[str],
    known_superframes: set[str],
    known_entity_types: set[str],
) -> None:
    expected_fields = {
        "registry",
        "version",
        "authority",
        "owner_ratification_required",
        "source",
        "changes_require_new_version",
        "policy_note",
        "condition_vocabulary",
        "coverage",
        "rules",
        "abstentions",
    }
    if set(registry) != expected_fields:
        raise RegistryError("superframe rule registry fields are not exact")
    _validate_proposed_v1_registry(
        registry,
        registry_id="superframe_rule_registry",
    )

    vocabulary = registry["condition_vocabulary"]
    if vocabulary != {
        "fields": ["subject_surface_tokens", "object_entity_types"],
        "operators": ["contains_any"],
    }:
        raise RegistryError("superframe condition vocabulary drifted")
    allowed_fields = set(vocabulary["fields"])
    allowed_operators = set(vocabulary["operators"])

    rules = registry["rules"]
    if not isinstance(rules, list) or not rules:
        raise RegistryError("superframe rule registry must contain rules")
    expected_rule_fields = {
        "rule_id",
        "priority",
        "predicates",
        "conditions",
        "frame_id",
        "terminal",
        "owner_attention",
        "source_line",
    }
    rule_ids: list[str] = []
    rule_predicates: set[str] = set()
    reachable_frames: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != expected_rule_fields:
            raise RegistryError("superframe rule fields are not exact")
        rule_id = rule["rule_id"]
        if not isinstance(rule_id, str) or not rule_id:
            raise RegistryError("superframe rule_id must be nonempty")
        rule_ids.append(rule_id)
        if (
            not isinstance(rule["priority"], int)
            or isinstance(rule["priority"], bool)
            or rule["priority"] < 0
        ):
            raise RegistryError(f"{rule_id}: priority must be a nonnegative integer")
        predicates = rule["predicates"]
        if (
            not isinstance(predicates, list)
            or not predicates
            or len(predicates) != len(set(predicates))
            or set(predicates) - set(controlled_predicates)
        ):
            raise RegistryError(f"{rule_id}: predicates are invalid or unknown")
        if rule["frame_id"] not in known_superframes:
            raise RegistryError(f"{rule_id}: unknown superframe {rule['frame_id']}")
        if rule["terminal"] is not True:
            raise RegistryError(f"{rule_id}: T9.1 rules must remain terminal")
        if not isinstance(rule["owner_attention"], bool):
            raise RegistryError(f"{rule_id}: owner_attention must be boolean")
        if not isinstance(rule["source_line"], str) or not rule["source_line"]:
            raise RegistryError(f"{rule_id}: source_line must be nonempty")

        conditions = rule["conditions"]
        if not isinstance(conditions, list):
            raise RegistryError(f"{rule_id}: conditions must be a list")
        for condition in conditions:
            if not isinstance(condition, dict) or set(condition) != {
                "field",
                "operator",
                "values",
            }:
                raise RegistryError(f"{rule_id}: condition fields are not exact")
            if condition["field"] not in allowed_fields:
                raise RegistryError(f"{rule_id}: unknown condition field")
            if condition["operator"] not in allowed_operators:
                raise RegistryError(f"{rule_id}: unknown condition operator")
            values = condition["values"]
            if (
                not isinstance(values, list)
                or not values
                or values != sorted(set(values))
            ):
                raise RegistryError(
                    f"{rule_id}: condition values must be sorted and unique"
                )
            if (
                condition["field"] == "object_entity_types"
                and set(values) - known_entity_types
            ):
                raise RegistryError(f"{rule_id}: unknown condition entity type")
        rule_predicates.update(predicates)
        reachable_frames.add(rule["frame_id"])
    if len(rule_ids) != len(set(rule_ids)):
        raise RegistryError("superframe rule IDs must be unique")

    abstentions = registry["abstentions"]
    if not isinstance(abstentions, list) or any(
        not isinstance(item, dict)
        or set(item) != {"predicate", "reason", "source_line"}
        for item in abstentions
    ):
        raise RegistryError("superframe abstention fields are not exact")
    abstention_predicates = [item["predicate"] for item in abstentions]
    if any(
        not isinstance(item["reason"], str)
        or not item["reason"]
        or not isinstance(item["source_line"], str)
        or not item["source_line"]
        for item in abstentions
    ):
        raise RegistryError("superframe abstentions require reason and source line")
    if len(abstention_predicates) != len(set(abstention_predicates)):
        raise RegistryError("superframe abstention predicates must be unique")
    if set(abstention_predicates) - set(controlled_predicates):
        raise RegistryError("superframe abstention references unknown predicate")
    if rule_predicates & set(abstention_predicates):
        raise RegistryError("a predicate cannot have both a rule and abstention")
    if rule_predicates | set(abstention_predicates) != set(controlled_predicates):
        raise RegistryError(
            "every controlled predicate requires a rule or explicit abstention"
        )
    if abstention_predicates != ["ASSOCIATED_WITH"]:
        raise RegistryError("ASSOCIATED_WITH must remain the explicit abstention")

    used_for_rules = [rule for rule in rules if "USED_FOR" in rule["predicates"]]
    if len(used_for_rules) != 1 or not used_for_rules[0]["owner_attention"]:
        raise RegistryError("USED_FOR mapping must be flagged for owner attention")
    if any(
        rule["owner_attention"] and "USED_FOR" not in rule["predicates"]
        for rule in rules
    ):
        raise RegistryError("only USED_FOR may carry the v1 owner-attention flag")

    specialization = [
        rule
        for rule in rules
        if rule["rule_id"] == "predicate_frame.decreases_cumulative_baseline.v1"
    ]
    if len(specialization) != 1 or specialization[0]["frame_id"] != "MF15":
        raise RegistryError("the owner MF15 specialization is missing")
    base_decreases_priorities = [
        rule["priority"]
        for rule in rules
        if "DECREASES" in rule["predicates"] and rule["frame_id"] == "MF04"
    ]
    if not base_decreases_priorities or specialization[0]["priority"] <= max(
        base_decreases_priorities
    ):
        raise RegistryError("MF15 specialization must outrank the MF04 base rule")

    coverage = registry["coverage"]
    expected_coverage_fields = {
        "controlled_predicate_count",
        "rule_covered_predicate_count",
        "explicit_abstention_count",
        "reachable_superframe_ids",
        "reachable_superframe_count",
        "total_superframe_count",
        "coverage_note",
    }
    if set(coverage) != expected_coverage_fields:
        raise RegistryError("superframe coverage fields are not exact")
    if coverage["controlled_predicate_count"] != len(controlled_predicates):
        raise RegistryError("superframe controlled-predicate count is dishonest")
    if coverage["rule_covered_predicate_count"] != len(rule_predicates):
        raise RegistryError("superframe rule-covered count is dishonest")
    if coverage["explicit_abstention_count"] != len(abstention_predicates):
        raise RegistryError("superframe abstention count is dishonest")
    if coverage["reachable_superframe_ids"] != sorted(reachable_frames):
        raise RegistryError("superframe reachable IDs are dishonest")
    if coverage["reachable_superframe_count"] != len(reachable_frames):
        raise RegistryError("superframe reachable count is dishonest")
    if coverage["total_superframe_count"] != len(known_superframes):
        raise RegistryError("superframe total-frame count is dishonest")
    if len(reachable_frames) != 8:
        raise RegistryError("predicate routing must honestly report 8/16 frames")


def _read(name: str) -> dict[str, Any]:
    path = REGISTRY_DIR / FILES[name]
    if not path.exists():
        raise RegistryError(f"registry file missing: {path}")
    with path.open() as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def load_all() -> dict[str, dict[str, Any]]:
    """Load, validate, and cross-check every registry. Cached per process."""
    data = {name: _read(name) for name in FILES}

    domains = {d["domain_id"]: d for d in data["domain"]["domains"]}
    superframes = {m["mf_id"]: m for m in data["superframe"]["superframes"]}
    motifs = {m["motif_id"]: m for m in data["motif"]["motifs"]}

    if len(domains) != 16:
        raise RegistryError(f"expected 16 domains, found {len(domains)}")
    if len(superframes) != 16:
        raise RegistryError(f"expected 16 superframes, found {len(superframes)}")
    if len(motifs) != 12:
        raise RegistryError(f"expected 12 motifs, found {len(motifs)}")

    for row in data["affinity"]["affinities"]:
        if row["domain_id"] not in domains:
            raise RegistryError(
                f"affinity references unknown domain {row['domain_id']}"
            )
        for mf in row["dominant_superframes"]:
            if mf not in superframes:
                raise RegistryError(
                    f"affinity[{row['domain_id']}] references unknown superframe {mf}"
                )

    bindings = {b["motif_id"]: b for b in data["binding"]["bindings"]}
    for motif_id, motif in motifs.items():
        if motif_id not in bindings:
            raise RegistryError(f"motif {motif_id} has no stage bindings")
        bound_stages = [s["stage"] for s in bindings[motif_id]["stages"]]
        if bound_stages != motif["stages"]:
            raise RegistryError(
                f"binding stages for {motif_id} do not match motif registry stages"
            )
        for stage in bindings[motif_id]["stages"]:
            adm = stage.get("admissible") or []
            if not adm:
                raise RegistryError(
                    f"{motif_id}/{stage['stage']}: no admissible superframes"
                )
            dominants = [a for a in adm if a.get("tier") == "dominant"]
            if len(dominants) != 1:
                raise RegistryError(
                    f"{motif_id}/{stage['stage']}: expected exactly 1 dominant, "
                    f"found {len(dominants)}"
                )
            for a in adm:
                if a["superframe"] not in superframes:
                    raise RegistryError(
                        f"{motif_id}/{stage['stage']}: unknown superframe {a['superframe']}"
                    )

    vocab = data["vocab"]
    for key in ("entity_types", "predicate_types", "modalities", "polarities"):
        if not vocab.get(key):
            raise RegistryError(f"extraction vocabulary missing {key}")

    _validate_domain_resolution_policy(data["domain_resolution"])
    _validate_superframe_rules(
        data["superframe_rule"],
        controlled_predicates=vocab["predicate_types"],
        known_superframes=set(superframes),
        known_entity_types=set(vocab["entity_types"]),
    )

    predicate_normalization = data["predicate_normalization"]
    expected_normalization_fields = {
        "registry",
        "version",
        "authority",
        "owner_ratification_required",
        "source",
        "unknown_policy",
        "default_predicate",
        "match_field",
        "negation_modality_polarity_out_of_scope",
        "changes_require_new_version",
        "normalizations",
    }
    if set(predicate_normalization) != expected_normalization_fields:
        raise RegistryError("predicate normalization registry fields are not exact")
    if predicate_normalization["registry"] != "predicate_normalization":
        raise RegistryError("predicate normalization registry has the wrong identity")
    if predicate_normalization["version"] != "v1":
        raise RegistryError("predicate normalization registry version must be v1")
    if predicate_normalization["authority"] != "executor-proposed, owner-ratifiable":
        raise RegistryError("predicate normalization authority mark is missing")
    if predicate_normalization["owner_ratification_required"] is not True:
        raise RegistryError("predicate normalization must remain owner-ratifiable")
    if predicate_normalization["unknown_policy"] != "unresolved_spans":
        raise RegistryError("unknown predicate lemmas must become unresolved spans")
    if predicate_normalization["default_predicate"] is not None:
        raise RegistryError("predicate normalization cannot define a default predicate")
    if predicate_normalization["match_field"] != "spacy_lemma_lowercase":
        raise RegistryError("predicate normalization must match lowercase spaCy lemmas")
    if predicate_normalization["negation_modality_polarity_out_of_scope"] is not True:
        raise RegistryError("predicate normalization cannot absorb qualifier semantics")
    if predicate_normalization["changes_require_new_version"] is not True:
        raise RegistryError("predicate normalization must require monotonic versions")

    rows = predicate_normalization["normalizations"]
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or set(row) != {"predicate_type", "lemmas"}
        for row in rows
    ):
        raise RegistryError("predicate normalization rows must have exact fields")
    predicate_order = [row["predicate_type"] for row in rows]
    if predicate_order != vocab["predicate_types"]:
        raise RegistryError(
            "predicate normalization must cover vocab predicates in order"
        )
    all_lemmas: list[str] = []
    for row in rows:
        lemmas = row["lemmas"]
        if not isinstance(lemmas, list) or lemmas != sorted(set(lemmas)):
            raise RegistryError(
                f"predicate normalization lemmas for {row['predicate_type']} "
                "must be sorted and unique"
            )
        if any(
            not isinstance(lemma, str)
            or not lemma
            or lemma != lemma.strip().lower()
            or not lemma.replace("-", "").isalpha()
            for lemma in lemmas
        ):
            raise RegistryError(
                "predicate normalization lemmas must be lowercase words"
            )
        all_lemmas.extend(lemmas)
    if len(all_lemmas) != len(set(all_lemmas)):
        raise RegistryError("predicate normalization lemmas must map uniquely")

    budgets = data["latent_policy"]["budgets"]
    for level, spec in budgets.items():
        raw, kept = spec.get("raw_generated"), spec.get("retained_distinct")
        if isinstance(raw, list) and isinstance(kept, list):
            if not (raw[0] <= raw[1] and kept[0] <= kept[1] and kept[1] <= raw[1]):
                raise RegistryError(f"latent budget for {level} is incoherent: {spec}")

    embedding_instructions = data["embedding_instruction"]
    if embedding_instructions.get("registry") != "embedding_instruction_registry":
        raise RegistryError("embedding instruction registry has the wrong identity")
    if not embedding_instructions.get("version"):
        raise RegistryError("embedding instruction registry is missing its version")
    for profile_name in ("baseline_live_v0", "universal"):
        profile = embedding_instructions.get(profile_name)
        if (
            not isinstance(profile, dict)
            or not str(profile.get("instruction") or "").strip()
        ):
            raise RegistryError(
                f"embedding instruction profile {profile_name!r} is missing canonical text"
            )

    return data


def registry_hashes() -> dict[str, str]:
    """Tamper-evident snapshot hash per registry (namespace: 'registry')."""
    return {name: namespace_hash("registry", _read(name)) for name in FILES}


# ── resolver API ─────────────────────────────────────────────────────────────


def domain(domain_id: str) -> dict[str, Any]:
    d = {x["domain_id"]: x for x in load_all()["domain"]["domains"]}
    if domain_id not in d:
        raise RegistryError(f"unknown domain {domain_id}")
    return d[domain_id]


def superframe(mf_id: str) -> dict[str, Any]:
    s = {x["mf_id"]: x for x in load_all()["superframe"]["superframes"]}
    if mf_id not in s:
        raise RegistryError(f"unknown superframe {mf_id}")
    return s[mf_id]


def motif(motif_id: str) -> dict[str, Any]:
    m = {x["motif_id"]: x for x in load_all()["motif"]["motifs"]}
    if motif_id not in m:
        raise RegistryError(f"unknown motif {motif_id}")
    return m[motif_id]


def admissible_superframes(motif_id: str, stage: str) -> list[dict[str, str]]:
    """[{superframe, tier}] for one motif stage; dominant listed first."""
    bindings = {b["motif_id"]: b for b in load_all()["binding"]["bindings"]}
    if motif_id not in bindings:
        raise RegistryError(f"unknown motif {motif_id}")
    for s in bindings[motif_id]["stages"]:
        if s["stage"] == stage:
            return sorted(s["admissible"], key=lambda a: a["tier"] != "dominant")
    raise RegistryError(f"unknown stage {stage!r} for motif {motif_id}")


def domain_affinity_priors(domain_id: str) -> list[str]:
    """Commonly-dominant superframes for a domain. PRIORS ONLY — callers must
    never use these to force or forbid an assignment (owner rule)."""
    rows = {r["domain_id"]: r for r in load_all()["affinity"]["affinities"]}
    if domain_id not in rows:
        raise RegistryError(f"unknown domain {domain_id}")
    return list(rows[domain_id]["dominant_superframes"])


def domain_resolution_policy() -> dict[str, Any]:
    """Return the immutable exact-match domain-resolution recipe."""

    return load_all()["domain_resolution"]


def superframe_rule_registry() -> dict[str, Any]:
    """Return the immutable candidate-only predicate→superframe recipe."""

    return load_all()["superframe_rule"]


def is_controlled_predicate(predicate: str) -> bool:
    return predicate in load_all()["vocab"]["predicate_types"]


def is_entity_type(entity_type: str) -> bool:
    return entity_type in load_all()["vocab"]["entity_types"]


def normalize_predicate_lemma(lemma: str) -> dict[str, str] | None:
    """Resolve one lowercase lemma without ever inventing a default edge."""

    normalized = str(lemma or "").strip().lower()
    registry = load_all()["predicate_normalization"]
    for row in registry["normalizations"]:
        if normalized in row["lemmas"]:
            return {
                "lemma": normalized,
                "predicate_type": row["predicate_type"],
                "registry": registry["registry"],
                "registry_version": registry["version"],
                "authority": registry["authority"],
            }
    return None


def latent_budget(level: str) -> dict[str, Any]:
    budgets = load_all()["latent_policy"]["budgets"]
    if level not in budgets:
        raise RegistryError(f"unknown latent budget level {level!r}")
    return budgets[level]


def embedding_instruction_profile(profile_name: str) -> dict[str, str]:
    """Resolve one immutable Qwen3 query-instruction profile.

    ``baseline_live_v0`` retains its historical profile version so existing
    cache keys remain stable. New registry profiles derive a version from the
    immutable registry id/version/profile tuple; the canonical wording stays
    in registry data rather than Python conditionals.
    """

    registry = load_all()["embedding_instruction"]
    if profile_name not in {"baseline_live_v0", "universal"}:
        raise RegistryError(
            f"unknown embedding instruction profile {profile_name!r}; "
            "valid: ['baseline_live_v0', 'universal']"
        )
    profile = registry[profile_name]
    instruction_version = str(profile.get("profile_version") or "").strip()
    if not instruction_version:
        instruction_version = (
            f"{registry['registry']}.{registry['version']}.{profile_name}"
        )
    return {
        "profile_name": profile_name,
        "instruction": str(profile["instruction"]).strip(),
        "instruction_version": instruction_version,
    }
