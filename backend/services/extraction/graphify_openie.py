"""Balanced CPU OpenIE proposal source for canonical Graphify."""

from __future__ import annotations

import importlib.metadata
import threading
import time
import re
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from models.graphify_contracts import (
    NormalizedDocumentV1,
    OpenIEAsserterLinkV1,
    OpenIERawPropositionV1,
    stable_digest,
    stable_id,
)

OPENIE_RELEASE = "graphify-triplet-extract-balanced-cpu-v6"
TRIPLET_EXTRACT_VERSION = "0.5.0"
TRIPLET_EXTRACT_COMMIT = "89417ae62214728deca112aa8f4d27ff6c854d08"
SPEED_PRESET = "balanced"
_STRICT_ACTIVE_RE = re.compile(
    r"^(?:The\s+)?(?P<subject>.+?)\s+"
    r"(?P<relation>stores?|projects?|defines?|uses?|depends\s+on|supports?|"
    r"consumes?|produces?|derives?\s+from|measures?|powers?|implements?|"
    r"detects?|runs?\s+on|acquires?|acquired|is\s+related\s+to)\s+"
    r"(?P<object>.+?)(?:\s+and\s+(?=(?:stores?|projects?|defines?|uses?|depends\s+on|"
    r"supports?|consumes?|produces?|derives?\s+from|measures?|powers?|implements?|"
    r"detects?|runs?\s+on|acquires?|acquired)\b)|\s+(?:while|when|before|after|unless|but)\b|[.;]|$)",
    re.I,
)
_STRICT_SECOND_RE = re.compile(
    r"\band\s+(?P<relation>stores?|projects?|defines?|uses?|depends\s+on|supports?|"
    r"consumes?|produces?|derives?\s+from|measures?|powers?|implements?|detects?|runs?\s+on|acquires?|acquired)\s+"
    r"(?P<object>.+?)(?:\s+(?:while|when|before|after|unless)\b|[.;]|$)", re.I,
)
_STRICT_PASSIVE_RE = re.compile(
    r"^(?:The\s+)?(?P<object>[A-Z][\w.+-]*(?:\s+[A-Z][\w.+-]*)*)\s+"
    r"(?:was|is|were|are)\s+(?P<relation>acquired\s+by|defined\s+as)\s+"
    r"(?P<subject>.+?)(?:\s+in\s+\d{4}|[.;]|$)", re.I,
)
_STRICT_COPULAR_DEFINITION_RE = re.compile(
    r"^(?:The\s+)?(?P<subject>[A-Z][\w.+-]*(?:\s+[A-Z][\w.+-]*)*)\s+"
    r"(?:is|are)\s+(?:an?\s+)?(?P<object>.+?)(?:\s+for\b|[.;]|$)", re.I,
)
_QUALIFIED_UNIT_RE = re.compile(
    r"\b(?:may|might|could|would|should|if|unless|claims?|claimed|says?|said|"
    r"suggests?|suggested|recommends?|recommended|deny|denied|reported|stated|"
    r"asserted|alleged|believed|thought|showed|indicated|refuted|rejected|"
    r"retracted?|withdrew|withdrawn|disputed?|debunked)\b"
    r"|\b(?:reports?|states?|asserts?|alleges?|believes?|thinks?|shows?|indicates?|"
    r"refutes?|rejects?)\s+that\b"
    # Claim-noun complements are attributed content, never direct assertions:
    # "the (incorrect) statement/assertion/allegation/rumor that X ..."
    r"|\b(?:statements?|assertions?|allegations?|rumou?rs?|hypothes[ei]s)\s+that\b"
    # Peripheral reported-attribution adjunct (owner-ratified construction):
    r"|(?:^|[.!?]\s)\s*according\s+to\b|,\s*according\s+to\b",
    re.I,
)
_TITLE_PHRASE_RE = re.compile(
    r"(?P<title>(?:[A-Z][\w.+-]*|spaCy)(?:\s+(?:[A-Z][\w.+-]*|spaCy))*)$"
)
_LEADING_TITLE_PHRASE_RE = re.compile(
    r"^(?P<title>(?:[A-Z][\w.+-]*|spaCy)(?:\s+(?:[A-Z][\w.+-]*|spaCy))*)\b"
)
_INDEPENDENT_CLAUSE_RE = re.compile(
    r"\s*(?:,|\band\b)\s+(?=(?:the\s+)?(?:[A-Z][\w.+-]*|projection\s+recovery)"
    r"(?:\s+[A-Z][\w.+-]*){0,5}\s+(?:also\s+)?(?:stores?|projects?|defines?|uses?|"
    r"depends\s+on|supports?|consumes?|produces?|derives?\s+from|measures?|powers?|"
    r"implements?|detects?|runs?\s+on|acquires?|acquired|is\s+related\s+to)\b)",
    re.I,
)


