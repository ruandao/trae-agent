# Copyright (c) 2025 ByteDance Ltd. and/or its affiliates
# SPDX-License-Identifier: MIT

"""
Ollama API client wrapper with tool integration
"""

import json
import time
import uuid
from typing import override

import openai
from ollama import chat as ollama_chat  # pyright: ignore[reportUnknownVariableType]
from openai.types.responses import (
    FunctionToolParam,
    ResponseFunctionToolCallParam,
    ResponseInputParam,
)
from openai.types.responses.response_input_param import FunctionCallOutput

from trae_agent.tools.base import Tool, ToolCall, ToolResult
from trae_agent.utils.config import ModelConfig
from trae_agent.utils.llm_clients.base_client import BaseLLMClient
from trae_agent.utils.llm_clients.llm_basics import LLMMessage, LLMResponse
from trae_agent.utils.llm_clients.llm_logger import LLMLogger
from trae_agent.utils.llm_clients.retry_utils import retry_with


class OllamaClient(BaseLLMClient):
    def __init__(self, model_config: ModelConfig):
        super().__init__(model_config)

        self.client: openai.OpenAI = openai.OpenAI(
            # by default ollama doesn't require any api key. It should set to be "ollama".
            api_key=self.api_key,
            base_url=model_config.model_provider.base_url
            if model_config.model_provider.base_url
            else "http://localhost:11434/v1",
        )

        self.message_history: ResponseInputParam = []
        self.logger = LLMLogger(model_config.model)

    @override
    def set_chat_history(self, messages: list[LLMMessage]) -> None:
        self.message_history = self.parse_messages(messages)

    def _create_ollama_response(
        self,
        model_config: ModelConfig,
        tool_schemas: list[FunctionToolParam] | None,
    ):
        """Create a response using Ollama API. This method will be decorated with retry logic."""
        tools_param = None
        if tool_schemas:
            tools_param = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool["parameters"],
                    },
                }
                for tool in tool_schemas
            ]
        return ollama_chat(
            messages=self.message_history,
            model=model_config.model,
            tools=tools_param,
        )

    @override
    def chat(
        self,
        messages: list[LLMMessage],
        model_config: ModelConfig,
        tools: list[Tool] | None = None,
        reuse_history: bool = True,
    ) -> LLMResponse:
        """
        A rewritten version of ollama chan
        """
        msgs: ResponseInputParam = self.parse_messages(messages)

        tool_schemas = None
        if tools:
            tool_schemas = [
                FunctionToolParam(
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.get_input_schema(),
                    strict=True,
                    type="function",
                )
                for tool in tools
            ]

        if reuse_history:
            self.message_history = self.message_history + msgs
        else:
            self.message_history = msgs

        # Log the request
        model_config_dict = {
            "model": model_config.model,
            "temperature": model_config.temperature,
            "top_p": model_config.top_p,
            "top_k": model_config.top_k,
        }
        self.logger.log_request(
            messages=[msg.__dict__ for msg in messages],
            tool_schemas=tool_schemas,
            model_config=model_config_dict,
        )

        # Apply retry decorator to the API call
        retry_decorator = retry_with(
            func=self._create_ollama_response,
            provider_name="Ollama",
            max_retries=model_config.max_retries,
        )

        # Measure latency
        start_time = time.time()
        try:
            response = retry_decorator(model_config, tool_schemas)
            latency = time.time() - start_time

            content = ""
            tool_calls: list[ToolCall] = []

            if response.message.tool_calls:
                for tool in response.message.tool_calls:
                    tool_calls.append(
                        ToolCall(
                            call_id=self._id_generator(),
                            name=tool.function.name,
                            arguments=dict(tool.function.arguments),
                            id=self._id_generator(),
                        )
                    )
            else:
                # consider response is not a tool call
                content = str(response.message.content)

            llm_response = LLMResponse(
                content=content,
                usage=None,
                model=model_config.model,
                finish_reason=None,  # seems can't get finish reason will check docs soon
                tool_calls=tool_calls if len(tool_calls) > 0 else None,
            )

            # Log the response
            response_dict = {
                "content": llm_response.content,
                "tool_calls": [tc.__dict__ for tc in tool_calls] if tool_calls else None,
                "finish_reason": llm_response.finish_reason,
                "model": llm_response.model,
            }
            self.logger.log_response(
                response=response_dict,
                usage=None,
                latency=latency,
            )

            if self.trajectory_recorder:
                self.trajectory_recorder.record_llm_interaction(
                    messages=messages,
                    response=llm_response,
                    provider="ollama",
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

    def parse_messages(self, messages: list[LLMMessage]) -> ResponseInputParam:
        """
        Ollama parse messages should be compatible with openai handling
        """
        openai_messages: ResponseInputParam = []
        for msg in messages:
            if msg.tool_result:
                openai_messages.append(self.parse_tool_call_result(msg.tool_result))
            elif msg.tool_call:
                openai_messages.append(self.parse_tool_call(msg.tool_call))
            else:
                if not msg.content:
                    raise ValueError("Message content is required")
                if msg.role == "system":
                    openai_messages.append({"role": "system", "content": msg.content})
                elif msg.role == "user":
                    openai_messages.append({"role": "user", "content": msg.content})
                elif msg.role == "assistant":
                    openai_messages.append({"role": "assistant", "content": msg.content})
                else:
                    raise ValueError(f"Invalid message role: {msg.role}")
        return openai_messages

    def parse_tool_call(self, tool_call: ToolCall) -> ResponseFunctionToolCallParam:
        """Parse the tool call from the LLM response."""
        return ResponseFunctionToolCallParam(
            call_id=tool_call.call_id,
            name=tool_call.name,
            arguments=json.dumps(tool_call.arguments),
            type="function_call",
        )

    def parse_tool_call_result(self, tool_call_result: ToolResult) -> FunctionCallOutput:
        """Parse the tool call result from the LLM response."""
        result: str = ""
        if tool_call_result.result:
            result = result + tool_call_result.result + "\n"
        if tool_call_result.error:
            result += tool_call_result.error
        result = result.strip()

        return FunctionCallOutput(
            call_id=tool_call_result.call_id,
            id=tool_call_result.id,
            output=result,
            type="function_call_output",
        )

    def _id_generator(self) -> str:
        """Generate a random ID string"""
        return str(uuid.uuid4())
