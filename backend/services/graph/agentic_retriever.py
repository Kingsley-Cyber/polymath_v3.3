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

_SELECT_DEEPEN_RESEARCH_PROMPT = (
    "You are Polymath's research strategist. Your job is to spot the 1-3 "
    "entities the analyst needs MORE EVIDENCE about before writing a precise, "
    "well-supported answer.\n\n"
    "Rules:\n"
    "- Prefer entities central to the user's query but thinly supported in the "
    "current evidence.\n"
    "- Prefer main thesis entities, under-supported claims, and missing proof "
    "around the central question.\n"
    "- Skip entities that are already well-covered. The goal is tighter proof, "
    "not more noise.\n"
    "- Only name entities that appear verbatim somewhere in the packet below. "
    "Do NOT invent new ones.\n\n"
    "Output ONLY a JSON object, nothing else:\n"
    "{\n"
    "  \"entities\": [\"name1\", \"name2\"],\n"
    "  \"reason\": \"<one sentence on why these close a proof gap>\"\n"
    "}\n\n"
    "Maximum 3 entities."
)

_SELECT_DEEPEN_IDEATION_PROMPT = (
    "You are Polymath's ideation scout. Your job is to spot the 1-3 bridges, "
    "gaps, analogies, transfers, or entities that would make the STRONGEST "
    "NEW IDEA more grounded.\n\n"
    "Rules:\n"
    "- Prefer structural components over raw entities. The goal is invention "
    "ingredients: bridges, gaps, analogies, transfers, and both sides of a "
    "promising combination.\n"
    "- For bridges, name BOTH endpoints as `A + B`.\n"
    "- For gaps, name the two sides as `A + B`.\n"
    "- For analogies, name source and target as `A + B`.\n"
    "- For transfers, name the hub and target domain/entity as `A + B`.\n"
    "- Use a raw entity only when it is the missing ingredient for a stronger "
    "idea.\n"
    "- Only name components that appear verbatim somewhere in the packet below. "
    "Do NOT invent new ones.\n\n"
    "Output ONLY a JSON object, nothing else:\n"
    "{\n"
    "  \"entities\": [\"Identity Map + User Profile\", \"Session Pacing\"],\n"
    "  \"reason\": \"<one sentence on why these improve the idea material>\"\n"
    "}\n\n"
    "Maximum 3 selections."
)

_SELECT_DEEPEN_GAP_PROMPT = (
    "You are Polymath's gap scout. The analyst will write a map of what the "
    "corpus does NOT yet connect. Your job is to spot the 1-3 entities or "
    "gap-endpoints to deepen so the analyst can tell a REAL absence from one "
    "that is merely under-retrieved.\n\n"
    "Rules:\n"
    "- Prefer the endpoints of the most credible gaps and fragile bridges "
    "below — deepen BOTH sides so the analyst can confirm whether the corpus "
    "truly never links them, or just hasn't surfaced the link yet.\n"
    "- For a gap or fragile bridge, name both sides as `A + B`.\n"
    "- Prefer endpoints with strong structural signals (high topology_sim / "
    "neighbor_jaccard, shared terms) but no asserted edge — those are where "
    "more evidence most changes the verdict.\n"
    "- Use a raw entity only when one side of a promising gap is thinly "
    "covered and needs grounding before the analyst can judge it.\n"
    "- Only name components that appear verbatim somewhere in the packet "
    "below. Do NOT invent new ones.\n\n"
    "Output ONLY a JSON object, nothing else:\n"
    "{\n"
    "  \"entities\": [\"Vector Index + Query Planner\", \"Cache Eviction\"],\n"
    "  \"reason\": \"<one sentence on why deepening these tests a candidate gap>\"\n"
    "}\n\n"
    "Maximum 3 selections."
)


def _select_prompt_for_mode(synthesis_mode: str) -> tuple[str, bool]:
    mode = (synthesis_mode or "").strip().lower()
    if mode == "ideation":
        return _SELECT_DEEPEN_IDEATION_PROMPT, True
    if mode == "research":
        return _SELECT_DEEPEN_RESEARCH_PROMPT, False
    if mode == "gap":
        return _SELECT_DEEPEN_GAP_PROMPT, True
    return _SELECT_DEEPEN_PROMPT, False