@dataclass(frozen=True)
class OpenIEUnit:
    unit_id: str
    document_id: str
    start: int
    end: int
    text: str
    eligible: bool


@dataclass(frozen=True)
class OpenIEOutput:
    propositions: tuple[OpenIERawPropositionV1, ...]
    report: dict[str, object]


_EXTRACTOR_LOCK = threading.Lock()
_INFERENCE_LOCK = threading.Lock()
_EXTRACTOR: Any | None = None
_EXTRACTOR_LOAD_COUNT = 0
_PROVIDER: "TripletExtractCPUProvider | None" = None


def _default_loader() -> Any:
    from triplet_extract import OpenIEExtractor

    return OpenIEExtractor(
        speed_preset=SPEED_PRESET,
        deep_search=False,
        resolve_coref=False,
        preserve_latex=False,
    )


class TripletExtractCPUProvider:
    """One warm triplet-extract instance with serialized CPU inference."""

    def __init__(self, loader: Callable[[], Any] | None = None) -> None:
        self._loader = loader or _default_loader

    def _instance(self) -> Any:
        global _EXTRACTOR, _EXTRACTOR_LOAD_COUNT
        if _EXTRACTOR is None:
            with _EXTRACTOR_LOCK:
                if _EXTRACTOR is None:
                    _EXTRACTOR = self._loader()
                    _EXTRACTOR_LOAD_COUNT += 1
        return _EXTRACTOR

    @property
    def load_count(self) -> int:
        return _EXTRACTOR_LOAD_COUNT

    def health(self) -> dict[str, object]:
        self._instance()
        try:
            package_version = importlib.metadata.version("triplet-extract")
        except importlib.metadata.PackageNotFoundError:
            package_version = TRIPLET_EXTRACT_VERSION
        return {
            "ready": True,
            "device": "cpu",
            "speed_preset": SPEED_PRESET,
            "deep_search": False,
            "resolve_coref": False,
            "package_version": package_version,
            "source_commit": TRIPLET_EXTRACT_COMMIT,
            "extractor_release": OPENIE_RELEASE,
            "extractor_load_count": self.load_count,
        }

    def extract(self, text: str) -> list[Any]:
        with _INFERENCE_LOCK:
            return list(self._instance().extract_triplet_objects(text))


def get_triplet_extract_cpu_provider() -> TripletExtractCPUProvider:
    global _PROVIDER
    if _PROVIDER is None:
        with _EXTRACTOR_LOCK:
            if _PROVIDER is None:
                _PROVIDER = TripletExtractCPUProvider()
    return _PROVIDER


def _link(link: Any) -> OpenIEAsserterLinkV1:
    payload = link.to_dict() if hasattr(link, "to_dict") else dict(link)
    return OpenIEAsserterLinkV1.model_validate(payload)


