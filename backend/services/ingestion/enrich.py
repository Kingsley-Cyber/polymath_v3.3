"""
enrich.py — Pass-1 deterministic enrichment (no model, bit-for-bit reproducible).

Two jobs, both pure Python:
  1. FACTS — extract the deterministically-structurable FactTypes (quantity, timestamp,
     threshold, property) and attach each to the nearest in-sentence entity.
  2. ALIASES — in-text aliases via Schwartz-Hearst (acronym<->expansion) + casing variants.

Also the SHARED CUE SOURCE: `CUES` maps each of the 9 ghost_b FactType values to a regex.
Pass-1 uses the deterministic-type cues to *extract* numeric facts; `extract_qualitative_facts()`
(added for the fully-local lane) uses the five SLM_GATED cues to *also extract*, deterministically
— status / category / tag / rule_condition / rule_action — slicing each value VERBATIM from the
sentence so there is no paraphrase or hallucination. The Pass-2 SLM adapter still imports
`qualitative_cue_hits()` for its Gate B, but in the local Ghost B stack the SLM is no longer in
the path: Python structures all nine FactTypes. One regex, several consumers.

Outputs match the backend Pydantic shapes (LLMFact / LLMEntity.query_aliases) so the
caller merges them into ExtractionResult in place. No model, no network, bit-for-bit
reproducible.

Confidence note: these functions emit fact dicts WITHOUT a `confidence` key (mirroring
the original extract_facts contract). The caller stamps the sentinel by FactType —
1.0 for the four DETERMINISTIC types, 0.9 for the five SLM_GATED types — so
`extract()`'s two fact lists stay independently labellable.

API:
    CUES                            dict[FactType, re.Pattern]   (shared)
    qualitative_cue_hits(text)      -> set[str]   FactTypes flagged by cue (legacy Gate B)
    extract_facts(text, ents)       -> [LLMFact-dict...]   numeric (quantity/timestamp/threshold/property)
    extract_qualitative_facts(t, e) -> [LLMFact-dict...]   status/category/tag/rule_condition/rule_action
    extract(text, entities)         -> {"facts": [...numeric...],
                                        "qualitative_facts": [...status/category/tag/rule_*...],
                                        "aliases": {canonical: [..]}}
"""
from __future__ import annotations

import re
from typing import Iterable

# ghost_b FactType Literal (mirror). Split by what Python can STRUCTURE vs only DETECT.
DETERMINISTIC = ("quantity", "timestamp", "threshold", "property")
SLM_GATED = ("status", "category", "tag", "rule_condition", "rule_action")

_WS = re.compile(r"\s+")
_SENT = re.compile(r"(?<=[.!?])\s+")

# ---- the shared cue taxonomy, 1:1 with the 9 FactType values ----------------
CUES: dict[str, re.Pattern] = {
    "quantity":  re.compile(
        r"\b\d[\d,]*\.?\d*\s?(?:%|x|MB|GB|TB|KB|ms|sec|seconds?|min(?:ute)?s?|hours?|"
        r"days?|tokens?|param(?:eter)?s?|req(?:uests?)?/?s(?:ec)?|qps|fps|dims?|"
        r"dimensions?|layers?|heads?|GHz|MHz|cores?|bits?|bytes?)\b", re.I),
    "timestamp": re.compile(
        r"\b(?:19|20)\d{2}\b|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
        r"[a-z]*\.?\s+\d{1,2},?\s+(?:19|20)\d{2}\b|\b(?:released|launched|published|"
        r"updated|since|as of)\b", re.I),
    "threshold": re.compile(
        r"\bat least\b|\bno more than\b|\bat most\b|\bup to\b|\bminimum\b|\bmaximum\b|"
        r"\bexceeds?\b|\bgreater than\b|\bless than\b|\bcap(?:ped)?\b|\blimit(?:ed)?\b|"
        r"[<>]=?|≤|≥", re.I),
    "property":  re.compile(
        r"\b(name|version|author|license|url|path|format|language|framework|maintainer|"
        r"homepage|repository|repo|default)\s*[:=]\s*\S+", re.I),
    "status":    re.compile(
        r"\b(?:deprecated|stable|beta|alpha|experimental|production(?:-ready)?|released?|"
        r"GA|end[- ]of[- ]life|EOL|legacy|preview|maintenance|archived)\b", re.I),
    "category":  re.compile(
        r"\bis an?\b|\bis a (?:kind|type) of\b|\bcategor(?:y|ized) as\b|\bclassified as\b|"
        r"\bbelongs to\b", re.I),
    "tag":       re.compile(r"(?:^|\n)\s*(?:tags?|keywords?|labels?)\s*[:\-]", re.I),
    "rule_condition": re.compile(
        r"\b(?:if|when|unless|while|provided that|in case|whenever|as long as)\b", re.I),
    "rule_action":    re.compile(
        r"\b(?:must|shall|should|do not|don't|never|always|required to|need to|have to|"
        r"may not|prohibited)\b", re.I),
}

