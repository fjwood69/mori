"""Bifrost / direct provider client — routes model calls through either
Bifrost VKs or a direct OpenAI-compatible endpoint.

Two modes controlled by MORI_PROVIDER_MODE env var:
- bifrost (default): Uses dummy VK API keys matched by Bifrost gateway
- direct: Uses a real provider API key + base URL
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from openai import OpenAI

logger = logging.getLogger(__name__)

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
        self.fast_model = os.environ.get("MORI_FAST_MODEL", "deepseek/deepseek-v4-flash")

        if self.mode == "direct" and not self.direct_api_key:
            logger.warning(
                "MORI_PROVIDER_MODE=direct but MORI_API_KEY is not set. "
                "API calls will likely fail."
            )

    def _client_for(self, vk: str = "advisor") -> OpenAI:
        """Create an OpenAI client for the appropriate provider.

        In bifrost mode, vk selects the VK key. In direct mode, vk
        selects the model (advisor vs dream).
        """
        if self.mode == "direct":
            model = self.direct_dream_model if vk == "dream" else self.direct_model
            return OpenAI(
                base_url=self.base_url,
                api_key=self.direct_api_key,
                timeout=self.timeout,
            ), model
        else:
            key_map = {
                "advisor": self.bifrost_advisor_vk,
                "dream": self.bifrost_dream_vk,
                "fast": self.bifrost_fast_vk,
            }
            effective_key = VK_CONFIG.get(vk) or key_map.get(vk, self.bifrost_advisor_vk)
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
            ), model

    def consult(
        self,
        system: str,
        user: str,
        vk: Literal["advisor", "dream", "fast"] = "advisor",
        max_tokens: int = 4096,
        temperature: float = 0.3,
    ) -> str:
        """Send a consult request.

        Args:
            system: System prompt.
            user: User message content.
            vk: Which model profile to use (advisor or dream).
            max_tokens: Max output tokens.
            temperature: Sampling temperature.

        Returns:
            The model's response text.
        """
        # Kimi K2.6 requires max_tokens >= 200
        if vk == "advisor" and max_tokens < 200:
            max_tokens = 200

        client, model = self._client_for(vk)

        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            logger.error("Consult failed (vk=%s, mode=%s): %s", vk, self.mode, e)
            raise