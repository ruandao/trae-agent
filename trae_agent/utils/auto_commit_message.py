# Copyright (c) 2025 ByteDance Ltd. and/or its affiliates
# SPDX-License-Identifier: MIT

"""Auto-generated git commit messages for agent workspaces (IDE-style subject + body)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DEFAULT_MAX_TOTAL = 4096
_MAX_SUBJECT = 72
_MAX_BODY_FINAL = 1400
_MAX_TASK_BLOCK = 1200
_MAX_STEP_BULLETS = 10
_MAX_STEP_LINE = 180
_MAX_FILES_IN_BODY = 50


def load_latest_trajectory_data(layer_root: Path) -> dict[str, Any] | None:
    """Load the newest ``.trajectories/trajectory_*.json`` under ``layer_root``, if any."""
    traj_dir = layer_root / ".trajectories"
    if not traj_dir.is_dir():
        return None
    candidates = [f for f in traj_dir.glob("trajectory_*.json") if f.is_file()]
    if not candidates:
        return None
    latest = max(candidates, key=lambda f: f.stat().st_mtime)
    try:
        raw = latest.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _first_line(text: str, max_len: int) -> str:
    line = text.replace("\r\n", "\n").replace("\r", "\n").split("\n", 1)[0].strip()
    if len(line) > max_len:
        return line[: max_len - 1] + "…"
    return line


def _infer_scope(files: list[str]) -> str | None:
    if not files:
        return None
    tops: dict[str, int] = {}
    for f in files:
        parts = [p for p in f.replace("\\", "/").split("/") if p]
        if not parts:
            continue
        top = parts[0]
        if top.startswith("."):
            continue
        tops[top] = tops.get(top, 0) + 1
    if not tops:
        return None
    return max(tops, key=tops.get)


def _infer_commit_type(task: str, files: list[str]) -> str:
    t = (task or "").lower()
    if any(k in t for k in ("修复", "fix", "bug", "错误", "崩溃", "crash", "失败", "broken")):
        return "fix"
    if any(k in t for k in ("测试", "test", "pytest", "单测", "unittest")):
        return "test"
    if any(k in t for k in ("文档", "readme", "doc", "注释")):
        return "docs"
    if any(k in t for k in ("重构", "refactor", "整理代码")):
        return "refactor"
    if any(k in t for k in ("实现", "添加", "新增", "feat", "功能", "支持")):
        return "feat"
    norm = [f.replace("\\", "/") for f in files]
    tests_only = bool(norm) and all(
        "/tests/" in f
        or f.startswith("tests/")
        or "_test." in f
        or f.endswith("_test.py")
        or "/test/" in f
        for f in norm
    )
    if tests_only:
        return "test"
    return "chore"


def _subject_line(
    *,
    command_hint: str | None,
    files: list[str],
    trajectory: dict[str, Any] | None,
) -> str:
    cmd = (command_hint or "").strip()
    traj_task = ""
    if trajectory:
        tt = trajectory.get("task")
        if isinstance(tt, str):
            traj_task = tt.strip()
    task_for_type = cmd or traj_task
    ctype = _infer_commit_type(task_for_type, files)
    scope = _infer_scope(files)
    scope_part = f"({scope})" if scope else ""
    prefix = f"{ctype}{scope_part}: "
    room = _MAX_SUBJECT - len(prefix)
    if room < 12:
        room = 12

    if cmd:
        summ = _first_line(cmd, room)
    elif trajectory:
        fr = trajectory.get("final_result")
        if isinstance(fr, str) and fr.strip():
            summ = _first_line(fr.strip(), room)
        elif traj_task:
            summ = _first_line(traj_task, room)
        else:
            summ = f"更新工作区 {len(files)} 个文件" if files else "工作区变更"
    else:
        summ = f"更新工作区 {len(files)} 个文件" if files else "工作区变更"

    if len(summ) > room:
        summ = summ[: room - 1] + "…"
    return prefix + summ


def _step_bullets(agent_steps: list[Any]) -> list[str]:
    out: list[str] = []
    for step in agent_steps:
        if not isinstance(step, dict):
            continue
        line = (step.get("delivery_summary") or step.get("lakeview_summary") or "").strip()
        if not line:
            st = step.get("state")
            if st:
                line = str(st).strip()
        if line:
            out.append(line)
    if len(out) > _MAX_STEP_BULLETS:
        out = out[-_MAX_STEP_BULLETS :]
    return out


def build_auto_commit_message(
    *,
    command_hint: str | None,
    stat_text: str,
    shortstat: str,
    files: list[str],
    trajectory: dict[str, Any] | None = None,
    max_total_len: int = _DEFAULT_MAX_TOTAL,
) -> str:
    """Build a conventional commit message: imperative type(scope): summary + structured body.

    Mirrors common IDE/assistant commit style: one clear subject line, then context bullets,
    not a raw dump of ``git diff --stat``.
    """
    subject = _subject_line(command_hint=command_hint, files=files, trajectory=trajectory)
    lines: list[str] = [subject, ""]

    final_result: str | None = None
    agent_steps: list[Any] = []
    traj_task: str | None = None
    if trajectory:
        fr = trajectory.get("final_result")
        if isinstance(fr, str) and fr.strip():
            final_result = fr.strip()
        tt = trajectory.get("task")
        if isinstance(tt, str) and tt.strip():
            traj_task = tt.strip()
        steps_raw = trajectory.get("agent_steps")
        if isinstance(steps_raw, list):
            agent_steps = steps_raw

    if final_result:
        body = final_result
        if len(body) > _MAX_BODY_FINAL:
            body = body[: _MAX_BODY_FINAL - 12].rstrip() + "\n…（已截断）"
        lines.append("【代理结论】")
        lines.append(body)
        lines.append("")

    task_block = (command_hint or "").strip() or (traj_task or "")
    if task_block:
        tb = task_block.replace("\r\n", "\n").replace("\r", "\n")
        if len(tb) > _MAX_TASK_BLOCK:
            tb = tb[: _MAX_TASK_BLOCK - 12].rstrip() + "\n…（已截断）"
        lines.append("【任务】")
        lines.append(tb)
        lines.append("")

    bullets = _step_bullets(agent_steps)
    if bullets:
        lines.append("【执行轨迹（节选）】")
        for b in bullets:
            one = b.replace("\n", " ").strip()
            if len(one) > _MAX_STEP_LINE:
                one = one[: _MAX_STEP_LINE - 1] + "…"
            lines.append(f"- {one}")
        lines.append("")

    lines.append("【变更统计】")
    lines.append((shortstat or stat_text or "").strip() or "—")
    lines.append("")

    lines.append("【涉及文件】")
    if files:
        for name in files[:_MAX_FILES_IN_BODY]:
            lines.append(f"- {name}")
        if len(files) > _MAX_FILES_IN_BODY:
            lines.append(f"- … 另有 {len(files) - _MAX_FILES_IN_BODY} 个文件")
    else:
        lines.append("- （无法列出）")

    msg = "\n".join(lines)
    if len(msg) > max_total_len:
        msg = msg[: max_total_len - 24].rstrip() + "\n…(说明已截断)"
    return msg
