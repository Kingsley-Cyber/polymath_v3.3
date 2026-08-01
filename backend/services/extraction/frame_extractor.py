"""Frame-licensed relation extraction — the rebuilt pairing model.

WHY THIS REPLACES SHORTEST-PATH PAIRING
---------------------------------------
`dep_path_extractor.DepPathExtractor.extract` iterates over EVERY entity pair in
a chunk (O(n^2)), finds the shortest dependency path between their head tokens,
and tries to name a predicate for it. Inside a single sentence the dependency
tree is connected, so a path ALWAYS exists — which means the resolver is handed
an opportunity to name a relation for pairs that have no relation at all.

Hand-judged on live corpus text (2026-07-30), that model produced ~0.25
precision even after four rounds of guard repair. The residual errors had no
dominant rule left; they were the model itself:

    (INSIDE, instance_of, Earth)              — co-present in a chapter heading
    (Figure 16-6, uses, Death Valley)         — co-present in a caption
    (November-December, derived_from, Lowell Steele) — co-present in a citation
    (place, causes, this time)                — co-present in a long sentence

None of those pairs stand in an argument relation to a shared predicate. They
were merely nearby.

THE REBUILT MODEL
-----------------
Invert the control flow. The FRAME is the primary object; entities are matched
into its slots:

    pass 1  find predicate-bearing constructions (frames) in the dep tree
    pass 2  fill each frame's subject/object slots with entity spans
    pass 3  name the predicate (reusing the existing T1-T4 resolver + ontology)

A frame emits nothing unless BOTH slots are filled by distinct entities. Pairs
that are merely co-present never form a candidate, because co-presence is not a
frame. This is a structural guarantee, not another guard stacked on top.

Consequences that fall out for free:
  - Direction errors largely vanish: the subject slot IS the grammatical
    subject, rather than "whichever entity came first in character offset".
  - The multi-clause and conjunct-crossing guards become unnecessary: a frame is
    one clause by construction. Those two were suppressing 5.43 candidates per
    chunk in the old model — candidates that now never form.
  - Cost drops from O(n^2) path searches to O(frames), and frames are sparse.

The predicate resolver, ontology gate, qualifier extraction, and suppression
counters are REUSED unchanged from dep_path_extractor. This module changes which
pairs get proposed, not how a proposed pair is named or validated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from spacy.tokens import Doc, Token

from services.extraction.dep_path_extractor import (
    EntitySpan,
    ExtractedTriple,
    _extract_qualifiers_inline,
    _inc,
    _is_adjectival_argument,
    _is_contrast_subject,
    _is_expletive_subject,
    _is_in_attribution_context,
    _is_in_conditional_clause,
    _is_light_verb_construction,
    _is_low_parse_confidence,
    _is_pronoun_argument,
    _is_verbless_sentence,
    pair_allowed,
    rejection_counters,
    resolve_predicate,
)

logger = logging.getLogger(__name__)

# Dependency labels that can hold a direct object slot.
_OBJECT_DEPS = frozenset({"dobj", "obj", "oprd"})
# Nominal predicate complements ("X is a Y").
_ATTR_DEPS = frozenset({"attr"})
# Subject slots.
_SUBJ_DEPS = frozenset({"nsubj"})
_PASSIVE_SUBJ_DEPS = frozenset({"nsubjpass", "nsubj:pass"})

# Predicates NAMED FROM THE OBJECT'S PERSPECTIVE. `created_by` asserts "the
# subject was created by the object", so an ACTIVE sentence must be inverted:
#   "The team built the framework"  -> (framework, created_by, team)
# not (team, created_by, framework). The T3 synonym table maps build/create/
# develop -> created_by and always reports swap=False, because in the old
# character-order model direction was decided elsewhere. In a frame model the
# grammatical roles are known exactly, so the inversion belongs here.
# Passive frames already carry the correct direction and are excluded.
_OBJECT_PERSPECTIVE_PREDICATES = frozenset({
    "created_by", "preceded_by", "derived_from", "trained_on",
})

# CONTAINER verbs. predicate_synonyms maps include/contain/comprise/consist ->
# part_of with no swap, so an active sentence emitted the containment backwards:
#   "Network security controls include NAC systems"
#       -> (Network security controls, part_of, NAC systems)   WRONG
#       -> (NAC systems, part_of, Network security controls)   correct
# This is lemma-specific, NOT predicate-specific: part_of must NOT invert in
# general, because "Shannon and Weaver belong to the arrogance tradition"
# already yields the right direction. Gate v2 found four of these in the paper
# stratum alone.
_CONTAINER_LEMMAS = frozenset({
    "include", "contain", "comprise", "consist", "encompass",
})

# Possessive over a PERSON / GROUP / ORGANIZATION is affiliation or kinship,
# not ownership. "Jeff's team", "Jacinda's organization", "Selena's father",
# "Virgin's Branson" all emitted `owns`, which asserts something false about
# people. affiliated_with carries no allowed_pairs constraint in ontology.yaml,
# so it is a safe landing predicate. Gate v2 found nine of these across strata.
_PERSONAL_OBJECT_TYPES = frozenset({"Person", "Organization"})

# Predicates whose meaning REQUIRES a preposition. predicate_synonyms maps
# run/runs/operate/execute -> runs_on at T3 (flat lemma lookup, no signature),
# and runs_on carries no allowed_pairs constraint, so a plain transitive object
# sailed through:
#   "let business analysts run ad hoc analytic queries"
#       -> (business analysts, runs_on, ad hoc analytic queries)   WRONG
#   "the system continuously performs ... context engineering"
#       -> (system, runs_on, context engineering)                  WRONG
# runs_on asserts "X executes ON platform Y" — that claim is only licensed when
# the sentence actually says "on". Gate v2 attributed 3 of 33 residual errors
# here. Same logic for trained_on ("trained ON data").
_PREPOSITIONAL_PREDICATES: dict[str, frozenset[str]] = {
    "runs_on": frozenset({"on", "upon", "atop"}),
    "trained_on": frozenset({"on", "upon"}),
}


@dataclass(slots=True)
class Frame:
    """One predicate-bearing construction with two argument slots."""

    subj_tok: Token
    pred_tok: Token
    obj_tok: Token
    frame_type: str
    signature: str
    # True when the grammatical subject is the SEMANTIC object (passive+agent).
    swap: bool = False
    # Structural tier, not a calibrated probability. 1.0 = the predicate token
    # directly governs both slots; 0.9 = one hop further (prepositional object).
    confidence: float = 1.0


def _conjuncts(tok: Token, *, max_depth: int = 6) -> list[Token]:
    """Coordination siblings of a slot token: A in "A, B and C" -> [B, C].

    R3. MEASURED: relation recall is 0.028, and coordination is the single
    largest identifiable cause. The frame model binds only the FIRST conjunct
    because only it carries the dobj/pobj/agent label -- the rest hang off it
    as `conj`. So "Shadow Cities uses chat, friending, and mechanics" yields one
    relation of three, and "written by Shepherd, Brown and Clark" yields one
    author of three.

    This is NOT the old dep-path conjunct guard being removed. That guard
    existed because shortest-path pairing crossed coordination into unrelated
    clauses. Here the head edge is already frame-licensed; distribution only
    copies a predicate the resolver has ALREADY named onto siblings of the same
    grammatical slot. It cannot invent a predicate.
    """
    out: list[Token] = []
    frontier = [tok]
    seen = {tok.i}
    while frontier and len(out) < max_depth:
        current = frontier.pop()
        for child in current.children:
            if child.dep_ == "conj" and child.i not in seen:
                seen.add(child.i)
                out.append(child)
                frontier.append(child)
    return out


# Coordination that does NOT distribute. "the tradeoff between A and B" asserts
# one relation about a pair, not two relations. Symmetric predicates behave the
# same way, and distributing over them manufactures a false clique.
_NON_DISTRIBUTIVE_PREPS = frozenset({"between", "among", "amongst", "across"})
_SYMMETRIC_PREDICATES = frozenset({
    "overlaps", "synonym_of", "contradicts", "related_to", "affiliated_with",
})


def _distributes(slot: Token, sibling: Token) -> bool:
    """Whether a predicate may be copied from `slot` onto `sibling`."""
    if sibling.pos_ not in ("NOUN", "PROPN"):
        return False
    # Negation lives anywhere in the sibling's SUBTREE, not just on its head.
    # In "uses HNSW but not brute force", spaCy attaches `not` to `brute`
    # (amod), so a direct-children check misses it entirely.
    if any(t.dep_ == "neg" for t in sibling.subtree):
        return False
    # Contrastive conjunctions attach to the HEAD of the coordination, not to
    # the sibling: `but` hangs off HNSW, not off force. Checking only the
    # sibling's children silently distributed across a contrast.
    contrastive = {"but", "yet", "rather", "nor"}
    for source in (sibling, slot):
        for child in source.children:
            if child.dep_ == "cc" and child.lemma_.lower() in contrastive:
                return False
    # Governed by a reciprocal preposition -> one relation about a pair.
    head = slot.head
    if head is not None and head.dep_ == "prep" and \
            head.lemma_.lower() in _NON_DISTRIBUTIVE_PREPS:
        return False
    return True


def _find_frames(doc: Doc) -> list[Frame]:
    """Enumerate predicate frames in a parsed doc.

    Each frame is anchored on a specific construction. A token that heads no
    recognized construction yields nothing — silence is the default.
    """
    frames: list[Frame] = []

    for tok in doc:
        # ---- Verbal frames -------------------------------------------------
        if tok.pos_ in ("VERB", "AUX"):
            subjects = [c for c in tok.children if c.dep_ in _SUBJ_DEPS]
            passive_subjects = [c for c in tok.children if c.dep_ in _PASSIVE_SUBJ_DEPS]
            objects = [c for c in tok.children if c.dep_ in _OBJECT_DEPS]
            attrs = [c for c in tok.children if c.dep_ in _ATTR_DEPS]

            # 1. Active transitive: "Microsoft acquired GitHub"
            for s in subjects:
                for o in objects:
                    frames.append(Frame(
                        subj_tok=s, pred_tok=tok, obj_tok=o,
                        frame_type="active_transitive",
                        signature=f"{s.dep_}-VERB-{o.dep_}",
                        confidence=1.0,
                    ))
                    # R3: distribute across object conjuncts.
                    for sib in _conjuncts(o):
                        if _distributes(o, sib):
                            frames.append(Frame(
                                subj_tok=s, pred_tok=tok, obj_tok=sib,
                                frame_type="active_transitive",
                                signature=f"{s.dep_}-VERB-{o.dep_}",
                                confidence=0.85,
                            ))
                # R3: distribute across SUBJECT conjuncts ("A and B use X").
                for ssib in _conjuncts(s):
                    if _distributes(s, ssib):
                        for o in objects:
                            frames.append(Frame(
                                subj_tok=ssib, pred_tok=tok, obj_tok=o,
                                frame_type="active_transitive",
                                signature=f"{s.dep_}-VERB-{o.dep_}",
                                confidence=0.85,
                            ))

            # 2. Copular nominal: "Qdrant is a vector database"
            for s in subjects:
                for a in attrs:
                    # Adjectival complements are properties, not relations.
                    if a.pos_ in ("ADJ", "ADV"):
                        continue
                    frames.append(Frame(
                        subj_tok=s, pred_tok=tok, obj_tok=a,
                        frame_type="copular",
                        signature=f"{s.dep_}-VERB-attr",
                        confidence=1.0,
                    ))

            # 3. Passive with explicit agent: "GitHub was acquired by Microsoft"
            #    Agentless passives form NO frame — an unstated agent is not an
            #    argument, so the construction simply does not license an edge.
            for s in passive_subjects:
                for agent in (c for c in tok.children if c.dep_ == "agent"):
                    for pobj in (g for g in agent.children if g.dep_ == "pobj"):
                        frames.append(Frame(
                            subj_tok=s, pred_tok=tok, obj_tok=pobj,
                            frame_type="passive_agent",
                            signature=f"{s.dep_}-VERB-agent-pobj",
                            swap=True,
                            confidence=1.0,
                        ))
                        # R3: "written by Shepherd, Brown and Clark" -- three
                        # authors, not one.
                        for sib in _conjuncts(pobj):
                            if _distributes(pobj, sib):
                                frames.append(Frame(
                                    subj_tok=s, pred_tok=tok, obj_tok=sib,
                                    frame_type="passive_agent",
                                    signature=f"{s.dep_}-VERB-agent-pobj",
                                    swap=True, confidence=0.85,
                                ))

            # 4. Prepositional object: "Qdrant runs on Kubernetes"
            for s in subjects:
                for prep in (c for c in tok.children if c.dep_ == "prep"):
                    for pobj in (g for g in prep.children if g.dep_ == "pobj"):
                        sig = f"{s.dep_}-VERB-prep:{prep.lemma_.lower()}-pobj"
                        frames.append(Frame(
                            subj_tok=s, pred_tok=tok, obj_tok=pobj,
                            frame_type="prep_object", signature=sig,
                            confidence=0.9,
                        ))
                        if prep.lemma_.lower() not in _NON_DISTRIBUTIVE_PREPS:
                            for sib in _conjuncts(pobj):
                                if _distributes(pobj, sib):
                                    frames.append(Frame(
                                        subj_tok=s, pred_tok=tok, obj_tok=sib,
                                        frame_type="prep_object",
                                        signature=sig, confidence=0.8,
                                    ))

        # ---- Nominal frames ------------------------------------------------
        # 5. Possessive: "Google's TensorFlow" -> (Google, owns, TensorFlow).
        #    Strict and local: the possessor is a `poss` CHILD of the possessed.
        for poss in (c for c in tok.children if c.dep_ == "poss"):
            if poss.pos_ in ("PRON", "DET"):
                continue  # "its API" — possessor is unresolvable here
            frames.append(Frame(
                subj_tok=poss, pred_tok=tok, obj_tok=tok,
                frame_type="possessive",
                signature="poss",
                confidence=1.0,
            ))

        # 6. Appositive: "Qdrant, a vector database, ..."
        for appos in (c for c in tok.children if c.dep_ == "appos"):
            frames.append(Frame(
                subj_tok=tok, pred_tok=tok, obj_tok=appos,
                frame_type="appositive",
                signature="appos",
                confidence=1.0,
            ))

    return frames


def _build_slot_index(
    doc: Doc, entities: list[EntitySpan]
) -> dict[int, EntitySpan]:
    """Map token index -> the entity span covering it.

    A slot token "is filled by" an entity when the token falls inside that
    entity's character span. Where spans overlap, the SHORTER span wins: it is
    the more specific mention, and preferring it avoids letting a long noisy
    span swallow a precise one.
    """
    index: dict[int, EntitySpan] = {}
    ordered = sorted(
        entities, key=lambda e: (e.end_char - e.start_char), reverse=True
    )
    for ent in ordered:
        for tok in doc:
            tok_start = tok.idx
            tok_end = tok.idx + len(tok.text)
            if tok_start >= ent.start_char and tok_end <= ent.end_char:
                index[tok.i] = ent  # shorter spans applied later, so they win
    return index


def _resolve_slot(
    tok: Token, slot_index: dict[int, EntitySpan], *, strict: bool = False
) -> EntitySpan | None:
    """Find the entity filling a slot.

    A slot token may be a determiner or modifier inside a longer entity mention
    ("the recommendation engine"), so a miss on the exact token is retried
    against its head — bounded to two hops to avoid drifting into a different
    constituent.

    strict=True disables the head walk entirely. Required for NOMINAL frames
    (possessive, appositive), where the slot token IS the possessed/apposed noun
    exactly. Walking up from it lands on an unrelated entity elsewhere in the
    sentence and fabricates a relation:
        "it has a halo effect on your prospects' perception"
            poss=prospects, possessed=perception (not an entity)
            -> head walk reached `halo effect` -> (prospects, owns, halo effect)
    If the possessed noun is not itself an entity, the construction simply does
    not license an edge between two entities. Silence is correct.
    """
    direct = slot_index.get(tok.i)
    if direct is not None:
        return direct
    if strict:
        return None
    current = tok
    for _ in range(2):
        if current.head.i == current.i:
            break
        current = current.head
        found = slot_index.get(current.i)
        if found is not None:
            return found
    return None


# Bibliography/citation context. Appositives are a normal prose construction
# ("Qdrant, a vector database") but in reference lists the same shape is pure
# formatting, and produced a steady stream of false edges:
#     "Newbury Park, CA: Sage (1990)"  -> (Newbury Park, instance_of, Sage)
#     "Siegrist, M., Cvetkovich, G.T.: Shared values, social trust, ..."
#         -> (Siegrist M., instance_of, Shared values)
# These pass allowed_pairs legitimately, so the ontology cannot catch them —
# the sentence itself has to be recognized as a citation.
_CITATION_MARKERS = (
    "et al.", " ed.", " eds.", " pp.", " vol.", " no.", "doi:", "isbn",
    "press,", "press:", "journal", "reprinted", "trans.",
)
_INITIALS_RE = None  # compiled lazily to keep import cost off the hot path


def _is_bibliographic_context(sent) -> bool:
    """True when the sentence is NOT running prose.

    Covers three non-prose shapes that all reuse ordinary syntax as pure
    formatting, so the ontology cannot reject what they produce:

      bibliography   "Newbury Park, CA: Sage (1990)"
                     "Csikszentmihalyi M, Hunter J. Happiness in everyday life"
      captions       "Figure 5.1: Vertical scaling Versus Horizontal scaling"
      page furniture "## Page 300 schemaless and flexible ..."
                     "page:99 source:text --> The Depth of Complexity"

    Gate v2 attributed 7 of 37 residual errors to these. Ordinary definitional
    appositives ("Qdrant, a vector database") are unaffected, which is tested.
    """
    global _INITIALS_RE
    if _INITIALS_RE is None:
        import re
        # A personal-name initial ending a name field. Several shapes occur:
        #   "Siegrist, M., Cvetkovich, G.T.: ..."   period + comma
        #   "Gandy, O.: The Panoptic Sort"          period + colon
        #   "Csikszentmihalyi M, Hunter J. Happiness ..."  BARE initial + comma
        #   "Brams S.J. and A.D. Taylor (1996)"     initials mid-sentence
        _INITIALS_RE = re.compile(
            r"\b[A-Z]\.(?:\s*[A-Z]\.)*\s*[,:]"      # "M.," / "G.T.:"
            r"|\b[A-Z][a-z]{2,},\s+[A-Z]\b"          # "Csikszentmihalyi M,"
            r"|\b[A-Z]\.[A-Z]\.\s"                   # "S.J. " / "A.D. "
        )
    text = sent.text
    low = text.lower()

    # --- document furniture: markdown headings, page/source markers ---------
    stripped = text.lstrip()
    if stripped.startswith("#"):
        return True
    # Numbered reference-list entries: "[46 ] Serge Abiteboul, ... : Foundations
    # of Databases", "[7 ] Theo Harder and Andreas Reuter: ...". These carry no
    # name initials and no publisher markers, so nothing else catches them.
    import re as _re_ref
    if _re_ref.match(r"^\s*\[\s*\d+\s*\]", stripped):
        return True
    if "page:" in low or "source:" in low or "-->" in text:
        return True

    import re as _re
    # "## Page 300 ...", "Page 135 contains ..." at the head of the sentence.
    if _re.match(r"^\s*#*\s*page\s+\d+", low):
        return True
    # Captions: "Figure 5.1: ...", "Table 3: ...", "Figure 16-3 Management ..."
    if _re.match(r"^\s*(figure|table|exhibit)\s+[\d.\-]+\s*[:.]", low):
        return True

    # --- bibliography -------------------------------------------------------
    if any(m in low for m in _CITATION_MARKERS):
        return True
    if _INITIALS_RE.search(text):
        return True
    # Page locators: "p 125", "p. 125", "pp. 12-34" — reference formatting.
    if _re.search(r"\bpp?\.?\s+\d+", low):
        return True
    # "(Chicago: University of Chicago Press, 1984)" / "(1990)" plus a colon —
    # a year in parentheses alongside a publisher colon is citation formatting.
    if _re.search(r"\(\d{4}\)", text) and ":" in text:
        return True
    return False


# Surfaces that are document furniture rather than things. These reach the
# relation lane because the upstream tagger emits them as entities; the lane
# then faithfully relates garbage it was handed. Filtering here is a stopgap at
# the relation boundary — the real fix belongs in the entity tagger.
_ARTIFACT_SURFACES = frozenset({
    "text", "page", "source", "figure", "table", "chapter", "section",
    "note", "notes", "appendix", "index", "contents", "abstract",
})


def _is_structural_artifact(surface: str) -> bool:
    """True when an entity surface is page furniture, not a referent."""
    s = (surface or "").strip()
    if not s:
        return True
    low = s.lower().strip(" .:-—")
    if low in _ARTIFACT_SURFACES:
        return True
    # "Page 135", "Page 300" — a locator, not an entity. NOTE: "Figure 5.18" is
    # deliberately NOT filtered; "Figure 5.18 shows a spillmap" is a correct
    # references edge. Captions are handled by context above instead.
    import re as _re
    if _re.match(r"^page\s+[\d.\-]+$", low):
        return True
    # Speaker labels and OCR noise: "M M", "A B", single characters.
    if len(s) <= 1:
        return True
    if _re.match(r"^(?:[A-Z]\s+){1,}[A-Z]$", s):
        return True
    return False


class FrameExtractor:
    """Frame-licensed relation extractor.

    Drop-in for DepPathExtractor.extract(): same signature, same
    ExtractedTriple output, same counters.
    """

    def __init__(self, model_name: str = "en_core_web_sm"):
        try:
            from services.extraction.appos_enrichment import get_shared_nlp
            self._nlp = get_shared_nlp()
        except Exception:  # noqa: BLE001 - fall back to a direct load
            import spacy
            self._nlp = spacy.load(model_name, disable=["ner", "textcat"])

    def extract(
        self,
        text: str,
        entities: list[EntitySpan],
        *,
        section_path: str = "",
        chunk_id: str = "",
        doc_id: str = "",
        doc: Doc | None = None,
        suppression_counters: dict[str, int] | None = None,
        trace: list[dict] | None = None,
    ) -> list[ExtractedTriple]:
        """Extract frame-licensed relations.

        `trace`, when given, receives one record PER CANDIDATE FRAME describing
        exactly where it died (or that it survived). The aggregate counters say
        HOW MANY died at each guard; they cannot say WHICH candidate died where,
        so they cannot answer "did my correct answer get filtered out, and by
        what?". Recall debugging needs the per-candidate view, and reconstructing
        it outside this method means reimplementing the guard order — which is
        how you end up measuring a copy of the pipeline instead of the pipeline.
        Off by default; costs one list append per frame when on.
        """
        if not text.strip() or len(entities) < 2:
            return []

        if doc is None:
            doc = self._nlp(text)

        # ---- Sentence-level suppression (unchanged from the old model) -----
        if _is_low_parse_confidence(doc):
            _inc(suppression_counters, "skipped_low_parse_confidence")
            if trace is not None:
                # Chunk-level bail. Without a record here the candidates this
                # discards vanish with no trace row at all, and an audit that
                # sums trace rows silently under-counts the loss.
                for f in _find_frames(doc):
                    trace.append({
                        "subject_token": f.subj_tok.text,
                        "predicate_token": f.pred_tok.text,
                        "object_token": f.obj_tok.text,
                        "subject_entity": None, "object_entity": None,
                        "frame_type": f.frame_type, "signature": f.signature,
                        "died_at": "skipped_low_parse_confidence",
                    })
            return []

        # NOTE: no chunk-level verbless bail here. The old model returned early
        # when every sentence looked verbless, which silently discarded valid
        # NOMINAL frames — and "verbless" is unreliable precisely because sm
        # mistags domain verbs as nouns ("Google's TensorFlow powers many
        # systems" reads as verbless because `powers` tags NOUN). Verbal frames
        # are still checked individually below; nominal frames no longer need a
        # verb to exist.
        frames = _find_frames(doc)
        if not frames:
            _inc(suppression_counters, "skipped_verbless")
            return []

        slot_index = _build_slot_index(doc, entities)
        sent_idx_by_start = {s.start: i for i, s in enumerate(doc.sents)}

        triples: list[ExtractedTriple] = []
        seen: set[tuple[str, str, str, int]] = set()

        for frame in frames:
            # Filled in once the slots resolve, so a death record can name the
            # ENTITY that died and not just the head token. Fresh per frame.
            slots: list[str | None] = [None, None]

            def _die(reason: str, _f: Frame = frame, _s: list = slots) -> None:
                """Record a suppression AND which candidate it killed."""
                _inc(suppression_counters, reason)
                if trace is not None:
                    trace.append({
                        "subject_token": _f.subj_tok.text,
                        "predicate_token": _f.pred_tok.text,
                        "object_token": _f.obj_tok.text,
                        "subject_entity": _s[0],
                        "object_entity": _s[1],
                        "frame_type": _f.frame_type,
                        "signature": _f.signature,
                        "died_at": reason,
                    })

            # ---- pass 2: slot filling. Both slots or nothing. --------------
            is_nominal_frame = frame.frame_type in ("possessive", "appositive")

            # Non-prose context (bibliography, caption, page furniture) reuses
            # ordinary syntax as pure formatting, and the resulting type pairs
            # are legitimate — so allowed_pairs cannot reject them and the
            # context has to be recognized directly. Applies to BOTH nominal
            # shapes that yield instance_of: appositives AND copulas.
            #   "Figure 5.1: Vertical scaling Versus Horizontal scaling"
            #   "Csikszentmihalyi M, Hunter J. Happiness in everyday life"
            if frame.frame_type in ("appositive", "copular") and \
                    _is_bibliographic_context(frame.pred_tok.sent):
                _die("frame_bibliographic_appositive")
                continue

            # Nominal frames resolve strictly: the slot token IS the possessed
            # or apposed noun, so a head walk would land on an unrelated entity.
            subj_ent = _resolve_slot(
                frame.subj_tok, slot_index, strict=is_nominal_frame)
            obj_ent = _resolve_slot(
                frame.obj_tok, slot_index, strict=is_nominal_frame)
            if subj_ent is None or obj_ent is None:
                slots[:] = [subj_ent.surface if subj_ent else None,
                            obj_ent.surface if obj_ent else None]
                _die("frame_slot_unfilled")
                continue
            slots[:] = [subj_ent.surface, obj_ent.surface]
            if (subj_ent.start_char == obj_ent.start_char
                    and subj_ent.end_char == obj_ent.end_char):
                _die("frame_self_loop")
                continue
            # Surface-level self-loop: distinct spans, same string.
            # "Actions: Actions define the specific task" -> (Actions, defines,
            # Actions). A node cannot stand in a relation to itself here.
            if subj_ent.surface.strip().lower() == obj_ent.surface.strip().lower():
                _die("frame_self_loop")
                continue
            # Document furniture tagged as an entity by the upstream tagger.
            if (_is_structural_artifact(subj_ent.surface)
                    or _is_structural_artifact(obj_ent.surface)):
                _die("frame_structural_artifact")
                continue

            # ---- argument hygiene (P1, reused) -----------------------------
            if (_is_pronoun_argument(frame.subj_tok, subj_ent)
                    or _is_pronoun_argument(frame.obj_tok, obj_ent)):
                _die("suppressed_pronoun_argument")
                continue
            if (_is_adjectival_argument(doc, subj_ent, frame.subj_tok)
                    or _is_adjectival_argument(doc, obj_ent, frame.obj_tok)):
                _die("suppressed_adjectival_argument")
                continue
            if _is_expletive_subject(frame.subj_tok):
                _die("suppressed_expletive")
                continue
            if _is_contrast_subject(frame.subj_tok, doc):
                _die("suppressed_contrast")
                continue

            pred_tok = frame.pred_tok
            # Nominal frames (possessive, appositive) carry no verb by nature —
            # "Google's TensorFlow", "Qdrant, a vector database". Requiring a
            # verbal sentence would discard them wholesale, and does: sm tags
            # "powers" in "Google's TensorFlow powers many systems" as NOUN,
            # making the whole sentence read as verbless (backlog PF-2).
            if not is_nominal_frame and _is_verbless_sentence(pred_tok.sent):
                _die("skipped_verbless")
                continue
            if _is_light_verb_construction(pred_tok):
                _die("suppressed_light_verb")
                continue
            if "except" in frame.signature or "pcomp" in frame.signature:
                _die("suppressed_exception_boundary")
                continue

            is_attributed = _is_in_attribution_context(pred_tok)
            is_conditional = _is_in_conditional_clause(pred_tok)
            if is_attributed:
                _inc(suppression_counters, "qualified_attributed")
            if is_conditional:
                _inc(suppression_counters, "qualified_conditional")

            # ---- pass 3: name the predicate (resolver reused unchanged) ----
            if frame.frame_type == "possessive":
                # "Jeff's team" / "Selena's father" is affiliation or kinship,
                # never ownership. Asserting `owns` over a person is false.
                if obj_ent.entity_type in _PERSONAL_OBJECT_TYPES:
                    resolved: tuple[str, bool] | None = ("affiliated_with", False)
                else:
                    resolved = ("owns", False)
                lemma = "own"
            elif frame.frame_type == "appositive":
                resolved = ("instance_of", False)
                lemma = "be"
            else:
                lemma = pred_tok.lemma_.lower()
                resolved = resolve_predicate(
                    signature=frame.signature,
                    lemma=lemma,
                    subject_type=subj_ent.entity_type,
                    object_type=obj_ent.entity_type,
                    pred_tok=pred_tok,
                    object_tok=frame.obj_tok,
                )
            if resolved is None:
                _die("frame_predicate_unnamed")
                continue

            predicate, resolver_swap = resolved

            # R3 guard: never distribute a SYMMETRIC predicate. Copying
            # "overlaps" across conjuncts manufactures a false clique.
            if frame.confidence < 1.0 and predicate in _SYMMETRIC_PREDICATES:
                _die("frame_symmetric_no_distribute")
                continue

            # A prepositional predicate needs its preposition actually present.
            required_preps = _PREPOSITIONAL_PREDICATES.get(predicate)
            if required_preps is not None:
                sig_prep = None
                if frame.frame_type == "prep_object" and ":" in frame.signature:
                    sig_prep = frame.signature.split("prep:", 1)[-1].split("-")[0]
                if sig_prep not in required_preps:
                    _die("frame_missing_required_preposition")
                    continue
            # Direction has exactly ONE owner per frame type, never two.
            # For passive_agent the frame already knows the grammatical subject
            # is the semantic object. The resolver, handed the same passive
            # signature, independently reports the same swap — XOR-ing them
            # cancelled the correction and emitted
            # (GitHub, owns, Microsoft) for "GitHub was acquired by Microsoft".
            # The frame is structural and always right here, so it wins.
            if frame.frame_type == "passive_agent":
                swap = frame.swap
            else:
                swap = bool(resolver_swap)
                # Object-perspective predicate reached from an active frame:
                # "The team built the framework" -> (framework, created_by, team)
                if (frame.frame_type in ("active_transitive", "prep_object")
                        and predicate in _OBJECT_PERSPECTIVE_PREDICATES):
                    swap = not swap
                # Container verb: "X includes Y" asserts Y is part_of X.
                elif (frame.frame_type == "active_transitive"
                        and predicate == "part_of"
                        and lemma in _CONTAINER_LEMMAS):
                    swap = not swap
            if swap:
                s_ent, o_ent = obj_ent, subj_ent
            else:
                s_ent, o_ent = subj_ent, obj_ent

            if not pair_allowed(predicate, s_ent.entity_type, o_ent.entity_type):
                reason = (
                    f"disallowed_pair:{predicate}:"
                    f"{s_ent.entity_type}:{o_ent.entity_type}"
                )
                rejection_counters[reason] = rejection_counters.get(reason, 0) + 1
                _die("adapter_allowed_pairs_rejected")
                continue

            polarity, modality, temporal = _extract_qualifiers_inline(pred_tok)
            if polarity == "NEGATIVE":
                _inc(suppression_counters, "qualified_negated")
            if modality != "ASSERTED":
                _inc(suppression_counters, "qualified_modal")

            if is_attributed:
                assertion_mode = "attributed"
            elif is_conditional:
                assertion_mode = "conditional"
            else:
                assertion_mode = "direct"

            sent = pred_tok.sent
            sent_idx = sent_idx_by_start.get(sent.start, 0)

            key = (s_ent.surface, predicate, o_ent.surface, sent_idx)
            if key in seen:
                # Not a suppression — the same relation reached here twice (e.g.
                # via conjunct distribution). Traced anyway so a recall audit
                # never mistakes a dedupe for a filter kill.
                if trace is not None:
                    trace.append({
                        "subject_token": frame.subj_tok.text,
                        "predicate_token": frame.pred_tok.text,
                        "object_token": frame.obj_tok.text,
                        "frame_type": frame.frame_type,
                        "subject_entity": s_ent.surface,
                        "object_entity": o_ent.surface,
                        "signature": frame.signature,
                        "died_at": "duplicate_of_earlier_frame",
                    })
                continue
            seen.add(key)

            if trace is not None:
                trace.append({
                    "subject_token": frame.subj_tok.text,
                    "predicate_token": frame.pred_tok.text,
                    "object_token": frame.obj_tok.text,
                    "frame_type": frame.frame_type,
                    "subject_entity": s_ent.surface,
                    "object_entity": o_ent.surface,
                    "signature": frame.signature,
                    "died_at": None,
                    "emitted": f"({s_ent.surface}) -{predicate}-> ({o_ent.surface})",
                })

            triples.append(ExtractedTriple(
                subject_surface=s_ent.surface,
                subject_start=s_ent.start_char,
                subject_end=s_ent.end_char,
                predicate=predicate,
                predicate_lemma=lemma,
                predicate_surface=pred_tok.text,
                object_surface=o_ent.surface,
                object_start=o_ent.start_char,
                object_end=o_ent.end_char,
                confidence=frame.confidence,
                dep_signature=f"{frame.frame_type}:{frame.signature}",
                polarity=polarity,
                modality=modality,
                assertion_mode=assertion_mode,
                temporal_cue=temporal,
                sentence_text=sent.text.strip(),
                sentence_idx=sent_idx,
                chunk_id=chunk_id,
                doc_id=doc_id,
                section_path=section_path,
            ))

        return triples
