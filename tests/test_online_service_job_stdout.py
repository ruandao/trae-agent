"""onlineService job stdout 分块解码与 subprocess 环境（UTF-8 / COLUMNS）。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_SERVICE = Path(__file__).resolve().parents[1] / "onlineService"
if str(_SERVICE) not in sys.path:
    sys.path.insert(0, str(_SERVICE))

from app.jobs import _iter_stdout_text, _job_subprocess_env  # noqa: E402


def test_iter_stdout_utf8_split_across_binary_chunks() -> None:
    async def _run() -> str:
        s = "你好"
        raw = s.encode("utf-8")
        r = asyncio.StreamReader()
        r.feed_data(raw[:1])
        r.feed_data(raw[1:])
        r.feed_eof()
        parts: list[str] = []
        async for t in _iter_stdout_text(r):
            parts.append(t)
        return "".join(parts)

    assert asyncio.run(_run()) == "你好"


def test_iter_stdout_multiple_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRAE_JOB_STDOUT_CHUNK_BYTES", "64")

    async def _run() -> tuple[str, int]:
        r = asyncio.StreamReader()
        r.feed_data(b"x" * 400)
        r.feed_eof()
        parts: list[str] = []
        async for t in _iter_stdout_text(r):
            parts.append(t)
        return "".join(parts), len(parts)

    joined, n = asyncio.run(_run())
    assert joined == "x" * 400
    assert n >= 4


def test_job_subprocess_env_sets_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRAE_JOB_COLUMNS", "240")
    env = _job_subprocess_env()
    assert env["COLUMNS"] == "240"
    assert env["PYTHONUNBUFFERED"] == "1"


def test_job_subprocess_env_default_wide_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRAE_JOB_COLUMNS", raising=False)
    env = _job_subprocess_env()
    assert env["COLUMNS"] == "999999"


def test_job_subprocess_env_unlimited_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRAE_JOB_COLUMNS", "unlimited")
    env = _job_subprocess_env()
    assert env["COLUMNS"] == "999999"
