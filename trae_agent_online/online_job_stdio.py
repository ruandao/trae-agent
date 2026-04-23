"""任务子进程 stdout 解码、trae 噪声行过滤、环境变量与 trae-cli 命令行拼装。"""

from __future__ import annotations

import asyncio
import codecs
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from trae_agent_online.online_project_paths import repo_root, venv_activate_path

_DEFAULT_JOB_WIDE_COLUMNS = 999_999
_MAX_JOB_COLUMNS = 9_999_999


def stdout_chunk_bytes() -> int:
    raw = os.environ.get("TRAE_JOB_STDOUT_CHUNK_BYTES", "16384")
    try:
        return max(int(raw), 64)
    except ValueError:
        return 16384


async def iter_stdout_text(stream: asyncio.StreamReader) -> AsyncIterator[str]:
    """Decode subprocess stdout in fixed-size binary chunks (not line-based)."""
    chunk_sz = stdout_chunk_bytes()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        block = await stream.read(chunk_sz)
        if not block:
            tail = decoder.decode(b"", final=True)
            if tail:
                yield tail
            break
        text = decoder.decode(block)
        if text:
            yield text


def is_trae_noise_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    return (
        s.startswith("Changed working directory to:")
        or s == "Initialising MCP tools..."
        or s.startswith("Trajectory saved to:")
    )


def filter_trae_output_chunk(text: str, carry: str = "") -> tuple[str, str]:
    """Filter noisy fixed trae-cli lines and keep chunk boundaries safe."""
    merged = f"{carry}{text}" if carry else text
    if not merged:
        return "", ""
    lines = merged.splitlines(keepends=True)
    next_carry = ""
    if lines and not lines[-1].endswith(("\n", "\r")):
        next_carry = lines.pop()
    kept: list[str] = []
    for line in lines:
        if is_trae_noise_line(line.rstrip("\r\n")):
            continue
        kept.append(line)
    return "".join(kept), next_carry


def finalize_trae_output_carry(carry: str) -> str:
    if not carry:
        return ""
    return "" if is_trae_noise_line(carry.rstrip("\r\n")) else carry


def job_subprocess_columns() -> str:
    raw = (os.environ.get("TRAE_JOB_COLUMNS") or "").strip().lower()
    if not raw or raw in ("0", "unlimited", "none", "max", "inf"):
        return str(_DEFAULT_JOB_WIDE_COLUMNS)
    if raw.isdigit():
        n = int(raw)
        if n <= 0:
            return str(_DEFAULT_JOB_WIDE_COLUMNS)
        return str(min(n, _MAX_JOB_COLUMNS))
    return str(_DEFAULT_JOB_WIDE_COLUMNS)


def normalize_job_env(extra_env: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(extra_env, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in extra_env.items():
        key = str(k).strip()
        if not key:
            continue
        out[key] = str(v)
    return out


def job_subprocess_env(
    *,
    trae_json_log_dir: str | None = None,
    extra_env: dict[str, Any] | None = None,
) -> dict[str, str]:
    base = {**os.environ, "PYTHONUNBUFFERED": "1"}
    base["COLUMNS"] = job_subprocess_columns()
    if not base.get("PYTHONPATH"):
        base["PYTHONPATH"] = str(repo_root())
    if trae_json_log_dir:
        base["TRAE_AGENT_JSON_OUTPUT_DIR"] = trae_json_log_dir
    base.update(normalize_job_env(extra_env))
    return base


def venv_python_path() -> Path:
    activate = venv_activate_path()
    return activate.parent / "python"


def is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def build_trae_run_cmd(
    cfg: Path,
    work: str,
    cmd_text: str,
    *,
    trajectory_file: str | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> list[str]:
    activate = venv_activate_path()
    trae_bin = activate.parent / "trae-cli"
    py = venv_python_path()
    py3 = activate.parent / "python3"
    if is_executable_file(py):
        base = [str(py), "-m", "trae_agent.cli"]
    elif is_executable_file(py3):
        base = [str(py3), "-m", "trae_agent.cli"]
    elif is_executable_file(trae_bin):
        base = [str(trae_bin)]
    else:
        base = [sys.executable, "-m", "trae_agent.cli"]
    cmd = [
        *base,
        "run",
        cmd_text,
        f"--config-file={str(cfg)}",
        f"--working-dir={work}",
    ]
    if provider:
        cmd.append(f"--provider={provider}")
    if model:
        cmd.append(f"--model={model}")
    if trajectory_file:
        cmd.append(f"--trajectory-file={trajectory_file}")
    return cmd


def resolve_model_cli_args_from_command_env(
    command_env: dict[str, str] | None,
) -> tuple[str | None, str | None]:
    """将任务级环境变量映射为 trae-cli 参数。"""
    if not isinstance(command_env, dict):
        return None, None
    provider = str(command_env.get("TRAE_MODEL_PROVIDER") or "").strip() or None
    model = str(command_env.get("TRAE_MODEL") or "").strip() or None
    return provider, model
