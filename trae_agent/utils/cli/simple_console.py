# Copyright (c) 2025 ByteDance Ltd. and/or its affiliates
# SPDX-License-Identifier: MIT

"""Simple CLI Console implementation — 结构化输出写入文件系统（按步分子目录 + 分层 JSON 文件）。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, override

from rich.console import Console

from trae_agent.agent.agent_basics import AgentExecution, AgentState, AgentStep, AgentStepState
from trae_agent.utils.cli.cli_console import (
    CLIConsole,
    ConsoleMode,
    ConsoleStep,
)
from trae_agent.utils.config import LakeviewConfig

# 与 onlineService 子进程默认一致：Rich 无「无限宽」，用大整数近似不折行。
_WIDE_CONSOLE_FALLBACK = 999_999


def _console_mirror_enabled() -> bool:
    raw = (os.environ.get("TRAE_AGENT_JSON_CONSOLE_MIRROR") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _simple_console_width() -> int | None:
    """Rich 在非 TTY 上默认 80 列；优先用环境 COLUMNS（正整数），否则用大宽度近似不限制折行。"""
    cols = os.environ.get("COLUMNS")
    if cols:
        s = cols.strip()
        if s.isdigit():
            n = int(s)
            if n > 0:
                return None
    try:
        if sys.stdout.isatty():
            return None
    except (AttributeError, ValueError, OSError):
        pass
    return _WIDE_CONSOLE_FALLBACK


def _json_friendly(obj: Any) -> Any:
    """将 dataclass / Enum / 容器递归转为 JSON 可序列化结构。"""
    if obj is None:
        return None
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _json_friendly(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_friendly(x) for x in obj]
    if is_dataclass(obj) and not isinstance(obj, type):
        return _json_friendly(asdict(obj))
    return str(obj)


def _safe_tool_file_name(name: str, max_len: int = 48) -> str:
    s = re.sub(r"[^\w\-.]+", "_", name.strip() or "tool", flags=re.UNICODE)
    return (s[:max_len] if s else "tool").rstrip("_") or "tool"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8")


class SimpleCLIConsole(CLIConsole):
    """在运行目录下写入分层 JSON 文件；可选镜像到 stdout（TRAE_AGENT_JSON_CONSOLE_MIRROR）。"""

    def __init__(
        self, mode: ConsoleMode = ConsoleMode.RUN, lakeview_config: LakeviewConfig | None = None
    ):
        super().__init__(mode, lakeview_config)
        cw = _simple_console_width()
        self.console: Console = Console(width=cw) if cw is not None else Console()
        self._file_log_root: Path | None = None

    def _resolve_file_log_root(self) -> Path:
        if self._file_log_root is not None:
            return self._file_log_root
        explicit = (os.environ.get("TRAE_AGENT_JSON_OUTPUT_DIR") or "").strip()
        if explicit:
            root = Path(explicit).expanduser().resolve()
        else:
            base = Path(os.getcwd()).resolve()
            root = base / ".trae_agent_file_log" / f"run_{os.getpid()}_{time.time_ns()}"
        root.mkdir(parents=True, exist_ok=True)
        _write_json(
            root / "run_meta.json",
            {
                "type": "run_meta",
                "path": str(root),
                "pid": os.getpid(),
                "cwd": str(Path.cwd().resolve()),
                "created_at": time.time(),
            },
        )
        self._file_log_root = root
        return root

    def _step_dir(self, step_number: int) -> Path:
        return self._resolve_file_log_root() / f"step_{step_number:06d}"

    def _maybe_mirror_stdout(self, payload: dict[str, Any]) -> None:
        if not _console_mirror_enabled():
            return
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        self.console.print(text, markup=False, highlight=False)

    def _write_agent_step_tree(
        self,
        agent_step: AgentStep,
        agent_execution: AgentExecution | None,
    ) -> None:
        """一步一目录：概要、全量快照、llm_response、tool_calls 子目录分层文件。"""
        step_dir = self._step_dir(agent_step.step_number)
        step_dir.mkdir(parents=True, exist_ok=True)

        full_tree = _json_friendly(asdict(agent_step))
        if not isinstance(full_tree, dict):
            full_tree = {"value": full_tree}

        llm_response = full_tree.pop("llm_response", None)
        tool_calls = full_tree.pop("tool_calls", None)
        tool_results = full_tree.pop("tool_results", None)

        summary: dict[str, Any] = {"type": "agent_step", **full_tree}
        if agent_execution and agent_execution.total_tokens:
            summary["total_tokens_run"] = _json_friendly(asdict(agent_execution.total_tokens))

        _write_json(step_dir / "agent_step.json", summary)
        self._maybe_mirror_stdout(summary)

        full_inner = _json_friendly(asdict(agent_step))
        if not isinstance(full_inner, dict):
            full_inner = {"data": full_inner}
        full_payload: dict[str, Any] = {"type": "agent_step_full", **full_inner}
        if agent_execution and agent_execution.total_tokens:
            full_payload["total_tokens_run"] = _json_friendly(asdict(agent_execution.total_tokens))
        _write_json(step_dir / "agent_step_full.json", full_payload)

        if llm_response is not None:
            lr_dir = step_dir / "llm_response"
            lr_dir.mkdir(exist_ok=True)
            _write_json(lr_dir / "body.json", llm_response)

        if tool_calls:
            tc_dir = step_dir / "tool_calls"
            tc_dir.mkdir(exist_ok=True)
            results_by_id: dict[Any, Any] = {}
            if isinstance(tool_results, list):
                for tr in tool_results:
                    if isinstance(tr, dict):
                        cid = tr.get("call_id")
                        if cid is not None:
                            results_by_id[cid] = tr
            for i, call in enumerate(tool_calls):
                if not isinstance(call, dict):
                    call = {"raw": call}
                cid = call.get("call_id")
                name = _safe_tool_file_name(str(call.get("name") or "tool"))
                fn = f"{i:03d}_{name}.json"
                payload = {
                    "type": "tool_invocation",
                    "index": i,
                    "call": call,
                    "result": results_by_id.get(cid),
                }
                _write_json(tc_dir / fn, _json_friendly(payload))

    @override
    def update_status(
        self, agent_step: AgentStep | None = None, agent_execution: AgentExecution | None = None
    ):
        if agent_step:
            if agent_step.step_number not in self.console_step_history:
                self.console_step_history[agent_step.step_number] = ConsoleStep(agent_step)

            if (
                agent_step.state in [AgentStepState.COMPLETED, AgentStepState.ERROR]
                and not self.console_step_history[agent_step.step_number].agent_step_printed
            ):
                self._write_agent_step_tree(agent_step, agent_execution)
                self.console_step_history[agent_step.step_number].agent_step_printed = True

                if (
                    self.lake_view
                    and not self.console_step_history[
                        agent_step.step_number
                    ].lake_view_panel_generator
                ):
                    self.console_step_history[
                        agent_step.step_number
                    ].lake_view_panel_generator = asyncio.create_task(
                        self._create_lakeview_step_display(agent_step)
                    )

        self.agent_execution = agent_execution

    @override
    async def start(self):
        while self.agent_execution is None or (
            self.agent_execution.agent_state != AgentState.COMPLETED
            and self.agent_execution.agent_state != AgentState.ERROR
        ):
            await asyncio.sleep(1)

        if self.lake_view and self.agent_execution:
            await self._write_lakeview_files()

        if self.agent_execution:
            self._write_execution_summary_file()

    async def _write_lakeview_files(self):
        root = self._resolve_file_log_root()
        _write_json(
            root / "lakeview_summary.json",
            {"type": "lakeview_summary", "steps": list(self.console_step_history.keys())},
        )
        self._maybe_mirror_stdout({"type": "lakeview_summary"})

        for step in self.console_step_history.values():
            if step.lake_view_panel_generator:
                lake_view_step = await step.lake_view_panel_generator
                if lake_view_step:
                    payload = {
                        "type": "lakeview_step",
                        "step_number": step.agent_step.step_number,
                        **_json_friendly(asdict(lake_view_step)),
                    }
                    _write_json(
                        self._step_dir(step.agent_step.step_number) / "lakeview_step.json",
                        payload,
                    )
                    self._maybe_mirror_stdout(payload)

    def _write_execution_summary_file(self):
        if not self.agent_execution:
            return
        summary: dict[str, Any] = {
            "type": "execution_summary",
            "task": self.agent_execution.task,
            "success": self.agent_execution.success,
            "steps": len(self.agent_execution.steps),
            "execution_time_s": round(self.agent_execution.execution_time, 4),
            "agent_state": self.agent_execution.agent_state.value,
        }
        if self.agent_execution.total_tokens:
            summary["total_tokens"] = _json_friendly(asdict(self.agent_execution.total_tokens))
        if self.agent_execution.final_result is not None:
            summary["final_result"] = self.agent_execution.final_result
        path = self._resolve_file_log_root() / "execution_summary.json"
        _write_json(path, summary)
        self._maybe_mirror_stdout(summary)

    @override
    def print_task_details(self, details: dict[str, str]):
        payload = {"type": "task_details", "details": details}
        path = self._resolve_file_log_root() / "task_details.json"
        _write_json(path, payload)
        self._maybe_mirror_stdout(payload)

    @override
    def print(self, message: str, color: str = "blue", bold: bool = False):
        message = f"[bold]{message}[/bold]" if bold else message
        message = f"[{color}]{message}[/{color}]"
        self.console.print(message)

    @override
    def get_task_input(self) -> str | None:
        if self.mode != ConsoleMode.INTERACTIVE:
            return None

        self.console.print("\n[bold blue]Task:[/bold blue] ", end="")
        try:
            task = input()
            if task.lower() in ["exit", "quit"]:
                return None
            return task
        except (EOFError, KeyboardInterrupt):
            return None

    @override
    def get_working_dir_input(self) -> str:
        if self.mode != ConsoleMode.INTERACTIVE:
            return ""

        self.console.print("[bold blue]Working Directory:[/bold blue] ", end="")
        try:
            return input()
        except (EOFError, KeyboardInterrupt):
            return ""

    @override
    def stop(self):
        pass

    async def _create_lakeview_step_display(self, agent_step: AgentStep):
        if self.lake_view is None:
            return None

        lake_view_step = await self.lake_view.create_lakeview_step(agent_step)

        if lake_view_step is None:
            return None

        return lake_view_step
