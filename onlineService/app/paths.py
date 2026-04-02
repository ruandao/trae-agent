"""Resolved filesystem paths for the service."""

import os
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parent.parent


def service_root() -> Path:
    return _SERVICE_ROOT


def repo_root() -> Path:
    return Path(os.environ.get("REPO_ROOT", _SERVICE_ROOT.parent)).resolve()


def runtime_dir() -> Path:
    d = service_root() / "runtime"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_file_path() -> Path:
    return runtime_dir() / "service_config.yaml"


def jobs_state_path() -> Path:
    return runtime_dir() / "jobs_state.json"


def layers_root() -> Path:
    root = Path(os.environ.get("ONLINE_PROJECT_LAYERS", repo_root() / "onlineProject" / "layers"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def venv_activate_path() -> Path:
    custom = os.environ.get("TRAE_VENV")
    if custom:
        return Path(custom) / "bin" / "activate"
    return repo_root() / ".venv" / "bin" / "activate"
