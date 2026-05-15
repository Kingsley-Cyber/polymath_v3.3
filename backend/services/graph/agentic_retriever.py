"""Sprint #3 — Agentic retrieval loop.

The default Polymath retrieval path is one-shot: embed query → retrieve K
chunks → extract seeds → walk 2 hops → synthesize. This module adds a
bounded multi-round loop that lets the LLM ask for MORE evidence about
specific entities after seeing the initial packet:

    Round 1: standard retrieval (already done by caller)
    Round 2-N: LLM picks 1-3 entities to deepen → re-retrieve on those
               targeted seeds → merge into the packet
    Final:   synthesis sees the merged super-packet

Bounded by:
  - MAX_ROUNDS (default 3) — hard cap on iterations
  - Empty entity selection — if the LLM returns no entities to deepen,
    the loop exits early
  - LLM transport failure — exits with what it has, never raises

The loop is gated by `agentic=True` on GraphDiscoverRequest (existing
flag, previously only used as a label). The cost is roughly:
  base synthesis + N rounds × (selection LLM call + targeted retrieval)
so a 3-round loop costs ~4 LLM calls. Latency: ~10-15s for full agentic.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Hard ceiling on how many deepening rounds run, regardless of LLM
# behavior. The base packet counts as round 0; this constant counts
# ADDITIONAL rounds, so MAX_ROUNDS=3 means 4 total retrieval passes
# in the worst case.
MAX_ROUNDS = 3

# Cap on how many entities the LLM can ask to deepen per round. Three
# is enough to triangulate (subject, verb, object) but not so many that
# the targeted retrieval pool explodes.
MAX_ENTITIES_PER_ROUND = 3

# Per-round retrieval budget — how many additional chunks to fetch when
# deepening one entity. Kept small so the merged packet stays under the
# synthesis prompt budget.
PER_ENTITY_RETRIEVAL_K = 5


_SELECT_DEEPEN_PROMPT = (
    "You are Polymath's research strategist. You've just received an "
    "evidence packet that an analyst will synthesize into an answer. "
    "Your job: spot the 1-3 entities (concept names, API names, file "
    "paths, person names) the analyst would benefit from KNOWING MORE "
    "ABOUT before writing the answer.\n\n"
    "Rules:\n"
    "- Only name entities that are MENTIONED in the evidence packet "
    "  below. Do NOT invent new ones.\n"
    "- Prefer entities that appear in MULTIPLE evidence items — these "
    "  are the load-bearing concepts where more context is highest-ROI.\n"
    "- Skip entities that are already well-explained in the packet "
    "  (multiple paragraphs, clear definitions). The point is to fill "
    "  gaps, not pile on what's already covered.\n"
    "- If the packet has enough evidence to answer the user's query "
    "  without more retrieval, return an empty list. Quitting is a "
    "  valid choice.\n\n"
    "Output ONLY a JSON object, nothing else:\n"
    "{\n"
    "  \"entities\": [\"name1\", \"name2\"],\n"
    "  \"reason\": \"<one sentence on why these — what gap they close>\"\n"
    "}\n\n"
    "Maximum 3 entities. Each name MUST be a string that appears "
    "verbatim somewhere in the evidence packet."
)


async def select_entities_to_deepen(
    *,
    llm_service,
    packet: dict[str, Any],
    user_query: str,
    round_index: int,
    creds: dict[str, Any],
    timeout_seconds: float = 30.0,
) -> tuple[list[str], Optional[str]]:
    """Ask the LLM which 1-3 entities to deepen this round.

    Returns (entity_names, reason). Empty list = quit the loop.
    On any failure, returns ([], None) so the caller stops cleanly.
    """
    # Build a compact view of the packet for the strategist — enough
    # to spot under-covered entities but not the full synthesis payload.
    evidence = (packet.get("evidence") or [])[:12]
    edges = (packet.get("edges") or [])[:10]
    anchors = packet.get("anchors") or []

    user_msg_parts: list[str] = [
        f"Round {round_index} of {MAX_ROUNDS}.",
        f"User query: {user_query}",
        "",
    ]
    if anchors:
        user_msg_parts.append(
            "Anchor entities: " + ", ".join(str(a) for a in anchors[:5])
        )
        user_msg_parts.append("")

    user_msg_parts.append("Evidence items (truncated):")
    for i, item in enumerate(evidence, start=1):
        label = ""
        if isinstance(item, dict):
            src = item.get("source") or {}
            label = (
                (src.get("label") if isinstance(src, dict) else None)
                or item.get("source_label")
                or item.get("doc_id")
                or "source"
            )
            text = (item.get("text") or item.get("summary") or "")[:200]
        else:
            text = str(item)[:200]
        user_msg_parts.append(f"[{i}] {label}: {text}")
    user_msg_parts.append("")

    if edges:
        user_msg_parts.append("Entity relations (subject — predicate — object):")
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            s = edge.get("source_name") or edge.get("source") or edge.get("s") or "?"
            p = edge.get("predicate") or edge.get("p") or "?"
            t = edge.get("target_name") or edge.get("target") or edge.get("t") or "?"
            user_msg_parts.append(f"- {s} {p} {t}")
        user_msg_parts.append("")

    user_msg_parts.append(
        "Which entities would you deepen before the analyst writes the "
        "synthesis? Output JSON only."
    )
    user_msg = "\n".join(user_msg_parts)

    messages = [
        {"role": "system", "content": _SELECT_DEEPEN_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    extra: dict[str, Any] = dict(creds.get("extra_params") or {})

    try:
        raw = await llm_service.complete_sync(
            messages=messages,
            model=creds["model"],
            temperature=0.2,
            max_tokens=400,
            api_base=creds.get("api_base"),
            api_key=creds.get("api_key"),
            timeout=timeout_seconds,
            extra_params=extra,
        )
    except Exception as exc:
        logger.warning("agentic select-deepen LLM call failed: %s", exc)
        return [], None

    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        first_nl = cleaned.find("\n")
        if first_nl > 0:
            cleaned = cleaned[first_nl + 1:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        logger.info("agentic select-deepen: no JSON in response — quitting loop")
        return [], None
    try:
        data = json.loads(match.group(0))
    except Exception as exc:
        logger.warning("agentic select-deepen JSON parse failed: %s", exc)
        return [], None

    if not isinstance(data, dict):
        return [], None
    raw_entities = data.get("entities")
    if not isinstance(raw_entities, list):
        return [], None
    entities: list[str] = []
    seen: set[str] = set()
    for name in raw_entities[:MAX_ENTITIES_PER_ROUND]:
        s = str(name or "").strip()
        if not s or s.lower() in seen:
            continue
        seen.add(s.lower())
        # Defense: only accept entities that appear verbatim somewhere
        # in the packet. The prompt asks for this but a model might
        # still hallucinate. Cheap O(N) substring check across the
        # evidence + edge text we sent it.
        if not _entity_appears_in_packet(s, packet):
            logger.info(
                "agentic select-deepen: dropping hallucinated entity %r",
                s,
            )
            continue
        entities.append(s)
    reason = str(data.get("reason") or "").strip()[:200]
    logger.info(
        "agentic select-deepen round=%d entities=%s reason=%r",
        round_index, entities, reason,
    )
    return entities, reason


def _entity_appears_in_packet(entity_name: str, packet: dict[str, Any]) -> bool:
    """Quick substring check — is the entity name actually mentioned
    in any evidence chunk or edge of the packet? Protects against
    LLM hallucinating names that weren't in the input."""
    needle = entity_name.lower()
    for item in (packet.get("evidence") or [])[:24]:
        if not isinstance(item, dict):
            continue
        text = (item.get("text") or "") + " " + (item.get("summary") or "")
        if needle in text.lower():
            return True
    for edge in (packet.get("edges") or [])[:24]:
        if not isinstance(edge, dict):
            continue
        haystack = " ".join(
            str(edge.get(k) or "")
            for k in ("source", "target", "source_name", "target_name", "s", "t", "rationale")
        )
        if needle in haystack.lower():
            return True
    for anchor in packet.get("anchors") or []:
        if needle in str(anchor).lower():
            return True
    return False


