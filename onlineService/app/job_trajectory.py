"""Compatibility shim：实现位于 ``trae_agent_online.job_trajectory``（轻量包，不依赖 trae_agent 主包）。"""

from __future__ import annotations

from trae_agent_online.job_trajectory import load_agent_steps_for_job, load_agent_steps_for_layer

__all__ = ["load_agent_steps_for_job", "load_agent_steps_for_layer"]