# number + optional unit, used to pull the concrete value for quantity/threshold/timestamp
_NUM_UNIT = re.compile(
    r"\d[\d,]*\.?\d*\s?(?:%|x|[KMGT]B|ms|sec|seconds?|min(?:ute)?s?|hours?|days?|tokens?|"
    r"param(?:eter)?s?|req/?s(?:ec)?|qps|fps|dims?|layers?|heads?|GHz|MHz|cores?|bits?)?",
    re.I)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")


def norm(s) -> str:
    return _WS.sub(" ", str(s or "")).strip()


def qualitative_cue_hits(text: str) -> set[str]:
    """FactTypes that need the SLM (Gate B). Only the types Python can't structure —
    status/category/tag/rule_condition/rule_action. Returns the set present in `text`."""
    return {ft for ft in SLM_GATED if CUES[ft].search(text or "")}


def should_enrich_facts(text: str, extracted_facts: list[dict]) -> bool:
    """Gate B for the adapter: route this chunk to the SLM if there are fact cues Pass-1
    didn't resolve — any qualitative (SLM-only) cue, OR a deterministic cue present whose
    type Pass-1 failed to extract (usually coreference, e.g. 'It needs 4 GB'). This is what
    keeps coref-blocked numeric facts from falling through the crack."""
    if qualitative_cue_hits(text):
        return True
    got = {f["fact_type"] for f in extracted_facts}
    return any(ft not in got and CUES[ft].search(text or "") for ft in DETERMINISTIC)


# ----------------------------------------------------------------- aliases
def _valid_abbr(short: str, long_form: str) -> bool:
    """Canonical Schwartz-Hearst: match short-form chars to the long form right-to-left."""
    s = [c.lower() for c in short if c.isalnum()]
    if not s:
        return False
    l = long_form.lower()
    si, li = len(s) - 1, len(l) - 1
    while si >= 0:
        while li >= 0 and l[li] != s[si]:
            li -= 1
        if li < 0:
            return False
        si -= 1
        li -= 1
    return True


_SH_LONG_PAREN = re.compile(r"([A-Za-z][\w-]*(?:\s+[\w-]+){0,5})\s*\(([A-Za-z][A-Za-z0-9\-]{1,9})\)")
_SH_PAREN_LONG = re.compile(r"\b([A-Z][A-Za-z0-9\-]{1,9})\s*\(([A-Za-z][\w\s-]{2,60}?)\)")


def schwartz_hearst(text: str) -> list[tuple[str, str]]:
    """Return (short, long_form) abbreviation pairs found in the text."""
    out = []
    for m in _SH_LONG_PAREN.finditer(text):
        long_form, short = norm(m.group(1)), norm(m.group(2))
        if _valid_abbr(short, long_form):
            out.append((short, long_form))
    for m in _SH_PAREN_LONG.finditer(text):
        short, long_form = norm(m.group(1)), norm(m.group(2))
        if _valid_abbr(short, long_form):
            out.append((short, long_form))
    return out


def _casing_variants(name: str) -> list[str]:
    n = norm(name)
    cand = {n.lower(), n.replace(" ", "_").lower(), n.replace(" ", "-").lower(),
            n.replace("-", " ").lower(), n.replace("_", " ").lower()}
    cand.discard(n.lower())
    return [c for c in cand if c]


