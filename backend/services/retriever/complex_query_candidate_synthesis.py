"""Dark candidate synthesis — generated for comparison, never user-visible."""

from __future__ import annotations

import time
from typing import Any

import re

from services.retriever.complex_query_candidate_adoption import (
    format_packet_for_synthesis,
    verify_generated_answer,
)

_CITATION_RE = re.compile(
    r"\b(?:doc_[a-zA-Z0-9_]+|[a-f0-9]{16,}(?:_[a-z0-9_]+)+)\b"
)

SHARED_SYSTEM_PROMPT = (
    "You are answering from a grounded evidence packet. "
    "Every factual sentence MUST include at least one evidence id in parentheses, "
    "using the exact chunk_id values from the evidence list "
    "(example: The handoff precedes the scraper (abc123_0000).). "
    "Do not invent Microsoft→combat links. "
    "Keep Information Retrieval and Infrared distinct. "
    "Disclose contradictions when present. "
    "If evidence is insufficient, say so explicitly and do not speculate."
)
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 900


async def _resolve_synthesis_route(
    *,
    model: str,
    user_id: str,
    api_base: str | None,
    api_key: str | None,
    extra_params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve pool:/profile: strings into concrete LiteLLM route credentials."""

    route = {
        "model": model,
        "api_base": api_base,
        "api_key": api_key,
        "extra_params": dict(extra_params or {}),
    }
    raw = str(model or "").strip()
    if raw.startswith("pool:") or raw.startswith("profile:"):
        entry_id = raw.split(":", 1)[1].strip()
        try:
            from services.query_model_resolver import resolve_by_entry_id

            resolved = await resolve_by_entry_id(user_id, entry_id)
            if resolved:
                route = {
                    "model": resolved.get("model") or model,
                    "api_base": resolved.get("api_base") or api_base,
                    "api_key": resolved.get("api_key") or api_key,
                    "extra_params": dict(
                        resolved.get("extra_params") or extra_params or {}
                    ),
                    "entry_id": entry_id,
                }
        except Exception as exc:  # noqa: BLE001
            route["resolve_error"] = f"{type(exc).__name__}: {exc}"[:200]
    return route


def _format_evidence(chunks: list[dict[str, Any]], *, label: str) -> str:
    lines: list[str] = []
    for i, ch in enumerate(chunks[:12], 1):
        cid = str(ch.get("chunk_id") or "")
        text = str(ch.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"[{i}] id={cid}\n{text[:900]}")
    return "\n\n".join(lines) or f"(no {label} evidence text)"


async def _complete_once(
    *,
    llm_service: Any,
    route: dict[str, Any],
    model: str,
    system: str,
    user: str,
) -> tuple[str, str | None]:
    try:
        answer = await llm_service.complete_sync(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            model=str(route.get("model") or model),
            api_base=route.get("api_base"),
            api_key=route.get("api_key"),
            temperature=DEFAULT_TEMPERATURE,
            max_tokens=DEFAULT_MAX_TOKENS,
            extra_params=route.get("extra_params"),
        )
        if isinstance(answer, dict):
            answer = str(answer.get("content") or answer.get("text") or "")
        return str(answer or ""), None
    except Exception as exc:  # noqa: BLE001
        return "", f"{type(exc).__name__}: {exc}"[:400]


async def _synthesize_and_reverify(
    *,
    llm_service: Any,
    route: dict[str, Any],
    model: str,
    query: str,
    chunks: list[dict[str, Any]],
    context_packet: dict[str, Any],
    query_class: str,
    lane: str,
) -> dict[str, Any]:
    """generate → verify → revise → regenerate → reverify → pass|block."""

    packet_block = format_packet_for_synthesis(context_packet or {})
    evidence = _format_evidence(chunks, label=lane)
    user = (
        f"{packet_block}\n\n"
        f"Question: {query}\n\n"
        f"{lane.title()} evidence:\n{evidence}\n\n"
        "Answer with grounded claims and evidence ids."
    )
    answer, error = await _complete_once(
        llm_service=llm_service,
        route=route,
        model=model,
        system=SHARED_SYSTEM_PROMPT,
        user=user,
    )
    ids = [str(ch.get("chunk_id") or "") for ch in chunks if ch.get("chunk_id")]
    verification = verify_generated_answer(
        answer=answer,
        candidate_finalist_ids=ids,
        context_packet=context_packet,
        query_class=query_class,
    )
    attempts = [{"stage": "initial", "verification": verification.model_dump()}]
    regenerated = False
    if verification.verification_status == "revise" and answer and not error:
        regenerated = True
        fix_user = (
            f"{user}\n\n"
            f"Previous draft (must revise):\n{answer[:1800]}\n\n"
            "Remove or rewrite every unsupported sentence. "
            "Keep only claims grounded in the evidence ids above. "
            "If a claim cannot be grounded, omit it or state insufficiency."
        )
        answer2, error2 = await _complete_once(
            llm_service=llm_service,
            route=route,
            model=model,
            system=SHARED_SYSTEM_PROMPT,
            user=fix_user,
        )
        if not error2 and answer2.strip():
            answer = answer2
            error = None
        else:
            error = error2 or error
        verification = verify_generated_answer(
            answer=answer,
            candidate_finalist_ids=ids,
            context_packet=context_packet,
            query_class=query_class,
        )
        attempts.append(
            {"stage": "regenerated", "verification": verification.model_dump()}
        )

    # Deterministic salvage: keep only sentences that already carry citations.
    # Avoids shipping residual unsupported claims without another LLM call.
    if (
        answer
        and not error
        and (
            verification.verification_status in {"revise", "block"}
            or int(verification.claims_unsupported or 0) > 0
        )
    ):
        kept = [
            s.strip()
            for s in re.split(r"(?<=[.!?])\s+", answer)
            if len(s.strip()) > 20 and _CITATION_RE.search(s)
        ]
        if kept:
            answer = " ".join(kept)
            verification = verify_generated_answer(
                answer=answer,
                candidate_finalist_ids=ids,
                context_packet=context_packet,
                query_class=query_class,
            )
            attempts.append(
                {"stage": "citation_salvage", "verification": verification.model_dump()}
            )

    # Revise is non-terminal: residual unsupported after salvage → block.
    if (
        verification.verification_status == "revise"
        or int(verification.claims_unsupported or 0) > 0
    ):
        verification = verification.model_copy(
            update={"verification_status": "block"}
        ).with_hash()

    terminal_pass = (
        verification.verification_status == "pass"
        and int(verification.claims_unsupported or 0) == 0
        and not error
    )
    return {
        "lane": lane,
        "answer": answer,
        "answer_chars": len(answer),
        "error": error,
        "verification": verification.model_dump(),
        "terminal_pass": terminal_pass,
        "regenerated": regenerated,
        "attempts": attempts,
    }


async def run_dark_candidate_synthesis(
    *,
    query: str,
    candidate_chunks: list[dict[str, Any]],
    context_packet: dict[str, Any],
    model: str,
    user_id: str = "",
    api_base: str | None = None,
    api_key: str | None = None,
    extra_params: dict[str, Any] | None = None,
    query_class: str = "",
    baseline_chunks: list[dict[str, Any]] | None = None,
    pair_baseline: bool = False,
) -> dict[str, Any]:
    """Synthesize candidate (and optionally paired baseline) answers.

    Result is stored for comparison only — callers must not stream it to users
    unless a separate visible-enablement flag authorizes it after terminal pass.
    """

    from services.llm import llm_service

    started = time.perf_counter()
    route = await _resolve_synthesis_route(
        model=model,
        user_id=user_id,
        api_base=api_base,
        api_key=api_key,
        extra_params=extra_params,
    )

    candidate = await _synthesize_and_reverify(
        llm_service=llm_service,
        route=route,
        model=model,
        query=query,
        chunks=list(candidate_chunks or []),
        context_packet=context_packet,
        query_class=query_class,
        lane="candidate",
    )

    baseline: dict[str, Any] | None = None
    if pair_baseline and baseline_chunks is not None:
        baseline = await _synthesize_and_reverify(
            llm_service=llm_service,
            route=route,
            model=model,
            query=query,
            chunks=list(baseline_chunks or []),
            context_packet=context_packet,
            query_class=query_class,
            lane="baseline",
        )

    # Failed candidate reverification → fall back to baseline answer (dark).
    used_baseline_fallback = False
    usable = candidate
    if not candidate.get("terminal_pass"):
        if baseline and (
            baseline.get("terminal_pass")
            or baseline.get("answer")
        ):
            usable = baseline
            used_baseline_fallback = True

    return {
        "returned_to_user": False,
        "baseline_answer_remains_visible": True,
        "model": model,
        "answer": usable.get("answer") or "",
        "answer_chars": int(usable.get("answer_chars") or 0),
        "error": candidate.get("error"),
        "synthesis_ms": round((time.perf_counter() - started) * 1000.0, 2),
        "verification": usable.get("verification") or {},
        "candidate": candidate,
        "baseline": baseline,
        "used_baseline_fallback": used_baseline_fallback,
        "terminal_pass": bool(usable.get("terminal_pass")),
        "context_packet_hash": (context_packet or {}).get("context_hash"),
        "user_id": user_id,
        "paired": bool(baseline is not None),
        "controls": {
            "temperature": DEFAULT_TEMPERATURE,
            "max_tokens": DEFAULT_MAX_TOKENS,
            "system_prompt": SHARED_SYSTEM_PROMPT,
        },
    }
