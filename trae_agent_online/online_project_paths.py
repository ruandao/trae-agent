"""与 ``onlineService`` / ``onlineServiceJS`` 共用的运行时目录布局（默认相对 trae-agent 仓库根）。"""

from __future__ import annotations

import os
from pathlib import Path

_TRAE_AGENT_ROOT = Path(__file__).resolve().parents[1]


def repo_root() -> Path:
    return Path(os.environ.get("REPO_ROOT", _TRAE_AGENT_ROOT)).resolve()


def state_root() -> Path:
    raw = os.environ.get("ONLINE_PROJECT_STATE_ROOT")
    root = Path(raw) if raw else repo_root() / "onlineProject_state"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def runtime_dir() -> Path:
    d = state_root() / "runtime"
    d.mkdir(parents=True, exist_ok=True)
    return d


def layers_root() -> Path:
    raw = os.environ.get("ONLINE_PROJECT_LAYERS")
    root = Path(raw) if raw else state_root() / "layers"
    root.mkdir(parents=True, exist_ok=True)
    return root


def runtime_job_logs_root(*, ensure: bool = True) -> Path:
    d = runtime_dir() / "job_logs"
    if ensure:
        d.mkdir(parents=True, exist_ok=True)
    return d


def job_agent_json_root(job_id: str, *, ensure: bool = True) -> Path:
    d = runtime_job_logs_root(ensure=ensure) / "trae_agent_json" / str(job_id)
    if ensure:
        d.mkdir(parents=True, exist_ok=True)
    return d


def job_trajectory_dir(job_id: str, *, ensure: bool = True) -> Path:
    d = runtime_job_logs_root(ensure=ensure) / "trajectories" / str(job_id)
    if ensure:
        d.mkdir(parents=True, exist_ok=True)
    return d


def venv_activate_path() -> Path:
    custom = os.environ.get("TRAE_VENV")
    if custom:
        return Path(custom) / "bin" / "activate"
    return repo_root() / ".venv" / "bin" / "activate"
