"""job_trajectory：Overlay 任务结束后 .trae_agent_json 在 diff/ 下也应被读到。"""

from __future__ import annotations

import json
from pathlib import Path


def test_load_agent_steps_from_diff_tae_agent_json(monkeypatch, tmp_path: Path) -> None:
    layers = tmp_path / "layers"
    layers.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ONLINE_PROJECT_LAYERS", str(layers))

    layer = layers / "20260101_000000_abcd12"
    layer.mkdir(parents=True, exist_ok=True)
    job_id = "11111111-1111-1111-1111-111111111111"
    step_dir = layer / "diff" / ".trae_agent_json" / job_id / "step_000001"
    step_dir.mkdir(parents=True, exist_ok=True)
    payload = {"type": "agent_step_full", "step_number": 1, "state": "completed"}
    (step_dir / "agent_step_full.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    from onlineService.app.job_trajectory import load_agent_steps_for_job

    out = load_agent_steps_for_job(str(layer.resolve()), job_id)
    assert len(out["steps"]) == 1
    assert out["steps"][0].get("step_number") == 1
    assert "diff" in (out.get("trajectory_file") or "")


def test_prefers_root_with_more_steps(monkeypatch, tmp_path: Path) -> None:
    """层根与 diff 下都有目录时，取 step 条数更多的（模拟 diff 为完整落盘）。"""
    layers = tmp_path / "layers"
    layers.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ONLINE_PROJECT_LAYERS", str(layers))

    layer = layers / "20260102_000000_abcd12"
    layer.mkdir(parents=True, exist_ok=True)
    job_id = "22222222-2222-2222-2222-222222222222"

    stale = layer / ".trae_agent_json" / job_id / "step_000001"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "agent_step_full.json").write_text(
        json.dumps({"step_number": 1, "type": "agent_step_full"}),
        encoding="utf-8",
    )

    diff_root = layer / "diff" / ".trae_agent_json" / job_id
    for sn, num in ((1, 1), (2, 2)):
        d = diff_root / f"step_{sn:06d}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "agent_step_full.json").write_text(
            json.dumps({"step_number": num, "type": "agent_step_full"}),
            encoding="utf-8",
        )

    from onlineService.app.job_trajectory import load_agent_steps_for_job

    out = load_agent_steps_for_job(str(layer.resolve()), job_id)
    assert len(out["steps"]) == 2


def test_backfills_llm_response_content_from_delivery_summary(monkeypatch, tmp_path: Path) -> None:
    layers = tmp_path / "layers"
    layers.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ONLINE_PROJECT_LAYERS", str(layers))

    layer = layers / "20260103_000000_abcd12"
    layer.mkdir(parents=True, exist_ok=True)
    job_id = "33333333-3333-3333-3333-333333333333"
    step_dir = layer / ".trae_agent_json" / job_id / "step_000001"
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

    from onlineService.app.job_trajectory import load_agent_steps_for_job

    out = load_agent_steps_for_job(str(layer.resolve()), job_id)
    got = out["steps"][0]["llm_response"]["content"]
    assert got == "ReadFile README.md"
