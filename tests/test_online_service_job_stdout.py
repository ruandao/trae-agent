"""任务子进程 stdout 分块解码与 subprocess 环境（UTF-8 / COLUMNS）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from trae_agent_online.online_job_stdio import (
    build_trae_run_cmd,
    filter_trae_output_chunk,
    finalize_trae_output_carry,
    iter_stdout_text,
    job_subprocess_env,
    resolve_model_cli_args_from_command_env,
)


def test_iter_stdout_utf8_split_across_binary_chunks() -> None:
    async def _run() -> str:
        s = "你好"
        raw = s.encode("utf-8")
        r = asyncio.StreamReader()
        r.feed_data(raw[:1])
        r.feed_data(raw[1:])
        r.feed_eof()
        parts: list[str] = []
        async for t in iter_stdout_text(r):
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
        async for t in iter_stdout_text(r):
            parts.append(t)
        return "".join(parts), len(parts)

    joined, n = asyncio.run(_run())
    assert joined == "x" * 400
    assert n >= 4


def test_job_subprocess_env_sets_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRAE_JOB_COLUMNS", "240")
    env = job_subprocess_env()
    assert env["COLUMNS"] == "240"
    assert env["PYTHONUNBUFFERED"] == "1"


def test_job_subprocess_env_default_wide_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRAE_JOB_COLUMNS", raising=False)
    env = job_subprocess_env()
    assert env["COLUMNS"] == "999999"


def test_job_subprocess_env_unlimited_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRAE_JOB_COLUMNS", "unlimited")
    env = job_subprocess_env()
    assert env["COLUMNS"] == "999999"


def test_filter_trae_output_chunk_removes_known_noise_lines() -> None:
    text = (
        "Changed working directory to: /tmp/demo\n"
        "Initialising MCP tools...\n"
        "real output\n"
        "Trajectory saved to: /tmp/a.json\n"
    )
    out, carry = filter_trae_output_chunk(text, "")
    assert carry == ""
    assert out == "real output\n"


def test_filter_trae_output_chunk_handles_split_lines() -> None:
    part1 = "Changed working directory to: /tmp/demo"
    out1, carry1 = filter_trae_output_chunk(part1, "")
    assert out1 == ""
    assert carry1 == part1

    part2 = "\nkept line\nTrajec"
    out2, carry2 = filter_trae_output_chunk(part2, carry1)
    assert out2 == "kept line\n"
    assert carry2 == "Trajec"

    part3 = "tory saved to: /tmp/t.json\nnext\n"
    out3, carry3 = filter_trae_output_chunk(part3, carry2)
    assert carry3 == ""
    assert out3 == "next\n"


def test_finalize_trae_output_carry_drops_noise_tail() -> None:
    assert finalize_trae_output_carry("Initialising MCP tools...") == ""
    assert finalize_trae_output_carry("useful tail") == "useful tail"


def test_resolve_model_cli_args_from_command_env() -> None:
    provider, model = resolve_model_cli_args_from_command_env(
        {"TRAE_MODEL_PROVIDER": "openai", "TRAE_MODEL": "gpt-4o-mini"}
    )
    assert provider == "openai"
    assert model == "gpt-4o-mini"


def test_build_trae_run_cmd_appends_model_and_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "trae_agent_online.online_job_stdio.venv_activate_path", lambda: Path("/mock/bin/activate")
    )
    monkeypatch.setattr(
        "trae_agent_online.online_job_stdio.venv_python_path", lambda: Path("/mock/bin/python")
    )
    monkeypatch.setattr(
        "trae_agent_online.online_job_stdio.is_executable_file",
        lambda p: str(p) == "/mock/bin/python",
    )
    cmd = build_trae_run_cmd(
        Path("/tmp/trae_config.yaml"),
        "/tmp/workspace",
        "hello",
        trajectory_file="/tmp/job/trajectory.json",
        model="gpt-4.1",
        provider="openai",
    )
    assert "--provider=openai" in cmd
    assert "--model=gpt-4.1" in cmd
