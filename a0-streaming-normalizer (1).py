"""
Agent Zero — ChatGenerationResult-style Streaming Normalizer

Extracted from /a0/models.py (Agent Zero framework).
This is the actual production code Agent Zero uses to normalize streaming LLM output.

Pipeline:
    raw LiteLLM SSE chunk
    → _parse_chunk()          # extract content + reasoning_content from provider delta
    → ChatGenerationResult.add_chunk()
        → native reasoning? pass through
        → else: thinking-tag state machine (partial buffering, multiple tag pairs)
    → yields ChatChunk{response_delta, reasoning_delta}
    → orchestrator sees only clean normalized chunks

Supports:
    - Native reasoning_content (DeepSeek R1, OpenAI o1/o3, etc.)
    - Inline thinking tags: <think|reasoning|...>...</think|reasoning|...>
    - Robust partial tag buffering (handles split across chunks)
    - Configurable tag pairs via thinking_pairs
    - Clean provider-normalized output before orchestrator
"""

from typing import TypedDict


class ChatChunk(TypedDict):
    """Simplified response chunk for chat models."""
    response_delta: str
    reasoning_delta: str


class ChatGenerationResult:
    """
    Stateful streaming normalizer that sits between raw SSE chunks and the orchestrator.

    Flow:
        for raw_chunk in litellm_stream():
            parsed = _parse_chunk(raw_chunk)        # provider → ChatChunk
            normalized = result.add_chunk(parsed)   # state machine → ChatChunk
            # normalized has clean {response_delta, reasoning_delta}

    State machine handles:
        1. Native reasoning detection — if provider sends reasoning_content, pass through
        2. Thinking tag parsing — detect <think|reasoning> opening tags
        3. Partial tag buffering — handle tags split across chunks
        4. Multiple tag pairs — configurable via thinking_pairs
    """

    def __init__(self, chunk: ChatChunk | None = None):
        self.reasoning = ""
        self.response = ""
        self.thinking = False
        self.thinking_tag = ""
        self.unprocessed = ""
        self.native_reasoning = False
        self.thinking_pairs = [
            ("<think", "</think"),
            ("<reasoning>", "</reasoning>"),
        ]
        if chunk:
            self.add_chunk(chunk)

    def add_chunk(self, chunk: ChatChunk) -> ChatChunk:
        """Process a raw chunk through the state machine, return normalized ChatChunk."""
        if chunk["reasoning_delta"]:
            self.native_reasoning = True

        # if native reasoning detection works, there's no need to worry about thinking tags
        if self.native_reasoning:
            processed_chunk = ChatChunk(
                response_delta=chunk["response_delta"],
                reasoning_delta=chunk["reasoning_delta"],
            )
        else:
            # if the model outputs thinking tags, we need to parse them manually as reasoning
            processed_chunk = self._process_thinking_chunk(chunk)

        self.reasoning += processed_chunk.get("reasoning_delta", "")
        self.response += processed_chunk.get("response_delta", "")

        return processed_chunk

    def _process_thinking_chunk(self, chunk: ChatChunk) -> ChatChunk:
        response_delta = self.unprocessed + chunk["response_delta"]
        self.unprocessed = ""
        return self._process_thinking_tags(response_delta, chunk["reasoning_delta"])

    def _process_thinking_tags(self, response: str, reasoning: str) -> ChatChunk:
        if self.thinking:
            # currently inside a thinking block — look for closing tag
            close_pos = response.find(self.thinking_tag)
            if close_pos != -1:
                reasoning += response[:close_pos]
                response = response[close_pos + len(self.thinking_tag):]
                self.thinking = False
                self.thinking_tag = ""
            else:
                # no close tag found — check for partial closing tag at end
                if self._is_partial_closing_tag(response):
                    self.unprocessed = response
                    response = ""
                else:
                    reasoning += response
                    response = ""
        else:
            # not in thinking block — scan for opening tags
            for opening_tag, closing_tag in self.thinking_pairs:
                if response.startswith(opening_tag):
                    response = response[len(opening_tag):]
                    self.thinking = True
                    self.thinking_tag = closing_tag

                    close_pos = response.find(closing_tag)
                    if close_pos != -1:
                        reasoning += response[:close_pos]
                        response = response[close_pos + len(closing_tag):]
                        self.thinking = False
                        self.thinking_tag = ""
                    else:
                        if self._is_partial_closing_tag(response):
                            self.unprocessed = response
                            response = ""
                        else:
                            reasoning += response
                            response = ""
                    break
                elif (
                    len(response) < len(opening_tag)
                    and self._is_partial_opening_tag(response, opening_tag)
                ):
                    self.unprocessed = response
                    response = ""
                    break

        return ChatChunk(response_delta=response, reasoning_delta=reasoning)

    def _is_partial_opening_tag(self, text: str, opening_tag: str) -> bool:
        """Check if text could be the start of an opening tag split across chunks."""
        for i in range(1, len(opening_tag)):
            if text == opening_tag[:i]:
                return True
        return False

    def _is_partial_closing_tag(self, text: str) -> bool:
        """Check if text ends with a prefix of the closing tag (partial buffer)."""
        if not self.thinking_tag or not text:
            return False
        max_check = min(len(text), len(self.thinking_tag) - 1)
        for i in range(1, max_check + 1):
            if text.endswith(self.thinking_tag[:i]):
                return True
        return False

    def output(self) -> ChatChunk:
        """Return final merged output, flushing any unprocessed remainder."""
        response = self.response
        reasoning = self.reasoning
        if self.unprocessed:
            if reasoning and not response:
                reasoning += self.unprocessed
            else:
                response += self.unprocessed
        return ChatChunk(response_delta=response, reasoning_delta=reasoning)


