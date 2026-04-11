# Copyright (c) 2025 ByteDance Ltd. and/or its affiliates
# SPDX-License-Identifier: MIT

"""Logger for LLM API calls."""

import json
import os
from dataclasses import is_dataclass
from datetime import datetime


class LLMLogger:
    """Logger for LLM API calls."""

    def __init__(self, model_name: str):
        """Initialize the logger.

        Args:
            model_name: The name of the model being used.
        """
        self.model_name = model_name
        self.log_dir = os.getenv("TRAE_LOG_DIR", os.path.expanduser("~/.trae-agent/logs"))
        os.makedirs(self.log_dir, exist_ok=True)
        self.log_file = os.path.join(self.log_dir, f"{model_name}.log")

    def log_request(self, messages: list, tool_schemas: list | None, model_config: dict):
        """Log the request details.

        Args:
            messages: The messages being sent to the API.
            tool_schemas: The tool schemas being sent to the API.
            model_config: The model configuration.
        """
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "type": "request",
            "model": self.model_name,
            "messages": messages,
            "tool_schemas": tool_schemas,
            "model_config": model_config,
        }
        self._write_log(log_entry)

    def log_response(self, response: dict, usage: dict | None, latency: float):
        """Log the response details.

        Args:
            response: The response from the API.
            usage: The usage details from the API.
            latency: The latency of the API call in seconds.
        """
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "type": "response",
            "model": self.model_name,
            "response": response,
            "usage": usage,
            "latency": latency,
        }
        self._write_log(log_entry)

    def log_error(self, error: str, traceback: str | None = None):
        """Log an error.

        Args:
            error: The error message.
            traceback: The traceback of the error.
        """
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "type": "error",
            "model": self.model_name,
            "error": error,
            "traceback": traceback,
        }
        self._write_log(log_entry)

    def _write_log(self, log_entry: dict):
        """Write the log entry to the log file.

        Args:
            log_entry: The log entry to write.
        """
        safe_entry = self._to_json_safe(log_entry)
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
        with open(self.log_file, "a", encoding="utf-8") as f:
            json.dump(safe_entry, f, ensure_ascii=False)
            f.write("\n")

    def _to_json_safe(self, value):
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(k): self._to_json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._to_json_safe(v) for v in value]
        if is_dataclass(value):
            return {
                key: self._to_json_safe(val)
                for key, val in value.__dict__.items()
                if not key.startswith("_")
            }
        if hasattr(value, "__dict__"):
            return {
                key: self._to_json_safe(val)
                for key, val in value.__dict__.items()
                if not key.startswith("_")
            }
        return str(value)
