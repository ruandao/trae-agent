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


def test_latest_trajectory_empty_steps_returns_note(monkeypatch, tmp_path: Path) -> None:
    """无精确 job 轨迹时，若层下最新 trajectory 存在但 agent_steps 为空，应返回说明而非泛化缺数据。"""
    layers, state_root = _state_and_layer(monkeypatch, tmp_path)

    layer = layers / "layer_for_latest_traj"
    layer.mkdir(parents=True, exist_ok=True)
    job_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    traj_dir = state_root / "runtime" / "layer_artifacts" / layer.name / ".trajectories"
    traj_dir.mkdir(parents=True, exist_ok=True)
    (traj_dir / "trajectory_otherjob.json").write_text(
        json.dumps({"task": "other", "agent_steps": []}, ensure_ascii=False),
        encoding="utf-8",
    )

    from trae_agent_online.job_trajectory import load_agent_steps_for_job

    out = load_agent_steps_for_job(str(layer.resolve()), job_id)
    assert out["steps"] == []
    assert out.get("trajectory_file")
    assert out.get("note") and "empty" in out["note"].lower()


def test_empty_exact_but_llm_interactions_yield_steps(monkeypatch, tmp_path: Path) -> None:
    """首轮 LLM 已写入 llm_interactions、整步未 finalize 时应有可展示步骤。"""
    layers, state_root = _state_and_layer(monkeypatch, tmp_path)

    layer = layers / "20260511_llm_only_layer"
    layer.mkdir(parents=True, exist_ok=True)
    job_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    traj_dir = state_root / "runtime" / "layer_artifacts" / layer.name / ".trajectories"
    traj_dir.mkdir(parents=True, exist_ok=True)
    traj_file = traj_dir / f"trajectory_{job_id}.json"
    traj_file.write_text(
        json.dumps(
            {
                "task": "ping",
                "agent_steps": [],
                "llm_interactions": [
                    {
                        "timestamp": "2026-05-11T00:00:00",
                        "response": {"content": "thinking…", "model": "deepseek-reasoner"},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    from trae_agent_online.job_trajectory import load_agent_steps_for_job

    out = load_agent_steps_for_job(str(layer.resolve()), job_id)
    assert len(out["steps"]) == 1
    assert out["steps"][0].get("trajectory_provisional") is True
    assert out["steps"][0].get("state") == "llm_interaction"
    assert out.get("note") is None


def test_runtime_trajectory_llm_interactions_only(monkeypatch, tmp_path: Path) -> None:
    """仅 runtime/job_logs/trajectories/{job_id} 下有文件且仅有 llm_interactions 时也应出步。"""
    layers, state_root = _state_and_layer(monkeypatch, tmp_path)

    layer = layers / "20260511_runtime_only"
    layer.mkdir(parents=True, exist_ok=True)
    job_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    rt_dir = state_root / "runtime" / "job_logs" / "trajectories" / job_id
    rt_dir.mkdir(parents=True, exist_ok=True)
    (rt_dir / "chunk.json").write_text(
        json.dumps(
            {
                "task": "rt",
                "agent_steps": [],
                "llm_interactions": [
                    {"timestamp": "t1", "response": {"content": "hi", "model": "m"}},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    from trae_agent_online.job_trajectory import load_agent_steps_for_job

    out = load_agent_steps_for_job(str(layer.resolve()), job_id)
    assert len(out["steps"]) == 1
    assert out["steps"][0].get("trajectory_provisional") is True
    assert "trajectories" in (out.get("trajectory_file") or "")


def test_empty_exact_prefers_tae_json_when_state_agent_steps_empty(
    monkeypatch, tmp_path: Path
) -> None:
    """层上 trajectory 仍为空 agent_steps 时，不应短路，应读到 tae_agent_json 下已落盘的步。"""
    layers, state_root = _state_and_layer(monkeypatch, tmp_path)

    layer = layers / "20260511_dual_source"
    layer.mkdir(parents=True, exist_ok=True)
    job_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    traj_dir = state_root / "runtime" / "layer_artifacts" / layer.name / ".trajectories"
    traj_dir.mkdir(parents=True, exist_ok=True)
    (traj_dir / f"trajectory_{job_id}.json").write_text(
        json.dumps({"task": "x", "agent_steps": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    step_dir = state_root / "runtime" / "job_logs" / "trae_agent_json" / job_id / "step_000001"
    step_dir.mkdir(parents=True, exist_ok=True)
    (step_dir / "agent_step_full.json").write_text(
        json.dumps({"type": "agent_step_full", "step_number": 1, "state": "completed"}),
        encoding="utf-8",
    )

    from trae_agent_online.job_trajectory import load_agent_steps_for_job

    out = load_agent_steps_for_job(str(layer.resolve()), job_id)
    assert len(out["steps"]) == 1
    assert "runtime/job_logs/trae_agent_json" in (out.get("trajectory_file") or "")


def test_empty_exact_trajectory_returns_note_not_missing_data(monkeypatch, tmp_path: Path) -> None:
    """start_recording 会先写入 agent_steps 为空的 trajectory；不应误判为缺少 runtime 数据。"""
    layers, state_root = _state_and_layer(monkeypatch, tmp_path)

    layer = layers / "20260511_062319_0e335a"
    layer.mkdir(parents=True, exist_ok=True)
    job_id = "69162e33-b395-46df-a8da-eceb6842e5a8"
    traj_dir = state_root / "runtime" / "layer_artifacts" / layer.name / ".trajectories"
    traj_dir.mkdir(parents=True, exist_ok=True)
    traj_file = traj_dir / f"trajectory_{job_id}.json"
    traj_file.write_text(
        json.dumps({"task": "hello", "agent_steps": []}, ensure_ascii=False),
        encoding="utf-8",
    )

    from trae_agent_online.job_trajectory import load_agent_steps_for_job

    out = load_agent_steps_for_job(str(layer.resolve()), job_id)
    assert out["steps"] == []
    assert out.get("trajectory_file")
    assert out.get("note") and "empty" in out["note"].lower()


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
