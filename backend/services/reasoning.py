"""
Reasoning modes (Phase 15) — synthesis layer that affects LLM output.

Two mechanisms under one interface:
  1. Prompt-only modes — prepend a template to the system prompt. Zero extra
     LLM calls. Changes how the model thinks.
  2. Retrieval/pipeline modes (atomic, self_correct) — mutate the pipeline:
     decompose queries into sub-queries, or generate → review → revise.

12 curated modes for the primary UI dropdown; 40 raw modes available as a
power-user blend pool (concatenated into the prompt alongside the main mode).

Ports the design from Reference/Reasoning.py with Polymath-native signatures:
  - LLM calls go through llm_service.complete_sync (single-turn, non-streaming)
  - atomic_retrieve wraps retriever_orchestrator.retrieve (Polymath's entry point)
  - self_correct_retrieve takes an `emit` async callback for streaming critique chunks
"""

from __future__ import annotations

import asyncio
import json
import logging
from enum import Enum

from models.schemas import RetrievalResult, RetrievalTier, SourceChunk, SourceFact

logger = logging.getLogger(__name__)


# ── 12 curated modes (primary UI dropdown) ──────────────────────────────────


class ReasoningMode(str, Enum):
    NONE = "none"
    STEP_BY_STEP = "step_by_step"
    BRANCHING = "branching"
    CREATIVE = "creative"
    ANALYTICAL = "analytical"
    SELF_CORRECT = "self_correct"
    ATOMIC = "atomic"
    PLANNING = "planning"
    GRAPH_REASON = "graph_reason"
    DEBATE = "debate"
    DEEP_RESEARCH = "deep_research"
    CONCISE = "concise"
    META = "meta"


