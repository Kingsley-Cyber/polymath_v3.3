# backend/services/context_manager.py
# Sliding window, summarization, token budgeting
# All functions are async. Import: from services.context_manager import ContextManager

import logging
from dataclasses import dataclass
from typing import Optional

from config import get_settings
from models.schemas import ChatMessage, SourceChunk
from utils.tokens import count_tokens, count_tokens_messages, get_model_context_limit

logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class TrimResult:
    """Result of history trimming operation."""

    messages: list[ChatMessage]
    was_trimmed: bool
    original_count: int
    trimmed_count: int
    tokens_before: int
    tokens_after: int
    details: str


class ContextManager:
    """
    Manages conversation context window to fit within model token limits.

    Implements sliding window strategy: drops oldest message pairs
    (user + assistant) until history fits within token budget.
    """

    def __init__(self) -> None:
        """Initialize context manager with settings."""
        self._default_max_tokens = settings.MAX_CONTEXT_TOKENS
        self._reserve_tokens = settings.RESERVE_TOKENS

    def _messages_to_dicts(self, messages: list[ChatMessage]) -> list[dict]:
        """
        Convert ChatMessage objects to dicts for token counting.

        Args:
            messages: List of ChatMessage objects

        Returns:
            List of message dicts with role and content
        """
        return [{"role": msg.role, "content": msg.content} for msg in messages]

    def _calculate_tokens(
        self,
        messages: list[ChatMessage],
        model: str,
    ) -> int:
        """
        Calculate total tokens for a list of messages.

        Args:
            messages: List of ChatMessage objects
            model: Model name for token counting

        Returns:
            Total token count
        """
        if not messages:
            return 0
        message_dicts = self._messages_to_dicts(messages)
        return count_tokens_messages(message_dicts, model)

    def _find_user_assistant_pair_boundary(
        self,
        messages: list[ChatMessage],
        start_index: int,
    ) -> int:
        """
        Find the index where a user+assistant pair ends.

        For sliding window, we want to drop complete conversation pairs.
        A pair is typically: user message followed by assistant message.

        Args:
            messages: List of messages
            start_index: Index to start searching from

        Returns:
            End index of the pair (exclusive), or start_index + 1 if no pair found
        """
        if start_index >= len(messages):
            return len(messages)

        # If we start with a user message, look for the assistant response
        if messages[start_index].role == "user":
            for i in range(start_index + 1, len(messages)):
                if messages[i].role == "assistant":
                    return i + 1  # Include the assistant message
            # No assistant response found, just remove the user message
            return start_index + 1

        # If we start with an assistant message (edge case), just remove it
        return start_index + 1

    def _build_trim_details(
        self,
        original_count: int,
        trimmed_count: int,
        tokens_before: int,
        tokens_after: int,
        pairs_removed: int,
    ) -> str:
        """
        Build a human-readable trimming details string.

        Args:
            original_count: Original message count
            trimmed_count: Trimmed message count
            tokens_before: Token count before trimming
            tokens_after: Token count after trimming
            pairs_removed: Number of conversation pairs removed

        Returns:
            Formatted trimming details string
        """
        messages_removed = original_count - trimmed_count
        tokens_saved = tokens_before - tokens_after

        if messages_removed == 0:
            return "No trimming needed"

        return (
            f"History trimmed: {original_count} → {trimmed_count} messages "
            f"({pairs_removed} pairs removed, {tokens_saved} tokens saved)"
        )

    def trim_history(
        self,
        messages: list[ChatMessage],
        model: str,
        max_tokens: Optional[int] = None,
        reserve_tokens: Optional[int] = None,
    ) -> TrimResult:
        """
        Trim conversation history to fit within token budget.

        Uses sliding window strategy: drops oldest conversation pairs
        (user + assistant) until the remaining messages fit within
        the token budget.

        Args:
            messages: List of ChatMessage objects (oldest to newest)
            model: Model name for context limit and token counting
            max_tokens: Override max context tokens (optional)
            reserve_tokens: Tokens to reserve for response (optional)

        Returns:
            TrimResult with trimmed messages and metadata

        Examples:
            >>> messages = [user_msg1, assistant_msg1, user_msg2, assistant_msg2]
            >>> result = context_manager.trim_history(messages, "ollama/llama3.2:3b")
            >>> result.was_trimmed
            False
            >>> result.messages  # Original messages unchanged
        """
        # Handle empty input
        if not messages:
            return TrimResult(
                messages=[],
                was_trimmed=False,
                original_count=0,
                trimmed_count=0,
                tokens_before=0,
                tokens_after=0,
                details="No messages to process",
            )

        # Get token limits
        if max_tokens is None:
            max_tokens = get_model_context_limit(model)
            # Use settings default if model limit not found
            if max_tokens == 4096:  # Default fallback
                max_tokens = self._default_max_tokens

        if reserve_tokens is None:
            reserve_tokens = self._reserve_tokens

        available_tokens = max_tokens - reserve_tokens

        # Calculate initial tokens
        tokens_before = self._calculate_tokens(messages, model)
        original_count = len(messages)

        # Check if trimming is needed
        if tokens_before <= available_tokens:
            return TrimResult(
                messages=messages,
                was_trimmed=False,
                original_count=original_count,
                trimmed_count=original_count,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                details="History fits within token budget",
            )

        # Need to trim - find how many messages to keep from the end
        logger.info(
            f"Trimming needed: {tokens_before} tokens exceeds {available_tokens} limit "
            f"(model: {model}, max_tokens: {max_tokens}, reserve: {reserve_tokens})"
        )

        # Work backwards to find messages that fit
        kept_messages: list[ChatMessage] = []
        current_tokens = 0
        pairs_removed = 0
        trim_from_start = 0

        # Start from the end and work backwards
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            msg_tokens = count_tokens_messages(
                [{"role": msg.role, "content": msg.content}],
                model,
            )

            if current_tokens + msg_tokens > available_tokens:
                # This message would exceed budget
                # Count how many pairs we're removing from the start
                trim_from_start = i + 1
                break

            kept_messages.insert(0, msg)
            current_tokens += msg_tokens

        # Invariant: never emit zero messages. If even the final user turn
        # alone exceeds the budget (heavy RAG context + a long question), keep
        # it anyway and let the upstream provider's own truncation handle it.
        # Dropping to [] always yields an "empty messages" 400 from the LLM.
        if not kept_messages and messages:
            last = messages[-1]
            kept_messages = [last]
            current_tokens = count_tokens_messages(
                [{"role": last.role, "content": last.content}],
                model,
            )
            trim_from_start = len(messages) - 1

        # Count pairs removed (estimate based on message roles removed)
        removed_messages = messages[:trim_from_start]
        user_removed = sum(1 for m in removed_messages if m.role == "user")
        assistant_removed = sum(1 for m in removed_messages if m.role == "assistant")
        pairs_removed = min(user_removed, assistant_removed)

        # Calculate final stats
        trimmed_count = len(kept_messages)
        tokens_after = self._calculate_tokens(kept_messages, model)

        # Build details string
        details = self._build_trim_details(
            original_count=original_count,
            trimmed_count=trimmed_count,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            pairs_removed=pairs_removed,
        )

        logger.info(
            f"Trimming complete: {original_count} → {trimmed_count} messages, "
            f"{tokens_before} → {tokens_after} tokens"
        )

        return TrimResult(
            messages=kept_messages,
            was_trimmed=True,
            original_count=original_count,
            trimmed_count=trimmed_count,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            details=details,
        )

    def trim_for_completion(
        self,
        messages: list[ChatMessage],
        model: str,
        max_completion_tokens: Optional[int] = None,
    ) -> TrimResult:
        """
        Trim history specifically for a completion request.

        Accounts for both the context window AND the expected completion length.

        Args:
            messages: List of ChatMessage objects
            model: Model name
            max_completion_tokens: Expected max tokens for completion response

        Returns:
            TrimResult with trimmed messages
        """
        if max_completion_tokens is None:
            max_completion_tokens = settings.MAX_COMPLETION_TOKENS

        return self.trim_history(
            messages=messages,
            model=model,
            reserve_tokens=max_completion_tokens,
        )

    def can_add_message(
        self,
        messages: list[ChatMessage],
        new_message: ChatMessage,
        model: str,
        max_tokens: Optional[int] = None,
    ) -> bool:
        """
        Check if a new message can be added without exceeding token limit.

        Args:
            messages: Current message list
            new_message: Message to check
            model: Model name
            max_tokens: Override max tokens (optional)

        Returns:
            True if message can be added within budget
        """
        if max_tokens is None:
            max_tokens = get_model_context_limit(model)

        current_tokens = self._calculate_tokens(messages, model)
        new_tokens = count_tokens_messages(
            [{"role": new_message.role, "content": new_message.content}],
            model,
        )

        return (current_tokens + new_tokens) <= max_tokens

    def get_token_usage(
        self,
        messages: list[ChatMessage],
        model: str,
    ) -> dict:
        """
        Get token usage statistics for a message list.

        Args:
            messages: List of ChatMessage objects
            model: Model name

        Returns:
            Dict with token usage stats
        """
        total_tokens = self._calculate_tokens(messages, model)
        max_tokens = get_model_context_limit(model)

        user_tokens = sum(
            count_tokens(msg.content, model) for msg in messages if msg.role == "user"
        )
        assistant_tokens = sum(
            count_tokens(msg.content, model)
            for msg in messages
            if msg.role == "assistant"
        )
        system_tokens = sum(
            count_tokens(msg.content, model) for msg in messages if msg.role == "system"
        )

        return {
            "total_tokens": total_tokens,
            "max_tokens": max_tokens,
            "available_tokens": max_tokens - total_tokens,
            "usage_percent": (total_tokens / max_tokens * 100) if max_tokens > 0 else 0,
            "user_tokens": user_tokens,
            "assistant_tokens": assistant_tokens,
            "system_tokens": system_tokens,
            "message_count": len(messages),
        }

    def build_augmented_prompt(
        self,
        query: str,
        sources: list[SourceChunk],
        corpus_ids: Optional[list[str]] = None,
        reasoning_mode: Optional[str] = None,
        reasoning_blend: Optional[list[str]] = None,
        active_skills: Optional[list[dict]] = None,
        analysis: Optional[str] = None,
        facts: Optional[list] = None,
    ) -> str:
        """
        Build the RAG-augmented prompt with spec-compliant [Source: ...] headers.

        Format per spec:
          [Source: "{corpus_name}", Document: "{doc_name}"]
          <parent chunk text>

        Multi-corpus synthesis instruction is injected when >1 corpus selected.

        Phase 15 — `reasoning_mode` / `reasoning_blend` prepend a reasoning
        template to the final prompt (see services/reasoning.py). None/"none"
        leaves the prompt unchanged.

        Pt 10a (Cluster 1) — when `facts` is non-empty, a `[Key Facts]` block
        is rendered BEFORE the chunk `<context>` block. Facts come from
        services/retriever/fact_retrieval.py and bypass the reranker — they're
        pre-distilled answer units with confidence and evidence_phrase already
        attached. Giving the LLM facts first reduces TTFT and prevents the
        model from synthesizing definitions when one is already encoded.
        """
        if not sources and not facts:
            base = query
        else:
            # Phase 23 — inline prose attribution instead of block
            # [Source: ...] headers. The old block format primed the model
            # to mirror the structure back as numbered paragraphs. Synthesis
            # guidance (don't narrate chunk-by-chunk) now lives in the
            # baseline system prompt, so the multi-corpus nudge here is
            # removed as redundant.
            passages: list[str] = []
            for s in sources:
                corpus_label = s.corpus_name or s.corpus_id or "Unknown"
                doc_label = s.doc_name or s.doc_id or "Unknown"
                section = " / ".join(s.heading_path) if s.heading_path else ""
                attribution = f'from "{doc_label}"'
                if section:
                    attribution += f" §{section}"
                attribution += f' in "{corpus_label}"'
                # Phase 16.1 — graph provenance: bridging entity + confidence.
                # Pt 10a (Cluster 5) — ontology-aware citation context. Each
                # provenance entry may carry domain_type, canonical_family,
                # surface_form, evidence_phrase, and (Mode C only) predicate +
                # relation_family. We render compactly: only fields that are
                # present and informative get inlined.
                if s.provenance:
                    via_parts: list[str] = []
                    for p in s.provenance[:3]:
                        entity = p.get("entity")
                        if not entity:
                            continue
                        part = f"{entity}@{float(p.get('confidence') or 0.0):.2f}"
                        predicate = p.get("predicate")
                        if predicate:
                            family = p.get("relation_family")
                            part += (
                                f" --{predicate}({family})-->" if family
                                else f" --{predicate}-->"
                            )
                        domain = p.get("domain_type")
                        if domain:
                            part += f" [{domain}]"
                        via_parts.append(part)
                    if via_parts:
                        attribution += f" (via {'; '.join(via_parts)})"
                passages.append(f"{attribution}: {s.text}")

            context_block = "<context>\n" + "\n\n".join(passages) + "\n</context>" if passages else ""

            # Pt 10a (Cluster 1) — pre-distilled facts rendered first, before
            # chunk excerpts. Each fact line: subject + type + property→value
            # + evidence_phrase. Skipping the reranker preserves Ghost B's
            # quality ordering (confidence descending).
            facts_block = ""
            if facts:
                fact_lines: list[str] = []
                for f in facts:
                    subject = getattr(f, "subject", "") or ""
                    fact_type = getattr(f, "fact_type", "") or ""
                    prop = getattr(f, "property_name", None) or ""
                    val = getattr(f, "value", None) or ""
                    unit = getattr(f, "unit", None) or ""
                    cond = getattr(f, "condition", None) or ""
                    ev = getattr(f, "evidence_phrase", None) or ""
                    conf = float(getattr(f, "confidence", 0.0) or 0.0)
                    parts = [f"- {subject} ({fact_type})"]
                    if prop and val:
                        parts.append(f": {prop} = {val}{(' ' + unit) if unit else ''}")
                    elif val:
                        parts.append(f": {val}{(' ' + unit) if unit else ''}")
                    if cond:
                        parts.append(f" [when {cond}]")
                    parts.append(f" (conf={conf:.2f})")
                    line = "".join(parts)
                    if ev:
                        line += f'\n    Evidence: "{ev}"'
                    fact_lines.append(line)
                facts_block = "<key_facts>\n" + "\n".join(fact_lines) + "\n</key_facts>\n\n"

            if facts_block and context_block:
                base = f"{facts_block}{context_block}\n\nQuestion: {query}"
            elif facts_block:
                base = f"{facts_block}Question: {query}"
            else:
                base = f"{context_block}\n\nQuestion: {query}"

        # Phase 24 — Skills as context. Each active skill's `instructions`
        # is wrapped in a <skill> block and prepended above <context>. Skills
        # are reference material the model uses while answering — not identity.
        if active_skills:
            skill_blocks = []
            for s in active_skills:
                name = s.get("name", "skill")
                slash = s.get("slash_command") or ""
                attrs = f'name="{name}"'
                if slash:
                    attrs += f' command="{slash}"'
                instructions = s.get("instructions", "").strip()
                skill_blocks.append(
                    f"<skill {attrs}>\n{instructions}\n</skill>"
                )
            skills_envelope = "<skills_active>\n" + "\n\n".join(skill_blocks) + "\n</skills_active>"
            base = f"{skills_envelope}\n\n{base}"

        # Phase 24 — Reasoning cascade output as <analysis> block. Sits between
        # skills and context so the model treats it as authoritative pre-digestion
        # of the retrieved chunks.
        if analysis and analysis.strip():
            analysis_block = f"<analysis>\n{analysis.strip()}\n</analysis>"
            # Insert analysis just above Question: by splitting on it
            if "\n\nQuestion:" in base:
                head, _, tail = base.rpartition("\n\nQuestion:")
                base = f"{head}\n\n{analysis_block}\n\nQuestion:{tail}"
            else:
                base = f"{analysis_block}\n\n{base}"

        # Phase 15 — prepend reasoning template(s) when requested
        if reasoning_mode or reasoning_blend:
            from services.reasoning import apply_reasoning

            base = apply_reasoning(base, mode=reasoning_mode, blend=reasoning_blend)

        return base


# Singleton instance for app-wide use
context_manager = ContextManager()