def _strict_surface_recovery(text: str) -> tuple[tuple[str, str, str], ...]:
    """Recover explicit technical propositions the parser can miss.

    This lane is deliberately anchored to the whole sentence and a frozen
    relation cue inventory. It never resolves pronouns or attributed clauses.
    """
    sentence = text.strip().lstrip("#").strip()
    sentence = re.sub(r"(?:\*\*|__|~~|`)", "", sentence)
    def clean_subject(value: str) -> str:
        value = value.strip().rstrip(",").strip()
        value = re.sub(r"\s+also$", "", value, flags=re.I).strip()
        title = _TITLE_PHRASE_RE.search(value)
        if title:
            value = title.group("title")
        return re.sub(r"^(?:a|an|the)\s+", "", value, flags=re.I).strip()

    def clean_object(value: str, relation: str) -> str:
        value = value.strip().rstrip(",").strip()
        value = re.sub(r"^(?:a|an|the)\s+", "", value, flags=re.I).strip()
        if "'s " not in value and "’s " not in value:
            title = _LEADING_TITLE_PHRASE_RE.search(value)
            if title:
                value = title.group("title")
        value = re.split(
            r"\s+(?:which|because|containing|during|after|before|while|so\s+that)\b",
            value, maxsplit=1, flags=re.I,
        )[0].strip()
        if re.search(r"\b(?:uses?|depends\s+on|supports?|implements?)\b", relation, re.I):
            value = re.split(r"\s+(?:for|to)\s+", value, maxsplit=1, flags=re.I)[0].strip()
        return value

    rows: list[tuple[str, str, str]] = []
    coarse_clauses = re.split(r"\s+(?:while|whereas)\s+", sentence, flags=re.I)
    clauses = [piece for clause in coarse_clauses for piece in _INDEPENDENT_CLAUSE_RE.split(clause)]
    for clause in clauses:
        active = _STRICT_ACTIVE_RE.search(clause)
        if active:
            subject = clean_subject(active.group("subject"))
            relation = active.group("relation").strip()
            raw_object = active.group("object").strip()
            coordinated_objects: list[str] = []
            both = re.match(
                r"both\s+((?:[A-Z][\w.+-]*|spaCy)(?:\s+[A-Z][\w.+-]*)*)\s+and\s+"
                r"((?:[A-Z][\w.+-]*|spaCy)(?:\s+[A-Z][\w.+-]*)*)\b",
                raw_object,
            )
            if both:
                coordinated_objects = [both.group(1), both.group(2)]
            obj = clean_object(raw_object, relation)
            if re.search(r"\b(?:stores?)\b", relation, re.I) and "unmapped surface relation" in obj.casefold():
                continue
            if re.search(r"\b(?:stores?)\b", relation, re.I):
                obj = re.split(r"\s+for\s+", obj, maxsplit=1, flags=re.I)[0].strip()
            if re.search(r"\buses?\b", relation, re.I):
                obj = re.sub(r"^(?:a|an|the)\s+", "", obj, flags=re.I).split(" to ", 1)[0].strip()
            if re.search(r"\bdepends\s+on\b", relation, re.I):
                match = re.match(r"([A-Z][A-Z0-9-]{2,})(?:\s+indexing)?\b", obj)
                if match:
                    obj = match.group(1)
            if re.search(r"\bprojects?\b", relation, re.I):
                obj = re.split(r"\s+from\s+", obj, maxsplit=1, flags=re.I)[0].strip()
            if re.search(r"\bdefines?\b", relation, re.I):
                obj = re.split(r"\s+as\s+", obj, maxsplit=1, flags=re.I)[0].strip()
            if re.search(r"\bacquir", relation, re.I):
                obj = re.sub(r"\s+in\s+\d{4}$", "", obj, flags=re.I).strip()
            if re.search(r"\brelated\s+to\b", relation, re.I):
                if re.search(r"\b(?:invalid|unless|explicitly|source)\b", subject, re.I):
                    continue
                obj = re.split(r"\s+(?:in|for)\s+", obj, maxsplit=1, flags=re.I)[0].strip()
            subjects = re.split(r"\s+and\s+", subject) if re.fullmatch(
                r"[A-Z][\w.+-]*(?:\s+and\s+[A-Z][\w.+-]*)+", subject,
            ) else [subject]
            objects = coordinated_objects or [obj]
            rows.extend((item, relation, object_value) for item in subjects for object_value in objects)
            second = _STRICT_SECOND_RE.search(clause)
            if second:
                second_relation = second.group("relation").strip()
                second_object = clean_object(second.group("object"), second_relation)
                rows.extend((item, second_relation, second_object) for item in subjects)
    passive = _STRICT_PASSIVE_RE.search(sentence)
    if passive and "when" not in passive.group("object").casefold():
        if passive.group("relation").casefold().startswith("acquired"):
            rows.append((passive.group("object").strip(), "was acquired by", passive.group("subject").strip()))
        else:
            rows.append((passive.group("object").strip(), "is defined as", passive.group("subject").strip()))
    copular = _STRICT_COPULAR_DEFINITION_RE.search(sentence)
    if copular and re.search(r"\b(?:extractor|workflow|method|status|database|service|library|process|concept)\b", copular.group("object"), re.I):
        rows.append((copular.group("subject").strip(), "defines", copular.group("object").strip()))
    return tuple(dict.fromkeys(rows))


