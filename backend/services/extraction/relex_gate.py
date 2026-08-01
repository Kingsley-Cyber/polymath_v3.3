"""Precision gate for GLiNER-Relex output.

WHY THIS EXISTS
    MEASURED 2026-07-31 on 30 blind-gold chunks, same matcher for both:
        frame extractor, end to end        recall 0.000   0.03 rel/chunk
        frame extractor, generator ceiling recall 0.229   8.27 rel/chunk
        GLiNER-Relex, end to end           recall 0.694  17.20 rel/chunk

    The generator was the binding constraint (77% of gold never got proposed),
    so it is being replaced. The FILTER was never the problem and is not thrown
    away — every guard here already earned its place against hand-judged gold
    during gate v2, which the frame extractor passed at 0.8015 precision.

    So: new generator, same standards. GLiNER-Relex proposes; this decides.

WHAT IS REUSED VERBATIM, AND WHY EACH ONE STILL APPLIES
    judge_relation_anchor   relex still emits ~2.1% pronoun endpoints
                            ("(I) -works for-> (Al)"). Hard rules only.
    _is_structural_artifact document furniture tagged as an entity.
    _is_bibliographic_context reference lists reuse ordinary syntax as pure
                            formatting; relex reads them as prose and emits
                            "(Cornell University Press) -located in-> (Ithaca)"
                            from a citation line.
    pair_allowed            the ontology type gate. Load-bearing only because
                            the v2 entity vocabulary maps 1:1 onto
                            ontology.yaml entity_types.
    _normalize_to_schema    predicate must land in the stored schema.

WHAT IS NEW HERE, AND WHY THE FRAME MODEL NEVER NEEDED IT
    Relex emits overlapping spans by design (flat_ner=False), so one assertion
    arrives several times at different granularities:
        (Web hypertexts) -part of-> (global experiment)
        (Web hypertexts) -part of-> (global experiment in digital textuality)
        (hypertexts)     -part of-> (global experiment)
    The frame model bound one entity span per slot and could never produce
    this. Keeping all three inflates volume, triple-counts one fact in the
    graph, and makes precision look better than it is. `subsumption` keeps the
    most specific mention of each assertion and drops the rest.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from services.extraction.dep_path_extractor import pair_allowed
from services.extraction.entity_quality import judge_relation_anchor
from services.extraction.frame_extractor import (
    _is_bibliographic_context,
    _is_structural_artifact,
)
from services.extraction.spacy_relation_adapter import (
    _normalize_to_schema,
    normalize_entity_type,
)

# Natural-language relation labels handed to the model, and the internal
# predicate each maps back to. Wording matters to a zero-shot model, so the
# prompt side is plain English while storage stays snake_case.
RELATION_LABEL_TO_PREDICATE: dict[str, str] = {
    "affiliated with": "affiliated_with", "works for": "works_for",
    "created by": "created_by", "owns": "owns", "part of": "part_of",
    "located in": "located_in", "causes": "causes", "detects": "detects",
    "uses": "uses", "produces": "produces", "derived from": "derived_from",
    "is an instance of": "instance_of", "synonym of": "synonym_of",
    "includes": "includes", "supports": "supports", "implements": "implements",
    "has part": "has_part", "references": "references",
    "depends on": "depends_on", "member of": "member_of",
    "example of": "example_of", "evaluates": "evaluates",
    "deploys": "deploys", "creates": "creates", "trains": "trains",
    "runs on": "runs", "quantizes": "quantizes",
}
RELATION_LABELS: list[str] = list(RELATION_LABEL_TO_PREDICATE)

# v2 entity vocabulary. MEASURED better than v1's 25 abstract labels: 22% more
# graph-eligible entities from 21% fewer raw mentions, and it maps 1:1 onto
# ontology.yaml so pair_allowed can actually check something.
ENTITY_LABELS: list[str] = [
    "person", "organization", "location", "product", "software", "document",
    "method", "concept", "event", "standard", "artifact",
]

GATE_VERSION = "polymath.relex_gate.v1"

# --------------------------------------------------------------------------
# POST-HOC guards, added 2026-07-31 after hand-judging 150 gated relations.
# They target the three error classes that dominated that sample. Because they
# were designed BY LOOKING AT those errors, any precision they show on the same
# 150 is optimistic by construction and is labelled as such. A fresh holdout is
# required before treating the improvement as real.
# --------------------------------------------------------------------------

# 1. CONTAINMENT. One endpoint is a substring of the other, which makes the
#    assertion degenerate or tautological:
#        (public cloud) -synonym_of-> (public cloud arena)
#        (Google) -owns-> (Google DeepMind)
#        (virtual simulation) -depends_on-> (authorized virtual simulation)
#        (Lessac 1978: 176) -references-> (Lessac 1978)
#    15 of 81 errors in the judged sample were exactly this shape.
#    created_by is EXEMPT: "(Aristotle's Poetics) -created_by-> (Aristotle)"
#    contains its object and is correct — possessive authorship legitimately
#    puts the creator's name inside the work's name.
_CONTAINMENT_EXEMPT_PREDICATES = frozenset({"created_by"})

# 2. CONTRAST. The sentence explicitly says two things are NOT the same, and
#    the model asserted that they are:
#        "Don't confuse Total Cost per Unit with Variable Cost per Unit"
#            -> (Total Cost per Unit) -synonym_of-> (Variable Cost per Unit)
#        "compensatory versus non-compensatory decisions"
#            -> (compensatory) -synonym_of-> (non-compensatory decisions)
#    Only equivalence predicates are affected; contrast does not invalidate
#    "X causes Y" the way it invalidates "X is the same as Y".
_CONTRAST_MARKERS = (
    "don't confuse", "do not confuse", "rather than", "as opposed to",
    " versus ", " vs ", " vs. ", "not to be confused",
)
_EQUIVALENCE_PREDICATES = frozenset({"synonym_of", "instance_of", "example_of"})

# 3. MALFORMED ENDPOINTS. Table cells and verb phrases arriving as entities:
#        (Agent roles) -includes-> (Responsibility | Prohibited behavior Row 7:
#                                   Agent=Style agent)
#        (cost reductions) -causes-> (democratised the means of production)
#    An endpoint carrying a table separator or an assignment is not a referent.
_MALFORMED_MARKERS = ("|", "=", "Row ", "Columns:")

GATE_KEYS: tuple[str, ...] = (
    "relex_anchor_rejected",
    "relex_self_loop",
    "relex_endpoint_containment",
    "relex_contrast_negated_equivalence",
    "relex_malformed_endpoint",
    "relex_structural_artifact",
    "relex_bibliographic_context",
    "relex_predicate_unmapped",
    "relex_predicate_not_in_schema",
    "relex_disallowed_pair",
    "relex_subsumed_duplicate",
    "relex_exact_duplicate",
)


def new_gate_counters() -> dict[str, int]:
    return dict.fromkeys(GATE_KEYS, 0)


@dataclass(slots=True)
class GatedRelation:
    subject: str
    predicate: str
    object: str
    subject_type: str
    object_type: str
    score: float
    evidence: str
    chunk_id: str = ""
    doc_id: str = ""
    dropped_reason: str | None = None
    provenance: str = GATE_VERSION
    extras: dict = field(default_factory=dict)

    @property
    def kept(self) -> bool:
        return self.dropped_reason is None


def _norm(s: str) -> str:
    return " ".join((s or "").split()).strip().lower()


def _sentence_for(doc, start: int | None, end: int | None):
    """The sentence covering a character span, for bibliographic context."""
    if doc is None or start is None:
        return None
    for sent in doc.sents:
        if sent.start_char <= start and (end or start) <= sent.end_char:
            return sent
    return None


# Beyond this, a "sentence" is really unpunctuated transcript and quoting it
# from the start shows text unrelated to the relation. Evidence has to contain
# the thing it is evidence FOR — a reviewer shown the wrong span cannot judge,
# and a stored edge whose evidence omits its own assertion is unverifiable.
_MAX_EVIDENCE_CHARS = 400


def _evidence_for(text: str, sent, span: tuple[int, int] | None) -> str:
    """The sentence, or a window centred on the relation when it is huge."""
    body = sent.text.strip() if sent is not None else text[:_MAX_EVIDENCE_CHARS]
    if len(body) <= _MAX_EVIDENCE_CHARS or span is None:
        return body
    lo, hi = span
    pad = (_MAX_EVIDENCE_CHARS - min(hi - lo, 200)) // 2
    a, b = max(0, lo - pad), min(len(text), hi + pad)
    return ("…" if a else "") + text[a:b].strip() + ("…" if b < len(text) else "")


def gate_relations(
    raw: list[dict],
    *,
    text: str = "",
    doc=None,
    chunk_id: str = "",
    doc_id: str = "",
    counters: dict[str, int] | None = None,
) -> list[GatedRelation]:
    """Filter one chunk's relex output. Returns EVERY candidate, marked.

    Nothing is silently discarded: dropped candidates come back with
    `dropped_reason` set. `[r for r in gate_relations(...) if r.kept]` is the
    production result; the rest is the audit trail that makes a precision
    number checkable.
    """
    def _inc(key: str) -> None:
        if counters is not None:
            counters[key] = counters.get(key, 0) + 1

    out: list[GatedRelation] = []
    seen: set[tuple[str, str, str]] = set()
    # (predicate, subj, obj) triples that survived, for subsumption.
    survivors: list[GatedRelation] = []

    for r in raw:
        head = r.get("head") or {}
        tail = r.get("tail") or {}
        s_txt = (head.get("text") if isinstance(head, dict) else str(head)) or ""
        o_txt = (tail.get("text") if isinstance(tail, dict) else str(tail)) or ""
        s_raw_type = (head.get("type") or head.get("label") or "") if isinstance(head, dict) else ""
        o_raw_type = (tail.get("type") or tail.get("label") or "") if isinstance(tail, dict) else ""
        label = r.get("relation") or ""
        score = float(r.get("score") or 0.0)

        s_type = normalize_entity_type(s_raw_type)
        o_type = normalize_entity_type(o_raw_type)
        sent = _sentence_for(doc, head.get("start") if isinstance(head, dict) else None,
                             head.get("end") if isinstance(head, dict) else None)

        span = None
        if isinstance(head, dict) and isinstance(tail, dict) and \
                head.get("start") is not None and tail.get("start") is not None:
            span = (min(head["start"], tail["start"]),
                    max(head.get("end", head["start"]), tail.get("end", tail["start"])))
        g = GatedRelation(
            subject=s_txt.strip(), predicate="", object=o_txt.strip(),
            subject_type=s_type, object_type=o_type, score=score,
            evidence=_evidence_for(text, sent, span),
            chunk_id=chunk_id, doc_id=doc_id,
            extras={"raw_label": label, "raw_subject_type": s_raw_type,
                    "raw_object_type": o_raw_type},
        )

        def drop(reason: str) -> None:
            _inc(reason)
            g.dropped_reason = reason
            out.append(g)

        # --- 1. endpoints must be real referents (hard rules only) ----------
        if not (judge_relation_anchor(g.subject, s_raw_type).keep
                and judge_relation_anchor(g.object, o_raw_type).keep):
            drop("relex_anchor_rejected")
            continue
        # --- 2. a node cannot stand in a relation to itself ------------------
        if _norm(g.subject) == _norm(g.object):
            drop("relex_self_loop")
            continue
        # --- 3. document furniture ------------------------------------------
        if _is_structural_artifact(g.subject) or _is_structural_artifact(g.object):
            drop("relex_structural_artifact")
            continue
        # --- 3b. table cells and verb phrases are not referents --------------
        if any(m in g.subject or m in g.object for m in _MALFORMED_MARKERS):
            drop("relex_malformed_endpoint")
            continue
        # --- 4. citations are formatting, not prose --------------------------
        # at_char narrows the check to the LINE holding the relation. Without
        # it, a markdown heading merged into the following sentence condemned
        # the prose after it: MEASURED 159 of 757 relations killed that way,
        # including correct ones like (retrieval service, depends_on, Qdrant).
        h_start = head.get("start") if isinstance(head, dict) else None
        if sent is not None and _is_bibliographic_context(sent, at_char=h_start):
            drop("relex_bibliographic_context")
            continue
        # --- 5. predicate must be nameable and storable ----------------------
        pred = RELATION_LABEL_TO_PREDICATE.get(label.strip().lower())
        if pred is None:
            drop("relex_predicate_unmapped")
            continue
        schema_pred = _normalize_to_schema(pred)
        if schema_pred is None:
            drop("relex_predicate_not_in_schema")
            continue
        g.predicate = schema_pred
        # --- 5b. degenerate containment --------------------------------------
        sn, on = _norm(g.subject), _norm(g.object)
        if schema_pred not in _CONTAINMENT_EXEMPT_PREDICATES and \
                (sn in on or on in sn):
            drop("relex_endpoint_containment")
            continue
        # --- 5c. the sentence denies the equivalence it was asked to assert ---
        if schema_pred in _EQUIVALENCE_PREDICATES:
            ev_low = g.evidence.lower()
            if any(m in ev_low for m in _CONTRAST_MARKERS):
                drop("relex_contrast_negated_equivalence")
                continue
        # --- 6. ontology type gate ------------------------------------------
        if not pair_allowed(schema_pred, s_type, o_type):
            drop("relex_disallowed_pair")
            continue
        # --- 7. exact duplicate ----------------------------------------------
        key = (_norm(g.subject), schema_pred, _norm(g.object))
        if key in seen:
            drop("relex_exact_duplicate")
            continue
        seen.add(key)
        survivors.append(g)
        out.append(g)

    # --- 8. subsumption, after all survivors are known ----------------------
    # Same predicate, and each endpoint of A is a substring of B's. B is the
    # more specific mention of the same assertion, so A is redundant.
    for a in survivors:
        if a.dropped_reason:
            continue
        for b in survivors:
            if b is a or b.dropped_reason or b.predicate != a.predicate:
                continue
            sa, oa, sb, ob = (_norm(a.subject), _norm(a.object),
                              _norm(b.subject), _norm(b.object))
            if (sa, oa) == (sb, ob):
                continue
            if sa in sb and oa in ob:
                _inc("relex_subsumed_duplicate")
                a.dropped_reason = "relex_subsumed_duplicate"
                break
    return out


def kept_only(gated: list[GatedRelation]) -> list[GatedRelation]:
    return [g for g in gated if g.kept]
