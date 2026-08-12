"""Deterministic compiler: encoder proposals -> canonical, gated facts.

Owner-specified architecture (2026-08-12). The extraction model terminates
its responsibility at PROPOSAL GENERATION. It is not asked to settle
ontology representation, canonical identity, direction, or deduplication —
those are different error classes and belong here, in deterministic code.

    encoder proposals (IR)
        -> entity normalizer      canonical name, alias resolution, typing
        -> predicate normalizer   surface label -> canonical predicate
        -> argument compiler      endpoint existence, type signature, direction
        -> duplicate collapse
        -> acceptance gate        ACCEPT | REVIEW | REJECT (multi-signal)

What this fixes, measured on the live corpus before it existed:
  - 32,549 of 32,971 entities (98.7%) stored with entity_type "other";
    untypeable spans like "full listing" became graph nodes
  - relations whose endpoints were never entities at all
  - a single confidence threshold acting as the entire acceptance policy

The gate is NOT a score cutoff. A proposal is accepted only when every hard
constraint holds: both endpoints resolve to surviving canonical entities,
the (subject_type, object_type) pair is legal for the canonical predicate
under config/ontology.yaml, the evidence span is non-empty, and the
extraction evidence is strong. Anything plausible-but-unresolved routes to
REVIEW for adjudication rather than being silently kept or silently dropped.

Every rejection carries a machine-readable reason so the quality loop can
attribute failures to model / compiler / ontology instead of guessing.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# Untypeable spans are proposals, not knowledge. The encoder saying "other"
# is it telling us the span matched no ontology class; persisting those as
# nodes is what produced the 98.7% junk layer.
DROP_UNTYPED = True
UNTYPED_LABELS = {"other", "", "unknown"}


@dataclass
class CompiledFact:
    subject_id: str
    subject_name: str
    subject_type: str
    predicate: str
    object_id: str
    object_name: str
    object_type: str
    confidence: float
    evidence_phrase: str
    chunk_id: str
    verdict: str  # accept | review
    reasons: tuple[str, ...] = ()


@dataclass
class CompileReport:
    facts: list[CompiledFact] = field(default_factory=list)
    entities_in: int = 0
    entities_kept: int = 0
    relations_in: int = 0
    accepted: int = 0
    review: int = 0
    rejected: int = 0
    reject_reasons: Counter = field(default_factory=Counter)
    entity_drop_reasons: Counter = field(default_factory=Counter)

    def as_metrics(self) -> dict[str, Any]:
        total = self.accepted + self.review + self.rejected
        return {
            "entities_in": self.entities_in,
            "entities_kept": self.entities_kept,
            "relations_in": self.relations_in,
            "accepted": self.accepted,
            "review": self.review,
            "rejected": self.rejected,
            "accept_rate": round(self.accepted / total, 4) if total else 0.0,
            "review_fraction": round(self.review / total, 4) if total else 0.0,
            "reject_reasons": dict(self.reject_reasons.most_common(10)),
            "entity_drop_reasons": dict(self.entity_drop_reasons.most_common(10)),
        }


def _thresholds() -> tuple[float, float]:
    def _f(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, "") or default)
        except ValueError:
            return default

    # accept_at: strong evidence; review_at: plausible, needs adjudication
    return _f("COMPILER_ACCEPT_CONFIDENCE", 0.60), _f("COMPILER_REVIEW_CONFIDENCE", 0.40)


def _ontology_signatures() -> dict[str, set[tuple[str, str]]]:
    """canonical predicate -> legal (subject_type, object_type) pairs."""
    try:
        import yaml

        from services.extraction.canonical import find_config_dir

        path = find_config_dir(__file__) / "ontology.yaml"
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:  # noqa: BLE001 — no ontology means no pair gate, not a crash
        return {}
    out: dict[str, set[tuple[str, str]]] = {}
    for name, spec in (data.get("predicates") or {}).items():
        pairs = (spec or {}).get("allowed_pairs") or []
        legal = {
            (str(p[0]).strip().lower(), str(p[1]).strip().lower())
            for p in pairs
            if isinstance(p, (list, tuple)) and len(p) == 2
        }
        if legal:
            out[str(name)] = legal
    return out


def _all_signature_types(sigs: dict[str, set[tuple[str, str]]]) -> set[tuple[str, str]]:
    return {pair for legal in sigs.values() for pair in legal}


def _domain_signatures(domain: str) -> dict[str, set[tuple[str, str]]]:
    """Load config/ontology/<domain>.yaml. Pure function of file content.

    Deterministic: the same file always yields the same frozen pair sets,
    independent of dict iteration order or call order. Idempotent: loading
    repeatedly neither accumulates nor mutates state — the cache below is
    keyed by domain and holds the same value it computed the first time.
    """
    safe = "".join(ch for ch in str(domain or "") if ch.isalnum() or ch in "_-")
    if not safe:
        return {}
    try:
        import yaml

        from services.extraction.canonical import find_config_dir

        path = find_config_dir(__file__) / "ontology" / f"{safe}.yaml"
        if not path.exists():
            return {}
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:  # noqa: BLE001 — a missing domain is a gap, not a crash
        return {}
    out: dict[str, set[tuple[str, str]]] = {}
    for name, spec in sorted((data.get("predicates") or {}).items()):
        pairs = (spec or {}).get("allowed_pairs") or []
        legal = {
            (str(p[0]).strip().lower(), str(p[1]).strip().lower())
            for p in pairs
            if isinstance(p, (list, tuple)) and len(p) == 2
        }
        if legal:
            out[str(name).strip().lower()] = legal
    return out


_SIGNATURES: dict[str, set[tuple[str, str]]] | None = None
_DOMAIN_CACHE: dict[str, dict[str, set[tuple[str, str]]]] = {}


def signatures(domain: str | None = None) -> dict[str, set[tuple[str, str]]]:
    """Global signatures, overlaid with the corpus domain's when given.

    A domain predicate REPLACES the global entry for that predicate rather
    than unioning with it: the domain file is the authority for its own
    vocabulary, so `uses` in film_production means film pairs, not the
    global ones. Merge order is fixed, so the result is deterministic.
    """
    global _SIGNATURES
    if _SIGNATURES is None:
        _SIGNATURES = _ontology_signatures()
    if not domain:
        return _SIGNATURES
    key = str(domain).strip().lower()
    if key not in _DOMAIN_CACHE:
        _DOMAIN_CACHE[key] = _domain_signatures(key)
    domain_sigs = _DOMAIN_CACHE[key]
    if not domain_sigs:
        return _SIGNATURES
    merged = dict(_SIGNATURES)
    merged.update(domain_sigs)
    return merged


@dataclass(frozen=True)
class _Entity:
    entity_id: str
    name: str
    entity_type: str
    confidence: float


def compile_entities(
    proposals: Sequence[Any], report: CompileReport
) -> dict[str, _Entity]:
    """Normalize proposals into canonical entities, keyed by proposal name.

    Drops what the ontology cannot represent: untyped spans, generic names
    ("full listing", "the system"), and empty canonical forms.
    """
    from services.extraction.canonical import (
        canonical_entity_type,
        canonicalize_entity_name,
        entity_id_with_type,
        is_generic_entity_name,
    )

    resolved: dict[str, _Entity] = {}
    for proposal in proposals:
        report.entities_in += 1
        raw_name = str(getattr(proposal, "canonical_name", "") or "")
        raw_type = str(getattr(proposal, "entity_type", "") or "")
        if DROP_UNTYPED and raw_type.strip().lower() in UNTYPED_LABELS:
            report.entity_drop_reasons["untyped_by_encoder"] += 1
            continue
        try:
            name = canonicalize_entity_name(raw_name)
            etype = canonical_entity_type(raw_type)
            # canonical_entity_type only knows the GLOBAL vocabulary, so a
            # per-corpus domain label ("Film", "Narrative Device", "Shot
            # Type") collapses to "other" and the entity gets dropped —
            # which then strands every relation that referenced it. When the
            # encoder returned a real label that the global normalizer does
            # not recognise, keep the domain type instead of destroying it.
            if etype.strip().lower() in UNTYPED_LABELS and raw_type.strip().lower() not in UNTYPED_LABELS:
                etype = raw_type.strip().lower()
        except Exception:  # noqa: BLE001
            report.entity_drop_reasons["canonicalization_error"] += 1
            continue
        if not name:
            report.entity_drop_reasons["empty_canonical_name"] += 1
            continue
        if is_generic_entity_name(name):
            report.entity_drop_reasons["generic_name"] += 1
            continue
        if etype.strip().lower() in UNTYPED_LABELS:
            report.entity_drop_reasons["type_resolved_to_other"] += 1
            continue
        entity = _Entity(
            entity_id=entity_id_with_type(name, etype),
            name=name,
            entity_type=etype,
            confidence=float(getattr(proposal, "confidence", 0.0) or 0.0),
        )
        # keyed by BOTH the raw proposal name and the canonical form so
        # relation endpoints resolve regardless of which the encoder emitted
        resolved.setdefault(raw_name.lower(), entity)
        resolved.setdefault(name, entity)
        report.entities_kept += 1
    return resolved


def compile_relations(
    proposals: Sequence[Any],
    entities: dict[str, _Entity],
    chunk_id: str,
    report: CompileReport,
    domain: str | None = None,
) -> list[CompiledFact]:
    """Apply the acceptance policy. Never a bare score comparison."""
    from services.extraction.canonical import (
        canonicalize_entity_name,
        canonicalize_predicate_label,
    )

    accept_at, review_at = _thresholds()
    sigs = signatures(domain)
    facts: list[CompiledFact] = []

    for proposal in proposals:
        report.relations_in += 1
        reasons: list[str] = []
        subj_raw = str(getattr(proposal, "subject", "") or "")
        obj_raw = str(getattr(proposal, "object", "") or "")
        predicate = canonicalize_predicate_label(
            str(getattr(proposal, "predicate", "") or "")
        )
        confidence = float(getattr(proposal, "confidence", 0.0) or 0.0)
        evidence = str(getattr(proposal, "evidence_phrase", "") or "").strip()

        subject = entities.get(subj_raw.lower()) or entities.get(
            canonicalize_entity_name(subj_raw)
        )
        obj = entities.get(obj_raw.lower()) or entities.get(
            canonicalize_entity_name(obj_raw)
        )

        # HARD CONSTRAINT: both endpoints must be surviving canonical entities.
        # This is what stops "hook part_of creative system" when neither side
        # was ever extracted as an entity.
        if subject is None or obj is None:
            report.rejected += 1
            report.reject_reasons["unresolved_endpoint"] += 1
            continue
        if subject.entity_id == obj.entity_id:
            report.rejected += 1
            report.reject_reasons["self_loop"] += 1
            continue
        if not predicate:
            report.rejected += 1
            report.reject_reasons["no_canonical_predicate"] += 1
            continue
        if not evidence:
            report.rejected += 1
            report.reject_reasons["missing_evidence"] += 1
            continue

        # HARD CONSTRAINT: type signature must be legal for this predicate.
        # BUG 2 fix: a predicate with NO ontology signature is unconstrained,
        # which silently let anything through. The ontology is the authority —
        # an unknown predicate is an ontology gap, so route to REVIEW and
        # record it for the quality loop rather than accepting blind.
        legal = sigs.get(predicate)
        subject_type = subject.entity_type.strip().lower()
        object_type = obj.entity_type.strip().lower()
        # Corpora may carry DOMAIN vocabularies ("Programming Language",
        # "Shot Type") that the global ontology has no signatures for. An
        # unknown type is an ontology gap, not a violation — route to REVIEW
        # so the quality loop can close it, rather than destroying the fact.
        known_types = {t for pair in _all_signature_types(sigs) for t in pair}
        domain_typed = subject_type not in known_types or object_type not in known_types
        if legal is None:
            reasons.append("predicate_has_no_ontology_signature")
        elif domain_typed:
            reasons.append("domain_type_outside_global_ontology")
        elif (subject_type, object_type) not in legal:
            if (object_type, subject_type) in legal:
                # direction is invertible under the same predicate — flag for
                # adjudication rather than silently flipping the claim
                reasons.append("direction_suspect_invertible")
            else:
                report.rejected += 1
                report.reject_reasons["illegal_type_signature"] += 1
                continue

        if confidence >= accept_at and not reasons:
            verdict = "accept"
            report.accepted += 1
        elif confidence >= review_at:
            verdict = "review"
            reasons.append("below_accept_confidence" if confidence < accept_at else "flagged")
            report.review += 1
        else:
            report.rejected += 1
            report.reject_reasons["below_review_confidence"] += 1
            continue

        facts.append(
            CompiledFact(
                subject_id=subject.entity_id,
                subject_name=subject.name,
                subject_type=subject.entity_type,
                predicate=predicate,
                object_id=obj.entity_id,
                object_name=obj.name,
                object_type=obj.entity_type,
                confidence=round(confidence, 4),
                evidence_phrase=evidence,
                chunk_id=chunk_id,
                verdict=verdict,
                reasons=tuple(reasons),
            )
        )
    return facts


def collapse_duplicates(facts: Sequence[CompiledFact]) -> list[CompiledFact]:
    """One fact per (subject_id, predicate, object_id); keep best evidence."""
    best: dict[tuple[str, str, str], CompiledFact] = {}
    for fact in facts:
        key = (fact.subject_id, fact.predicate, fact.object_id)
        current = best.get(key)
        if current is None or fact.confidence > current.confidence:
            best[key] = fact
    return sorted(best.values(), key=lambda f: (f.subject_id, f.predicate, f.object_id))


def compile_extraction(
    results: Iterable[Any], domain: str | None = None
) -> tuple[list[CompiledFact], CompileReport]:
    """Compile a batch of per-chunk encoder proposals into gated facts.

    `domain` selects config/ontology/<domain>.yaml so a corpus's own type
    signatures apply. Compiling the same input twice with the same domain
    yields byte-identical facts in the same order.
    """
    report = CompileReport()
    all_facts: list[CompiledFact] = []
    for result in results:
        entities = compile_entities(list(getattr(result, "entities", None) or []), report)
        all_facts.extend(
            compile_relations(
                list(getattr(result, "relations", None) or []),
                entities,
                str(getattr(result, "chunk_id", "")),
                report,
                domain,
            )
        )
    report.facts = collapse_duplicates(all_facts)
    return report.facts, report


def filter_results_to_compiled(
    results: Sequence[Any],
    *,
    domain: str | None = None,
    result_cls: Any,
    include_review: bool = False,
) -> tuple[list[Any], dict[str, Any]]:
    """Return per-chunk results carrying ONLY compiled, gated content.

    This is the graph-write gate. Raw proposals stay in the extraction
    ledger (ghost_b_extractions) as the record of what the model said; the
    graph receives only what survived the compiler. Nothing is rewritten —
    surviving items are the originals, filtered.

    include_review=False keeps REVIEW facts out of the graph until an
    adjudicator exists, which is the conservative reading of the acceptance
    policy: a fact nobody has adjudicated is not yet knowledge.
    """
    facts, report = compile_extraction(results, domain)
    verdicts = {"accept", "review"} if include_review else {"accept"}
    kept = [f for f in facts if f.verdict in verdicts]

    # index the surviving facts by chunk so each result keeps its own
    by_chunk: dict[str, set[tuple[str, str, str]]] = {}
    entity_names: dict[str, set[str]] = {}
    for fact in kept:
        by_chunk.setdefault(fact.chunk_id, set()).add(
            (fact.subject_name, fact.predicate, fact.object_name)
        )
        names = entity_names.setdefault(fact.chunk_id, set())
        names.add(fact.subject_name)
        names.add(fact.object_name)

    from services.extraction.canonical import canonicalize_entity_name

    out: list[Any] = []
    for result in results:
        chunk_id = str(getattr(result, "chunk_id", ""))
        legal_names = entity_names.get(chunk_id, set())
        legal_edges = by_chunk.get(chunk_id, set())
        entities = [
            e
            for e in (getattr(result, "entities", None) or [])
            if canonicalize_entity_name(str(getattr(e, "canonical_name", ""))) in legal_names
        ]
        relations = [
            r
            for r in (getattr(result, "relations", None) or [])
            if (
                canonicalize_entity_name(str(getattr(r, "subject", ""))),
                str(getattr(r, "predicate", "")),
                canonicalize_entity_name(str(getattr(r, "object", ""))),
            )
            in legal_edges
        ]
        out.append(
            result_cls(
                schema_version=getattr(result, "schema_version", ""),
                chunk_id=chunk_id,
                doc_id=str(getattr(result, "doc_id", "")),
                corpus_id=str(getattr(result, "corpus_id", "")),
                entities=entities,
                relations=relations,
                facts=[],
                text=str(getattr(result, "text", "") or ""),
            )
        )
    metrics = report.as_metrics()
    metrics["graph_written_facts"] = len(kept)
    metrics["domain"] = domain or "global"
    return out, metrics
