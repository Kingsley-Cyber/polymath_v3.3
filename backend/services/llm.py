# backend/services/llm.py
# LiteLLM wrapper, streaming chat completions, embeddings
# All LLM calls route through LiteLLM proxy. No direct provider SDK calls.
# Import: from services.llm import LLMService, llm_service

import asyncio
import json
import logging
import math
from typing import Any, AsyncGenerator

import httpx
from config import get_settings
from models.schemas import ModelOverrides
from services.provider_payload import provider_payload_extras
from services.streaming_normalizer import StreamingNormalizer, extract_stream_delta

logger = logging.getLogger(__name__)
settings = get_settings()

# Markers for TRANSIENT connection failures worth retrying before the first
# token streams (a burst-load DNS blip on the LiteLLM endpoint, a dropped
# connect). Matched case-insensitively against str(exc). Deliberately narrow —
# HTTP status errors, auth failures, and model errors must NOT retry.
_TRANSIENT_STREAM_ERROR_MARKERS = (
    "name or service not known",  # getaddrinfo errno -2 (the burst DNS blip)
    "temporary failure in name resolution",  # errno -3
    "getaddrinfo",
    "errno -2",
    "errno -3",
    "connection reset",
    "connection refused",
    "connection aborted",
    "server disconnected",
    "timed out",
    "timeout",
)


