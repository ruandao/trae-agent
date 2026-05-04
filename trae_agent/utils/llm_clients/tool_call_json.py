# Copyright (c) 2025 ByteDance Ltd. and/or its affiliates
# SPDX-License-Identifier: MIT

"""Robust parsing of chat tool-call ``function.arguments`` strings."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(
    r"^```(?:json)?\s*(.*?)\s*```\s*$",
    re.DOTALL | re.IGNORECASE,
)


def parse_tool_call_arguments(arguments: str | dict[str, Any] | None) -> dict[str, Any]:
    """Return a dict from provider tool arguments, never raising JSONDecodeError.

    Some models return markdown fences, trailing commentary after a JSON object,
    or other strict-invalid JSON; ``json.loads`` then fails with
    ``Expecting value: line 1 column …`` and aborts the run.
    """
    if arguments is None:
        return {}
    if isinstance(arguments, dict):
        return arguments
    s = str(arguments).strip()
    if not s:
        return {}
    m = _FENCE_RE.match(s)
    if m:
        s = m.group(1).strip()
    decoder = json.JSONDecoder()
    for i, ch in enumerate(s):
        if ch not in "{[":
            continue
        try:
            val, _end = decoder.raw_decode(s[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(val, dict):
            return val
    try:
        val = json.loads(s)
        return val if isinstance(val, dict) else {}
    except json.JSONDecodeError:
        return {}