REASONING_TEMPLATES: dict[str, str] = {
    "none": "",
    # ── 12 curated modes ──
    "step_by_step": (
        "Think through this step by step with deliberate care. Show each reasoning "
        "step explicitly. Examine each step for validity before proceeding to the next. "
        "If a step seems wrong, backtrack and correct it before continuing.\n\n"
    ),
    "branching": (
        "Consider multiple possible approaches to this question. For each approach, "
        "briefly explore where it leads. Evaluate which branches are most promising "
        "based on the evidence, prune the weaker ones, and present the strongest "
        "path with your reasoning for choosing it.\n\n"
    ),
    "creative": (
        "Approach this from unexpected angles. Consider unconventional interpretations "
        "and lateral connections. Generate multiple diverse perspectives before "
        "converging on your answer. Prioritize novel insight over obvious conclusions.\n\n"
    ),
    "analytical": (
        "Break this into its smallest independent claims. For each claim, evaluate "
        "the evidence for and against it. Rank claims by strength of evidence. "
        "Build your answer only from well-supported claims, explicitly noting "
        "any that lack sufficient evidence.\n\n"
    ),
    "self_correct": (
        "Answer this question, then immediately review your own answer for errors. "
        "Check if the evidence actually supports each claim you made. If you find "
        "issues, revise your answer. Show both the original reasoning and any "
        "corrections you made.\n\n"
    ),
    "planning": (
        "Before answering, create an explicit plan: what sub-questions need answering, "
        "in what order, and what would constitute a good answer to each. Then execute "
        "each step of your plan systematically. Decompose goals into sub-goals until "
        "each is directly answerable.\n\n"
    ),
    "graph_reason": (
        "Trace the conceptual connections between the retrieved sources. Map out how "
        "entities, ideas, and claims relate to each other — what causes what, what "
        "depends on what, what contradicts what. Synthesize your answer by walking "
        "the relationship graph you've constructed.\n\n"
    ),
    "debate": (
        "Argue this from at least two opposing perspectives. For each position, "
        "present the strongest possible case using the evidence. Then identify where "
        "the positions agree, where they genuinely conflict, and synthesize a balanced "
        "conclusion that acknowledges the strongest points from each side.\n\n"
    ),
    "deep_research": (
        "Decompose this into sub-problems from simplest to hardest. Solve the foundational "
        "questions first, then use those answers to tackle progressively harder ones. "
        "Each answer should build on the previous. Show how your understanding compounds "
        "as you work through the layers.\n\n"
    ),
    "concise": (
        "Be maximally concise. One key insight per sentence. No hedging, no filler, "
        "no unnecessary elaboration. If a shorter word works, use it. Compress your "
        "reasoning to its essential signal.\n\n"
    ),
    "meta": (
        "Before answering, briefly assess what type of question this is and what "
        "reasoning approach would produce the best answer. Then apply that approach. "
        "You are free to choose any thinking style — step-by-step, branching, "
        "creative, analytical, or any combination — based on what the question demands.\n\n"
    ),
    # ── 40 raw modes (power-user blend pool) ──
    # Sequential / Chain
    "chain_of_thought": "Think through this step by step. Show your reasoning before giving the answer.\n\n",
    "self_consistent_cot": "Think through this multiple ways. Try at least two different reasoning paths, then select the answer that is most consistent across paths.\n\n",
    "deliberate_cot": "Reason slowly and intentionally. Before moving to each next step, verify the current step is correct. Prioritize correctness over speed.\n\n",
    "react": "Alternate between reasoning and information-gathering. State what you think, what you need to verify, and what action you'd take, then reason about the result.\n\n",
    "atomic_thoughts": "Decompose this into the smallest indivisible claims. Verify each claim independently against the evidence before combining them.\n\n",
    "micro_cot": "One sentence per reasoning step. No elaboration. Be maximally compressed.\n\n",
    # Branching / Tree
    "tree_of_thought": "Explore multiple reasoning branches in parallel. For each branch, evaluate its promise. Prune weak branches early and expand promising ones.\n\n",
    "guided_tot": "Explore multiple approaches, but score each branch at every step using this heuristic: how well does it address the core question? Expand only top-scoring branches.\n\n",
    "monte_carlo_tot": "Generate several random candidate approaches. For each, mentally simulate where it leads. Select the approach with the best projected outcome.\n\n",
    "beam_search_tot": "Keep the top 2-3 most promising reasoning paths at each step. Expand only those, discarding the rest. Converge on the single best path.\n\n",
    "reflexion_tot": "After each reasoning attempt, reflect on what went wrong or right. Feed that reflection into your next attempt. Improve iteratively.\n\n",
    "program_aided_tot": "Express your reasoning as if writing pseudocode or a logical program. Use structured logic rather than prose to evaluate each branch.\n\n",
    # Graph / Network
    "graph_of_thought": "Let your thoughts form a non-linear graph. Ideas can merge, split, and loop back. Draw connections between separate insights and synthesize where they converge.\n\n",
    "dynamic_graph": "Build your reasoning as an evolving graph. As you learn more, restructure your thinking — add new connections, remove wrong ones, re-route your logic.\n\n",
    "kg_augmented": "Ground your reasoning in structured relationships: entity → relation → entity. Map out the knowledge structure before reasoning over it.\n\n",
    # Self-Correction / Refinement
    "reflexion": "Generate your answer, then produce explicit self-critique. What did you get wrong? What's weak? Then revise based on your critique.\n\n",
    "self_refine": "Generate a first draft, critique it, refine it, critique again, refine again. At least two passes of improvement before your final answer.\n\n",
    "multi_agent_debate": "Simulate three perspectives arguing about this: an optimist, a skeptic, and a pragmatist. Let them debate, then synthesize the consensus.\n\n",
    # Tool-Augmented
    "toolformer": "As you reason, identify moments where a calculation, lookup, or verification would be more reliable than inference. Note where tools would help.\n\n",
    "program_of_thought": "Express your reasoning AS a program. The logic, variables, and control flow of your thinking should be explicit and executable.\n\n",
    "scratchpad": "Maintain a visible working memory. Write down intermediate results, refer back to them, and track multiple variables as you reason.\n\n",
    # Planning / Decomposition
    "plan_and_solve": "First generate an explicit plan with numbered steps. Then execute each step in order, checking off as you go.\n\n",
    "least_to_most": "Identify the easiest sub-problem first. Solve it. Use that answer to unlock the next harder sub-problem. Build up to the full answer.\n\n",
    "goal_tree": "Decompose your goal into sub-goals, and sub-goals into sub-sub-goals, until each leaf is directly actionable. Then execute bottom-up.\n\n",
    "plan_and_execute": "Generate a full execution plan with explicit checkpoints. Execute each phase, verify at each checkpoint before proceeding.\n\n",
    # Agentic / Loop
    "prar_loop": "Follow this cycle: Perceive (what do I know?), Reason (what does it mean?), Act (what should I do?), Reflect (did that work?). Repeat as needed.\n\n",
    "tool_use_reasoning": "Reason explicitly about which tools or methods to use, why each is appropriate, and what to do with their outputs.\n\n",
    "memory_augmented": "Draw on all available context — prior conversation, stored knowledge, established patterns. Let memory inform your current reasoning.\n\n",
    # Stochastic / Sampling
    "monte_carlo_sampling": "Generate many diverse candidate answers. Score each for quality and relevance. Select the best one and explain why it won.\n\n",
    "stochastic_exploration": "Intentionally explore unexpected directions. Break out of obvious reasoning patterns. Look for angles you wouldn't normally consider.\n\n",
    "hypothesis_ranking": "Generate 3-5 hypotheses. For each, list supporting and contradicting evidence. Rank by plausibility. Proceed with the top-ranked hypothesis.\n\n",
    "self_consistent_sampling": "Generate multiple independent answers. Select the one with the highest internal consistency — where every part supports every other part.\n\n",
    # Hybrid / Advanced
    "graphrag_integrated": "Combine graph-structured knowledge with generative reasoning. Pull structured relationships, then reason over them as a connected whole.\n\n",
    "modular_pipelines": "Break this into specialized subtasks. Handle each with the most appropriate method. Chain the outputs: each module feeds the next.\n\n",
    "thought_distillation": "After thorough reasoning, compress everything to its essential insight. Remove all scaffolding. Keep only the signal.\n\n",
    "multimodal_integration": "Reason across multiple representations simultaneously — text, structure, logic, examples. Let different framings reinforce each other.\n\n",
    "recursive_introspection": "Repeatedly examine your own reasoning process. Am I reasoning correctly? Am I making assumptions? Check your thinking about your thinking.\n\n",
    "meta_reasoning": "Before reasoning, decide HOW to reason. What strategy fits this problem? Choose deliberately, then execute that strategy.\n\n",
    "dynamic_routing": "Start reasoning with your best guess at an approach. If it stops working, explicitly switch to a different approach and explain why.\n\n",
    "hybrid_agentic": "Combine planning, tool awareness, self-reflection, and context memory into a unified approach. Use whatever cognitive tool the moment demands.\n\n",
}


