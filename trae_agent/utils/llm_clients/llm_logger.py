# Copyright (c) 2025 ByteDance Ltd. and/or its affiliates
# SPDX-License-Identifier: MIT

"""Logger for LLM API calls."""

import json
import os
import time
from datetime import datetime


class LLMLogger:
    """Logger for LLM API calls."""

    def __init__(self, model_name: str):
        """Initialize the logger.

        Args:
            model_name: The name of the model being used.
        """
        self.model_name = model_name
        self.log_dir = "logs"
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
        with open(self.log_file, "a", encoding="utf-8") as f:
            json.dump(log_entry, f, ensure_ascii=False)
            f.write("\n")
