# Copyright (c) 2025 ByteDance Ltd. and/or its affiliates
# SPDX-License-Identifier: MIT

"""Anthropic API client wrapper with tool integration."""

import json
import time
from typing import override

import anthropic
import httpx
from anthropic.types.tool_union_param import TextEditor20250429

from trae_agent.tools.base import Tool, ToolCall, ToolResult
from trae_agent.utils.config import ModelConfig
from trae_agent.utils.llm_clients.base_client import BaseLLMClient
from trae_agent.utils.llm_clients.llm_basics import LLMMessage, LLMResponse, LLMUsage
from trae_agent.utils.llm_clients.llm_logger import LLMLogger
from trae_agent.utils.llm_clients.retry_utils import retry_with


class AnthropicClient(BaseLLMClient):
    """Anthropic client wrapper with tool schema generation."""

    DEFAULT_TIMEOUT = 120.0

    def __init__(self, model_config: ModelConfig):
        super().__init__(model_config)

        timeout = getattr(model_config, "timeout", None) or self.DEFAULT_TIMEOUT
        http_client = httpx.Client(timeout=httpx.Timeout(timeout, connect=60.0))
        self.client: anthropic.Anthropic = anthropic.Anthropic(
            api_key=self.api_key, base_url=self.base_url, http_client=http_client
        )
        self.message_history: list[anthropic.types.MessageParam] = []
        self.system_message: str | anthropic.NotGiven = anthropic.NOT_GIVEN
        self.logger = LLMLogger(model_config.model)

    @override
    def set_chat_history(self, messages: list[LLMMessage]) -> None:
        """Set the chat history."""
        self.message_history = self.parse_messages(messages)

    def _create_anthropic_response(
        self,
        model_config: ModelConfig,
        tool_schemas: list[anthropic.types.ToolUnionParam] | anthropic.NotGiven,
    ) -> anthropic.types.Message:
        """Create a response using Anthropic API. This method will be decorated with retry logic."""
        return self.client.messages.create(
            model=model_config.model,
            messages=self.message_history,
            max_tokens=model_config.max_tokens,
            system=self.system_message,
            tools=tool_schemas,
            temperature=model_config.temperature,
            top_p=model_config.top_p,
            top_k=model_config.top_k,
        )

    @override
    def chat(
        self,
        messages: list[LLMMessage],
        model_config: ModelConfig,
        tools: list[Tool] | None = None,
        reuse_history: bool = True,
    ) -> LLMResponse:
        """Send chat messages to Anthropic with optional tool support."""
        # Convert messages to Anthropic format
        anthropic_messages: list[anthropic.types.MessageParam] = self.parse_messages(messages)

        self.message_history = (
            self.message_history + anthropic_messages if reuse_history else anthropic_messages
        )

        # Add tools if provided
        tool_schemas: list[anthropic.types.ToolUnionParam] | anthropic.NotGiven = (
            anthropic.NOT_GIVEN
        )
        if tools:
            tool_schemas = []
            for tool in tools:
                if tool.name == "edit_file":
                    tool_schemas.append(
                        TextEditor20250429(
                            name="edit_file",
                            type="text_editor_20250429",
                        )
                    )
                elif tool.name == "bash":
                    tool_schemas.append(
                        anthropic.types.ToolBash20250124Param(name="bash", type="bash_20250124")
                    )
                else:
                    tool_schemas.append(
                        anthropic.types.ToolParam(
                            name=tool.name,
                            description=tool.description,
                            input_schema=tool.get_input_schema(),
                        )
                    )

        # Log the request
        model_config_dict = {
            "model": model_config.model,
            "temperature": model_config.temperature,
            "top_p": model_config.top_p,
            "top_k": model_config.top_k,
            "max_tokens": model_config.max_tokens,
        }
        self.logger.log_request(
            messages=[msg.__dict__ for msg in messages],
            tool_schemas=tool_schemas,
            model_config=model_config_dict,
        )

        # Apply retry decorator to the API call
        retry_decorator = retry_with(
            func=self._create_anthropic_response,
            provider_name="Anthropic",
            max_retries=model_config.max_retries,
        )

        # Measure latency
        start_time = time.time()
        try:
            response = retry_decorator(model_config, tool_schemas)
            latency = time.time() - start_time

            # Handle tool calls in response
            content = ""
            tool_calls: list[ToolCall] = []

            for content_block in response.content:
                if content_block.type == "text":
                    content += content_block.text
                    self.message_history.append(
                        anthropic.types.MessageParam(role="assistant", content=content_block.text)
                    )
                elif content_block.type == "tool_use":
                    tool_calls.append(
                        ToolCall(
                            call_id=content_block.id,
                            name=content_block.name,
                            arguments=content_block.input,  # pyright: ignore[reportArgumentType]
                        )
                    )
                    self.message_history.append(
                        anthropic.types.MessageParam(role="assistant", content=[content_block])
                    )

            usage = None
            if response.usage:
                usage = LLMUsage(
                    input_tokens=response.usage.input_tokens or 0,
                    output_tokens=response.usage.output_tokens or 0,
                    cache_creation_input_tokens=response.usage.cache_creation_input_tokens or 0,
                    cache_read_input_tokens=response.usage.cache_read_input_tokens or 0,
                )

            llm_response = LLMResponse(
                content=content,
                usage=usage,
                model=response.model,
                finish_reason=response.stop_reason,
                tool_calls=tool_calls if len(tool_calls) > 0 else None,
            )

            # Log the response
            response_dict = {
                "content": llm_response.content,
                "tool_calls": [tc.__dict__ for tc in tool_calls] if tool_calls else None,
                "finish_reason": llm_response.finish_reason,
                "model": llm_response.model,
            }
            usage_dict = {
                "input_tokens": llm_response.usage.input_tokens if llm_response.usage else 0,
                "output_tokens": llm_response.usage.output_tokens if llm_response.usage else 0,
            }
            self.logger.log_response(
                response=response_dict,
                usage=usage_dict,
                latency=latency,
            )

            # Record trajectory if recorder is available
            if self.trajectory_recorder:
                self.trajectory_recorder.record_llm_interaction(
                    messages=messages,
                    response=llm_response,
                    provider="anthropic",
                    model=model_config.model,
                    tools=tools,
                )

            return llm_response
        except Exception as e:
            latency = time.time() - start_time
            self.logger.log_error(
                error=str(e),
                traceback=str(e.__traceback__),
            )
            raise

    def parse_messages(self, messages: list[LLMMessage]) -> list[anthropic.types.MessageParam]:
        """Parse the messages to Anthropic format."""
        anthropic_messages: list[anthropic.types.MessageParam] = []
        for msg in messages:
            if msg.role == "system":
                self.system_message = msg.content if msg.content else anthropic.NOT_GIVEN
            elif msg.tool_result:
                anthropic_messages.append(
                    anthropic.types.MessageParam(
                        role="user",
                        content=[self.parse_tool_call_result(msg.tool_result)],
                    )
                )
            elif msg.tool_call:
                anthropic_messages.append(
                    anthropic.types.MessageParam(
                        role="assistant", content=[self.parse_tool_call(msg.tool_call)]
                    )
                )
            else:
                if msg.role == "user":
                    role = "user"
                elif msg.role == "assistant":
                    role = "assistant"
                else:
                    raise ValueError(f"Invalid message role: {msg.role}")

                if not msg.content:
                    raise ValueError("Message content is required")

                anthropic_messages.append(
                    anthropic.types.MessageParam(role=role, content=msg.content)
                )
        return anthropic_messages

    def parse_tool_call(self, tool_call: ToolCall) -> anthropic.types.ToolUseBlockParam:
        """Parse the tool call from the LLM response."""
        return anthropic.types.ToolUseBlockParam(
            type="tool_use",
            id=tool_call.call_id,
            name=tool_call.name,
            input=json.dumps(tool_call.arguments),
        )

    def parse_tool_call_result(
        self, tool_call_result: ToolResult
    ) -> anthropic.types.ToolResultBlockParam:
        """Parse the tool call result from the LLM response."""
        result: str = ""
        if tool_call_result.result:
            result = result + tool_call_result.result + "\n"
        if tool_call_result.error:
            result += "Tool call failed with error:\n"
            result += tool_call_result.error
        result = result.strip()

        # Provide a default error message if the tool failed but didn't provide details
        if not tool_call_result.success and not result:
            result = "Tool execution failed without providing error details."

        return anthropic.types.ToolResultBlockParam(
            tool_use_id=tool_call_result.call_id,
            type="tool_result",
            content=result,
            is_error=not tool_call_result.success,
        )
