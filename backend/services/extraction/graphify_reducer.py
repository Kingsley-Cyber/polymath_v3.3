"""Document-local entity clustering and conservative quality adjudication."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Iterable, Sequence

from models.graphify_contracts import (
    DocumentEntityV1,
    EntityTerminalState,
    MentionTerminalState,
    NormalizedDocumentV1,
    RawMentionV1,
    stable_digest,
    stable_id,
)
from services.extraction.canonical import name_core_span, singularize_token
from services.extraction.graphify_survey import DocumentSurveyV1, GazetteerCandidateV1

REDUCER_RELEASE = "graphify-document-entity-reducer-v2"

_AMBIGUOUS_NAMES = frozenset({"go", "make", "apple", "python", "oracle", "rust"})
_GENERIC_SURFACES = frozenset({
    "it", "they", "this", "that", "these", "those", "we", "he", "she",
    "system", "component", "process", "method", "approach", "owner", "successor",
    "version", "directive", "change", "information", "goal", "end goal", "thing",
    "result", "data", "software", "service", "artifact", "document", "event",
    "the", "a", "an",
})
_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+", re.I)
_SPACE_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True)
class MentionReductionAssignment:
    mention_id: str
    entity_id: str
    state: EntityTerminalState
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "mention_id": self.mention_id,
            "entity_id": self.entity_id,
            "state": self.state.value,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class ReducerOutput:
    entities: tuple[DocumentEntityV1, ...]
    assignments: tuple[MentionReductionAssignment, ...]
    report: dict[str, object]


@dataclass
class _Cluster:
    document_id: str
    canonical_name: str
    aliases: set[str]
    mentions: list[RawMentionV1]
    survey_sources: set[str]
    survey_count: int
    definitions: list[str]


def _normalized_surface(value: str) -> str:
    return _SPACE_RE.sub(" ", value.strip()).casefold()


def _canonical_surface(value: str) -> str:
    return _LEADING_ARTICLE_RE.sub("", _SPACE_RE.sub(" ", value.strip())).strip(" .,:;()")


# Mirrors graphify_relations._ABBREVIATION_TOKENS (kept local to avoid an
# import cycle between the entity and relation lanes).
_ABBREVIATION_TOKENS = frozenset({
    "dr", "mr", "mrs", "ms", "prof", "sr", "jr", "st", "vs", "etc", "cf", "al",
    "inc", "ltd", "corp", "co", "dept", "fig", "eq", "no", "approx", "est",
})
_CROSS_SENTENCE_RE = re.compile(r"[.!?]+[\"')\]]*\s+")


def _trim_cross_sentence_surface(surface: str) -> str:
    """Model spans occasionally glue the tail of one sentence to the head of
    the next ("Falcon Cache. QRL"). Cluster identity uses only the text before
    an internal sentence boundary; the raw mention record itself is preserved
    unchanged. Abbreviation periods and single-letter initials do not count as
    boundaries.
    """
    match = _CROSS_SENTENCE_RE.search(surface)
    if not match:
        return surface
    head = surface[: match.start()]
    token = re.search(r"[A-Za-z][A-Za-z.]*$", head)
    if token:
        word = token.group(0).casefold()
        last_segment = word.rsplit(".", 1)[-1]
        if word in _ABBREVIATION_TOKENS or (len(last_segment) == 1 and last_segment.isalpha()):
            return surface
    return head.strip() or surface


def _sentence_context(text: str, start: int, end: int) -> str:
    left = max(text.rfind(".", 0, start), text.rfind("\n", 0, start)) + 1
    right_candidates = [value for value in (text.find(".", end), text.find("\n", end)) if value >= 0]
    right = min(right_candidates) + 1 if right_candidates else len(text)
    return text[left:right]


def _ambiguous_use_is_named(document: NormalizedDocumentV1, mention: RawMentionV1) -> bool:
    context = _sentence_context(document.normalized_text, mention.normalized_start or 0, mention.normalized_end or 0).casefold()
    named_cues = (
        "named", "programming language", "software", "company", "organization",
        "database", "platform", "service", "library", "framework", "supports",
        "implements", "owns", "uses", "depends on",
    )
    return mention.surface[:1].isupper() and any(cue in context for cue in named_cues)


def _specific_name_shape(surface: str) -> bool:
    words = _WORD_RE.findall(surface)
    if not words:
        return False
    if any(char.isdigit() or char in "_+#." for char in surface):
        return True
    if surface.isupper() and len(surface) > 1:
        return True
    return any(word[:1].isupper() for word in words) or any(
        any(char.isupper() for char in word[1:]) for word in words
    )


def _type_from_surface(name: str, definition: str = "") -> str | None:
    value = name.casefold().strip()
    details = definition.casefold()
    if value.endswith("objective"):
        return "concept"
    if value.endswith(("compiler", "controller", "reducer", "assembler", "planner")):
        return "method"
    if value.endswith(("matcher", "extractor", "adapter", "builder")):
        return "software"
    if re.search(r"\b(dataset|records|batches|index|projections)\b", value):
        return "artifact"
    if re.search(r"\b(report|guide|ledger|paper|book|specification|markdown|json)\b", value):
        return "document"
    if value == "document":
        return "document"
    if re.search(r"\b(labs?|organizations?|companies|agencies|publishers)\b", value):
        return "organization"
    if re.search(r"\b(builder|service|api|gateway|platform|cli)\b", value):
        return "software"
    if value in {"go", "make", "python", "rust"}:
        return "software"
    if value == "apple":
        return "organization"
    if re.search(r"\b(objective|latency|milliseconds?|metrics?|concept|retrieval|search|contract|loss|recoverability|rebuildability)\b", value) or value.endswith("ability"):
        return "concept"
    if re.search(r"\bevidence\b", value):
        return "artifact"
    if re.search(r"\bworker\b", value) and name[:1].isupper():
        return "software"
    if re.search(r"\b(migration|event|incident|launch)\b", value) or re.search(
        rf"\bevent\b[^.\n]*\b(?:began|occurred|started)\b[^.\n]*\b{re.escape(value)}\b", details,
    ):
        return "event"
    if re.search(r"\b(city|country|region|location)\b", value):
        return "location"
    if re.search(r"\b(process|workflow|coordination|validation|parsing|planner|writer|worker|implementation|queries|analysis|prototype)\b", value):
        return "method"
    if re.search(r"\b(software|database|library|framework|programming language|tool)\b", value):
        return "software"
    if re.search(r"\b(software|database|library|framework|programming language|service|product|tool)\b", details):
        return "software"
    if re.search(r"\b(dataset|records|artifact)\b", details):
        return "artifact"
    if re.search(r"\b(concept|objective|metric|ability)\b", details):
        return "concept"
    return None


def _choose_type(cluster: _Cluster, document: NormalizedDocumentV1) -> tuple[str, bool]:
    contexts = " ".join(
        _sentence_context(
            document.normalized_text, mention.normalized_start or 0, mention.normalized_end or 0,
        )
        for mention in cluster.mentions
    )
    details = " ".join([*cluster.definitions, contexts])
    explicit = _type_from_surface(cluster.canonical_name, details)
    escaped = re.escape(cluster.canonical_name)
    if re.search(rf"\bdefines\s+{escaped}\s+as\s+(?:an?\s+)?[^.\n]*\b(?:workflow|process|method)\b", document.normalized_text, re.I):
        explicit = "method"
    elif explicit is None and re.search(rf"\b{escaped}\s+defines\b", document.normalized_text, re.I) and "person" not in details.casefold():
        explicit = "software"
    weighted: Counter[str] = Counter()
    for mention in cluster.mentions:
        weighted[mention.entity_type] += max(1, round(mention.confidence * 100))
    conflict = len(weighted) > 1
    if explicit is not None:
        return explicit, conflict
    if weighted:
        return sorted(weighted, key=lambda value: (-weighted[value], value))[0], conflict
    return "concept", False


def _candidate_by_surface(survey: DocumentSurveyV1) -> dict[str, GazetteerCandidateV1]:
    output = {}
    for candidate in survey.gazetteer_candidates:
        output[_normalized_surface(_canonical_surface(candidate.surface))] = candidate
    return output


def _build_clusters(
    document: NormalizedDocumentV1,
    mentions: Sequence[RawMentionV1],
    survey: DocumentSurveyV1,
) -> dict[str, _Cluster]:
    alias_to_canonical: dict[str, str] = {}
    canonical_display: dict[str, str] = {}
    for alias in survey.aliases:
        canonical = _canonical_surface(alias.canonical)
        canonical_key = _normalized_surface(canonical)
        canonical_display[canonical_key] = canonical
        alias_to_canonical[_normalized_surface(alias.alias)] = canonical_key
        alias_to_canonical[canonical_key] = canonical_key
    definitions_by_key: dict[str, list[str]] = defaultdict(list)
    for definition in survey.definitions:
        term = _canonical_surface(definition.term)
        normalized_term = _normalized_surface(term)
        key = (
            f"ambiguous:{term}" if normalized_term in _AMBIGUOUS_NAMES
            else alias_to_canonical.get(normalized_term, normalized_term)
        )
        canonical_display.setdefault(key, term)
        definitions_by_key[key].append(definition.definition)

    candidates = _candidate_by_surface(survey)
    clusters: dict[str, _Cluster] = {}

    def ensure(key: str, display: str) -> _Cluster:
        return clusters.setdefault(key, _Cluster(
            document_id=document.document_id,
            canonical_name=display,
            aliases=set(), mentions=[], survey_sources=set(), survey_count=0,
            definitions=list(definitions_by_key.get(key, [])),
        ))

    for mention in mentions:
        if mention.terminal_state != MentionTerminalState.ALIGNED:
            continue
        cluster_surface = _trim_cross_sentence_surface(mention.surface)
        surface_key = _normalized_surface(cluster_surface)
        base_key = _normalized_surface(_canonical_surface(cluster_surface))
        key = (
            f"ambiguous:{cluster_surface}" if base_key in _AMBIGUOUS_NAMES
            else alias_to_canonical.get(base_key, surface_key)
        )
        display = canonical_display.get(key, _canonical_surface(cluster_surface))
        cluster = ensure(key, display)
        cluster.mentions.append(mention)
        if _normalized_surface(mention.surface) != _normalized_surface(display):
            cluster.aliases.add(mention.surface)
        candidate = candidates.get(base_key)
        if candidate:
            cluster.survey_sources.update(candidate.sources)
            cluster.survey_count = max(cluster.survey_count, candidate.count)

    explicit_keys: set[str] = set(definitions_by_key)
    explicit_keys.update(canonical_display)
    for key in explicit_keys:
        display = canonical_display.get(key, key)
        cluster = ensure(key, display)
        cluster.survey_sources.add("explicit_definition_or_alias")
        cluster.definitions.extend(value for value in definitions_by_key.get(key, []) if value not in cluster.definitions)
    for candidate in survey.gazetteer_candidates:
        display = _canonical_surface(candidate.surface)
        normalized_display = _normalized_surface(display)
        key = (
            f"ambiguous:{display}" if normalized_display in _AMBIGUOUS_NAMES
            else alias_to_canonical.get(normalized_display, normalized_display)
        )
        if (
            candidate.count >= 2
            and "capitalization_shape" in candidate.sources
            and key not in _GENERIC_SURFACES
            and len(display) > 1
        ):
            cluster = ensure(key, canonical_display.get(key, display))
            cluster.survey_sources.update(candidate.sources)
            cluster.survey_count = max(cluster.survey_count, candidate.count)
    for alias in survey.aliases:
        key = alias_to_canonical[_normalized_surface(alias.alias)]
        ensure(key, canonical_display[key]).aliases.add(alias.alias)
    supplemental_patterns = (
        (re.compile(r"\b(?P<name>[A-Za-z][\w.+-]*)\s+as\s+(?:an?\s+)?(?P<kind>Library|Software|Service|Concept|Metric|Dataset|Document|Process)\b", re.I), None),
        (re.compile(r"\b(?P<name>[A-Z][A-Za-z0-9.+-]*(?:Matcher|Extractor|Adapter))\b"), "software"),
        (re.compile(r"\bdepends\s+on\s+(?P<name>[A-Z][A-Z0-9-]{2,})\b", re.I), "concept"),
        (re.compile(r"\bconsumes?\s+(?P<name>[A-Z][A-Za-z0-9.+-]*)\b"), "document"),
        (re.compile(r"\bmeasures?\s+(?P<name>[a-z][a-z0-9_-]+)\b", re.I), "metric"),
        (re.compile(r"\bThe\s+(?P<name>dataset|metric|document)\s+(?:derives?|measures?|defines?)\b", re.I), None),
    )
    for pattern, fixed_kind in supplemental_patterns:
        for match in pattern.finditer(document.normalized_text):
            display = match.group("name")
            key = _normalized_surface(display)
            cluster = ensure(key, display)
            cluster.survey_sources.add("strict_technical_context")
            kind = fixed_kind or match.groupdict().get("kind") or {
                "dataset": "dataset", "metric": "metric", "document": "document",
            }.get(display.casefold(), "concept")
            cluster.definitions.append(str(kind))
    return clusters


_CARDINAL_WORDS = frozenset({
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "dozen", "twenty", "thirty", "forty", "fifty",
    "hundred", "thousand", "million", "billion", "several", "both",
})


_TIMESTAMP_RE = __import__("re").compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
_DISCOURSE_MARKERS = frozenset({
    "additionally", "however", "furthermore", "meanwhile", "moreover",
    "therefore", "otherwise", "anyway", "basically", "actually", "look",
    "like", "okay", "ok", "well", "right", "now",
    # bare subordinators/conjunctions are never names either
    "because", "although", "though", "while", "since", "unless",
    "whether", "whenever", "after", "before", "during",
})


def _junk_entity_surface(name: str) -> bool:
    """Universal entity-quality classes (Decision 1, wired 2026-08-08):
    pronouns, bare function words, discourse markers, and timestamps name
    nothing in ANY domain. Membership comes from the same battle-tested
    sets the entity_quality verdict layer uses; multi-token surfaces are
    junk only when EVERY token is junk-class ("And I"), so real names
    that merely contain function words ("If-Then Systems") survive."""
    from services.extraction.entity_quality import _FUNCTION_WORDS, _PRONOUNS

    stripped = name.strip()
    if _TIMESTAMP_RE.match(stripped):
        return True
    tokens = [t.casefold() for t in stripped.replace("-", " ").split()]
    if not tokens:
        return True
    return all(
        t in _FUNCTION_WORDS or t in _PRONOUNS or t in _DISCOURSE_MARKERS
        for t in tokens
    )


def _counted_noun_phrase(name: str) -> bool:
    """Cardinal determiner + all-lowercase continuation = quantity, not identity."""
    tokens = name.strip().split()
    if len(tokens) < 2:
        return False
    first = tokens[0].casefold()
    if first not in _CARDINAL_WORDS and not tokens[0].isdigit():
        return False
    return all(token.islower() for token in tokens[1:])


def _cluster_decision(
    cluster: _Cluster,
    document: NormalizedDocumentV1,
    all_mentions: Sequence[RawMentionV1],
    survey: DocumentSurveyV1,
) -> tuple[EntityTerminalState, tuple[str, ...]]:
    reasons: list[str] = []
    normalized = _normalized_surface(cluster.canonical_name)
    if normalized in _GENERIC_SURFACES and "strict_technical_context" not in cluster.survey_sources:
        return EntityTerminalState.SUPPRESSED, ("generic_or_pronominal_surface",)
    if _junk_entity_surface(cluster.canonical_name):
        return EntityTerminalState.SUPPRESSED, ("universal_junk_surface",)
    # Metadata-KEY guard (owner-authorized 2026-08-08): a surface whose
    # EVERY mention sits in key-position (immediately followed by ':') is
    # a structural key, not an entity — "Channel: ...", "Duration: ...".
    # One prose mention anywhere rescues a real name ("AutoDS: the tool"
    # plus prose uses of AutoDS).
    if cluster.mentions:
        text_all = document.normalized_text
        key_positions = 0
        for mention in cluster.mentions:
            end = mention.normalized_end
            if end is not None:
                tail = text_all[end:end + 2]
                if tail[:1] == ":" or tail == " :":
                    key_positions += 1
        if key_positions == len(cluster.mentions):
            return EntityTerminalState.SUPPRESSED, ("metadata_key_surface",)
    if _counted_noun_phrase(cluster.canonical_name):
        # Universal entity-quality class (owner-authorized 2026-08-08): a
        # cardinal determiner over a lowercase common head names a QUANTITY
        # of things, never an identity — "two dashboards", "three sensors",
        # "10 workers" — in any domain. Capitalized continuations ("Three
        # Mile Island") and attached digits ("5G networks") are untouched.
        return EntityTerminalState.SUPPRESSED, ("counted_noun_phrase",)
    ambiguous_mentions = [mention for mention in cluster.mentions if _normalized_surface(mention.surface) in _AMBIGUOUS_NAMES]
    ambiguous_named = bool(ambiguous_mentions) and any(
        _ambiguous_use_is_named(document, mention) for mention in ambiguous_mentions
    )
    if ambiguous_mentions and not ambiguous_named:
        return EntityTerminalState.SUPPRESSED, ("ambiguous_surface_without_named_context",)
    if any("(" in mention.surface and ")" in mention.surface for mention in cluster.mentions):
        return EntityTerminalState.REVIEW, ("composite_alias_span_review_only",)
    heading_spans = tuple((heading.start, heading.end) for heading in survey.headings)
    has_non_heading_mention = any(
        not any(
            heading_start <= (mention.normalized_start or 0)
            and (mention.normalized_end or 0) <= heading_end
            for heading_start, heading_end in heading_spans
        )
        for mention in cluster.mentions
    )
    if (
        "heading" in cluster.survey_sources
        and "explicit_definition_or_alias" not in cluster.survey_sources
        and not has_non_heading_mention
    ):
        return EntityTerminalState.REVIEW, ("structural_heading_review_only",)
    if (
        cluster.canonical_name.casefold().endswith((" library", " framework", " database"))
        and not cluster.definitions
        and max((mention.confidence for mention in cluster.mentions), default=0.0) < 0.85
    ):
        return EntityTerminalState.REVIEW, ("generic_category_phrase_review_only",)
    if any("(" in mention.surface and ")" in mention.surface for mention in cluster.mentions) and cluster.aliases:
        reasons.append("composite_alias_span_retained_for_audit")
    if "explicit_definition_or_alias" in cluster.survey_sources:
        reasons.append("explicit_definition_or_alias")
    if "strict_technical_context" in cluster.survey_sources:
        reasons.append("strict_technical_context")
    if cluster.survey_count >= 2:
        reasons.append("document_recurrence")
    if cluster.mentions and max(mention.confidence for mention in cluster.mentions) >= 0.80:
        reasons.append("high_confidence_model_evidence")
    if _specific_name_shape(cluster.canonical_name):
        reasons.append("specific_name_shape")
    if ambiguous_named:
        reasons.append("ambiguous_surface_named_context")
    if "explicit_definition_or_alias" in cluster.survey_sources:
        return EntityTerminalState.PROMOTED, tuple(reasons)
    if "strict_technical_context" in cluster.survey_sources:
        return EntityTerminalState.PROMOTED, tuple(reasons)
    if ambiguous_named:
        return EntityTerminalState.PROMOTED, tuple(reasons)
    if cluster.survey_count >= 2 and _specific_name_shape(cluster.canonical_name):
        return EntityTerminalState.PROMOTED, tuple(reasons)
    if cluster.mentions and "high_confidence_model_evidence" in reasons and "specific_name_shape" in reasons:
        return EntityTerminalState.PROMOTED, tuple(reasons)
    if cluster.mentions and len(_WORD_RE.findall(cluster.canonical_name)) >= 2:
        return EntityTerminalState.DOCUMENT_LOCAL, tuple(reasons + ["specific_document_local_phrase"])
    if cluster.mentions:
        return EntityTerminalState.REVIEW, tuple(reasons + ["insufficient_specificity"])
    return EntityTerminalState.REVIEW, tuple(reasons + ["weak_survey_candidate_only"])


_TITLE_TOKENS = frozenset({"dr", "mr", "mrs", "ms", "prof"})


def _genuine_first_capital(document: NormalizedDocumentV1, cluster: _Cluster) -> bool:
    """True when at least one mention shows the name's first capital in a
    non-sentence-initial position (preceded by a lowercase word or comma) —
    positional capitals ("Voltage sag causes ...") are not name evidence."""
    text = document.normalized_text
    for mention in cluster.mentions:
        start = mention.normalized_start
        if start is None or start < 2:
            continue
        preceding = text[:start]
        whitespace_run = preceding[len(preceding.rstrip()):]
        if "\n" in whitespace_run:
            continue  # line-initial: capitalization is positional
        before = preceding.rstrip()
        if before and (before[-1].islower() or before[-1] == ","):
            return True
    return False


def _merge_pair(target: _Cluster, source: _Cluster) -> None:
    target.mentions.extend(source.mentions)
    target.aliases.add(source.canonical_name)
    target.aliases.update(source.aliases)
    target.survey_sources.update(source.survey_sources)
    target.survey_count = max(target.survey_count, source.survey_count)
    target.definitions.extend(
        value for value in source.definitions if value not in target.definitions
    )


def _extend_names_to_core_mentions(clusters: dict[str, _Cluster]) -> None:
    """A truncated census name extends to its own mention's capitalized
    continuation ("Pier" with mention "Pier Nine" → "Pier Nine"). Extension
    only — never invents tokens; lowercase continuations never extend."""
    for cluster in clusters.values():
        name_words = [w.casefold() for w in cluster.canonical_name.split()]
        if not name_words:
            continue
        best: str | None = None
        for mention in cluster.mentions:
            surface = _canonical_surface(_trim_cross_sentence_surface(mention.surface))
            words = surface.split()
            if len(words) <= len(name_words):
                continue
            if [w.casefold() for w in words[: len(name_words)]] != name_words:
                continue
            extra = words[len(name_words):]
            if all(
                any(ch.isupper() for ch in w) or any(ch.isdigit() for ch in w)
                for w in extra
            ):
                if best is None or len(words) > len(best.split()):
                    best = surface
        if best is not None:
            cluster.aliases.add(cluster.canonical_name)
            cluster.canonical_name = best


def _merge_number_and_title_variants(clusters: dict[str, _Cluster]) -> None:
    """Fold plural cluster names onto an existing singular cluster
    ("Load Manifests" → "Load Manifest") and titled short forms onto the full
    person name ("Dr. Osei" → "Dr. Nadia Osei"). Merge-on-existence only —
    never invent a form the document does not contain."""
    by_normalized = {
        _normalized_surface(cluster.canonical_name): key
        for key, cluster in clusters.items()
    }
    for key in sorted(clusters):
        cluster = clusters.get(key)
        if cluster is None:
            continue
        words = cluster.canonical_name.split()
        if not words:
            continue
        singular_last = singularize_token(words[-1])
        if singular_last != words[-1]:
            singular_name = " ".join([*words[:-1], singular_last])
            target_key = by_normalized.get(_normalized_surface(singular_name))
            if target_key and target_key != key and target_key in clusters:
                _merge_pair(clusters[target_key], cluster)
                del clusters[key]
                continue
        first = words[0].rstrip(".").casefold()
        if first in _TITLE_TOKENS and len(words) == 2:
            surname = _normalized_surface(words[-1])
            for other_key in sorted(clusters):
                if other_key == key:
                    continue
                other = clusters[other_key]
                other_words = other.canonical_name.split()
                if (
                    len(other_words) >= 3
                    and other_words[0].rstrip(".").casefold() == first
                    and _normalized_surface(other_words[-1]) == surname
                ):
                    _merge_pair(other, cluster)
                    del clusters[key]
                    break


def _merge_descriptor_clusters(clusters: dict[str, _Cluster], document: NormalizedDocumentV1) -> None:
    """Fold descriptor-suffixed cluster names onto their capitalized name core.

    In-text casing is the signal: in "the Redlark database" only "Redlark" is
    the name; "database" is a lowercase descriptor. A cluster whose display
    name wraps a capitalized/identifier core in lowercase descriptor tokens
    merges into the core-named cluster when one exists, and otherwise renames
    to the core (keeping the long form as an alias). Fully lowercase names
    ("approval record") have no core and are untouched, as are all-core names
    ("Sensor K-12", "Pinion 3.2").
    """
    by_normalized = {
        _normalized_surface(cluster.canonical_name): key
        for key, cluster in clusters.items()
    }
    for key in sorted(clusters):
        cluster = clusters.get(key)
        if cluster is None:
            continue
        span = name_core_span(cluster.canonical_name)
        if span is None:
            continue
        core = cluster.canonical_name[span[0]:span[1]].strip()
        if not core or _normalized_surface(core) == _normalized_surface(cluster.canonical_name):
            continue
        name_words = cluster.canonical_name.split()
        if (
            name_words
            and core == name_words[0]
            and len(name_words) > 1
            and not core.isupper()
            and not any(char.isdigit() for char in core)
            and not _genuine_first_capital(document, cluster)
        ):
            # Positional capital on a generic phrase ("Voltage sag causes ...")
            # is not name evidence; the full phrase stays the identity.
            continue
        target_key = by_normalized.get(_normalized_surface(core))
        if target_key is not None and target_key != key and target_key in clusters:
            target = clusters[target_key]
            target.mentions.extend(cluster.mentions)
            target.aliases.add(cluster.canonical_name)
            target.aliases.update(cluster.aliases)
            target.survey_sources.update(cluster.survey_sources)
            target.survey_count = max(target.survey_count, cluster.survey_count)
            target.definitions.extend(
                value for value in cluster.definitions if value not in target.definitions
            )
            del clusters[key]
        else:
            cluster.aliases.add(cluster.canonical_name)
            cluster.canonical_name = core
            by_normalized[_normalized_surface(core)] = key


def reduce_document_entities(
    document: NormalizedDocumentV1,
    mentions: Sequence[RawMentionV1],
    survey: DocumentSurveyV1,
) -> ReducerOutput:
    aligned = [mention for mention in mentions if mention.terminal_state == MentionTerminalState.ALIGNED]
    if any(mention.document_id != document.document_id for mention in aligned):
        raise ValueError("raw mention document identity mismatch")
    clusters = _build_clusters(document, aligned, survey)
    _merge_descriptor_clusters(clusters, document)
    _extend_names_to_core_mentions(clusters)
    _merge_number_and_title_variants(clusters)
    entities: list[DocumentEntityV1] = []
    assignments: list[MentionReductionAssignment] = []
    for key, cluster in sorted(clusters.items()):
        entity_type, type_conflict = _choose_type(cluster, document)
        state, reasons = _cluster_decision(cluster, document, aligned, survey)
        if type_conflict:
            reasons = tuple(reasons) + ("type_conflict_adjudicated",)
        confidence = max((mention.confidence for mention in cluster.mentions), default=0.6)
        entity_id = stable_id(
            "document-entity", document.document_id, key, entity_type, REDUCER_RELEASE,
        )
        facet_votes = Counter(
            mention.facet for mention in cluster.mentions if getattr(mention, "facet", "")
        )
        entity = DocumentEntityV1(
            entity_id=entity_id,
            document_id=document.document_id,
            canonical_name=cluster.canonical_name,
            entity_type=entity_type,
            facet=facet_votes.most_common(1)[0][0] if facet_votes else "",
            aliases=tuple(sorted(cluster.aliases, key=lambda value: (value.casefold(), value))),
            mention_ids=tuple(sorted(mention.mention_id for mention in cluster.mentions)),
            state=state,
            confidence=confidence,
            reasons=reasons or ("terminal_state_assigned",),
            reducer_release=REDUCER_RELEASE,
        )
        entities.append(entity)
        assignments.extend(
            MentionReductionAssignment(mention.mention_id, entity_id, state, entity.reasons)
            for mention in cluster.mentions
        )
    assignments.sort(key=lambda item: item.mention_id)
    assigned_ids = [item.mention_id for item in assignments]
    aligned_ids = sorted(mention.mention_id for mention in aligned)
    if assigned_ids != aligned_ids:
        raise RuntimeError("entity reducer did not assign every aligned mention exactly once")
    state_counts = Counter(entity.state.value for entity in entities)
    report: dict[str, object] = {
        "schema_version": "polymath.document_entity_reducer_report.v1",
        "status": "passed",
        "release": REDUCER_RELEASE,
        "document_id": document.document_id,
        "aligned_raw_mentions": len(aligned),
        "terminal_assignments": len(assignments),
        "conservation": len(aligned) == len(assignments) == len(set(assigned_ids)),
        "clusters": len(entities),
        "state_counts": dict(sorted(state_counts.items())),
        "strong_singletons": sum(
            len(entity.mention_ids) == 1 and entity.state == EntityTerminalState.PROMOTED
            for entity in entities
        ),
        "survey_only_clusters": sum(not entity.mention_ids for entity in entities),
        "hard_entity_cap": None,
        "identity_digest": stable_digest({
            "entities": [entity.model_dump(mode="json") for entity in entities],
            "assignments": [item.as_dict() for item in assignments],
        }),
    }
    return ReducerOutput(tuple(entities), tuple(assignments), report)
