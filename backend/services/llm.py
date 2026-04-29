# backend/services/llm.py
# LiteLLM wrapper, streaming chat completions, embeddings
# All LLM calls route through LiteLLM proxy. No direct provider SDK calls.
# Import: from services.llm import LLMService, llm_service

import asyncio
import json
import logging
from typing import AsyncGenerator

import httpx
from config import get_settings
from models.schemas import ModelOverrides

logger = logging.getLogger(__name__)
settings = get_settings()


class LLMService:
    """
    Service for LLM operations via LiteLLM proxy.

    All LLM/embedding calls MUST route through LiteLLM.
    Never call provider SDKs directly.
    Model names use provider/model format: ollama/llama3.2:3b, openai/gpt-4o
    """

    def __init__(self) -> None:
        """Initialize LLM service with settings."""
        self._base_url: str = settings.LITELLM_URL
        self._master_key: str = settings.LITELLM_MASTER_KEY
        self._default_model: str = settings.DEFAULT_COMPLETION_MODEL
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """
        Get or create async HTTP client.
        Reuses existing client if available and not closed.

        Returns:
            httpx.AsyncClient instance
        """
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(300.0, connect=10.0),
                follow_redirects=True,
            )
        return self._client

    def _get_headers(self) -> dict[str, str]:
        """
        Get headers for LiteLLM API requests.

        Returns:
            Dict with Authorization and Content-Type headers
        """
        headers: dict[str, str] = {
            "Content-Type": "application/json",
        }
        if self._master_key:
            headers["Authorization"] = f"Bearer {self._master_key}"
        return headers

    def _build_request_body(
        self,
        messages: list[dict[str, str]],
        model: str,
        overrides: ModelOverrides | None = None,
        stream: bool = True,
        tools: list[dict] | None = None,
    ) -> dict:
        """
        Build request body for LiteLLM completion endpoint.

        Args:
            messages: List of message dicts with role and content
            model: Model name in provider/model format
            overrides: Optional model parameter overrides
            stream: Whether to stream the response
            tools: Optional list of tool schemas for function calling

        Returns:
            Request body dict
        """
        body: dict = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }

        if tools:
            body["tools"] = tools

        # Apply overrides if provided
        if overrides:
            if overrides.temperature is not None:
                body["temperature"] = overrides.temperature
            if overrides.top_p is not None:
                body["top_p"] = overrides.top_p
            if overrides.max_tokens is not None:
                body["max_tokens"] = overrides.max_tokens
            if overrides.model is not None:
                body["model"] = overrides.model

        return body

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        max_retries: int = 3,
        base_delay: float = 1.0,
        **kwargs,
    ) -> httpx.Response:
        """
        Make HTTP request with exponential backoff retry logic.

        Args:
            method: HTTP method (GET, POST, etc.)
            url: Request URL
            max_retries: Maximum number of retry attempts
            base_delay: Base delay in seconds for exponential backoff
            **kwargs: Additional arguments passed to httpx client

        Returns:
            httpx.Response object

        Raises:
            httpx.HTTPStatusError: If all retries fail
        """
        client = await self._get_client()
        last_exception: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                response = await client.request(method, url, **kwargs)

                # Don't retry on 4xx errors (client errors)
                if 400 <= response.status_code < 500:
                    return response

                # Retry on 5xx errors (server errors)
                if response.status_code < 500:
                    return response

                # Server error - will retry
                last_exception = httpx.HTTPStatusError(
                    f"Server error: {response.status_code}",
                    request=response.request,
                    response=response,
                )

            except httpx.TimeoutException as e:
                last_exception = e
                logger.warning(
                    f"Request timeout on attempt {attempt + 1}/{max_retries + 1}: {url}"
                )
            except httpx.ConnectError as e:
                last_exception = e
                logger.warning(
                    f"Connection error on attempt {attempt + 1}/{max_retries + 1}: {url}"
                )
            except Exception as e:
                last_exception = e
                logger.warning(
                    f"Request failed on attempt {attempt + 1}/{max_retries + 1}: {e}"
                )

            # Don't sleep after last attempt
            if attempt < max_retries:
                delay = base_delay * (2**attempt)
                logger.info(f"Retrying in {delay:.1f}s...")
                await asyncio.sleep(delay)

        # All retries exhausted
        if last_exception:
            raise last_exception
        raise RuntimeError("Request failed after all retries")

    @staticmethod
    def _provider_for_model(model: str) -> str | None:
        """
        Map a `provider/model` string to the provider key used by the secrets
        service. Returns None when the model is local-only (Ollama) or a
        provider we don't manage centrally.

        Phase 19.3 — delegates to `services.secrets.KNOWN_PROVIDERS` so this
        stays in sync with the allowlist (no more duplicate hardcoded sets).
        Supports hyphenated prefixes (e.g. `glm-coding`, `mimo-coding`).
        """
        if not model or "/" not in model:
            return None
        from services.secrets import KNOWN_PROVIDERS

        prefix = model.split("/", 1)[0].lower()
        return prefix if prefix in KNOWN_PROVIDERS else None

    async def _resolve_api_key(self, model: str) -> str | None:
        """
        Look up a Mongo-stored, decrypted API key for the model's provider.
        Falls back to env var (handled implicitly by LiteLLM if api_key is
        not in the request body).
        """
        provider = self._provider_for_model(model)
        if not provider:
            return None
        try:
            from services.settings import settings_service

            return await settings_service.get_plaintext_key_any_user(provider)
        except Exception as exc:  # never break the chat call on key lookup
            logger.debug("api-key lookup failed for %s: %s", provider, exc)
            return None

    async def complete_sync(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        api_base: str | None = None,
        api_key: str | None = None,
        extra_params: dict | None = None,
        timeout: float = 120.0,
    ) -> str:
        """
        Non-streaming single-turn chat completion — for reasoning helpers
        (atomic decompose, self_correct review, HyDE) that need the full text back.

        Args:
            messages: list of {"role", "content"} dicts
            model: provider/model string; falls back to DEFAULT_COMPLETION_MODEL
            temperature: sampling temp (0 for deterministic)
            max_tokens: output cap
            api_base: per-call base URL (pool entry override). Forwarded to
                LiteLLM as `api_base` in the request body.
            api_key: per-call plaintext API key (pool entry override). Wins
                over the auto-resolved key.
            extra_params: extra body params merged in. Reserved keys
                {model, messages, temperature, max_tokens, stream} are NOT
                clobbered.
            timeout: hard wall on the call. Phase 24: callers (HyDE,
                reasoning cascade) pass a tight budget so a reasoning model
                that accidentally got picked can't burn the whole turn.
                On timeout, httpx raises ReadTimeout — caller decides how
                to fall back.

        Returns:
            The assistant message content as a plain string.

        Phase 24 — reasoning-content fallback. Some providers (DeepSeek R1,
        GLM-5.x, OpenAI o-series) return their answer in `reasoning_content`
        with empty `content` when the response was truncated or the model
        spent all tokens thinking. We extract the tail of `reasoning_content`
        as a last-resort answer so the caller never sees a silent empty
        return. The caller's logging will still flag this as degraded.
        """
        model = model or self._default_model
        url = f"{self._base_url}/chat/completions"
        client = await self._get_client()

        body: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if api_base:
            body["api_base"] = api_base
        # Pool/profile-supplied key wins over the auto-resolved one.
        if api_key:
            body["api_key"] = api_key
        else:
            resolved_key = await self._resolve_api_key(model)
            if resolved_key:
                body["api_key"] = resolved_key
        if extra_params:
            for k, v in extra_params.items():
                if k in {"model", "messages", "temperature", "max_tokens", "stream"}:
                    continue
                body[k] = v

        headers = self._get_headers()

        resp = await client.post(
            url,
            json=body,
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
        )
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message", {})
        content = message.get("content", "") or ""
        if content.strip():
            return content
        # Phase 24 — reasoning-model fallback. content is empty: try to salvage
        # the last paragraph of reasoning_content. If the model truncated mid-
        # think (finish_reason=length), the tail is usually the closest thing
        # to a final answer.
        reasoning_content = (message.get("reasoning_content") or "").strip()
        if reasoning_content:
            finish = choices[0].get("finish_reason")
            logger.warning(
                "complete_sync: model=%s returned empty content; "
                "extracting tail of reasoning_content (finish_reason=%s, "
                "reasoning_chars=%d). Pick a non-reasoning model to fix.",
                model, finish, len(reasoning_content),
            )
            # Take the last ~600 chars of reasoning as a fallback answer
            tail = reasoning_content[-600:]
            return f"[reasoning-model fallback — finish_reason={finish}]\n{tail}"
        return ""

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        overrides: ModelOverrides | None = None,
        tools: list[dict] | None = None,
        *,
        api_base: str | None = None,
        api_key: str | None = None,
        extra_params: dict | None = None,
    ) -> AsyncGenerator[dict, None]:
        """
        Stream chat completion from LiteLLM proxy with thinking and tool support.

        Args:
            messages: List of message dicts with role and content
            model: Model name in provider/model format (uses default if None)
            overrides: Optional model parameter overrides
            tools: Optional list of tool schemas for function calling
            api_base: Phase 19.3 — per-call base URL (profile override). Forwarded
                      to LiteLLM as `api_base` in the request body.
            api_key: Phase 19.3 — per-call plaintext API key (profile override).
                     Wins over auto-resolution from Settings → API Keys.
            extra_params: Phase 19.3 — extra body params merged in. Keys
                     `model`, `messages`, `stream`, `tools` are reserved.

        Yields:
            Dicts with "content", "thinking", or "tool_calls" fields

        Raises:
            httpx.HTTPStatusError: If LiteLLM returns an error
            Exception: On streaming errors
        """
        # Use provided model or default
        model = model or self._default_model

        # Build request
        url = f"{self._base_url}/chat/completions"
        body = self._build_request_body(
            messages, model, overrides, stream=True, tools=tools
        )
        # Phase 19.3 — explicit profile api_key wins over auto-resolved.
        # Falls back to per-user Mongo key (Phase 19.2) when profile key absent.
        resolved_key = api_key or await self._resolve_api_key(model)
        if resolved_key:
            body["api_key"] = resolved_key
        if api_base:
            body["api_base"] = api_base
        if extra_params:
            for _k, _v in extra_params.items():
                if _k not in ("model", "messages", "stream", "tools"):
                    body[_k] = _v
        headers = self._get_headers()

        client = await self._get_client()

        logger.info(
            f"Streaming chat completion: model={model}, messages={len(messages)}"
        )

        try:
            async with client.stream(
                "POST",
                url,
                json=body,
                headers=headers,
                timeout=httpx.Timeout(300.0, connect=10.0),
            ) as response:
                # Check for errors in initial response
                if response.status_code != 200:
                    error_body = await response.aread()
                    error_msg = error_body.decode() if error_body else "Unknown error"
                    logger.error(
                        f"LiteLLM error: status={response.status_code}, body={error_msg}"
                    )
                    response.raise_for_status()

                # Track thinking state for models that use <think> tags
                in_thinking = False
                buffer = ""

                # Stream the response
                async for line in response.aiter_lines():
                    if not line:
                        continue

                    # Parse SSE format: "data: {...}"
                    if line.startswith("data: "):
                        data = line[6:]  # Remove "data: " prefix

                        # Check for stream end
                        if data.strip() == "[DONE]":
                            break

                        # Parse JSON and extract content
                        try:
                            chunk = json.loads(data)

                            # Extract token from choices
                            choices = chunk.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                tool_calls = delta.get("tool_calls")

                                # Handle tool calls first - they are exclusive
                                if tool_calls:
                                    yield {"tool_calls": tool_calls}
                                    continue

                                # Check for thinking content (Claude 3.7, etc.)
                                thinking = delta.get("thinking") or delta.get(
                                    "reasoning_content"
                                )
                                content = delta.get("content", "")

                                # Handle DeepSeek-style <think> tags
                                if content:
                                    buffer += content
                                    while buffer:
                                        if not in_thinking:
                                            think_start = buffer.find("<think>")
                                            if think_start == -1:
                                                # No think tag, yield as normal content
                                                if buffer:
                                                    yield {"content": buffer}
                                                buffer = ""
                                            else:
                                                # Yield content before <think>
                                                if think_start > 0:
                                                    yield {
                                                        "content": buffer[:think_start]
                                                    }
                                                buffer = buffer[think_start + 7 :]
                                                in_thinking = True
                                        else:
                                            think_end = buffer.find("</think>")
                                            if think_end == -1:
                                                # Still in thinking, yield as thinking
                                                yield {"thinking": buffer}
                                                buffer = ""
                                            else:
                                                # End of thinking block
                                                yield {"thinking": buffer[:think_end]}
                                                buffer = buffer[think_end + 8 :]
                                                in_thinking = False

                                # Yield native thinking content
                                if thinking:
                                    yield {"thinking": thinking}

                        except json.JSONDecodeError:
                            logger.debug(f"Failed to parse SSE chunk: {data}")
                            continue

                # Yield any remaining buffer
                if buffer:
                    if in_thinking:
                        yield {"thinking": buffer}
                    else:
                        yield {"content": buffer}

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error during streaming: {e}")
            raise
        except Exception as e:
            logger.error(f"Error during streaming: {e}")
            raise

    async def complete_chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        overrides: ModelOverrides | None = None,
    ) -> str:
        """
        Get complete (non-streaming) chat response from LiteLLM.

        Uses exponential backoff retry logic for resilience.

        Args:
            messages: List of message dicts with role and content
            model: Model name in provider/model format (uses default if None)
            overrides: Optional model parameter overrides

        Returns:
            Complete response text

        Raises:
            httpx.HTTPStatusError: If LiteLLM returns an error after retries
        """
        model = model or self._default_model

        url = f"{self._base_url}/chat/completions"
        body = self._build_request_body(messages, model, overrides, stream=False)
        # Phase 19.2 — inject Mongo-stored API key when set
        api_key = await self._resolve_api_key(model)
        if api_key:
            body["api_key"] = api_key
        headers = self._get_headers()

        logger.info(f"Complete chat: model={model}, messages={len(messages)}")

        try:
            response = await self._request_with_retry(
                "POST",
                url,
                json=body,
                headers=headers,
                max_retries=3,
                base_delay=1.0,
            )

            if response.status_code != 200:
                logger.error(f"LiteLLM error: {response.status_code} - {response.text}")
                response.raise_for_status()

            data = response.json()
            choices = data.get("choices", [])
            if choices:
                message = choices[0].get("message", {})
                return message.get("content", "")

            return ""

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error in complete_chat: {e}")
            raise
        except Exception as e:
            logger.error(f"Error in complete_chat: {e}")
            raise

    # Phase 17 W1.0 cleanup — removed LLMService.embed_text and
    # LLMService.embed_batch. Both had zero callers anywhere in the repo
    # (verified via grep + graphify_4). The live embedding path is
    # `services/embedder.py` which dispatches between local_st / modal_tei /
    # siliconflow per the corpus's frozen embed_mode. Keep that the single
    # source of truth.

    async def get_available_models(self) -> list[dict]:
        """
        Get list of available models from LiteLLM.

        Returns:
            List of model dicts with id and metadata
        """
        url = f"{self._base_url}/models"
        headers = self._get_headers()

        try:
            response = await self._request_with_retry(
                "GET",
                url,
                headers=headers,
                max_retries=2,
                base_delay=0.5,
            )

            if response.status_code != 200:
                logger.warning(
                    f"Failed to get models from LiteLLM: {response.status_code}"
                )
                return []

            data = response.json()
            return data.get("data", [])

        except Exception as e:
            logger.error(f"Error getting LiteLLM models: {e}")
            return []

    async def health_check(self) -> bool:
        """
        Check LiteLLM proxy health.

        Returns:
            True if healthy, False otherwise
        """
        try:
            client = await self._get_client()
            response = await client.get(
                f"{self._base_url}/health",
                headers=self._get_headers(),
                timeout=5.0,
            )
            return response.status_code == 200
        except Exception as e:
            logger.error(f"LiteLLM health check failed: {e}")
            return False

    async def validate_embedding_model(self, model: str) -> tuple[bool, str]:
        """
        Validate that an embedding model is available and returns correct dimensions.

        Args:
            model: Model name to validate

        Returns:
            Tuple of (is_valid, message)
        """
        try:
            # Test with a small text
            test_text = "Test embedding validation"
            embedding = await self.embed_text(test_text, model)

            if not embedding:
                return False, f"Model {model} returned empty embedding"

            dimension = len(embedding)
            expected_dim = settings.EMBEDDING_DIMENSION

            if dimension != expected_dim:
                return (
                    False,
                    f"Model {model} returned dimension {dimension}, expected {expected_dim}",
                )

            return True, f"Model {model} valid, dimension: {dimension}"

        except Exception as e:
            return False, f"Model {model} validation failed: {str(e)}"

    async def close(self) -> None:
        """Close the HTTP client. Call on app shutdown."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            logger.info("LLM service client closed")


# Singleton instance for app-wide use
llm_service = LLMService()