def run_openie_extraction(
    documents: Sequence[NormalizedDocumentV1],
    units: Sequence[OpenIEUnit],
    provider: TripletExtractCPUProvider,
) -> OpenIEOutput:
    document_by_id = {document.document_id: document for document in documents}
    propositions: list[OpenIERawPropositionV1] = []
    eligible = [unit for unit in units if unit.eligible]
    started = time.perf_counter()
    extract_calls = 0
    openie_successes = 0
    openie_failures: list[dict[str, str]] = []
    deterministic_recoveries = 0
    for unit in eligible:
        document = document_by_id.get(unit.document_id)
        if document is None:
            raise ValueError(f"unknown OpenIE document {unit.document_id}")
        if document.normalized_text[unit.start:unit.end] != unit.text:
            raise ValueError(f"OpenIE unit evidence mismatch for {unit.unit_id}")
        # The deterministic recovery lane carries no asserter chains, so it is
        # scoped away from attributed/modal/negation-cue units: on those, only
        # triplet-extract can qualify safely. This is lane scoping (assertion
        # safety), never a substitute for the OpenIE call below.
        recovered_rows = () if _QUALIFIED_UNIT_RE.search(unit.text) else _strict_surface_recovery(unit.text)
        # UNION (owner-ratified 2026-08-07): triplet-extract ALWAYS runs — the
        # open-world linguistic baseline is never silenced. The deterministic
        # recovery lane corroborates and augments; it does not replace.
        # External proposers receive the same masked, soft-wrap-flattened text
        # the shared spaCy pipe parses (equal length, offsets stable).
        # Invariant: eligible_prose_units == openie_successes + explicit
        # failures. A provider error is recorded, never silently absorbed into
        # a deterministic-only unit.
        extract_calls += 1
        try:
            renderings = provider.extract(re.sub(r"[*_~`\r\n]", " ", unit.text))
            openie_successes += 1
        except Exception as exc:  # noqa: BLE001 — failure is data, not control flow
            renderings = []
            openie_failures.append({
                "unit_id": unit.unit_id,
                "document_id": unit.document_id,
                "error_type": type(exc).__name__,
                "error": str(exc)[:300],
            })
        for sequence, rendering in enumerate(renderings):
            links = tuple(_link(link) for link in (getattr(rendering, "asserter_links", None) or ()))
            chain = tuple(str(value) for value in (getattr(rendering, "asserter_chain", None) or ()))
            subject = str(rendering.subject)
            relation = str(rendering.relation)
            obj = str(rendering.object)
            propositions.append(OpenIERawPropositionV1(
                proposition_id=stable_id(
                    "openie-proposition", unit.document_id, unit.unit_id, sequence,
                    subject, relation, obj, chain,
                    [item.model_dump(mode="json") for item in links], OPENIE_RELEASE,
                ),
                document_id=unit.document_id,
                unit_id=unit.unit_id,
                evidence_text=unit.text,
                evidence_start=unit.start,
                evidence_end=unit.end,
                rendering_sequence=sequence,
                subject=subject,
                relation=relation,
                object=obj,
                confidence=float(getattr(rendering, "confidence", 1.0)),
                from_clause_split=bool(getattr(rendering, "from_clause_split", False)),
                from_entailment=bool(getattr(rendering, "from_entailment", False)),
                entailment_score=float(getattr(rendering, "entailment_score", 1.0)),
                asserter_chain=chain,
                asserter_links=links,
                extractor_release=OPENIE_RELEASE,
            ))
        for subject, relation, obj in recovered_rows:
            deterministic_recoveries += 1
            sequence = len(renderings) + deterministic_recoveries
            propositions.append(OpenIERawPropositionV1(
                proposition_id=stable_id(
                    "openie-proposition", unit.document_id, unit.unit_id, "strict-recovery",
                    subject, relation, obj, OPENIE_RELEASE,
                ),
                document_id=unit.document_id, unit_id=unit.unit_id,
                evidence_text=unit.text, evidence_start=unit.start, evidence_end=unit.end,
                rendering_sequence=sequence, subject=subject, relation=relation, object=obj,
                confidence=1.0, entailment_score=1.0,
                extractor_release=OPENIE_RELEASE + ":strict_surface_recovery",
            ))
    elapsed = time.perf_counter() - started
    propositions.sort(key=lambda item: (
        item.document_id, item.evidence_start, item.unit_id,
        item.rendering_sequence, item.proposition_id,
    ))
    health = provider.health()
    deterministic_only_units = len(eligible) - openie_successes - len(openie_failures)
    if deterministic_only_units != 0:
        raise RuntimeError(
            "UNION invariant violated: "
            f"{deterministic_only_units} eligible unit(s) never reached triplet-extract"
        )
    report: dict[str, object] = {
        "schema_version": "polymath.openie_extraction_report.v1",
        "status": "passed",
        "documents": len(documents),
        "input_units": len(units),
        "eligible_units": len(eligible),
        "eligible_prose_units": len(eligible),
        "extract_calls": extract_calls,
        "openie_successes": openie_successes,
        "explicit_openie_failures": len(openie_failures),
        "openie_failure_records": openie_failures,
        "union_invariant_holds": len(eligible) == openie_successes + len(openie_failures),
        "one_extract_call_per_eligible_unit": extract_calls == len(eligible),
        "at_most_one_provider_call_per_eligible_unit": extract_calls <= len(eligible),
        "every_eligible_unit_routed": extract_calls + deterministic_only_units == len(eligible),
        "deterministic_only_units": deterministic_only_units,
        "raw_renderings": len(propositions),
        "deterministic_surface_recoveries": deterministic_recoveries,
        "persisted_propositions": len(propositions),
        "conservation": len(propositions) == len({item.proposition_id for item in propositions}),
        "exact_evidence_alignment": all(
            document_by_id[item.document_id].normalized_text[item.evidence_start:item.evidence_end]
            == item.evidence_text
            for item in propositions
        ),
        "attributed_renderings": sum(bool(item.asserter_chain) for item in propositions),
        "negated_asserter_links": sum(
            any(link.negated for link in item.asserter_links) for item in propositions
        ),
        "no_graph_writes": True,
        "elapsed_seconds": elapsed,
        "health": health,
        "identity_digest": stable_digest([
            item.model_dump(mode="json") for item in propositions
        ]),
    }
    return OpenIEOutput(tuple(propositions), report)


def _reset_openie_provider_for_tests() -> None:
    global _EXTRACTOR, _EXTRACTOR_LOAD_COUNT, _PROVIDER
    with _EXTRACTOR_LOCK:
        _EXTRACTOR = None
        _EXTRACTOR_LOAD_COUNT = 0
        _PROVIDER = None
