"""Read agent steps from runtime job logs or legacy layer-local artifacts."""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from .paths import job_agent_json_root, job_trajectory_dir, layers_root

_STEP_DIR_RE = re.compile(r"^step_(\d+)$")


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


def _latest_runtime_trajectory_file(job_id: str) -> Path | None:
    if not _safe_job_id_segment(job_id):
        return None
    traj_dir = job_trajectory_dir(job_id, ensure=False)
    if not traj_dir.is_dir():
        return None
    candidates = [f for f in traj_dir.glob("*.json") if f.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f.stat().st_mtime)


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


def _fallback_llm_content_from_step(step: dict[str, Any]) -> str:
    """当 llm_response.content 为空时，回填可读摘要，避免前端全空白。"""
    for key in ("lakeview_summary", "delivery_summary", "reflection", "error"):
        v = step.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    calls = step.get("tool_calls")
    if isinstance(calls, list):
        names = [str(c.get("name") or "").strip() for c in calls if isinstance(c, dict)]
        names = [n for n in names if n]
        if names:
            return "调用工具: " + ", ".join(names[:6])
    return ""


def _ensure_step_llm_content(step: dict[str, Any]) -> None:
    lr = step.get("llm_response")
    if not isinstance(lr, dict):
        return
    content = lr.get("content")
    if isinstance(content, str) and content.strip():
        return
    fallback = _fallback_llm_content_from_step(step)
    if fallback:
        lr["content"] = fallback


def _safe_job_id_segment(job_id: str) -> bool:
    s = str(job_id).strip()
    if not s or "/" in s or "\\" in s or s in (".", ".."):
        return False
    return ".." not in s


def _load_agent_steps_from_trajectory_dir(layer_dir: Path) -> dict[str, Any]:
    """Return trajectory metadata and agent_steps from ``.trajectories/trajectory_*.json``."""
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
        for s in steps_out:
            if isinstance(s, dict):
                _ensure_step_llm_content(s)
    else:
        steps_out = deepcopy(steps_raw)
        for s in steps_out:
            if isinstance(s, dict):
                _ensure_step_llm_content(s)
                _truncate_step(s, max_cell)

    return {
        "trajectory_file": str(traj_file),
        "task": data.get("task"),
        "steps": steps_out,
        "note": None,
    }


def _load_agent_steps_from_runtime_trajectory(job_id: str) -> dict[str, Any] | None:
    traj_file = _latest_runtime_trajectory_file(job_id)
    if traj_file is None:
        return None
    try:
        data = json.loads(traj_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    steps_raw = data.get("agent_steps")
    if not isinstance(steps_raw, list):
        steps_raw = []
    steps_out = deepcopy(steps_raw)
    max_cell = _max_cell_chars()
    for s in steps_out:
        if isinstance(s, dict):
            _ensure_step_llm_content(s)
            if max_cell is not None:
                _truncate_step(s, max_cell)
    return {
        "trajectory_file": str(traj_file),
        "task": data.get("task"),
        "steps": steps_out,
        "note": None,
    }


def _steps_from_tae_agent_job_root(root: Path) -> dict[str, Any] | None:
    """Parse step JSON under ``root`` = ``.../.trae_agent_json/{job_id}``."""
    if not root.is_dir():
        return None

    indexed: list[tuple[int, Path]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        m = _STEP_DIR_RE.match(child.name)
        if not m:
            continue
        indexed.append((int(m.group(1)), child))
    if not indexed:
        return None
    indexed.sort(key=lambda x: x[0])

    steps_raw: list[Any] = []
    for _n, step_dir in indexed:
        full_p = step_dir / "agent_step_full.json"
        sum_p = step_dir / "agent_step.json"
        path = full_p if full_p.is_file() else sum_p if sum_p.is_file() else None
        if path is None:
            continue
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(row, dict):
            steps_raw.append(row)

    if not steps_raw:
        return None

    task_val: Any = None
    summary_p = root / "execution_summary.json"
    if summary_p.is_file():
        try:
            sm = json.loads(summary_p.read_text(encoding="utf-8"))
            if isinstance(sm, dict):
                task_val = sm.get("task")
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass

    max_cell = _max_cell_chars()
    if max_cell is None:
        steps_out = deepcopy(steps_raw)
        for s in steps_out:
            if isinstance(s, dict):
                _ensure_step_llm_content(s)
    else:
        steps_out = deepcopy(steps_raw)
        for s in steps_out:
            if isinstance(s, dict):
                _ensure_step_llm_content(s)
                _truncate_step(s, max_cell)

    return {
        "trajectory_file": str(root),
        "task": task_val,
        "steps": steps_out,
        "note": None,
    }


def _try_load_steps_from_trae_agent_json(layer_dir: Path, job_id: str) -> dict[str, Any] | None:
    """Load steps from ``SimpleCLIConsole`` output.

    新版在线服务默认写在 ``runtime/job_logs/trae_agent_json/{job_id}/``；
    为兼容历史数据，仍会回退扫描层目录内旧路径。

    Non-overlay 旧任务写在 ``{layer}/.trae_agent_json/{job_id}/``。
    Overlay 任务运行时写在 ``merged/.trae_agent_json/``，结束后随 upper 落盘到
    ``diff/.trae_agent_json/``；``JobRecord.layer_path`` 仅为层根目录，须在各候选
    路径中解析并择步数最多的一份。
    """
    if not _safe_job_id_segment(job_id):
        return None

    rel_job_roots = (
        Path(".trae_agent_json") / job_id,
        Path("diff") / ".trae_agent_json" / job_id,
        Path("merged") / ".trae_agent_json" / job_id,
        Path("upper") / ".trae_agent_json" / job_id,
    )
    runtime_root = job_agent_json_root(job_id, ensure=False)

    best: dict[str, Any] | None = None
    best_n = -1
    for rel in rel_job_roots:
        payload = _steps_from_tae_agent_job_root(layer_dir / rel)
        if payload is None:
            continue
        n = len(payload.get("steps") or [])
        if n > best_n:
            best_n = n
            best = payload
    payload = _steps_from_tae_agent_job_root(runtime_root)
    if payload is not None:
        n = len(payload.get("steps") or [])
        if n > best_n:
            best_n = n
            best = payload

    return best


def load_agent_steps_for_layer(layer_path: str) -> dict[str, Any]:
    """Return trajectory metadata and agent_steps (optionally truncated for JSON size)."""
    layer_dir = _layer_dir_must_be_allowed(layer_path)
    return _load_agent_steps_from_trajectory_dir(layer_dir)


def load_agent_steps_for_job(layer_path: str, job_id: str) -> dict[str, Any]:
    """优先读取 runtime job logs；再回退层目录遗留产物。"""
    layer_dir = _layer_dir_must_be_allowed(layer_path)
    from_json = _try_load_steps_from_trae_agent_json(layer_dir, job_id)
    if from_json is not None:
        return from_json
    runtime_traj = _load_agent_steps_from_runtime_trajectory(job_id)
    if runtime_traj is not None:
        return runtime_traj
    return _load_agent_steps_from_trajectory_dir(layer_dir)
