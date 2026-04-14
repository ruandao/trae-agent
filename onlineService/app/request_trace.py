"""FastAPI 请求内与出站 HTTP 共用的 trace id（头 ``X-Trace-Id`` / 环境 ``TRACE_ID``）。"""

from __future__ import annotations

import contextvars
import os
import re
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.requests import Request

TRACE_ID_HEADER = "X-Trace-Id"
TRACE_ID_ENV = "TRACE_ID"
_TRACE_ID_MAX = 256
_SAFE_RE = re.compile(r"^[A-Za-z0-9._:-]{8,256}$")

trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trae_online_trace_id", default=None
)


def _normalize_header_value(raw: str | None) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    if len(s) > _TRACE_ID_MAX:
        s = s[:_TRACE_ID_MAX]
    if not _SAFE_RE.match(s):
        return None
    return s


def trace_id_for_incoming_request(request: Request) -> str:
    raw = request.headers.get("x-trace-id") or request.headers.get("X-Trace-Id")
    tid = _normalize_header_value(raw)
    if tid:
        return tid
    return str(uuid.uuid4())


def get_trace_id_for_outbound_http() -> str:
    ctx = trace_id_var.get()
    if ctx:
        return ctx
    env = (os.environ.get(TRACE_ID_ENV) or "").strip()
    if env:
        return env[:_TRACE_ID_MAX]
    return str(uuid.uuid4())
