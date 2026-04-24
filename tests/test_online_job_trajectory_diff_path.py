"""job_trajectory：仅读取 ONLINE_PROJECT_STATE_ROOT 下轨迹与 tae json。"""

from __future__ import annotations

import json
from pathlib import Path


def _state_and_layer(monkeypatch, tmp_path: Path):
    layers = tmp_path / "layers"
    state_root = tmp_path / "onlineProject_state"
    layers.mkdir(parents=True, exist_ok=True)
    state_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ONLINE_PROJECT_LAYERS", str(layers))
    monkeypatch.setenv("ONLINE_PROJECT_STATE_ROOT", str(state_root))
    return layers, state_root


def test_load_agent_steps_from_state_tae_agent_json(monkeypatch, tmp_path: Path) -> None:
    layers, state_root = _state_and_layer(monkeypatch, tmp_path)

    layer = layers / "20260101_000000_abcd12"
    layer.mkdir(parents=True, exist_ok=True)
    job_id = "11111111-1111-1111-1111-111111111111"
    step_dir = state_root / "runtime" / "job_logs" / "trae_agent_json" / job_id / "step_000001"
    step_dir.mkdir(parents=True, exist_ok=True)
    payload = {"type": "agent_step_full", "step_number": 1, "state": "completed"}
    (step_dir / "agent_step_full.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    from trae_agent_online.job_trajectory import load_agent_steps_for_job

    out = load_agent_steps_for_job(str(layer.resolve()), job_id)
    assert len(out["steps"]) == 1
    assert out["steps"][0].get("step_number") == 1
    assert "runtime/job_logs/trae_agent_json" in (out.get("trajectory_file") or "")


def test_tae_json_multiple_steps_in_state(monkeypatch, tmp_path: Path) -> None:
    """state 下 job_logs/trae_agent_json 多步应完整读出。"""
    layers, state_root = _state_and_layer(monkeypatch, tmp_path)

    layer = layers / "20260102_000000_abcd12"
    layer.mkdir(parents=True, exist_ok=True)
    job_id = "22222222-2222-2222-2222-222222222222"
    tae = state_root / "runtime" / "job_logs" / "trae_agent_json" / job_id
    for sn, num in ((1, 1), (2, 2)):
        d = tae / f"step_{sn:06d}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "agent_step_full.json").write_text(
            json.dumps({"step_number": num, "type": "agent_step_full"}),
            encoding="utf-8",
        )

    from trae_agent_online.job_trajectory import load_agent_steps_for_job

    out = load_agent_steps_for_job(str(layer.resolve()), job_id)
    assert len(out["steps"]) == 2


def test_backfills_llm_response_content_from_delivery_summary(monkeypatch, tmp_path: Path) -> None:
    layers, state_root = _state_and_layer(monkeypatch, tmp_path)

    layer = layers / "20260103_000000_abcd12"
    layer.mkdir(parents=True, exist_ok=True)
    job_id = "33333333-3333-3333-3333-333333333333"
    step_dir = state_root / "runtime" / "job_logs" / "trae_agent_json" / job_id / "step_000001"
    step_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "type": "agent_step_full",
        "step_number": 1,
        "state": "calling_tool",
        "delivery_summary": "ReadFile README.md",
        "llm_response": {"content": "", "model": "m"},
        "tool_calls": [{"name": "ReadFile", "call_id": "c1", "arguments": {"path": "README.md"}}],
    }
    (step_dir / "agent_step_full.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    from trae_agent_online.job_trajectory import load_agent_steps_for_job

    out = load_agent_steps_for_job(str(layer.resolve()), job_id)
    got = out["steps"][0]["llm_response"]["content"]
    assert got == "ReadFile README.md"


def test_load_agent_steps_from_runtime_job_logs(monkeypatch, tmp_path: Path) -> None:
    """与上例一致，保留对 runtime job_logs 的回归命名。"""
    layers, state_root = _state_and_layer(monkeypatch, tmp_path)

    layer = layers / "20260104_000000_abcd12"
    layer.mkdir(parents=True, exist_ok=True)
    job_id = "44444444-4444-4444-4444-444444444444"
    step_dir = state_root / "runtime" / "job_logs" / "trae_agent_json" / job_id / "step_000001"
    step_dir.mkdir(parents=True, exist_ok=True)
    (step_dir / "agent_step_full.json").write_text(
        json.dumps({"type": "agent_step_full", "step_number": 1, "state": "completed"}),
        encoding="utf-8",
    )

    from trae_agent_online.job_trajectory import load_agent_steps_for_job

    out = load_agent_steps_for_job(str(layer.resolve()), job_id)
    assert len(out["steps"]) == 1
    assert out["steps"][0].get("step_number") == 1
    assert "runtime/job_logs/trae_agent_json" in (out.get("trajectory_file") or "")
