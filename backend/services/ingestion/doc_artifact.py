"""Passive document artifact compiler for source-role synthesis headers.

The artifact is not a summary and not retrieval metadata. It labels what a
document is useful for after retrieval has already selected child evidence.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

ARTIFACT_VERSION = "polymath.doc_artifact.v1"

SOURCE_ROLE_ENUM = (
    "model_specific_advice",
    "model_reference",
    "technique_theory",
    "workflow_guidance",
    "reference_material",
    "example_prompts",
    "research",
    "general_context",
)


VIDEO_MODEL_REGISTRY: dict[str, dict[str, Any]] = {
    "kling": {"label": "Kling", "aliases": ("kling", "kuaishou kling")},
    "seedance": {"label": "Seedance", "aliases": ("seedance", "seedance pro", "seedance 1")},
    "veo": {"label": "Veo", "aliases": ("veo", "veo 2", "veo 3", "google veo")},
    "sora": {"label": "Sora", "aliases": ("sora", "openai sora")},
    "runway": {"label": "Runway", "aliases": ("runway", "runwayml", "gen-2", "gen 2", "gen-3", "gen 3")},
    "pika": {"label": "Pika", "aliases": ("pika", "pika labs")},
    "hailuo": {"label": "Hailuo", "aliases": ("hailuo", "hailuo ai", "minimax video")},
    "luma": {"label": "Luma", "aliases": ("luma", "luma dream machine", "dream machine")},
    "wan": {"label": "Wan", "aliases": ("wan video", "wan 2", "wan2", "wan 2.1", "wan2.1")},
    "minimax": {"label": "Minimax", "aliases": ("minimax", "mini max")},
    "pixverse": {"label": "PixVerse", "aliases": ("pixverse", "pix verse")},
    "haiper": {"label": "Haiper", "aliases": ("haiper",)},
}

_AMBIGUOUS_ALIASES = {
    "wan",
    "runway",
    "luma",
    "minimax",
}


@dataclass(frozen=True)
class DocArtifact:
    owner_intent: str | None
    source_role: list[str]
    model_scope: list[str] | None
    synthesis_hint: str
    artifact_version: str
    field_provenance: dict[str, str]
    confidence: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner_intent": self.owner_intent,
            "source_role": list(self.source_role),
            "model_scope": list(self.model_scope) if self.model_scope else None,
            "synthesis_hint": self.synthesis_hint,
            "artifact_version": self.artifact_version,
            "field_provenance": dict(self.field_provenance),
            "confidence": dict(self.confidence),
        }


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_text(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_text(v) for v in value)
    return str(value)


def _norm_space(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", _text(value)).strip()
    return text[:limit].rstrip()


def _word_present(haystack: str, phrase: str) -> bool:
    phrase = phrase.strip().lower()
    if not phrase:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(phrase).replace(r"\ ", r"[\s_-]+") + r"(?![a-z0-9])"
    return re.search(pattern, haystack, flags=re.IGNORECASE) is not None


def _alias_allowed(alias: str, haystack: str) -> bool:
    alias_l = alias.lower().strip()
    if alias_l not in _AMBIGUOUS_ALIASES:
        return True
    guard = rf"(?<![a-z0-9]){re.escape(alias_l)}(?![a-z0-9]).{{0,36}}\b(video|model|prompt|generation|image-to-video|text-to-video)\b"
    reverse_guard = rf"\b(video|model|prompt|generation|image-to-video|text-to-video)\b.{{0,36}}(?<![a-z0-9]){re.escape(alias_l)}(?![a-z0-9])"
    return (
        re.search(guard, haystack, flags=re.IGNORECASE) is not None
        or re.search(reverse_guard, haystack, flags=re.IGNORECASE) is not None
    )


def _entity_texts(entities: list[Any] | tuple[Any, ...] | None) -> str:
    pieces: list[str] = []
    for ent in entities or []:
        if isinstance(ent, dict):
            for key in ("canonical_name", "surface_form", "name", "entity", "entity_id"):
                if ent.get(key):
                    pieces.append(str(ent[key]))
        else:
            pieces.append(str(ent))
    return " ".join(pieces)


def _detect_model_scope(
    *,
    doc_profile: dict[str, Any],
    facet_profile: dict[str, Any] | None,
    source_meta: dict[str, Any] | None,
    ghost_b_entities: list[Any] | tuple[Any, ...] | None,
    corpus_description: str | None,
) -> tuple[list[str] | None, float, str]:
    buckets = {
        "source": _text(source_meta),
        "profile": _text(doc_profile),
        "facet": _text(facet_profile),
        "ghost": _entity_texts(ghost_b_entities),
        "corpus": corpus_description or "",
    }
    weights = {"source": 3.0, "ghost": 3.0, "profile": 2.0, "facet": 1.5, "corpus": 0.5}
    scores: Counter[str] = Counter()
    for bucket, value in buckets.items():
        hay = value.lower()
        if not hay:
            continue
        for model_key, spec in VIDEO_MODEL_REGISTRY.items():
            for alias in spec["aliases"]:
                if _word_present(hay, alias) and _alias_allowed(alias, hay):
                    scores[model_key] += weights[bucket]
                    break

    if not scores:
        return None, 0.0, "none"

    ranked = scores.most_common()
    top_score = ranked[0][1]
    selected = [key for key, score in ranked if score >= max(1.5, top_score * 0.75)]
    labels = [VIDEO_MODEL_REGISTRY[key]["label"] for key in selected[:3]]
    if len(selected) == 1:
        confidence = 0.92 if top_score >= 3.0 else 0.72
    else:
        confidence = 0.64
    return labels, confidence, "deterministic"


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(_word_present(text, term) for term in terms)


def _detect_source_roles(
    *,
    combined_text: str,
    model_scope: list[str] | None,
    chunk_kind_stats: dict[str, int] | None,
) -> tuple[list[str], float, str]:
    text = combined_text.lower()
    roles: list[str] = []

    if _has_any(text, ("prompt", "prompts", "prompting", "negative prompt", "example prompt")):
        roles.append("example_prompts")
    if model_scope and _has_any(text, ("parameter", "settings", "syntax", "api", "constraint", "docs", "reference")):
        roles.append("model_reference")
    if model_scope and _has_any(text, ("tutorial", "how to", "workflow", "step", "guide", "recipe", "best practice")):
        roles.append("model_specific_advice")
    if _has_any(text, ("workflow", "pipeline", "step by step", "process", "checklist", "guide", "tutorial")):
        roles.append("workflow_guidance")
    if _has_any(text, ("theory", "technique", "cinematic", "cinematography", "directing", "film language", "composition", "lighting")):
        roles.append("technique_theory")
    if _has_any(text, ("manual", "reference", "documentation", "specification", "api", "schema", "glossary")):
        roles.append("reference_material")
    if _has_any(text, ("study", "paper", "research", "experiment", "evaluation", "benchmark")):
        roles.append("research")

    stats = chunk_kind_stats or {}
    if stats.get("code", 0) or stats.get("api", 0):
        roles.append("reference_material")

    deduped = [role for role in SOURCE_ROLE_ENUM if role in set(roles)]
    if deduped:
        return deduped[:3], 0.86 if deduped[0] != "general_context" else 0.45, "deterministic"
    return ["general_context"], 0.35, "none"


def _synthesis_hint(source_role: list[str], model_scope: list[str] | None) -> str:
    role_set = set(source_role or [])
    model_text = ", ".join(model_scope or [])
    if "model_specific_advice" in role_set and model_text:
        return (
            f"Use this source for {model_text}-specific workflow, syntax, constraints, "
            "and prompt construction. Child chunks remain the citation authority."
        )
    if "model_reference" in role_set and model_text:
        return (
            f"Use this source as a {model_text} reference. Treat transferable technique "
            "claims as secondary unless child chunks establish them."
        )
    if "example_prompts" in role_set:
        return (
            "Use this source as prompt/example material and adapt patterns only where "
            "the retrieved child chunks support the constraints."
        )
    if "technique_theory" in role_set:
        return (
            "Use this source for transferable technique, vocabulary, and structure; "
            "do not treat it as model-specific syntax unless child chunks say so."
        )
    if "workflow_guidance" in role_set:
        return (
            "Use this source for process ordering and workflow framing; ground factual "
            "claims in retrieved child chunks."
        )
    if "research" in role_set:
        return "Use this source for research findings and caveats supported by child chunks."
    if "reference_material" in role_set:
        return "Use this source as reference material; cite only retrieved child chunks."
    return "Use this source as general context only; cite only retrieved child chunks."


def build_doc_artifact(
    doc_profile: dict[str, Any] | None,
    facet_profile: dict[str, Any] | None = None,
    source_meta: dict[str, Any] | None = None,
    ghost_b_entities: list[Any] | tuple[Any, ...] | None = None,
    chunk_kind_stats: dict[str, int] | None = None,
    owner_fields: dict[str, Any] | None = None,
    corpus_description: str | None = None,
) -> dict[str, Any] | None:
    """Compile a passive document artifact from already-stored document signals.

    Missing doc_profile is a no-op because there is no stable source card to
    label. The compiler performs no I/O, no embedding, no retrieval, and no LLM
    calls.
    """
    profile = dict(doc_profile or {})
    if not _norm_space(profile.get("summary")) and not profile.get("concepts") and not profile.get("domains"):
        return None

    owner = dict(owner_fields or {})
    existing_owner = _norm_space(owner.get("owner_intent"))
    owner_source = _norm_space(
        (owner.get("field_provenance") or {}).get("owner_intent")
        if isinstance(owner.get("field_provenance"), dict)
        else owner.get("owner_intent_source"),
        limit=80,
    )
    corpus_owner = _norm_space(corpus_description, limit=500)
    if existing_owner and (not owner_source or owner_source == "owner"):
        owner_intent = existing_owner
        owner_prov = "owner"
    elif corpus_owner:
        owner_intent = corpus_owner
        owner_prov = "corpus_description"
    else:
        owner_intent = None
        owner_prov = "none"

    model_scope, model_conf, model_prov = _detect_model_scope(
        doc_profile=profile,
        facet_profile=facet_profile or {},
        source_meta=source_meta or {},
        ghost_b_entities=ghost_b_entities or [],
        corpus_description=corpus_description,
    )
    owner_model_scope = _valid_owner_scope(owner.get("model_scope"))
    if _field_source(owner, "model_scope") == "owner" and owner_model_scope:
        model_scope = owner_model_scope
        model_conf = 1.0
        model_prov = "owner"
    combined = " ".join([
        _text(source_meta or {}),
        _text(profile),
        _text(facet_profile or {}),
        _entity_texts(ghost_b_entities or []),
    ])
    roles, role_conf, role_prov = _detect_source_roles(
        combined_text=combined,
        model_scope=model_scope,
        chunk_kind_stats=chunk_kind_stats or {},
    )
    owner_roles = _valid_owner_roles(owner.get("source_role"))
    if _field_source(owner, "source_role") == "owner" and owner_roles:
        roles = owner_roles
        role_conf = 1.0
        role_prov = "owner"

    artifact = DocArtifact(
        owner_intent=owner_intent,
        source_role=roles,
        model_scope=model_scope,
        synthesis_hint=_synthesis_hint(roles, model_scope),
        artifact_version=ARTIFACT_VERSION,
        field_provenance={
            "owner_intent": owner_prov,
            "source_role": role_prov,
            "model_scope": model_prov,
            "synthesis_hint": "template",
        },
        confidence={
            "source_role": round(float(role_conf), 3),
            "model_scope": round(float(model_conf), 3),
        },
    )
    return artifact.to_dict()


def _label_list(values: list[str] | tuple[str, ...] | None) -> str:
    return ", ".join(str(v).replace("_", " ") for v in values or [] if str(v).strip())


def _field_source(owner_fields: dict[str, Any], field_name: str) -> str:
    provenance = owner_fields.get("field_provenance")
    if isinstance(provenance, dict):
        value = provenance.get(field_name)
        if value:
            return str(value).strip()
    legacy = owner_fields.get(f"{field_name}_source")
    return str(legacy or "").strip()


def _valid_owner_roles(value: Any) -> list[str]:
    roles = []
    for role in value or []:
        role_text = str(role or "").strip()
        if role_text in SOURCE_ROLE_ENUM and role_text not in roles:
            roles.append(role_text)
    return roles


def _valid_owner_scope(value: Any) -> list[str] | None:
    if not isinstance(value, (list, tuple, set)):
        return None
    scope = [_norm_space(item, 80) for item in value]
    scope = [item for item in scope if item]
    return scope or None


def format_source_role_header(doc_label: str, artifact: dict[str, Any] | None) -> str:
    """Render the compact passive header shown to the synthesis model."""
    if not artifact or artifact.get("artifact_version") != ARTIFACT_VERSION:
        return ""
    parts = [f'[Source: "{_norm_space(doc_label, 120) or "Unknown"}"']
    roles = _label_list(artifact.get("source_role") or [])
    if roles:
        parts.append(f"role: {roles}")
    models = _label_list(artifact.get("model_scope") or [])
    if models:
        parts.append(f"model scope: {models}")
    owner = _norm_space(artifact.get("owner_intent"), 180)
    if owner:
        parts.append(f'owner note: "{owner}"')
    hint = _norm_space(artifact.get("synthesis_hint"), 240)
    if hint:
        parts.append(f"hint: {hint}")
    parts.append("context only, not citable evidence]")
    return " - ".join(parts)