def extract_aliases(text: str, entities: list[dict]) -> dict[str, list[str]]:
    """In-text aliases per entity: Schwartz-Hearst pairs that match the entity's surface,
    plus casing/punctuation variants. Out-of-text synonyms are the SLM's job, not here."""
    pairs = schwartz_hearst(text)
    out: dict[str, list[str]] = {}
    for e in entities:
        canon = norm(e.get("canonical_name") or e.get("surface_form") or "")
        if not canon:
            continue
        surfaces = {norm(e.get("surface_form") or canon).lower(), canon.lower()}
        aliases: list[str] = []
        for short, long_form in pairs:
            if short.lower() in surfaces or long_form.lower() in surfaces:
                other = long_form if short.lower() in surfaces else short
                if other.lower() not in surfaces:
                    aliases.append(other)
        aliases += _casing_variants(canon)
        # dedupe, cap 5 (matches LLMEntity.query_aliases max_length)
        seen, uniq = set(), []
        for a in aliases:
            k = a.lower()
            if k and k not in seen and k not in surfaces:
                seen.add(k)
                uniq.append(a)
        if uniq:
            out[canon] = uniq[:5]
    return out


# ------------------------------------------------------------------ facts
def _entity_positions(sent: str, entities: list[dict]) -> list[tuple[int, str]]:
    """(char_index, canonical_name) for each entity surface located in the sentence."""
    sl = sent.lower()
    pos = []
    for e in entities:
        canon = norm(e.get("canonical_name") or e.get("surface_form") or "")
        for nm in {norm(e.get("surface_form") or ""), canon}:
            if nm and nm.lower() in sl:
                pos.append((sl.find(nm.lower()), canon))
                break
    return pos


def _nearest_subject(idx: int, ent_pos: list[tuple[int, str]]) -> str:
    """Nearest entity to a char index; prefer the closest preceding mention."""
    if not ent_pos:
        return ""
    preceding = [(i, n) for i, n in ent_pos if i <= idx]
    pool = preceding or ent_pos
    return min(pool, key=lambda p: abs(p[0] - idx))[1]


def _property_name_before(sent: str, idx: int) -> str:
    """Heuristic property_name = the 1-3 word noun phrase just before the value."""
    pre = sent[:idx].rstrip(" :=of")
    words = re.findall(r"[A-Za-z][\w-]*", pre)[-3:]
    drop = {"the", "a", "an", "is", "are", "was", "of", "with", "has", "have", "and", "to"}
    words = [w for w in words if w.lower() not in drop]
    return " ".join(words[-2:]) if words else ""


def extract_facts(text: str, entities: list[dict]) -> list[dict]:
    """Deterministic facts (quantity/timestamp/threshold/property) attached to the nearest
    in-sentence entity. Returns LLMFact-shaped dicts; the caller validates against LLMFact."""
    facts = []
    for sent in _SENT.split(norm(text)):
        ent_pos = _entity_positions(sent, entities)
        if not ent_pos:
            continue

        def add(ft, m, value, prop="", unit="", cond=""):
            subj = _nearest_subject(m.start(), ent_pos)
            if not subj:
                return
            facts.append({"subject": subj, "fact_type": ft,
                          "property_name": prop or _property_name_before(sent, m.start()),
                          "value": value.strip(), "unit": unit, "condition": cond,
                          "evidence_phrase": sent[:500]})

        # quantity: number + unit
        for m in CUES["quantity"].finditer(sent):
            add("quantity", m, m.group(0))
        # threshold: comparator (+ following number if any)
        for m in CUES["threshold"].finditer(sent):
            tail = sent[m.end():m.end() + 24]
            num = _NUM_UNIT.search(tail)
            add("threshold", m, (m.group(0) + (" " + num.group(0) if num else "")),
                cond=m.group(0).strip())
        # timestamp: years / dates
        for m in _YEAR.finditer(sent):
            add("timestamp", m, m.group(0), prop="year")
        # property: key: value / key = value
        for m in CUES["property"].finditer(sent):
            kv = re.split(r"[:=]", m.group(0), 1)
            if len(kv) == 2:
                add("property", m, kv[1].strip(), prop=kv[0].strip().lower())
    # dedupe identical facts
    seen, uniq = set(), []
    for f in facts:
        k = (f["subject"].lower(), f["fact_type"], f["property_name"].lower(), f["value"].lower())
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq


# --------------------------------------------------- qualitative facts (A.4)
# The five SLM_GATED FactTypes, structured deterministically. The SLM used to
# own these; in the fully-local Ghost B lane Python does, by slicing the value
# VERBATIM from the sentence (faithful substring — no paraphrase, no invented
# numbers). Lower recall than an SLM, but every emitted value is grounded.