def apply_reasoning(
    prompt: str,
    mode: str | None = "none",
    blend: list[str] | None = None,
) -> str:
    """
    Prepend reasoning template(s) to `prompt`. Used by context_manager after
    assembling the RAG context + synthesis instruction.

    Blend parts (if any) come FIRST, then the main mode, then the prompt.
    """
    parts: list[str] = []
    if blend:
        for key in blend:
            t = REASONING_TEMPLATES.get(key, "")
            if t:
                parts.append(t)
    if mode and mode != "none":
        t = REASONING_TEMPLATES.get(mode, "")
        if t:
            parts.append(t)
    if not parts:
        return prompt
    return "".join(parts) + prompt


# ── Retrieval-level mode detection ──────────────────────────────────────────


_RETRIEVAL_MODES = {ReasoningMode.ATOMIC.value, ReasoningMode.SELF_CORRECT.value}


def is_retrieval_mode(mode: str | None) -> bool:
    """True for modes that alter retrieval (atomic) or answer generation (self_correct)."""
    return (mode or "none") in _RETRIEVAL_MODES


# ── Atomic decomposition retrieval ─────────────────────────────────────────


def _parse_json_array(text: str) -> list[str]:
    """Best-effort JSON array extraction from LLM output."""
    s = text.strip()
    try:
        val = json.loads(s)
        if isinstance(val, list):
            return [str(x) for x in val]
    except json.JSONDecodeError:
        pass
    start = s.find("[")
    end = s.rfind("]")
    if start != -1 and end > start:
        try:
            val = json.loads(s[start : end + 1])
            if isinstance(val, list):
                return [str(x) for x in val]
        except json.JSONDecodeError:
            pass
    return []


def _parse_json_object(text: str) -> dict:
    """Best-effort JSON object extraction from LLM output."""
    s = text.strip()
    try:
        val = json.loads(s)
        if isinstance(val, dict):
            return val
    except json.JSONDecodeError:
        pass
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end > start:
        try:
            val = json.loads(s[start : end + 1])
            if isinstance(val, dict):
                return val
        except json.JSONDecodeError:
            pass
    return {}