def _provider_response_telemetry(
    response: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Return redacted usage/cost telemetry from one LiteLLM response.

    LiteLLM computes the routed provider cost and returns it in a response
    header. Only numeric token counts and that numeric cost cross this seam;
    provider bodies, credentials, request text, and model output do not.
    """

    raw_usage = payload.get("usage")
    usage: dict[str, int] = {}
    if isinstance(raw_usage, dict):
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = raw_usage.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                usage[field] = value

    headers = getattr(response, "headers", None)
    raw_cost = (
        headers.get("x-litellm-response-cost")
        if headers is not None and hasattr(headers, "get")
        else None
    )
    cost: float | None = None
    if raw_cost not in (None, "", "None"):
        try:
            candidate = float(raw_cost)
        except (TypeError, ValueError):
            candidate = float("nan")
        if math.isfinite(candidate) and candidate >= 0:
            cost = candidate

    return {
        "usage": usage,
        "actual_cost_usd": cost,
        "cost_source": (
            "litellm.x-litellm-response-cost" if cost is not None else None
        ),
    }


def _is_transient_stream_error(exc: Exception) -> bool:
    """True for connection-level blips that a quick retry typically clears."""
    if isinstance(exc, httpx.HTTPStatusError):
        return False  # a real HTTP error (4xx/5xx) — do not retry blindly
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_STREAM_ERROR_MARKERS)


class LLMService:
    """
    Service for LLM operations via LiteLLM proxy.

    Most LLM/embedding calls route through LiteLLM. Ollama chat streaming uses
    the native /api/chat endpoint so Polymath can preserve streamed
    message.thinking chunks that the pinned LiteLLM proxy drops.
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

    @staticmethod
    def _merge_provider_extra_params(
        body: dict[str, Any],
        extra_params: dict[str, Any] | None,
        *,
        reserved: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        """Merge saved model-pool params without leaking scheduler metadata.

        Provider cards share ``extra_params`` with Polymath routing and
        lifecycle configuration. Those internal fields are not valid LLM API
        arguments. ``disable_thinking`` is the one internal field with a wire
        effect: normalize it to the OpenAI-compatible provider contract used
        by SiliconFlow, DeepSeek, and LongCat.
        """
        if not extra_params:
            return

        raw_disable = extra_params.get("disable_thinking")
        disable_thinking = raw_disable is True or (
            isinstance(raw_disable, str)
            and raw_disable.strip().lower() in {"1", "true", "yes", "on"}
        )
        safe_params = provider_payload_extras(extra_params)
        for key, value in safe_params.items():
            if key not in reserved:
                body[key] = value

        # An explicit provider-native control always wins. Otherwise translate
        # the saved operator flag after sanitization instead of forwarding the
        # invalid ``disable_thinking`` key itself.
        if disable_thinking and not {
            "thinking",
            "enable_thinking",
            "reasoning_effort",
        }.intersection(safe_params):
            model = str(body.get("model") or "").lower()
            if "tencent/hy3" in model or "hy3" in model:
                body["enable_thinking"] = False
            else:
                body["thinking"] = {"type": "disabled"}

    def _build_request_body(
        self,
        messages: list[dict[str, Any]],
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
                try:
                    from utils.tokens import get_model_output_limit

                    output_limit = get_model_output_limit(overrides.model or model)
                    body["max_tokens"] = min(overrides.max_tokens, output_limit)
                except Exception:
                    body["max_tokens"] = overrides.max_tokens
            if overrides.model is not None:
                body["model"] = overrides.model

        # Phase 28 — thinking-effort dial. `auto` is the normal UI default:
        # omit/None from the request still means "apply the provider's
        # thinking default if this model has a supported dial." The mapper is
        # a no-op for ordinary chat models, and explicit "none" still disables
        # thinking for providers that support an off switch.
        thinking_effort = (
            getattr(overrides, "thinking_effort", None) if overrides else None
        ) or "auto"
        self._apply_thinking_effort(body, body.get("model") or model, thinking_effort)

        return body

    def _apply_thinking_effort(
        self,
        body: dict,
        model: str,
        thinking_effort: str | None,
    ) -> None:
        """Apply the provider-native thinking knob without blocking calls."""
        try:
            from services.thinking_mapper import apply_thinking_effort

            apply_thinking_effort(body, model, thinking_effort)
        except Exception as exc:  # pragma: no cover — defensive
            # Never block the LLM call on a mapper failure. Log and continue
            # with the unmapped body.
            logger.warning(
                "thinking_mapper failed (effort=%r model=%r): %s",
                thinking_effort,
                model,
                exc,
            )

    def _reapply_explicit_thinking_effort(
        self,
        body: dict,
        model: str,
        overrides: ModelOverrides | None,
    ) -> None:
        """Let the visible per-turn selector win over stale pool extras.

        Model pool ``extra_params`` are intentionally powerful, but a saved
        ``thinking: disabled`` should not silently defeat the live selector
        when the frontend explicitly sends AUTO/HIGH/NONE for this turn.
        """
        if overrides is None:
            return
        thinking_effort = getattr(overrides, "thinking_effort", None)
        if thinking_effort is None:
            return
        self._apply_thinking_effort(
            body,
            body.get("model") or model,
            thinking_effort,
        )

    def _is_ollama_chat_route(self, model: str | None) -> bool:
        normalized = (model or "").strip().lower()
        return normalized.startswith("ollama_chat/") or normalized.startswith("ollama/")

    def _ollama_native_model_name(self, model: str) -> str:
        if "/" not in model:
            return model
        return model.split("/", 1)[1]

    def _ollama_options_from_body(self, body: dict[str, Any]) -> dict[str, Any]:
        options: dict[str, Any] = {}
        if body.get("max_tokens") is not None:
            options["num_predict"] = body["max_tokens"]
        for source, target in (
            ("temperature", "temperature"),
            ("top_p", "top_p"),
            ("presence_penalty", "presence_penalty"),
            ("frequency_penalty", "frequency_penalty"),
        ):
            if body.get(source) is not None:
                options[target] = body[source]
        return options

    def _build_ollama_chat_body(
        self,
        *,
        body: dict[str, Any],
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict] | None,
    ) -> dict[str, Any]:
        ollama_body: dict[str, Any] = {
            "model": self._ollama_native_model_name(model),
            "messages": messages,
            "stream": True,
        }

        if body.get("think") is not None:
            ollama_body["think"] = body["think"]
        if body.get("format") is not None:
            ollama_body["format"] = body["format"]
        # Keep the local chat model resident between turns. Without an explicit
        # keep_alive, Ollama unloads after ~5m idle and the next turn pays a
        # multi-minute cold reload. Honor a request-provided value; otherwise
        # fall back to the configured default. (No effect on remote-API models.)
        keep_alive = body.get("keep_alive")
        if keep_alive is None:
            keep_alive = getattr(settings, "OLLAMA_KEEP_ALIVE", None) or None
        if keep_alive is not None:
            ollama_body["keep_alive"] = keep_alive
        if tools:
            ollama_body["tools"] = tools

        options = self._ollama_options_from_body(body)
        if options:
            ollama_body["options"] = options
        return ollama_body

    def _coerce_ollama_tool_calls(self, raw_calls: Any) -> list[dict[str, Any]]:
        if not isinstance(raw_calls, list):
            return []
        out: list[dict[str, Any]] = []
        for idx, call in enumerate(raw_calls):
            if not isinstance(call, dict):
                continue
            fn = call.get("function") or {}
            if not isinstance(fn, dict):
                continue
            args = fn.get("arguments") or {}
            if not isinstance(args, str):
                args = json.dumps(args)
            out.append(
                {
                    "id": call.get("id") or f"ollama-tool-{idx}",
                    "type": call.get("type") or "function",
                    "function": {
                        "name": str(fn.get("name") or ""),
                        "arguments": args,
                    },
                }
            )
        return out

    async def _stream_ollama_chat_native(
        self,
        *,
        body: dict[str, Any],
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict] | None,
        api_base: str | None,
        api_key: str | None,
    ) -> AsyncGenerator[dict, None]:
        """Stream directly from Ollama's /api/chat endpoint.

        LiteLLM v1.60 can route Ollama chat content, but it drops Ollama's
        native streamed `message.thinking` field. The direct path keeps
        Polymath's public stream contract unchanged while preserving the
        reasoning lane described in Ollama's API docs.
        """
        base_url = (api_base or settings.OLLAMA_URL).rstrip("/")
        url = f"{base_url}/api/chat"
        ollama_body = self._build_ollama_chat_body(
            body=body,
            model=model,
            messages=messages,
            tools=tools,
        )
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        client = await self._get_client()
        logger.info(
            "Streaming native Ollama chat: model=%s, messages=%d, think=%r",
            ollama_body["model"],
            len(messages),
            ollama_body.get("think"),
        )

        async with client.stream(
            "POST",
            url,
            json=ollama_body,
            headers=headers,
            timeout=httpx.Timeout(300.0, connect=10.0),
        ) as response:
            if response.status_code != 200:
                error_body = await response.aread()
                error_msg = error_body.decode() if error_body else "Unknown error"
                logger.error(
                    "Ollama error: status=%s, body=%s",
                    response.status_code,
                    error_msg,
                )
                response.raise_for_status()

            normalizer = StreamingNormalizer()
            async for line in response.aiter_lines():
                if not line:
                    continue
                data = line
                if data.startswith("data: "):
                    data = data[6:]
                    if data.strip() == "[DONE]":
                        break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    logger.debug("Failed to parse Ollama stream chunk: %s", line)
                    continue

                message = chunk.get("message") or {}
                content = ""
                thinking = ""
                if isinstance(message, dict):
                    tool_calls = self._coerce_ollama_tool_calls(
                        message.get("tool_calls")
                    )
                    if tool_calls:
                        yield {"tool_calls": tool_calls}

                    content = message.get("content") or ""
                    thinking = message.get("thinking") or ""

                if not content and not thinking:
                    content, thinking = extract_stream_delta(chunk)

                normalized = normalizer.add(
                    content=content,
                    thinking=thinking,
                )
                if normalized["thinking"]:
                    yield {"thinking": normalized["thinking"]}
                if normalized["content"]:
                    yield {"content": normalized["content"]}

                if chunk.get("done"):
                    break

            remaining = normalizer.flush()
            if remaining["thinking"]:
                yield {"thinking": remaining["thinking"]}
            if remaining["content"]:
                yield {"content": remaining["content"]}

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
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        api_base: str | None = None,
        api_key: str | None = None,
        extra_params: dict | None = None,
        response_format: dict[str, Any] | None = None,
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
            extra_params: extra body params merged in. Reserved request and
                credential fields are never clobbered.
            response_format: explicit JSON mode or strict JSON-Schema contract.
                It cannot be supplied or overridden through model-pool extras.
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
        if response_format is not None:
            body["response_format"] = response_format
        # Pool/profile-supplied key wins over the auto-resolved one.
        if api_key:
            body["api_key"] = api_key
        else:
            resolved_key = await self._resolve_api_key(model)
            if resolved_key:
                body["api_key"] = resolved_key
        self._merge_provider_extra_params(
            body,
            extra_params,
            reserved=frozenset(
                {
                    "model",
                    "messages",
                    "temperature",
                    "max_tokens",
                    "stream",
                    "api_base",
                    "api_key",
                    "response_format",
                }
            ),
        )

        # Non-streaming helper calls (HyDE, graph synthesis, query refinement,
        # JSON repair) need concise content, not provider reasoning traces.
        # DeepSeek V4 defaults thinking ON, which can consume the whole
        # max_tokens budget and return empty content. Respect explicit
        # operator params, otherwise force thinking off for helper calls.
        if "thinking" not in body and "reasoning_effort" not in body:
            try:
                from services.thinking_mapper import apply_thinking_effort

                apply_thinking_effort(body, model, "none")
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "thinking_mapper helper default failed (model=%r): %s",
                    model,
                    exc,
                )

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
                model,
                finish,
                len(reasoning_content),
            )
            # Take the last ~600 chars of reasoning as a fallback answer
            tail = reasoning_content[-600:]
            return f"[reasoning-model fallback — finish_reason={finish}]\n{tail}"
        return ""

    async def complete_tool_calls(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        overrides: ModelOverrides | None = None,
        tools: list[dict] | None = None,
        tool_choice: dict | str | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
        extra_params: dict | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Run a non-streaming native tool-call request through LiteLLM.

        This is for bounded planner/helper calls where the caller wants the
        provider's native ``tool_calls`` object, not text that we parse as JSON.
        """
        model = model or self._default_model
        url = f"{self._base_url}/chat/completions"
        body = self._build_request_body(
            messages,
            model,
            overrides,
            stream=False,
            tools=tools,
        )
        if tool_choice is not None:
            body["tool_choice"] = tool_choice

        resolved_key = api_key or await self._resolve_api_key(model)
        if resolved_key:
            body["api_key"] = resolved_key
        if api_base:
            body["api_base"] = api_base
        self._merge_provider_extra_params(
            body,
            extra_params,
            reserved=frozenset(
                {
                    "model",
                    "messages",
                    "stream",
                    "tools",
                    "tool_choice",
                    "api_base",
                    "api_key",
                }
            ),
        )
        self._reapply_explicit_thinking_effort(body, model, overrides)
        # Default thinking posture (2026-07-04): when the per-turn selector is
        # untouched, thinking-default-ON models (deepseek-v4*) burned 91s of a
        # 99s RAG answer on reasoning tokens. RAG chat pre-retrieves the
        # evidence — apply the server default (settings, "none") unless the
        # user explicitly set the dial this turn (the reapply above wins).
        _explicit = (
            overrides is not None
            and getattr(overrides, "thinking_effort", None) is not None
        )
        if not _explicit:
            _dflt = (
                str(
                    getattr(
                        settings,
                        "CHAT_DEFAULT_THINKING_EFFORT",
                        "none",
                    )
                    or ""
                )
                .strip()
                .lower()
            )
            if _dflt in ("none", "low", "medium", "high"):
                self._apply_thinking_effort(body, body.get("model") or model, _dflt)

        client = await self._get_client()
        resp = await client.post(
            url,
            json=body,
            headers=self._get_headers(),
            timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
        )
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return {
                "tool_calls": [],
                "content": "",
                "reasoning_content": "",
                "provider_telemetry": _provider_response_telemetry(resp, data),
            }

        message = choices[0].get("message") or {}
        return {
            "tool_calls": message.get("tool_calls") or [],
            "content": message.get("content") or "",
            "reasoning_content": message.get("reasoning_content") or "",
            "finish_reason": choices[0].get("finish_reason"),
            "provider_telemetry": _provider_response_telemetry(resp, data),
        }

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        overrides: ModelOverrides | None = None,
        tools: list[dict] | None = None,
        *,
        tool_choice: dict | str | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
        extra_params: dict | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Stream chat completion, retrying the CONNECTION on transient blips.

        Thin wrapper over ``_stream_chat_once``. If the underlying stream fails
        with a transient connection error (DNS getaddrinfo, connect reset) BEFORE
        any token has been emitted, retry up to ``LLM_STREAM_MAX_RETRIES`` with a
        short linear backoff — this kills the burst-load blank-answer where a
        momentary DNS hiccup on the LiteLLM endpoint surfaced as the answer. Once
        a token has streamed, the error propagates unchanged (never duplicate
        output); the caller's model-fallback handles post-token failures.
        """
        max_retries = max(0, int(getattr(settings, "LLM_STREAM_MAX_RETRIES", 2)))
        backoff = float(getattr(settings, "LLM_STREAM_RETRY_BACKOFF_SECONDS", 0.4))
        attempt = 0
        while True:
            started = False
            try:
                async for item in self._stream_chat_once(
                    messages,
                    model,
                    overrides,
                    tools,
                    tool_choice=tool_choice,
                    api_base=api_base,
                    api_key=api_key,
                    extra_params=extra_params,
                ):
                    started = True
                    yield item
                return
            except Exception as e:  # noqa: BLE001 — classify then re-raise
                if (
                    not started
                    and attempt < max_retries
                    and _is_transient_stream_error(e)
                ):
                    attempt += 1
                    await asyncio.sleep(backoff * attempt)
                    logger.warning(
                        "Transient stream connect error (attempt %d/%d), retrying: %s",
                        attempt,
                        max_retries,
                        e,
                    )
                    continue
                raise

    async def _stream_chat_once(
        self,
        # Phase 29 — message content may be a plain string (text-only)
        # OR a list of multimodal content blocks ({"type": "text", ...}
        # or {"type": "image_url", ...}). The chat orchestrator sends
        # the multimodal shape only for the final user message when
        # image attachments are present; all other messages stay text.
        # LiteLLM passes both shapes through unchanged.
        messages: list[dict[str, Any]],
        model: str | None = None,
        overrides: ModelOverrides | None = None,
        tools: list[dict] | None = None,
        *,
        tool_choice: dict | str | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
        extra_params: dict | None = None,
    ) -> AsyncGenerator[dict, None]:
        """
        Stream chat completion with thinking and tool support.

        Most providers route through the LiteLLM proxy. Ollama chat routes use
        native /api/chat streaming because the pinned LiteLLM proxy drops
        Ollama's streamed message.thinking chunks.

        Args:
            messages: List of message dicts with role and content
            model: Model name in provider/model format (uses default if None)
            overrides: Optional model parameter overrides
            tools: Optional list of tool schemas for function calling
            tool_choice: Optional native tool-choice directive for providers
                     that support forced/required tool selection.
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
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        # Phase 19.3 — explicit profile api_key wins over auto-resolved.
        # Falls back to per-user Mongo key (Phase 19.2) when profile key absent.
        resolved_key = api_key or await self._resolve_api_key(model)
        if resolved_key:
            body["api_key"] = resolved_key
        if api_base:
            body["api_base"] = api_base
        self._merge_provider_extra_params(
            body,
            extra_params,
            reserved=frozenset(
                {
                    "model",
                    "messages",
                    "stream",
                    "tools",
                    "tool_choice",
                    "api_base",
                    "api_key",
                }
            ),
        )
        self._reapply_explicit_thinking_effort(body, model, overrides)
        # Default thinking posture (2026-07-04): when the per-turn selector is
        # untouched, thinking-default-ON models (deepseek-v4*) burned 91s of a
        # 99s RAG answer on reasoning tokens. RAG chat pre-retrieves the
        # evidence — apply the server default (settings, "none") unless the
        # user explicitly set the dial this turn (the reapply above wins).
        _explicit = (
            overrides is not None
            and getattr(overrides, "thinking_effort", None) is not None
        )
        if not _explicit:
            _dflt = (
                str(
                    getattr(
                        settings,
                        "CHAT_DEFAULT_THINKING_EFFORT",
                        "none",
                    )
                    or ""
                )
                .strip()
                .lower()
            )
            if _dflt in ("none", "low", "medium", "high"):
                self._apply_thinking_effort(body, body.get("model") or model, _dflt)

        if self._is_ollama_chat_route(model):
            async for chunk in self._stream_ollama_chat_native(
                body=body,
                model=model,
                messages=messages,
                tools=tools,
                api_base=api_base,
                api_key=resolved_key,
            ):
                yield chunk
            return

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

                normalizer = StreamingNormalizer()
                pending_tool_calls: dict[int, dict[str, Any]] = {}

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
                                choice = choices[0]
                                delta = choice.get("delta", {})
                                finish_reason = choice.get("finish_reason")
                                tool_calls = delta.get("tool_calls")

                                content, thinking = extract_stream_delta(chunk)
                                normalized = normalizer.add(
                                    content=content,
                                    thinking=thinking,
                                )
                                if normalized["thinking"]:
                                    yield {"thinking": normalized["thinking"]}
                                if normalized["content"]:
                                    yield {"content": normalized["content"]}

                                # Handle streaming tool-call deltas after
                                # normalizing provider reasoning. Some
                                # OpenAI-compatible providers can send
                                # reasoning_content on the same chunks that
                                # carry tool-call argument fragments; skipping
                                # straight to `continue` here would hide live
                                # reasoning until the model stops using tools.
                                # OpenAI-compatible providers usually send
                                # arguments in fragments; emit only once the
                                # provider marks the assistant turn complete.
                                if tool_calls:
                                    for tool_call in tool_calls:
                                        index = int(
                                            tool_call.get(
                                                "index",
                                                len(pending_tool_calls),
                                            )
                                        )
                                        current = pending_tool_calls.setdefault(
                                            index,
                                            {
                                                "id": "",
                                                "type": "function",
                                                "function": {
                                                    "name": "",
                                                    "arguments": "",
                                                },
                                            },
                                        )
                                        if tool_call.get("id"):
                                            current["id"] = tool_call["id"]
                                        if tool_call.get("type"):
                                            current["type"] = tool_call["type"]

                                        fn_delta = tool_call.get("function") or {}
                                        current_fn = current.setdefault(
                                            "function",
                                            {"name": "", "arguments": ""},
                                        )
                                        if fn_delta.get("name"):
                                            current_fn["name"] += fn_delta["name"]
                                        if fn_delta.get("arguments"):
                                            current_fn["arguments"] += fn_delta[
                                                "arguments"
                                            ]

                                    if finish_reason != "tool_calls":
                                        continue

                                if finish_reason == "tool_calls" and pending_tool_calls:
                                    yield {
                                        "tool_calls": [
                                            pending_tool_calls[i]
                                            for i in sorted(pending_tool_calls)
                                        ]
                                    }
                                    pending_tool_calls = {}
                                    continue

                        except json.JSONDecodeError:
                            logger.debug(f"Failed to parse SSE chunk: {data}")
                            continue

                remaining = normalizer.flush()
                if remaining["thinking"]:
                    yield {"thinking": remaining["thinking"]}
                if remaining["content"]:
                    yield {"content": remaining["content"]}

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error during streaming: {e}")
            raise
        except Exception as e:
            logger.error(f"Error during streaming: {e}")
            raise

    async def complete_chat(
        self,
        messages: list[dict[str, Any]],
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
