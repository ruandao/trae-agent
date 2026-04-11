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


def test_load_agent_steps_from_runtime_job_logs(monkeypatch, tmp_path: Path) -> None:
    layers = tmp_path / "layers"
    state_root = tmp_path / "onlineProject_state"
    layers.mkdir(parents=True, exist_ok=True)
    state_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ONLINE_PROJECT_LAYERS", str(layers))
    monkeypatch.setenv("ONLINE_PROJECT_STATE_ROOT", str(state_root))

    layer = layers / "20260104_000000_abcd12"
    layer.mkdir(parents=True, exist_ok=True)
    job_id = "44444444-4444-4444-4444-444444444444"
    step_dir = state_root / "runtime" / "job_logs" / "trae_agent_json" / job_id / "step_000001"
    step_dir.mkdir(parents=True, exist_ok=True)
    (step_dir / "agent_step_full.json").write_text(
        json.dumps({"type": "agent_step_full", "step_number": 1, "state": "completed"}),
        encoding="utf-8",
    )

    from onlineService.app.job_trajectory import load_agent_steps_for_job

    out = load_agent_steps_for_job(str(layer.resolve()), job_id)
    assert len(out["steps"]) == 1
    assert out["steps"][0].get("step_number") == 1
    assert "runtime/job_logs/trae_agent_json" in (out.get("trajectory_file") or "")


def test_layer_changes_not_affected_by_runtime_job_logs(monkeypatch, tmp_path: Path) -> None:
    layers = tmp_path / "layers"
    state_root = tmp_path / "onlineProject_state"
    layers.mkdir(parents=True, exist_ok=True)
    state_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ONLINE_PROJECT_LAYERS", str(layers))
    monkeypatch.setenv("ONLINE_PROJECT_STATE_ROOT", str(state_root))

    parent = "20260105_000000_abcd12"
    child = "20260105_000001_abcd12"
    parent_dir = layers / parent
    child_dir = layers / child
    parent_dir.mkdir(parents=True, exist_ok=True)
    child_dir.mkdir(parents=True, exist_ok=True)
    (parent_dir / "README.md").write_text("same\n", encoding="utf-8")
    (child_dir / "README.md").write_text("same\n", encoding="utf-8")

    runtime_log = (
        state_root
        / "runtime"
        / "job_logs"
        / "trae_agent_json"
        / "j1"
        / "step_000001"
        / "agent_step_full.json"
    )
    runtime_log.parent.mkdir(parents=True, exist_ok=True)
    runtime_log.write_text(json.dumps({"step_number": 1}), encoding="utf-8")

    from onlineService.app.layer_git import list_layer_changes_vs_parent

    out = list_layer_changes_vs_parent(parent, child)
    assert out["same"] is True
    assert out["changes"] == []


def test_mixed_legacy_parent_overlay_child_keeps_parent_files(monkeypatch, tmp_path: Path) -> None:
    layers = tmp_path / "layers"
    layers.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ONLINE_PROJECT_LAYERS", str(layers))

    parent = "20260106_000000_abcd12"
    child = "20260106_000001_bcde23"
    parent_dir = layers / parent
    child_dir = layers / child
    parent_dir.mkdir(parents=True, exist_ok=True)
    child_dir.mkdir(parents=True, exist_ok=True)

    # legacy parent: 无 layer_meta，直接以层根作为完整视图
    (parent_dir / "somanyad" / "index.js").parent.mkdir(parents=True, exist_ok=True)
    (parent_dir / "somanyad" / "index.js").write_text("console.log('x')\n", encoding="utf-8")
    (parent_dir / "somanyad-emailD" / "README").parent.mkdir(parents=True, exist_ok=True)
    (parent_dir / "somanyad-emailD" / "README").write_text("legacy\n", encoding="utf-8")

    # overlay child: 仅 diff 中有新增 hello.js
    diff_dir = child_dir / "diff"
    diff_dir.mkdir(parents=True, exist_ok=True)
    (diff_dir / "hello.js").write_text("console.log('Hello World');\n", encoding="utf-8")

    from onlineService.app.layer_git import list_layer_changes_vs_parent
    from onlineService.app.layer_meta import write_layer_meta

    write_layer_meta(child, kind="job", parent_layer_id=parent)
    out = list_layer_changes_vs_parent(parent, child)

    assert out["same"] is False
    changes = out["changes"] or []
    assert changes == [{"path": "hello.js", "kind": "added"}]


def test_overlay_diff_compare_uses_isolated_snapshot(monkeypatch, tmp_path: Path) -> None:
    layers = tmp_path / "layers"
    layers.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ONLINE_PROJECT_LAYERS", str(layers))

    parent = "20260107_000000_abcd12"
    child = "20260107_000001_bcde23"
    parent_dir = layers / parent
    child_dir = layers / child
    (parent_dir / "base").mkdir(parents=True, exist_ok=True)
    (child_dir / "diff").mkdir(parents=True, exist_ok=True)
    (parent_dir / "base" / "README.md").write_text("parent\n", encoding="utf-8")
    (child_dir / "diff" / "hello.js").write_text("console.log('Hello');\n", encoding="utf-8")

    from onlineService.app.layer_git import list_layer_changes_vs_parent
    from onlineService.app.layer_meta import write_layer_meta

    write_layer_meta(parent, kind="clone", parent_layer_id=None)
    write_layer_meta(child, kind="job", parent_layer_id=parent)

    # 若对比逻辑退回共享 workspace_root，此 monkeypatch 会触发异常。
    def _boom(_lid: str):
        raise RuntimeError("should not be called for overlay compare")

    monkeypatch.setattr("onlineService.app.layer_git.layer_git_workspace_root", _boom)
    out = list_layer_changes_vs_parent(parent, child)
    assert out["changes"] == [{"path": "hello.js", "kind": "added"}]