async def atomic_retrieve(
    query: str,
    corpus_ids: list[str] | None,
    retrieval_tier: RetrievalTier,
    collections: list[str] | None,
    model: str,
) -> RetrievalResult:
    """
    Decompose `query` into sub-questions via LLM, retrieve each in parallel,
    merge+dedupe by chunk_id.

    Always returns a RetrievalResult (same shape as retriever_orchestrator.retrieve)
    so the orchestrator can treat the output identically.
    """
    from services.llm import llm_service
    from services.retriever import retriever_orchestrator
    from services.retriever.merge import merge_pools

    decompose_prompt = (
        "Break this question into 2-4 independent sub-questions that together "
        "cover the original. Return ONLY a JSON array of strings, nothing else.\n\n"
        f"Question: {query}"
    )
    try:
        raw = await llm_service.complete_sync(
            messages=[{"role": "user", "content": decompose_prompt}],
            model=model,
            temperature=0.3,
            max_tokens=512,
        )
    except Exception as exc:
        logger.warning("atomic_retrieve: decompose call failed (%s) — falling back to single retrieval", exc)
        return await retriever_orchestrator.retrieve(query, corpus_ids, retrieval_tier, collections)

    sub_questions = _parse_json_array(raw)
    if not sub_questions:
        logger.info("atomic_retrieve: no sub-questions parsed — falling back to original query")
        return await retriever_orchestrator.retrieve(query, corpus_ids, retrieval_tier, collections)

    logger.info("atomic_retrieve: %d sub-questions", len(sub_questions))

    # Fan out — retrieve for each sub-question in parallel
    results: list[RetrievalResult | BaseException] = await asyncio.gather(
        *[
            retriever_orchestrator.retrieve(sq, corpus_ids, retrieval_tier, collections)
            for sq in sub_questions
        ],
        return_exceptions=True,
    )

    pools: list[list[SourceChunk]] = []
    facts: list[SourceFact] = []
    seen_fact_ids: set[str] = set()
    requested_tier = retrieval_tier
    effective_tier = retrieval_tier
    downgrade_reason: str | None = None
    for r in results:
        if isinstance(r, BaseException):
            logger.warning("atomic_retrieve: sub-query failed: %s", r)
            continue
        pools.append(r.chunks)
        for fact in getattr(r, "facts", []) or []:
            key = fact.fact_id or f"{fact.subject}:{fact.chunk_id}"
            if key in seen_fact_ids:
                continue
            seen_fact_ids.add(key)
            facts.append(fact)
        # If any sub-query triggered a downgrade, preserve that signal
        effective_tier = r.effective_tier
        if r.downgrade_reason and not downgrade_reason:
            downgrade_reason = r.downgrade_reason

    merged = merge_pools(*pools)
    return RetrievalResult(
        chunks=merged,
        facts=facts,
        requested_tier=requested_tier,
        effective_tier=effective_tier,
        downgrade_reason=downgrade_reason,
    )


# ── Self-correct: generate → review → optionally revise ────────────────────


async def self_correct_review(
    query: str,
    chunks: list[SourceChunk],
    initial_answer: str,
    model: str,
) -> tuple[str, bool, list[str]]:
    """
    Given the initial answer and the retrieved chunks, ask the LLM to review
    for errors. Returns (final_answer, was_revised, issues).

    Called by chat_orchestrator AFTER the first answer has streamed. If
    was_revised=True, orchestrator emits a `thinking` chunk with the critique,
    then streams the revised answer as a second turn.
    """
    from services.llm import llm_service

    # Build context from chunks (mirrors context_manager.build_augmented_prompt)
    parts: list[str] = []
    for i, c in enumerate(chunks, 1):
        corpus_label = c.corpus_name or c.corpus_id or "Unknown"
        doc_label = c.doc_name or c.doc_id or "Unknown"
        parts.append(f'[Source {i}: "{corpus_label}" / "{doc_label}"]\n{c.text}')
    context = "\n\n---\n\n".join(parts)

    review_prompt = (
        "Review this answer for errors. Check if each claim is supported by the sources. "
        'Return ONLY a JSON object of the form:\n'
        '{"has_errors": bool, "revised_answer": string, "issues": [string]}\n\n'
        f"Original question:\n{query}\n\n"
        f"Sources:\n{context}\n\n"
        f"Answer to review:\n{initial_answer}\n\n"
        "JSON response:"
    )
    try:
        raw = await llm_service.complete_sync(
            messages=[{"role": "user", "content": review_prompt}],
            model=model,
            temperature=0.2,
            max_tokens=2048,
        )
    except Exception as exc:
        logger.warning("self_correct_review: LLM call failed (%s) — keeping initial answer", exc)
        return (initial_answer, False, [])

    parsed = _parse_json_object(raw)
    has_errors = bool(parsed.get("has_errors", False))
    issues = [str(x) for x in parsed.get("issues", []) if isinstance(x, (str, int, float))]
    revised = str(parsed.get("revised_answer") or "").strip()

    if has_errors and revised and issues:
        return (revised, True, issues)
    return (initial_answer, False, [])
