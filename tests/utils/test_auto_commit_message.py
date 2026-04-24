# Copyright (c) 2025 ByteDance Ltd. and/or its affiliates
# SPDX-License-Identifier: MIT

"""直接加载模块文件，避免 ``import trae_agent`` 拉全包（在低于 3.12 的环境便于单测收集）。"""

import importlib.util
import json
from pathlib import Path

import pytest


def _load_auto_commit_message():
    root = Path(__file__).resolve().parents[2]
    path = root / "trae_agent" / "utils" / "auto_commit_message.py"
    spec = importlib.util.spec_from_file_location("trae_agent.utils.auto_commit_message", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_acm = _load_auto_commit_message()
build_auto_commit_message = _acm.build_auto_commit_message
load_latest_trajectory_data = _acm.load_latest_trajectory_data


def test_build_subject_conventional_prefix_and_scope():
    msg = build_auto_commit_message(
        command_hint="修复登录后 token 未刷新的问题",
        stat_text="",
        shortstat="1 file changed, 2 insertions(+)",
        files=["src/auth.py", "src/session.py"],
    )
    lines = msg.split("\n")
    assert lines[0].startswith("fix(src):")
    assert "登录" in lines[0] or "token" in lines[0]


def test_build_includes_final_result_and_steps():
    traj = {
        "task": "添加重试",
        "final_result": "已在 client 中加入指数退避重试。",
        "agent_steps": [
            {"delivery_summary": "查看 client 实现"},
            {"delivery_summary": "编辑 http_client.py 增加重试"},
        ],
    }
    msg = build_auto_commit_message(
        command_hint="",
        stat_text="",
        shortstat="2 files changed",
        files=["pkg/http_client.py"],
        trajectory=traj,
    )
    assert "【代理结论】" in msg
    assert "指数退避" in msg
    assert "【执行轨迹（节选）】" in msg
    assert "http_client.py" in msg


def test_load_latest_trajectory_data_picks_newest(tmp_path: Path, monkeypatch):
    import time

    state = tmp_path / "state"
    layer_root = tmp_path / "my_layer"
    layer_root.mkdir()
    d = state / "runtime" / "layer_artifacts" / "my_layer" / ".trajectories"
    d.mkdir(parents=True)
    monkeypatch.setenv("ONLINE_PROJECT_STATE_ROOT", str(state))
    old = d / "trajectory_20000101_000000.json"
    new = d / "trajectory_20990101_000000.json"
    old.write_text(json.dumps({"task": "old"}), encoding="utf-8")
    time.sleep(0.05)
    new.write_text(json.dumps({"task": "newest"}), encoding="utf-8")

    data = load_latest_trajectory_data(layer_root)
    assert data is not None
    assert data.get("task") == "newest"


@pytest.mark.parametrize(
    "hint,expect_type",
    [
        ("为 README 补充安装说明", "docs"),
        ("重构订单服务拆分模块", "refactor"),
        ("添加用户导出 CSV 功能", "feat"),
    ],
)
def test_infer_commit_type(hint: str, expect_type: str):
    msg = build_auto_commit_message(
        command_hint=hint,
        stat_text="",
        shortstat="1 file changed",
        files=["a.py"],
    )
    assert msg.split("\n", 1)[0].startswith(f"{expect_type}(") or msg.startswith(f"{expect_type}:")
