"""Bifrost / direct provider client — routes model calls through either
Bifrost VKs or a direct OpenAI-compatible endpoint.

Two modes controlled by MORI_PROVIDER_MODE env var:
- bifrost (default): Uses dummy VK API keys matched by Bifrost gateway
- direct: Uses a real provider API key + base URL
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Literal

import httpx
from openai import APITimeoutError, DefaultHttpxClient, OpenAI

from mori_advisor import metrics as _metrics

logger = logging.getLogger(__name__)


def _attempt_tagger(call_id: str):
    """httpx request hook: give every HTTP attempt its own x-request-id.

    Bifrost stores the x-request-id it receives as the logs.id of that request, and keeps only
    ONE row per id — so the OpenAI client's automatic retries (which resend identical headers)
    would silently lose their rows. The first attempt is tagged `call_id`, retries
    `call_id.r<n>`. Bifrost's own server-side fallback hops are separate rows whose
    parent_request_id is the attempt's id, so the full chain for one call is:
        WHERE id LIKE '<call_id>%' OR parent_request_id LIKE '<call_id>%'
    """

    def _tag(request: httpx.Request) -> None:
        attempt = request.headers.get("x-stainless-retry-count", "0")
        request.headers["x-request-id"] = (
            call_id if attempt in ("", "0") else f"{call_id}.r{attempt}"
        )

    return _tag


def _served_provider(response) -> str:
    """Provider Bifrost actually routed to (response `extra_fields.provider`); 'unknown' if absent."""
    extra = getattr(response, "extra_fields", None)
    if isinstance(extra, dict) and extra.get("provider"):
        return str(extra["provider"])
    return "unknown"


# VK configuration: maps logical names to dummy API keys Bifrost uses
# to resolve VKs. Model routing is controlled by the VK's model_override
# in Bifrost DB. Not used in direct mode.
VK_CONFIG: dict[str, str] = {
    "advisor": "mori-advisor-local",
    "dream": "mori-dream-local",
    "fast": "mori-fast-local",
}


class BifrostClient:
    """Provider-agnostic LLM client.

    In Bifrost mode: sends requests through a Bifrost gateway using
    dummy VK keys. Bifrost handles model routing and provider selection.

    In direct mode: sends requests directly to an OpenAI-compatible
    API endpoint using a real API key and model name.
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: int = 300,
    ):
        mode = os.environ.get("MORI_PROVIDER_MODE", "bifrost")
        if mode not in ("bifrost", "direct"):
            logger.warning("Unknown MORI_PROVIDER_MODE=%s, falling back to bifrost", mode)
            mode = "bifrost"

        self.mode = mode

        # Default base_url differs by mode
        if base_url is not None:
            self.base_url = base_url
        elif mode == "direct":
            self.base_url = os.environ.get("MORI_BASE_URL", "https://api.openai.com/v1")
        else:
            self.base_url = os.environ.get("MORI_BASE_URL", "http://localhost:8787")

        # Ensure /v1 suffix
        if not self.base_url.endswith("/v1"):
            self.base_url = self.base_url.rstrip("/") + "/v1"

        self.timeout = timeout

        # Direct mode settings
        self.direct_api_key = os.environ.get("MORI_API_KEY", "")
        self.direct_model = os.environ.get("MORI_MODEL", "moonshotai/kimi-k2.6")
        self.direct_dream_model = os.environ.get(
            "MORI_DREAM_MODEL", os.environ.get("MORI_MODEL", "deepseek/deepseek-v4-flash")
        )

        # Bifrost mode VK overrides
        self.bifrost_advisor_vk = os.environ.get("MORI_BIFROST_ADVISOR_VK", "mori-advisor-local")
        self.bifrost_dream_vk = os.environ.get("MORI_BIFROST_DREAM_VK", "mori-dream-local")
        self.bifrost_fast_vk = os.environ.get("MORI_BIFROST_FAST_VK", "mori-fast-local")

        # Model names per VK (set via env, falls back to defaults)
        self.advisor_model = os.environ.get("MORI_ADVISOR_MODEL", "moonshotai/kimi-k2.6")
        self.dream_model = os.environ.get("MORI_DREAM_MODEL", "moonshotai/kimi-k2.6")
        self.fast_model = os.environ.get("MORI_FAST_MODEL", "Novita/deepseek/deepseek-v4-flash")

        if self.mode == "direct" and not self.direct_api_key:
            logger.warning(
                "MORI_PROVIDER_MODE=direct but MORI_API_KEY is not set. API calls will likely fail."
            )

    def _client_for(self, vk: str = "advisor", *, call_id: str | None = None) -> OpenAI:
        """Create an OpenAI client for the appropriate provider.

        In bifrost mode, vk selects the VK key. In direct mode, vk
        selects the model (advisor vs dream). When call_id is given, every HTTP
        attempt is tagged with its own x-request-id (see _attempt_tagger).
        """
        http_client = (
            DefaultHttpxClient(event_hooks={"request": [_attempt_tagger(call_id)]})
            if call_id
            else None
        )
        if self.mode == "direct":
            model = self.direct_dream_model if vk == "dream" else self.direct_model
            return OpenAI(
                base_url=self.base_url,
                api_key=self.direct_api_key,
                timeout=self.timeout,
                http_client=http_client,
            ), model
        else:
            key_map = {
                "advisor": self.bifrost_advisor_vk,
                "dream": self.bifrost_dream_vk,
                "fast": self.bifrost_fast_vk,
            }
            effective_key = key_map.get(vk) or VK_CONFIG.get(vk) or self.bifrost_advisor_vk
            model_map = {
                "advisor": self.advisor_model,
                "dream": self.dream_model,
                "fast": self.fast_model,
            }
            model = model_map.get(vk, self.advisor_model)
            return OpenAI(
                base_url=self.base_url,
                api_key=effective_key,
                timeout=self.timeout,
                http_client=http_client,
            ), model

    def _send(self, vk: str, kwargs: dict, ref: str | None):
        """Make one chat-completions call with lifecycle logging and metrics.

        Every outbound LLM call in mori funnels through here, so this is the one place that
        records send / receive / failure. Observability is fail-open: metrics helpers swallow
        their own errors, and the call's own exception is always re-raised unchanged.
        """
        call_id = f"mori-{vk}-{uuid.uuid4().hex[:16]}"
        client, model = self._client_for(vk, call_id=call_id)
        kwargs = {"model": model, **kwargs}
        req_bytes = len(json.dumps(kwargs["messages"], ensure_ascii=False).encode())
        logger.info(
            "llm.send call_id=%s ref=%s vk=%s mode=%s model=%s req_bytes=%d max_tokens=%s",
            call_id,
            ref or "-",
            vk,
            self.mode,
            model,
            req_bytes,
            kwargs.get("max_tokens"),
        )
        _metrics.llm_call_started(call_id, vk)
        t0 = time.monotonic()
        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as e:
            elapsed = time.monotonic() - t0
            outcome = "timeout" if isinstance(e, APITimeoutError) else "error"
            _metrics.llm_call_finished(call_id, vk, outcome, "unknown", elapsed)
            logger.error(
                "llm.fail call_id=%s ref=%s vk=%s mode=%s outcome=%s elapsed_s=%.1f error=%s: %s",
                call_id,
                ref or "-",
                vk,
                self.mode,
                outcome,
                elapsed,
                type(e).__name__,
                e,
            )
            raise
        finally:
            client.close()
        elapsed = time.monotonic() - t0
        provider = _served_provider(response)
        out_tokens = getattr(getattr(response, "usage", None), "completion_tokens", None)
        finish = getattr(response.choices[0], "finish_reason", None) if response.choices else None
        _metrics.llm_call_finished(call_id, vk, "ok", provider, elapsed, out_tokens)
        logger.info(
            "llm.recv call_id=%s ref=%s vk=%s provider=%s elapsed_s=%.1f out_tokens=%s finish=%s",
            call_id,
            ref or "-",
            vk,
            provider,
            elapsed,
            out_tokens,
            finish,
        )
        return response

    def consult(
        self,
        system: str,
        user: str,
        vk: Literal["advisor", "dream", "fast"] = "advisor",
        max_tokens: int = 4096,
        temperature: float | None = None,
        response_format: dict | None = None,
        *,
        ref: str | None = None,
    ) -> str:
        """Send a consult request.

        Args:
            system: System prompt.
            user: User message content.
            vk: Which model profile to use (advisor or dream).
            max_tokens: Max output tokens.
            temperature: Sampling temperature. ``None`` (the default) OMITS the field
                entirely so the provider's own default applies — some endpoints
                (Nebius/Kimi-K3) reject any client-supplied value.
            response_format: Optional OpenAI-style ``response_format`` (e.g. a
                ``{"type": "json_schema", ...}`` structured-output spec). Passed
                through to the provider verbatim when set; omitted otherwise.
                Kept provider-agnostic — callers own the schema.
            ref: Caller correlation id (e.g. a consult job_id), echoed in the call's log lines.

        Returns:
            The model's response text.
        """
        kwargs: dict = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if response_format is not None:
            kwargs["response_format"] = response_format

        response = self._send(vk, kwargs, ref)
        return response.choices[0].message.content or ""

    def consult_vision(
        self,
        system: str,
        user_text: str,
        images: list[str],
        vk: str = "dream",
        max_tokens: int = 16384,
        temperature: float = 0.3,
    ) -> str:
        """Send a multimodal consult request with images.

        Builds an OpenAI Vision API content array: text block + image_url
        blocks with base64 data URIs. Routes through Kimi K2.6 via the
        dream VK — the fast model (DeepSeek V4 Flash) does not support vision.

        Args:
            system: System prompt.
            user_text: Text portion of the user message.
            images: List of base64 data URI strings (e.g. "data:image/png;base64,...").
            vk: VK profile (must support vision — "dream" only in bifrost mode).
            max_tokens: Max output tokens.
            temperature: Sampling temperature.

        Returns:
            The model's response text.
        """
        # Build multimodal content array
        content: list[dict] = [
            {"type": "text", "text": user_text},
        ]
        for img_uri in images:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": img_uri},
                }
            )

        response = self._send(
            vk,
            {
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": content},
                ],
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            None,
        )
        return response.choices[0].message.content or ""
