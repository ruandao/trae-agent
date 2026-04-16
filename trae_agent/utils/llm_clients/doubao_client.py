# Copyright (c) 2025 ByteDance Ltd. and/or its affiliates
# SPDX-License-Identifier: MIT

"""Doubao client wrapper with tool integrations"""

import openai

from trae_agent.utils.config import ModelConfig
from trae_agent.utils.llm_clients.openai_compatible_base import (
    OpenAICompatibleClient,
    ProviderConfig,
)


class DoubaoProvider(ProviderConfig):
    """Doubao provider configuration."""

    def create_client(
        self,
        api_key: str,
        base_url: str | None,
        api_version: str | None,
        timeout: float | None = None,
    ) -> openai.OpenAI:
        """Create OpenAI client with Doubao base URL."""
        effective_timeout = timeout or 120.0
        import httpx

        http_client = httpx.Client(timeout=httpx.Timeout(effective_timeout, connect=60.0))
        return openai.OpenAI(base_url=base_url, api_key=api_key, http_client=http_client)

    def get_service_name(self) -> str:
        """Get the service name for retry logging."""
        return "Doubao"

    def get_provider_name(self) -> str:
        """Get the provider name for trajectory recording."""
        return "doubao"

    def get_extra_headers(self) -> dict[str, str]:
        """Get Doubao-specific headers (none needed)."""
        return {}

    def supports_tool_calling(self, model_name: str) -> bool:
        """Check if the model supports tool calling."""
        # Doubao models generally support tool calling
        return True


class DoubaoClient(OpenAICompatibleClient):
    """Doubao client wrapper that maintains compatibility while using the new architecture."""

    def __init__(self, model_config: ModelConfig):
        super().__init__(model_config, DoubaoProvider())