async def run_agentic_loop(
    *,
    base_packet: dict[str, Any],
    user_query: str,
    creds: dict[str, Any],
    llm_service,
    retrieve_for_entity: Callable[[str], Any],
    max_rounds: int = MAX_ROUNDS,
) -> dict[str, Any]:
    """Run the bounded deepening loop. Returns the (merged) packet.

    ``retrieve_for_entity`` is a coroutine the caller supplies — it takes
    one entity name string and returns either:
      - a list of evidence-shaped dicts to merge into packet["evidence"]
      - an empty list to signal "no more evidence for this entity"
    Keeping retrieval injected makes this module unit-testable and
    decouples it from the specific retriever implementation.
    """
    merged = dict(base_packet)
    merged_evidence: list[dict[str, Any]] = list(merged.get("evidence") or [])
    trace: list[dict[str, Any]] = []
    deepened_entities: set[str] = set()

    # Dedup tracker — chunk_ids already in the packet. Any agentic-retrieved
    # item whose chunk_id is in this set is skipped. Without this, the
    # full-retriever rerouting (Sprint #3 follow-up) can re-surface chunks
    # that were already in the base retrieval pool because the entity name
    # has high vector similarity to the original query.
    seen_chunk_ids: set[str] = set()
    for item in merged_evidence:
        if isinstance(item, dict):
            cid = str(item.get("chunk_id") or "")
            if cid:
                seen_chunk_ids.add(cid)

    for round_index in range(1, max_rounds + 1):
        entities, reason = await select_entities_to_deepen(
            llm_service=llm_service,
            packet=merged,
            user_query=user_query,
            round_index=round_index,
            creds=creds,
        )
        if not entities:
            logger.info(
                "agentic loop: quitting at round=%d (no entities to deepen)",
                round_index,
            )
            trace.append({
                "round": round_index,
                "entities": [],
                "reason": reason or "",
                "exit": "no_entities",
            })
            break

        added_this_round = 0
        deduped_this_round = 0
        for entity in entities:
            if entity.lower() in deepened_entities:
                # Avoid loop-pumping on the same entity round after round.
                continue
            deepened_entities.add(entity.lower())
            try:
                new_evidence = await retrieve_for_entity(entity)
            except Exception as exc:
                logger.warning(
                    "agentic loop: retrieve_for_entity(%r) failed: %s",
                    entity, exc,
                )
                continue
            if not new_evidence:
                continue
            for item in new_evidence:
                if not isinstance(item, dict):
                    continue
                cid = str(item.get("chunk_id") or "")
                # Dedup against base packet AND prior agentic rounds.
                # An empty chunk_id (some retrievers may omit it) is
                # ALWAYS allowed through; we'd rather have a duplicate
                # than drop unique-but-unidentified content.
                if cid and cid in seen_chunk_ids:
                    deduped_this_round += 1
                    continue
                if cid:
                    seen_chunk_ids.add(cid)
                merged_evidence.append(item)
                added_this_round += 1

        trace.append({
            "round": round_index,
            "entities": entities,
            "reason": reason or "",
            "added_evidence": added_this_round,
            "deduped": deduped_this_round,
        })
        merged["evidence"] = merged_evidence

        if added_this_round == 0:
            logger.info(
                "agentic loop: quitting at round=%d (no new evidence)",
                round_index,
            )
            break

    merged["agentic_trace"] = trace
    merged["agentic_rounds_run"] = len(trace)
    return merged