async def select_entities_to_deepen(
    *,
    llm_service,
    packet: dict[str, Any],
    user_query: str,
    round_index: int,
    creds: dict[str, Any],
    synthesis_mode: str = "nuance",
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
    prompt, show_structural = _select_prompt_for_mode(synthesis_mode)
    bridges = (packet.get("bridges") or [])[:5] if show_structural else []
    gaps = (packet.get("gaps") or [])[:5] if show_structural else []
    analogies = (packet.get("analogies") or [])[:5] if show_structural else []
    transfers = (packet.get("transfers") or [])[:5] if show_structural else []

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

    if show_structural:
        if bridges:
            user_msg_parts.append("Bridges (source + target):")
            for item in bridges:
                if not isinstance(item, dict):
                    continue
                s = item.get("source_name") or item.get("source") or "?"
                t = item.get("target_name") or item.get("target") or "?"
                kind = item.get("bridge_type") or "bridge"
                detail = item.get("rationale") or ", ".join(item.get("shared_terms") or [])
                user_msg_parts.append(f"- {s} + {t} ({kind}): {str(detail)[:180]}")
            user_msg_parts.append("")
        if gaps:
            user_msg_parts.append("Gaps (side A + side B):")
            for item in gaps:
                if not isinstance(item, dict):
                    continue
                sides = item.get("between") if isinstance(item.get("between"), list) else []
                a = (
                    item.get("cluster_a_label")
                    or item.get("entity_a_name")
                    or (sides[0] if len(sides) > 0 else "")
                    or item.get("cluster_a")
                    or "?"
                )
                b = (
                    item.get("cluster_b_label")
                    or item.get("entity_b_name")
                    or (sides[1] if len(sides) > 1 else "")
                    or item.get("cluster_b")
                    or "?"
                )
                gap_type = item.get("gap_type") or item.get("type") or "gap"
                question = item.get("question") or item.get("q") or ""
                user_msg_parts.append(f"- {a} + {b} ({gap_type}): {str(question)[:180]}")
            user_msg_parts.append("")
        if analogies:
            user_msg_parts.append("Analogies (source + target):")
            for item in analogies:
                if not isinstance(item, dict):
                    continue
                s = item.get("source_name") or item.get("source") or "?"
                t = item.get("target_name") or item.get("target") or "?"
                sim = item.get("topology_sim")
                sim_text = f", topology_sim={sim}" if sim is not None else ""
                rationale = item.get("rationale") or ""
                user_msg_parts.append(f"- {s} + {t}{sim_text}: {str(rationale)[:180]}")
            user_msg_parts.append("")
        if transfers:
            user_msg_parts.append("Transfers (hub + target domain/entity):")
            for item in transfers:
                if not isinstance(item, dict):
                    continue
                hub = item.get("hub_name") or item.get("hub") or "?"
                targets = item.get("target_domains") or [item.get("target_name") or item.get("target_domain") or item.get("target")]
                target_text = ", ".join(str(v) for v in targets if v) or "?"
                rationale = item.get("rationale") or ""
                user_msg_parts.append(f"- {hub} + {target_text}: {str(rationale)[:180]}")
            user_msg_parts.append("")

    user_msg_parts.append(
        "Which entities would you deepen before the analyst writes the "
        "synthesis? Output JSON only."
    )
    user_msg = "\n".join(user_msg_parts)

    messages = [
        {"role": "system", "content": prompt},
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
        if not _selection_appears_in_packet(s, packet):
            logger.info(
                "agentic select-deepen: dropping hallucinated selection %r",
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
    structural_fields = {
        "bridges": (
            "source",
            "target",
            "source_name",
            "target_name",
            "bridge_type",
            "rationale",
            "shared_terms",
        ),
        "gaps": (
            "cluster_a",
            "cluster_b",
            "cluster_a_label",
            "cluster_b_label",
            "entity_a_name",
            "entity_b_name",
            "question",
            "gap_type",
            "type",
            "between",
            "source_domain",
            "target_domain",
        ),
        "analogies": (
            "source",
            "target",
            "source_name",
            "target_name",
            "source_domain",
            "target_domain",
            "rationale",
        ),
        "transfers": (
            "hub",
            "hub_name",
            "hub_domain",
            "target",
            "target_name",
            "target_domain",
            "target_domains",
            "rationale",
            "analogs",
        ),
    }
    for section, fields in structural_fields.items():
        for item in (packet.get(section) or [])[:24]:
            if not isinstance(item, dict):
                continue
            haystack = " ".join(_stringify_structural_value(item.get(field)) for field in fields)
            if needle in haystack.lower():
                return True
    return False


def _stringify_structural_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(_stringify_structural_value(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_stringify_structural_value(v) for v in value)
    return str(value)


def _split_deepen_selection(selection: str) -> list[str]:
    raw = str(selection or "").strip()
    if not raw:
        return []
    parts = [
        part.strip(" \t\r\n`'\"")
        for part in re.split(r"\s*(?:\+|<->|↔)\s*", raw)
        if part.strip(" \t\r\n`'\"")
    ]
    return parts or [raw]


def _selection_appears_in_packet(selection: str, packet: dict[str, Any]) -> bool:
    parts = _split_deepen_selection(selection)
    if not parts:
        return False
    return all(_entity_appears_in_packet(part, packet) for part in parts)


async def run_agentic_loop(
    *,
    base_packet: dict[str, Any],
    user_query: str,
    creds: dict[str, Any],
    llm_service,
    retrieve_for_entity: Callable[[str], Any],
    synthesis_mode: str = "nuance",
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
            synthesis_mode=synthesis_mode,
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
            entity_parts = _split_deepen_selection(entity)
            if not entity_parts:
                continue
            if all(part.lower() in deepened_entities for part in entity_parts):
                # Avoid loop-pumping on the same entity round after round.
                continue
            for part in entity_parts:
                if part.lower() in deepened_entities:
                    continue
                deepened_entities.add(part.lower())
                try:
                    new_evidence = await retrieve_for_entity(part)
                except Exception as exc:
                    logger.warning(
                        "agentic loop: retrieve_for_entity(%r) failed: %s",
                        part,
                        exc,
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
