# Copyright (c) 2025 ByteDance Ltd. and/or its affiliates
# SPDX-License-Identifier: MIT

"""DeepSeek API client wrapper with tool integration."""

import json
import time
from typing import override

import openai
from openai.types.responses import (
    FunctionToolParam,
    ResponseFunctionToolCallParam,
    ResponseInputParam,
    ToolParam,
)
from openai.types.responses.response_input_param import FunctionCallOutput

from trae_agent.tools.base import Tool, ToolCall, ToolResult
from trae_agent.utils.config import ModelConfig
from trae_agent.utils.llm_clients.base_client import BaseLLMClient
from trae_agent.utils.llm_clients.llm_basics import LLMMessage, LLMResponse, LLMUsage
from trae_agent.utils.llm_clients.llm_logger import LLMLogger
from trae_agent.utils.llm_clients.retry_utils import retry_with


class DeepSeekClient(BaseLLMClient):
    """DeepSeek client wrapper with tool schema generation."""

    def __init__(self, model_config: ModelConfig):
        super().__init__(model_config)

        self.client: openai.OpenAI = openai.OpenAI(api_key=self.api_key, base_url=self.base_url)
        self.message_history: ResponseInputParam = []
        self.logger = LLMLogger(model_config.model)

    @override
    def set_chat_history(self, messages: list[LLMMessage]) -> None:
        """Set the chat history."""
        self.message_history = self.parse_messages(messages)

    def _create_deepseek_response(
        self,
        api_call_input: ResponseInputParam,
        model_config: ModelConfig,
        tool_schemas: list[ToolParam] | None,
    ) -> openai.types.chat.ChatCompletion:
        """Create a response using DeepSeek API. This method will be decorated with retry logic."""
        # 转换工具模式为OpenAI兼容格式
        openai_tools = None
        if tool_schemas:
            openai_tools = []
            for tool in tool_schemas:
                # 检查tool是对象还是字典
                if hasattr(tool, "name"):
                    # 如果是对象，使用属性访问
                    openai_tools.append(
                        {
                            "type": "function",
                            "function": {
                                "name": tool.name,
                                "description": tool.description,
                                "parameters": tool.parameters,
                            },
                        }
                    )
                else:
                    # 如果是字典，使用键访问
                    openai_tools.append(
                        {
                            "type": "function",
                            "function": {
                                "name": tool.get("name", ""),
                                "description": tool.get("description", ""),
                                "parameters": tool.get("parameters", {}),
                            },
                        }
                    )

        # 打印api_call_input，以便调试
        print("API call input:")
        for i, msg in enumerate(api_call_input):
            if hasattr(msg, "role"):
                print(f"Index {i}: {msg.role} - Content: '{msg.content[:100]}...'")
            else:
                print(
                    f"Index {i}: {msg.get('role')} - Content: '{msg.get('content', '')[:100]}...'"
                )

        # 转换LLMMessage对象为字典格式并清理消息
        cleaned_messages = []
        for msg in api_call_input:
            # 处理LLMMessage对象
            if hasattr(msg, "role"):
                if msg.tool_result:
                    # 工具结果消息
                    msg_dict = {
                        "role": "tool",
                        "content": msg.tool_result.result or msg.tool_result.error or "",
                        "tool_call_id": msg.tool_result.call_id,
                    }
                elif msg.tool_call:
                    # 工具调用消息
                    msg_dict = {
                        "role": "assistant",
                        "content": msg.content or "",
                        "tool_calls": [
                            {
                                "id": msg.tool_call.call_id,
                                "function": {
                                    "name": msg.tool_call.name,
                                    "arguments": json.dumps(msg.tool_call.arguments),
                                },
                                "type": "function",
                            }
                        ],
                    }
                else:
                    # 普通消息
                    msg_dict = {
                        "role": msg.role,
                        "content": msg.content or "",
                    }
            else:
                # 已经是字典格式，直接使用
                msg_dict = msg

            # 跳过空消息或格式不正确的消息
            if not msg_dict or not msg_dict.get("role"):
                continue

            # 确保没有连续的助手消息，并且消息格式正确
            if not cleaned_messages:
                # 第一个消息，直接添加
                if msg_dict.get("content") or msg_dict.get("tool_calls"):
                    cleaned_messages.append(msg_dict)
            else:
                # 获取前一个消息的角色
                prev_role = cleaned_messages[-1].get("role")
                current_role = msg_dict.get("role")

                # 如果当前消息不是助手消息，或者前一个消息不是助手消息，添加它；
                # 且助手消息需有内容或工具调用。
                if (current_role != "assistant" or prev_role != "assistant") and (
                    current_role != "assistant"
                    or (msg_dict.get("content") or msg_dict.get("tool_calls"))
                ):
                    # 对于助手消息，确保内容完整
                    if current_role == "assistant":
                        content = msg_dict.get("content", "")
                        # 检查是否有未关闭的标签
                        if "<task>" in content and "</task>" not in content:
                            # 跳过不完整的助手消息
                            continue
                        if "<details>" in content and "</details>" not in content:
                            # 跳过不完整的助手消息
                            continue
                        if "<tags>" in content and "</tags>" not in content:
                            # 跳过不完整的助手消息
                            continue
                        # 检查消息是否被截断
                        if content.endswith("...") or "..." in content:
                            # 跳过被截断的助手消息
                            continue
                    # 添加消息
                    cleaned_messages.append(msg_dict)

        # 打印清理后的消息，以便调试
        print("Cleaned messages:")
        for i, msg in enumerate(cleaned_messages):
            role = msg.get("role")
            content = msg.get("content", "")
            tool_calls = msg.get("tool_calls", [])
            print(
                f"Index {i}: {role} - Content: '{content[:100]}...' - Tool calls: {len(tool_calls)}"
            )

        # 打印完整的消息，以便调试
        print("Full cleaned messages:")
        print(cleaned_messages)

        return self.client.chat.completions.create(
            messages=cleaned_messages,
            model=model_config.model,
            tools=openai_tools if openai_tools else openai.NOT_GIVEN,
            temperature=model_config.temperature,
            top_p=model_config.top_p,
            max_tokens=model_config.max_tokens,
        )

    @override
    def chat(
        self,
        messages: list[LLMMessage],
        model_config: ModelConfig,
        tools: list[Tool] | None = None,
        reuse_history: bool = True,
    ) -> LLMResponse:
        """Send chat messages to DeepSeek with optional tool support."""
        deepseek_messages: ResponseInputParam = self.parse_messages(messages)

        # Clean deepseek_messages to avoid consecutive assistant messages
        if len(deepseek_messages) > 1:
            cleaned_deepseek_messages = []
            for msg in deepseek_messages:
                # Only add the message if it's not an assistant message or if the previous message is not an assistant message
                if (
                    not cleaned_deepseek_messages
                    or msg.get("role") != "assistant"
                    or cleaned_deepseek_messages[-1].get("role") != "assistant"
                ):
                    cleaned_deepseek_messages.append(msg)
            deepseek_messages = cleaned_deepseek_messages

        # Log the cleaned deepseek_messages for debugging
        self.logger.log_request(
            messages=[
                {
                    "role": "debug",
                    "content": "Cleaned deepseek_messages",
                    "debug": deepseek_messages,
                }
            ],
            tool_schemas=None,
            model_config={"model": model_config.model, "debug": True},
        )

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

        api_call_input: ResponseInputParam = []
        if reuse_history:
            api_call_input.extend(self.message_history)
        api_call_input.extend(deepseek_messages)

        # Log the final api_call_input for debugging
        self.logger.log_request(
            messages=[
                {"role": "debug", "content": "Final api_call_input", "debug": api_call_input}
            ],
            tool_schemas=None,
            model_config={"model": model_config.model, "debug": True},
        )

        # Log the request
        model_config_dict = {
            "model": model_config.model,
            "temperature": model_config.temperature,
            "top_p": model_config.top_p,
            "max_tokens": model_config.max_tokens,
        }
        self.logger.log_request(
            messages=[msg.__dict__ for msg in messages],
            tool_schemas=tool_schemas,
            model_config=model_config_dict,
        )

        # Apply retry decorator to the API call
        retry_decorator = retry_with(
            func=self._create_deepseek_response,
            provider_name="DeepSeek",
            max_retries=model_config.max_retries,
        )

        # Measure latency
        start_time = time.time()
        try:
            response = retry_decorator(api_call_input, model_config, tool_schemas)
            latency = time.time() - start_time

            choice = response.choices[0]

            content = choice.message.content or ""
            tool_calls: list[ToolCall] | None = None
            if choice.message.tool_calls:
                tool_calls = []
                for tool_call in choice.message.tool_calls:
                    tool_calls.append(
                        ToolCall(
                            call_id=tool_call.id,
                            name=tool_call.function.name,
                            arguments=(
                                json.loads(tool_call.function.arguments)
                                if tool_call.function.arguments
                                else {}
                            ),
                        )
                    )

            # Update message history with current turn input first.
            if reuse_history and deepseek_messages:
                self.message_history.extend(deepseek_messages)

            # Then append the model response for this turn.
            if tool_calls:
                self.message_history.append(
                    {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": [
                            {
                                "id": tool_call.call_id,
                                "function": {
                                    "name": tool_call.name,
                                    "arguments": json.dumps(tool_call.arguments),
                                },
                                "type": "function",
                            }
                            for tool_call in tool_calls
                        ],
                    }
                )
            elif content:
                self.message_history.append(
                    {
                        "role": "assistant",
                        "content": content,
                    }
                )

            usage = None
            if response.usage:
                usage = LLMUsage(
                    input_tokens=response.usage.prompt_tokens or 0,
                    output_tokens=response.usage.completion_tokens or 0,
                )

            llm_response = LLMResponse(
                content=content,
                usage=usage,
                model=response.model,
                finish_reason=choice.finish_reason,
                tool_calls=tool_calls if tool_calls and len(tool_calls) > 0 else None,
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
                    provider="deepseek",
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
        """Parse the messages to DeepSeek format."""
        deepseek_messages: ResponseInputParam = []
        for msg in messages:
            # Skip messages with no content or invalid structure
            if not msg:
                continue

            if msg.tool_result:
                # For tool results, use the OpenAI compatible format
                deepseek_messages.append(
                    {
                        "role": "tool",
                        "content": msg.tool_result.result or msg.tool_result.error or "",
                        "tool_call_id": msg.tool_result.call_id,
                    }
                )
            elif msg.tool_call:
                # For tool calls, use the OpenAI compatible format
                deepseek_messages.append(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": msg.tool_call.call_id,
                                "function": {
                                    "name": msg.tool_call.name,
                                    "arguments": json.dumps(msg.tool_call.arguments),
                                },
                                "type": "function",
                            }
                        ],
                    }
                )
            else:
                if not msg.content:
                    # Skip messages with no content
                    continue
                if msg.role == "system":
                    deepseek_messages.append({"role": "system", "content": msg.content})
                elif msg.role == "user":
                    deepseek_messages.append({"role": "user", "content": msg.content})
                elif msg.role == "assistant":
                    # Only add assistant message if it's not empty and not following another assistant message
                    if not deepseek_messages or deepseek_messages[-1].get("role") != "assistant":
                        deepseek_messages.append({"role": "assistant", "content": msg.content})
                else:
                    # Skip messages with invalid role
                    continue

        # Final cleaning to ensure no consecutive assistant messages
        if len(deepseek_messages) > 1:
            cleaned_messages = []
            for msg in deepseek_messages:
                if (
                    not cleaned_messages
                    or msg.get("role") != "assistant"
                    or cleaned_messages[-1].get("role") != "assistant"
                ):
                    cleaned_messages.append(msg)
            deepseek_messages = cleaned_messages

        return deepseek_messages

    def _clean_message_history(self):
        """Clean the message history to avoid consecutive assistant messages."""
        if not self.message_history:
            return

        cleaned_history = []
        for msg in self.message_history:
            # Get role from either LLMMessage object or dictionary
            msg_role = msg.role if hasattr(msg, "role") else msg.get("role", "")
            # Get previous role from either LLMMessage object or dictionary
            prev_role = (
                cleaned_history[-1].role
                if (cleaned_history and hasattr(cleaned_history[-1], "role"))
                else cleaned_history[-1].get("role", "")
                if cleaned_history
                else ""
            )
            # Only add the message if it's not an assistant message or if the previous message is not an assistant message
            if not cleaned_history or msg_role != "assistant" or prev_role != "assistant":
                cleaned_history.append(msg)

        self.message_history = cleaned_history

    def parse_tool_call(self, tool_call: ToolCall) -> ResponseFunctionToolCallParam:
        """Parse the tool call from the LLM response."""
        return ResponseFunctionToolCallParam(
            arguments=json.dumps(tool_call.arguments),
            call_id=tool_call.call_id,
            name=tool_call.name,
            type="function_call",
        )

    def parse_tool_call_result(self, tool_call_result: ToolResult) -> FunctionCallOutput:
        """Parse the tool call result from the LLM response to FunctionCallOutput format."""
        result_content: str = ""
        if tool_call_result.result is not None:
            result_content += str(tool_call_result.result)
        if tool_call_result.error:
            result_content += f"\nError: {tool_call_result.error}"
        result_content = result_content.strip()

        return FunctionCallOutput(
            type="function_call_output",  # Explicitly set the type field
            call_id=tool_call_result.call_id,
            output=result_content,
        )
