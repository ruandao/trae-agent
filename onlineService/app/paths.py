"""Resolved filesystem paths for the service."""

import os
import shutil
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
    root = Path(raw) if raw else repo_root() / "onlineProject_state"
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


def clear_runtime_ephemeral_task_dirs() -> dict[str, list[str]]:
    """删除 ``runtime/`` 下与任务相关的可再生目录（物化缓存、轨迹日志等）。

    供 ``POST /api/jobs/reset`` 使用；**不**删除 ``jobs_state.json``、``service_config.yaml``、
    ``state_root()/logs``、``reqLogs`` 等。
    """
    rt = runtime_dir()
    names = (
        "job_logs",
        "materialized",
        "materialized_compare",
        "materialized_commit_parent",
    )
    removed: list[str] = []
    for name in names:
        p = rt / name
        try:
            if p.is_symlink() or p.is_file():
                p.unlink(missing_ok=True)
                removed.append(name)
            elif p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
                removed.append(name)
        except OSError:
            continue
    return {"runtime_ephemeral_removed": removed}


def clear_state_dirs() -> dict[str, list[str]]:
    """清空 ``onlineProject_state/{logs, reqLogs, runtime}`` 目录内容。

    供 ``POST /api/jobs/reset`` 使用，重置所有运行时状态。
    """
    result: dict[str, list[str]] = {}

    for dir_name, dir_func in (
        ("logs", logs_dir),
        ("reqLogs", req_logs_dir),
        ("runtime", runtime_dir),
    ):
        d = dir_func()
        removed: list[str] = []
        try:
            for item in d.iterdir():
                try:
                    if item.is_symlink() or item.is_file():
                        item.unlink(missing_ok=True)
                        removed.append(item.name)
                    elif item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                        removed.append(item.name)
                except OSError:
                    continue
        except OSError:
            pass
        result[f"{dir_name}_removed"] = removed

    return result


def commands_log_path() -> Path:
    """历史/旁路命令日志（若有）；与 jobs_state 独立，重置任务时需一并清理。"""
    return runtime_dir() / "commands.json"


def job_events_dir() -> Path:
    """任务结构化事件目录（JSONL），按 job_id 分文件。"""
    d = runtime_dir() / "job_events"
    d.mkdir(parents=True, exist_ok=True)
    return d


def job_events_file(job_id: str) -> Path:
    return job_events_dir() / f"{job_id}.jsonl"


def job_events_job_dir(job_id: str) -> Path:
    d = job_events_dir() / str(job_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def runtime_job_logs_root(*, ensure: bool = True) -> Path:
    """任务运行日志根目录（``state_root()/runtime/job_logs``）。"""
    d = runtime_dir() / "job_logs"
    if ensure:
        d.mkdir(parents=True, exist_ok=True)
    return d


def job_agent_json_root(job_id: str, *, ensure: bool = True) -> Path:
    """SimpleCLIConsole 结构化步骤输出目录。"""
    d = runtime_job_logs_root(ensure=ensure) / "trae_agent_json" / str(job_id)
    if ensure:
        d.mkdir(parents=True, exist_ok=True)
    return d


def job_trajectory_dir(job_id: str, *, ensure: bool = True) -> Path:
    """Trajectory JSON 输出目录。"""
    d = runtime_job_logs_root(ensure=ensure) / "trajectories" / str(job_id)
    if ensure:
        d.mkdir(parents=True, exist_ok=True)
    return d


def online_project_root() -> Path:
    """仓库根下对外展示的联合视图路径（默认 ``<REPO_ROOT>/onlineProject``）。"""
    return Path(os.environ.get("ONLINE_PROJECT_ROOT", repo_root() / "onlineProject"))


def layers_root() -> Path:
    """可写层根目录：默认 ``state_root()/layers``（与 runtime 等同在 ``onlineProject_state`` 下）。

    可选环境变量 ``ONLINE_PROJECT_LAYERS`` 覆盖可写层根目录。
    """
    raw = os.environ.get("ONLINE_PROJECT_LAYERS")
    root = Path(raw) if raw else state_root() / "layers"
    root.mkdir(parents=True, exist_ok=True)
    return root


def venv_activate_path() -> Path:
    custom = os.environ.get("TRAE_VENV")
    if custom:
        return Path(custom) / "bin" / "activate"
    return repo_root() / ".venv" / "bin" / "activate"