# ---------------------------------------------------------------------------
# Provider chunk parser — normalizes LiteLLM ModelResponse → ChatChunk
# ---------------------------------------------------------------------------

def _parse_chunk(chunk) -> ChatChunk:
    """
    Extract content and reasoning_content from a LiteLLM ModelResponse chunk.

    Handles both streaming deltas and non-streaming messages, and both
    dict-style and attribute-style access patterns across providers.
    """
    delta = chunk["choices"][0].get("delta", {})
    message = (
        chunk["choices"][0].get("message", {})
        or chunk["choices"][0].get("model_extra", {}).get("message", {})
    )

    response_delta = (
        delta.get("content", "") if isinstance(delta, dict) else getattr(delta, "content", "")
    ) or (
        message.get("content", "") if isinstance(message, dict) else getattr(message, "content", "")
    ) or ""

    reasoning_delta = (
        delta.get("reasoning_content", "") if isinstance(delta, dict) else getattr(delta, "reasoning_content", "")
    ) or (
        message.get("reasoning_content", "") if isinstance(message, dict) else getattr(message, "reasoning_content", "")
    ) or ""

    return ChatChunk(reasoning_delta=reasoning_delta, response_delta=response_delta)


# ---------------------------------------------------------------------------
# Usage example / self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Example 1: thinking tags split across chunks (simulates streaming)
    print("=== Example 1: <think|reasoning> tags split across chunks ===")
    ex1 = "<think" + "reasoning goes here" + "</think" + "response goes here"
    result = ChatGenerationResult()
    for i, char in enumerate(ex1):
        chunk = ChatChunk(response_delta=char, reasoning_delta="")
        output = result.add_chunk(chunk)
        if output["response_delta"] or output["reasoning_delta"]:
            pass  # stream to orchestrator
    final = result.output()
    print(f"  reasoning: {final['reasoning_delta']!r}")
    print(f"  response:  {final['response_delta']!r}")

    # Example 2: partial closing tag buffered mid-stream
    print("\n=== Example 2: partial closing tag buffering ===")
    ex2 = "<think" + "some reasoning" + "</thi"  # truncated closing tag
    result2 = ChatGenerationResult()
    for char in ex2:
        chunk = ChatChunk(response_delta=char, reasoning_delta="")
        result2.add_chunk(chunk)
    # now send the rest of the closing tag + response
    for char in "nk>hello world":
        chunk = ChatChunk(response_delta=char, reasoning_delta="")
        result2.add_chunk(chunk)
    final2 = result2.output()
    print(f"  reasoning: {final2['reasoning_delta']!r}")
    print(f"  response:  {final2['response_delta']!r}")

    # Example 3: native reasoning_content (DeepSeek R1 / o1 style)
    print("\n=== Example 3: native reasoning_content ===")
    result3 = ChatGenerationResult()
    chunks = [
        ChatChunk(response_delta="", reasoning_delta="Let me think..."),
        ChatChunk(response_delta="", reasoning_delta=" about this step by step."),
        ChatChunk(response_delta="The answer is ", reasoning_delta=""),
        ChatChunk(response_delta="42.", reasoning_delta=""),
    ]
    for c in chunks:
        out = result3.add_chunk(c)
        if out["reasoning_delta"]:
            print(f"  [reasoning] {out['reasoning_delta']!r}")
        if out["response_delta"]:
            print(f"  [response]  {out['response_delta']!r}")
    final3 = result3.output()
    print(f"  final reasoning: {final3['reasoning_delta']!r}")
    print(f"  final response:  {final3['response_delta']!r}")