# Where a trailing noun phrase ends: first clause break, preposition, or
# conjunction. Keeps "X is a Y" category values tight ("vector database",
# not the rest of the sentence).
_PHRASE_STOP = re.compile(
    r"[.,;:!?()]|\b(?:that|which|who|whom|whose|and|but|or|nor|because|since|so|"
    r"if|when|unless|while|with|without|for|to|from|as|in|on|at|by|of)\b", re.I)

# Where a rule_condition clause ends: the comma / clause break, or the main
# clause kicking in ("then ...", "must ...").
_COND_STOP = re.compile(
    r"[,.;:]|\b(?:then|must|shall|should|will|would|do not|don't|never|always|"
    r"may not|required)\b", re.I)

# Where a rule_action clause ends: sentence-final punctuation or a trailing
# condition ("... must X if Y").
_ACT_STOP = re.compile(
    r"[.;:]|\b(?:if|when|unless|while|because|since|provided|whenever)\b", re.I)

_ARTICLES = {"a", "an", "the", "this", "that", "these", "those"}

# Single-word generic head nouns that survive "X is a ___" but carry no
# category signal ("Flame is a set of ...", "Flutter is a way to ..."). Dropped
# as category values; multi-word phrases and specific nouns are kept.
_GENERIC_CATEGORY = {
    "way", "kind", "type", "sort", "lot", "thing", "one", "bit", "piece",
    "form", "set", "number", "part", "range", "series", "group", "collection",
    "variety", "means", "tool", "system",
}

# status surface -> the maturity bucket it denotes. Falls back to the matched
# phrase itself for anything not in the map.
_STATUS_VALUE = {
    "production-ready": "production-ready", "production ready": "production-ready",
    "production": "production-ready", "ga": "ga", "stable": "stable",
    "beta": "beta", "alpha": "alpha", "experimental": "experimental",
    "preview": "preview", "deprecated": "deprecated", "legacy": "legacy",
    "eol": "end-of-life", "end-of-life": "end-of-life", "end of life": "end-of-life",
    "archived": "archived", "maintenance": "maintenance",
    "released": "released", "release": "released",
}


def _preceding_subject(idx: int, ent_pos: list[tuple[int, str]]) -> str:
    """Closest entity mention that STARTS at or before `idx`. Used for category
    ("X is a Y" — the subject is the entity to the left of the cue), where the
    nearest-overall fallback would wrongly grab the trailing noun phrase's
    entity."""
    preceding = [(i, n) for i, n in ent_pos if i <= idx]
    if not preceding:
        return ""
    return max(preceding, key=lambda p: p[0])[1]


def _trailing_phrase(sent: str, start: int, max_words: int = 4) -> str:
    """Short noun phrase beginning at char `start`, cut at the first clause
    break / preposition / conjunction and stripped of leading articles.
    Returns a verbatim substring (modulo leading-article trim)."""
    rest = sent[start:]
    m = _PHRASE_STOP.search(rest)
    span = rest[:m.start()] if m else rest
    words = span.split()
    while words and words[0].lower() in _ARTICLES:
        words = words[1:]
    return " ".join(words[:max_words]).strip(" ,.;:-")


def _clause_after(sent: str, cue_end: int, stop: re.Pattern) -> str:
    """Clause following a cue, cut at the first `stop` boundary. Verbatim."""
    rest = sent[cue_end:]
    m = stop.search(rest)
    clause = rest[:m.start()] if m else rest
    return clause.strip(" ,.;:-")


