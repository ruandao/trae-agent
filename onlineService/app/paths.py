"""Resolved filesystem paths for the service."""

import os
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parent.parent


def service_root() -> Path:
    return _SERVICE_ROOT


def repo_root() -> Path:
    return Path(os.environ.get("REPO_ROOT", _SERVICE_ROOT.parent)).resolve()


def state_root() -> Path:
    """运行时状态根目录：默认 ``<REPO_ROOT>/onlineProject_state``（与 ``onlineService`` 并列，避免热重载监视写入）。

    可用环境变量 ``ONLINE_PROJECT_STATE_ROOT`` 覆盖整棵状态树位置。
    """
    raw = os.environ.get("ONLINE_PROJECT_STATE_ROOT")
    if raw:
        root = Path(raw)
    else:
        root = repo_root() / "onlineProject_state"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def runtime_dir() -> Path:
    d = state_root() / "runtime"
    d.mkdir(parents=True, exist_ok=True)
    return d


def logs_dir() -> Path:
    """HTTP 等服务请求日志目录（位于 ``state_root()/logs``）。"""
    d = state_root() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def req_logs_dir() -> Path:
    """本服务对外发起的 HTTP 请求日志目录（``state_root()/reqLogs``）。"""
    d = state_root() / "reqLogs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_file_path() -> Path:
    return runtime_dir() / "service_config.yaml"


def jobs_state_path() -> Path:
    return runtime_dir() / "jobs_state.json"


def commands_log_path() -> Path:
    """历史/旁路命令日志（若有）；与 jobs_state 独立，重置任务时需一并清理。"""
    return runtime_dir() / "commands.json"


def online_project_root() -> Path:
    """仓库根下对外展示的联合视图路径（默认 ``<REPO_ROOT>/onlineProject``）。"""
    return Path(os.environ.get("ONLINE_PROJECT_ROOT", repo_root() / "onlineProject"))


def layers_root() -> Path:
    """可写层根目录：默认 ``state_root()/layers``（与 runtime 等同在 ``onlineProject_state`` 下）。

    可选环境变量 ``ONLINE_PROJECT_LAYERS`` 覆盖可写层根目录。
    """
    raw = os.environ.get("ONLINE_PROJECT_LAYERS")
    if raw:
        root = Path(raw)
    else:
        root = state_root() / "layers"
    root.mkdir(parents=True, exist_ok=True)
    return root


def venv_activate_path() -> Path:
    custom = os.environ.get("TRAE_VENV")
    if custom:
        return Path(custom) / "bin" / "activate"
    return repo_root() / ".venv" / "bin" / "activate"
