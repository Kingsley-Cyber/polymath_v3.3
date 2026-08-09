"""Verb-centric SVO candidate generation — textacy's algorithm, natively.

WHY THIS EXISTS
    MEASURED against blind gold (30 chunks, 36 gold relations):
        my hand-rolled frame finder : 0.07 triples/chunk, recall 0.028
        textacy subject_verb_object : 3.77 triples/chunk, recall 0.083
    A maintained library beat 769 lines of my code by 3x on recall, in one
    function call. The generator was the liability; the typing/ontology/gating
    layer downstream is the asset and is kept.

WHY NOT JUST DEPEND ON textacy
    textacy pulls 13 transitive dependencies (scipy, scikit-learn, networkx,
    floret, ...). This lane runs under a determinism contract that pins
    EXPECTED_DISTRIBUTIONS and hashes a source closure; adding those would force
    a new pod digest and widen the attack surface of a credential-free worker
    for one function. So the ALGORITHM is adopted, not the package. Everything
    here uses spaCy APIs already present in the pinned stack.

WHAT MY ORIGINAL GENERATOR GOT WRONG
    1. Hand-rolled conjunct traversal. spaCy exposes `tok.conjuncts` as a
       built-in property. I wrote a BFS frontier for it in R3.
    2. Pair-at-a-time emission. I emitted one frame per structurally-found
       (subject, object). The correct shape is verb-centric: collect the SET of
       subjects and the SET of objects per verb, then cross them. That single
       difference explains most of the 50x volume gap.
    3. No verb-conjunct propagation. "X uses A and builds B" -- the second verb
       inherits the subject. I dropped those entirely.
    4. No xcomp, no csubj, no compound expansion.

WHAT IS DELIBERATELY NOT ADOPTED
    textacy yields raw token spans with no types, no ontology, no evidence and
    no qualifiers, and emits things like "It made sense". Its output is a
    CANDIDATE STREAM, not relations. It is filtered downstream exactly as the
    frame generator's output was.
"""

from __future__ import annotations

from dataclasses import dataclass

from spacy.tokens import Doc, Span, Token

# Dependency labels, as strings so this needs no spacy.symbols import.
_NOMINAL_SUBJ = frozenset({"nsubj", "nsubjpass"})
_CLAUSAL_SUBJ = frozenset({"csubj", "csubjpass"})
_OBJ = frozenset({"dobj", "obj"})


@dataclass(slots=True)
class SVOCandidate:
    """One subject-verb-object candidate. Not yet a relation."""

    subject: Token          # head token of the subject span
    verb: Token
    object: Token           # head token of the object span
    passive: bool = False   # subject is the semantic object
    via_verb_conjunct: bool = False   # subject inherited from a conjugate verb
    source: str = "svo"


def _expand_noun(tok: Token) -> list[Token]:
    """A noun plus its conjuncts. textacy also folds in compound children.

    We keep the conjuncts (that is the recall win) but NOT the compounds:
    compounds belong to the same mention, and emitting them as separate
    candidate heads would produce ("vector", ..., x) alongside
    ("vector database", ..., x). Entity spans already carry the full mention.
    """
    return [tok] + list(tok.conjuncts)


def _is_agent_pobj(tok: Token) -> bool:
    """pobj under an `agent` -- the doer in "was written by X"."""
    return (
        tok.dep_ == "pobj"
        and tok.head.dep_ == "agent"
        and tok.head.head.pos_ in ("VERB", "AUX")
    )


def svo_candidates(doclike: Doc | Span) -> list[SVOCandidate]:
    """Verb-centric SVO extraction.

    Collect subjects and objects PER VERB, propagate across verb conjuncts,
    then cross-product. Deterministic: output order is fixed by token index.
    """
    sents = [doclike] if isinstance(doclike, Span) else list(doclike.sents)
    out: list[SVOCandidate] = []

    for sent in sents:
        subjects: dict[int, set[int]] = {}
        objects: dict[int, set[int]] = {}
        passive_verbs: set[int] = set()
        doc = sent.doc

        def add(bucket: dict[int, set[int]], verb: Token, tok: Token) -> None:
            bucket.setdefault(verb.i, set()).update(t.i for t in _expand_noun(tok))

        for tok in sent:
            head = tok.head
            if tok.pos_ in ("VERB", "AUX"):
                subjects.setdefault(tok.i, set())
                objects.setdefault(tok.i, set())

            if tok.dep_ in _NOMINAL_SUBJ and head.pos_ in ("VERB", "AUX"):
                add(subjects, head, tok)
                if tok.dep_ == "nsubjpass":
                    passive_verbs.add(head.i)
            elif tok.dep_ in _CLAUSAL_SUBJ and head.pos_ in ("VERB", "AUX"):
                add(subjects, head, tok)
            elif tok.dep_ in _OBJ and head.pos_ in ("VERB", "AUX"):
                add(objects, head, tok)
            elif _is_agent_pobj(tok):
                add(objects, tok.head.head, tok)
            elif tok.dep_ == "xcomp" and head.pos_ in ("VERB", "AUX"):
                # Open clausal complement as object, but only when the verb has
                # no direct object -- otherwise it duplicates the real one.
                if not any(c.dep_ in _OBJ for c in head.children):
                    add(objects, head, tok)

        # Verb-conjunct propagation: "X uses A and builds B" -- `builds` has no
        # subject of its own and inherits X. My original generator dropped every
        # one of these.
        inherited: set[int] = set()
        for vi in list(subjects):
            if not subjects[vi]:
                continue
            for conj in doc[vi].conjuncts:
                if conj.pos_ in ("VERB", "AUX") and not subjects.get(conj.i):
                    subjects.setdefault(conj.i, set()).update(subjects[vi])
                    inherited.add(conj.i)

        for vi in sorted(set(subjects) | set(objects)):
            subs = sorted(subjects.get(vi, ()))
            objs = sorted(objects.get(vi, ()))
            if not subs or not objs:
                continue
            for si in subs:
                for oi in objs:
                    if si == oi:
                        continue
                    out.append(SVOCandidate(
                        subject=doc[si], verb=doc[vi], object=doc[oi],
                        passive=vi in passive_verbs,
                        via_verb_conjunct=vi in inherited,
                    ))
    return out
