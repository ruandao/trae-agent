"""Read agent_steps from the latest trajectory JSON under a job layer directory."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from .paths import layers_root


def _layer_dir_must_be_allowed(layer_path: str) -> Path:
    root = layers_root().resolve()
    p = Path(layer_path).resolve()
    if not p.is_dir():
        raise ValueError("layer path is not a directory")
    if p != root and root not in p.parents:
        raise ValueError("layer path outside configured layers root")
    return p


def _latest_trajectory_file(layer_dir: Path) -> Path | None:
    traj = layer_dir / ".trajectories"
    if not traj.is_dir():
        return None
    files = [f for f in traj.glob("trajectory_*.json") if f.is_file()]
    if not files:
        return None
    return max(files, key=lambda f: f.stat().st_mtime)


def _max_cell_chars() -> int | None:
    raw = os.environ.get("TRAE_JOB_STEPS_MAX_CELL_CHARS", "400000").strip()
    if raw == "0" or raw.lower() == "unlimited":
        return None
    try:
        n = int(raw)
    except ValueError:
        return 400_000
    return n if n > 0 else None


def _truncate_str(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + "\n…(已截断)"


def _truncate_tool_result_block(obj: dict[str, Any], max_cell: int) -> None:
    r = obj.get("result")
    if isinstance(r, str):
        obj["result"] = _truncate_str(r, max_cell)


def _truncate_messages(msgs: list[Any] | None, max_cell: int) -> None:
    if not msgs:
        return
    for m in msgs:
        if not isinstance(m, dict):
            continue
        tr = m.get("tool_result")
        if isinstance(tr, dict):
            _truncate_tool_result_block(tr, max_cell)


def _truncate_step(step: dict[str, Any], max_cell: int) -> None:
    lr = step.get("llm_response")
    if isinstance(lr, dict):
        c = lr.get("content")
        if isinstance(c, str):
            lr["content"] = _truncate_str(c, max_cell)
    _truncate_messages(step.get("llm_messages"), max_cell)
    for tr in step.get("tool_results") or []:
        if isinstance(tr, dict):
            _truncate_tool_result_block(tr, max_cell)


def load_agent_steps_for_layer(layer_path: str) -> dict[str, Any]:
    """Return trajectory metadata and agent_steps (optionally truncated for JSON size)."""
    layer_dir = _layer_dir_must_be_allowed(layer_path)
    traj_file = _latest_trajectory_file(layer_dir)
    if traj_file is None:
        return {
            "trajectory_file": None,
            "task": None,
            "steps": [],
            "note": "no .trajectories/trajectory_*.json under layer",
        }

    try:
        data = json.loads(traj_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as e:
        return {
            "trajectory_file": str(traj_file),
            "task": None,
            "steps": [],
            "note": f"failed to read trajectory: {e}",
        }

    steps_raw = data.get("agent_steps")
    if not isinstance(steps_raw, list):
        steps_raw = []

    max_cell = _max_cell_chars()
    if max_cell is None:
        steps_out: list[Any] = deepcopy(steps_raw)
    else:
        steps_out = deepcopy(steps_raw)
        for s in steps_out:
            if isinstance(s, dict):
                _truncate_step(s, max_cell)

    return {
        "trajectory_file": str(traj_file),
        "task": data.get("task"),
        "steps": steps_out,
        "note": None,
    }
