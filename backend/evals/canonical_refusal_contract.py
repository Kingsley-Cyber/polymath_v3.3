"""Reusable deterministic refusal classifier and chat-trace contract.

This module is intentionally dependency-free so measurement harnesses can
share one frozen rule without importing the production settings or scorers.
"""

from __future__ import annotations

import re
from typing import Any, Sequence


CLASSIFIER_VERSION = "canonical_refusal_three_state.v2"
EXPECTED_CHAT_MODEL = "anthropic/minimax-m2.7"

REFUSAL_CUES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "speaker_cannot_answer",
        re.compile(
            r"\b(?:i|we)\s+(?:can(?:not|'t)|could(?:\s+not|n't)|am\s+unable"
            r"|are\s+unable)\s+(?:reliably\s+|fully\s+)?"
            r"(?:answer|provide|confirm|determine|identify|say|verify|infer)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "speaker_did_not_find",
        re.compile(
            r"\b(?:i|we)\s+(?:did\s+not|didn't|could\s+not|couldn't)\s+"
            r"(?:find|locate|verify|identify)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "corpus_absence",
        re.compile(
            r"\b(?:the\s+)?(?:selected|provided|retrieved|available)?\s*"
            r"(?:corpus|context|sources?|documents?|evidence|material|passages?)\s+"
            r"(?:do(?:es)?\s+not|doesn't|don't|cannot|can't|fail(?:s)?\s+to)\s+"
            r"(?:directly\s+)?(?:address|answer|contain|cover|describe|detail|"
            r"establish|include|mention|name|provide|recommend|specify|state|"
            r"support|verify)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "no_evidence",
        re.compile(
            r"\b(?:there\s+is\s+)?(?:no|not\s+enough|insufficient|inadequate)\s+"
            r"(?:source[- ]backed\s+)?(?:evidence|information|material|support|"
            r"context|detail|mention)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "speaker_lacks_information",
        re.compile(
            r"\b(?:i|we)\s+(?:do\s+not|don't)\s+have\s+(?:enough\s+)?"
            r"(?:source[- ]backed\s+)?(?:evidence|information|context|support)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "no_source_mentions",
        re.compile(
            r"\bno\s+(?:selected\s+|provided\s+|retrieved\s+|available\s+)?"
            r"(?:source|document|passage)\s+(?:addresses|answers|contains|covers|"
            r"describes|establishes|includes|mentions|names|provides|specifies|"
            r"states|supports)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "corpus_provides_no_information",
        re.compile(
            r"\b(?:the\s+)?(?:selected|provided|retrieved|available)?\s*"
            r"(?:corpus|context|sources?|documents?|evidence|material|passages?)\s+"
            r"(?:provides?|contains?|has)\s+no\s+(?:evidence|information|detail|"
            r"mention|support)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "not_in_scope",
        re.compile(
            r"\b(?:this|that|the\s+(?:answer|information|detail|claim|topic))\s+"
            r"(?:is|was)\s+not\s+(?:available|covered|established|included|"
            r"mentioned|present|provided|specified|stated|supported)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "outside_sources",
        re.compile(
            r"\b(?:outside|beyond)\s+(?:the\s+)?(?:selected|provided|retrieved)?\s*"
            r"(?:corpus|context|sources?|documents?|evidence|material)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "absent_from_corpus",
        re.compile(
            r"\b(?:(?:this|that|the)\s+(?:answer|information|detail|claim|topic))\s+"
            r"(?:is|was)\s+not\s+in\s+(?:the\s+)?"
            r"(?:selected|provided|retrieved|available)?\s*"
            r"(?:corpus|context|sources?|documents?|evidence|material)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "sources_silent",
        re.compile(
            r"\b(?:the\s+)?(?:selected|provided|retrieved|available)?\s*"
            r"(?:corpus|sources?|documents?|evidence|material)\s+"
            r"(?:is|are|remain(?:s)?)\s+silent\s+(?:about|on|regarding)\b",
            re.IGNORECASE,
        ),
    ),
)

_COURTESY_RE = re.compile(
    r"^(?:i(?:'m| am)\s+)?(?:sorry|apologize)|^apologies\b|^unfortunately\b|"
    r"^i(?:'m| am)\s+afraid\b",
    re.IGNORECASE,
)
_NON_SUBSTANTIVE_CLAUSES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^(?:please\s+)?(?:provide|share|add|select|upload)\b.*"
        r"(?:source|document|context|material|evidence).*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:if|once|when)\b.*(?:provide|share|add|select|upload)\b.*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:then\s+)?(?:i|we)\s+(?:can|could|would)\s+"
        r"(?:answer|help|review|summarize|check|analyze)\b.*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:answering|confirming|determining)\b.*\b(?:would\s+)?require(?:s)?\b"
        r".*(?:outside|additional|other|new)\b.*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:without|based\s+only\s+on)\b.*(?:source|corpus|context|evidence).*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:the\s+)?(?:nearest|closest)\s+(?:retrieved\s+)?material\s+"
        r"(?:comes?|is)\s+from\b.*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:sources?|documents?|materials?)\s+"
        r"(?:checked|consulted|retrieved|selected|available|used)\b.*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:the\s+)?(?:available|selected|retrieved)\s+"
        r"(?:sources?|documents?|materials?)\s+(?:are|include)\b.*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^the\s+retrieval\s+found\s+(?:some\s+)?related\s+material\b.*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:[\w .,'’()\-]+\.(?:pdf|md|txt|docx?))(?:\s*[,;/]\s*"
        r"[\w .,'’()\-]+\.(?:pdf|md|txt|docx?))*$",
        re.IGNORECASE,
    ),
)
_SOURCE_LIST_SPAN_RE = re.compile(
    r"(?:the\s+(?:nearest|closest)\s+(?:retrieved\s+)?material\s+"
    r"(?:comes?|is)\s+from|sources?\s+(?:checked|consulted|retrieved|selected|"
    r"available|used)|the\s+(?:available|selected|retrieved)\s+"
    r"(?:sources?|documents?|materials?)\s+(?:are|include))\s*:\s*"
    r"[^\n.!?]*(?:\.(?:pdf|md|txt|docx?))?[^\n.!?]*",
    re.IGNORECASE,
)
_CLAUSE_SPLIT_RE = re.compile(
    r"(?:[.!?]\s+|\n+|;\s*|:\s+|,\s*(?=(?:but|however|although|though|yet|while|"
    r"because|despite|nevertheless)\b)|\s+\b(?:but|however|although|though|yet|"
    r"while|because|despite|nevertheless)\b\s+|,\s+and\s+)",
    re.IGNORECASE,
)
_REFUSAL_RESIDUAL_ASSERTION_RE = re.compile(
    r"\b(?:am|is|are|was|were|be|been|being|has|have|had|does|do|did|will|"
    r"would|should|could|must|states?|shows?|says?|recommends?|requires?|"
    r"means?|causes?|uses?|published|developed|exaggerates?|equals?)\b",
    re.IGNORECASE,
)
_LEADING_DISCOURSE_RE = re.compile(
    r"^(?:(?:but|however|although|though|yet|while|because|despite|"
    r"nevertheless|and)\b[\s,:-]*)+",
    re.IGNORECASE,
)


def _clean_clause(value: str) -> str:
    value = re.sub(r"`{1,3}", "", value)
    value = re.sub(r"\[[^\]]+\]\([^)]+\)", " ", value)
    value = re.sub(r"^[\s>*#\-–—\d.)]+", "", value)
    value = _LEADING_DISCOURSE_RE.sub("", value)
    value = _COURTESY_RE.sub("", value)
    return re.sub(r"\s+", " ", value).strip(" \t\r\n,.:;-")


def _cue_ids(value: str) -> list[str]:
    return [cue_id for cue_id, pattern in REFUSAL_CUES if pattern.search(value)]


def _mask_refusal_cues(value: str) -> tuple[str, list[str]]:
    cue_ids: list[str] = []
    masked = list(value)
    for cue_id, pattern in REFUSAL_CUES:
        matches = list(pattern.finditer(value))
        if matches:
            cue_ids.append(cue_id)
        for match in matches:
            masked[match.start() : match.end()] = " " * (match.end() - match.start())
    return "".join(masked), cue_ids


def _mask_source_lists(value: str) -> str:
    masked = list(value)
    for match in _SOURCE_LIST_SPAN_RE.finditer(value):
        masked[match.start() : match.end()] = " " * (match.end() - match.start())
    return "".join(masked)


def _is_non_substantive(value: str, *, cue_removed: bool) -> bool:
    if not value:
        return True
    if any(pattern.fullmatch(value) for pattern in _NON_SUBSTANTIVE_CLAUSES):
        return True
    tokens = re.findall(r"[A-Za-z0-9]+", value)
    if len(tokens) < 2:
        return True
    if cue_removed and len(tokens) <= 12:
        # Objects and scope tails left by a removed refusal cue are not claims.
        # A predicate in that residual is an assertion and must remain visible.
        return _REFUSAL_RESIDUAL_ASSERTION_RE.search(value) is None
    return False


def classify_refusal(answer: str, *, model_skipped: bool) -> dict[str, Any]:
    """Classify an answer with a deterministic, assertion-preserving rule."""

    raw = str(answer or "")
    normalized = re.sub(r"\s+", " ", raw).strip()
    cue_ids = _cue_ids(normalized)
    if model_skipped:
        state = "gate_blocked"
        substantive: list[str] = []
    else:
        source_lists_masked = _mask_source_lists(raw)
        clauses: list[tuple[str, bool]] = []
        for part in _CLAUSE_SPLIT_RE.split(source_lists_masked):
            cleaned_original = _clean_clause(part)
            if not cleaned_original:
                continue
            masked, local_cues = _mask_refusal_cues(cleaned_original)
            cleaned_residual = _clean_clause(masked)
            clauses.append((cleaned_residual, bool(local_cues)))
        substantive = [
            clause
            for clause, cue_removed in clauses
            if not _is_non_substantive(clause, cue_removed=cue_removed)
        ]
        state = (
            "model_voiced_refusal"
            if normalized and cue_ids and not substantive
            else "answered"
        )
    return {
        "version": CLASSIFIER_VERSION,
        "state": state,
        "refused": state in {"gate_blocked", "model_voiced_refusal"},
        "refusal_cue_ids": cue_ids,
        "substantive_clause_count": len(substantive),
        "substantive_clause_excerpts": [value[:160] for value in substantive[:4]],
    }


def validate_chat_trace_contract(
    traces: Sequence[dict[str, Any]],
    done_events: Sequence[dict[str, Any]],
    *,
    expected_model: str = EXPECTED_CHAT_MODEL,
) -> dict[str, Any]:
    """Validate the exact final-trace/model-route/done-event agreement."""

    errors: list[str] = []
    final_traces = [
        trace for trace in traces if trace.get("title") == "Assistant final answer"
    ]
    final_metadata: dict[str, Any] = {}
    model_skipped: bool | None = None
    if len(final_traces) != 1:
        errors.append(
            f"assistant final trace count must be 1, observed {len(final_traces)}"
        )
    else:
        metadata = final_traces[0].get("metadata")
        if not isinstance(metadata, dict):
            errors.append("assistant final trace metadata must be an object")
        else:
            final_metadata = metadata
            value = metadata.get("model_skipped")
            if type(value) is not bool:
                errors.append("assistant final trace model_skipped must be boolean")
            else:
                model_skipped = value

    route_traces = [
        trace for trace in traces if trace.get("title") == "Chat model route"
    ]
    route_metadata: dict[str, Any] = {}
    route_model: str | None = None
    if len(route_traces) != 1:
        errors.append(
            f"chat model route trace count must be 1, observed {len(route_traces)}"
        )
    else:
        metadata = route_traces[0].get("metadata")
        if not isinstance(metadata, dict):
            errors.append("chat model route metadata must be an object")
        else:
            route_metadata = metadata
            route_model = str(metadata.get("model") or "")
            if route_model != expected_model:
                errors.append(
                    f"chat model route mismatch: {route_model!r} != {expected_model!r}"
                )

    done_models = [
        str(event.get("model_used"))
        for event in done_events
        if event.get("model_used") not in (None, "")
    ]
    mismatched_done = sorted({model for model in done_models if model != route_model})
    if mismatched_done:
        errors.append(
            "done/route model mismatch: "
            + ", ".join(repr(model) for model in mismatched_done)
        )

    return {
        "ok": not errors,
        "errors": errors,
        "assistant_final_trace_count": len(final_traces),
        "assistant_final_metadata": final_metadata,
        "model_skipped": model_skipped,
        "model_route_trace_count": len(route_traces),
        "model_route": {
            "model": route_model,
            "web_planner_split": route_metadata.get("web_planner_split"),
        },
        "expected_model": expected_model,
        "done_event_count": len(done_events),
        "done_models": done_models,
    }