def extract_qualitative_facts(text: str, entities: list[dict]) -> list[dict]:
    """Deterministically structure the five SLM_GATED FactTypes
    (status / category / tag / rule_condition / rule_action).

    Per sentence: locate each cue, attach to the nearest in-sentence entity
    (the subject), and slice value / condition VERBATIM from the sentence.
    A fact is only emitted when there is an entity in the sentence to anchor
    it (FactItem.subject requires min_length=1), which is also the main
    precision guard against entity-free prose.

    Returns LLMFact-shaped dicts WITHOUT `confidence` (the caller stamps the
    0.9 qualitative sentinel) — same contract as extract_facts(). `tag` lines
    are scanned on the RAW text because norm() collapses the newlines the
    `tag` cue anchors to."""
    facts: list[dict] = []

    for sent in _SENT.split(norm(text)):
        sent = sent.strip()
        if not sent:
            continue
        ent_pos = _entity_positions(sent, entities)
        if not ent_pos:
            continue  # no anchor entity -> no fact (precision guard)

        def emit(ft, subj, value, prop, condition=""):
            value = norm(value)
            if not subj or not value:
                return
            facts.append({"subject": subj, "fact_type": ft, "property_name": prop,
                          "value": value[:500], "unit": "",
                          "condition": norm(condition)[:300],
                          "evidence_phrase": sent[:500]})

        # status -> maturity. Map the surface to a canonical maturity bucket.
        for m in CUES["status"].finditer(sent):
            raw = m.group(0)
            emit("status", _nearest_subject(m.start(), ent_pos),
                 _STATUS_VALUE.get(raw.lower(), raw), "maturity")

        # category -> "X is a Y": subject = entity left of the cue, value = the
        # trailing noun phrase. Skip when there's no preceding named subject
        # (usually "It is a ..." coreference, which we can't resolve).
        for m in CUES["category"].finditer(sent):
            subj = _preceding_subject(m.start(), ent_pos)
            if not subj:
                continue
            cat = _trailing_phrase(sent, m.end())
            if cat and cat.lower() not in _GENERIC_CATEGORY:
                emit("category", subj, cat, "category")

        # rule_condition + rule_action, paired within the sentence. A lone
        # condition still emits (queryable trigger); an action carries its
        # in-sentence condition when one is present.
        cond_m = CUES["rule_condition"].search(sent)
        act_m = CUES["rule_action"].search(sent)
        cond_clause = _clause_after(sent, cond_m.end(), _COND_STOP) if cond_m else ""
        if act_m:
            emit("rule_action", _nearest_subject(act_m.start(), ent_pos),
                 _clause_after(sent, act_m.end(), _ACT_STOP), "obligation",
                 condition=cond_clause)
        if cond_m and cond_clause:
            emit("rule_condition", _nearest_subject(cond_m.start(), ent_pos),
                 cond_clause, "condition")

    # tags: line-anchored, so scan RAW text. Attach to the chunk's primary
    # subject (first entity in GLiNER order) — tag/keyword lines rarely name an
    # entity inline, and first-entity is a stable, deterministic proxy.
    primary = ""
    for e in entities:
        primary = norm(e.get("canonical_name") or e.get("surface_form") or "")
        if primary:
            break
    if primary:
        for m in CUES["tag"].finditer(text or ""):
            line = (text or "")[m.end():].split("\n", 1)[0].lstrip(" :-\t")
            for tag in re.split(r"[,;]", line):
                tag = norm(tag).strip(" .")
                if tag and len(tag) <= 60:
                    facts.append({"subject": primary, "fact_type": "tag",
                                  "property_name": "tags", "value": tag[:500],
                                  "unit": "", "condition": "",
                                  "evidence_phrase": norm(line)[:500]})

    # dedupe identical facts (subject/type/property/value/condition)
    seen, uniq = set(), []
    for f in facts:
        k = (f["subject"].lower(), f["fact_type"], f["property_name"].lower(),
             f["value"].lower(), (f["condition"] or "").lower())
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq


def extract(text: str, entities: list[dict]) -> dict:
    """Local Ghost B enrichment entry point for one chunk: deterministic numeric
    facts, qualitative facts, and in-text aliases. The two fact lists stay
    separate so the caller can stamp the right confidence sentinel on each
    (1.0 numeric, 0.9 qualitative)."""
    return {"facts": extract_facts(text, entities),
            "qualitative_facts": extract_qualitative_facts(text, entities),
            "aliases": extract_aliases(text, entities)}


if __name__ == "__main__":
    demo_text = ("Qdrant is a vector database that needs at least 4 GB of RAM and "
                 "stores 1000000 vectors. "
                 "Qdrant version: 1.7, released in 2021 and is production-ready. "
                 "The HNSW (Hierarchical Navigable Small World) index is used; "
                 "if the index is full, the system must reject new inserts.\n"
                 "Tags: vector-search, ann, embeddings")
    demo_ents = [{"canonical_name": "qdrant", "surface_form": "Qdrant", "entity_type": "Software"},
                 {"canonical_name": "hnsw", "surface_form": "HNSW", "entity_type": "Method"}]
    import json
    out = extract(demo_text, demo_ents)
    print(json.dumps(out, indent=2))
    print("numeric facts     :", len(out["facts"]))
    print("qualitative facts :", len(out["qualitative_facts"]))
    print("Gate-B cue hits   :", qualitative_cue_hits(demo_text))
