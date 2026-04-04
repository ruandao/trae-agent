"""
Playwright 核验：http://127.0.0.1:8765/ui/dev-local-token

前置条件：
  1. 已启动服务：./onlineService/run_local.sh（默认 ACCESS_TOKEN=dev-local-token）
  2. 安装依赖：
       pip install -r onlineService/e2e/requirements-e2e.txt
       playwright install chromium

运行：
  cd /path/to/trae-agent && pytest onlineService/e2e/test_trae_online_ui.py -v --tb=short

说明：每个测试前都会点击「重置」并确认对话框（与页面行为一致）。
"""

from __future__ import annotations

import os

import pytest
from playwright.sync_api import Page, expect

BASE_URL = os.environ.get("TRAE_UI_BASE", "http://127.0.0.1:8765")
UI_PATH = os.environ.get("TRAE_UI_PATH", "/ui/dev-local-token")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "dev-local-token")
TEST_REPO = os.environ.get(
    "TRAE_E2E_REPO", "https://github.com/ruandao/somanyad.git"
)


def _ui_url() -> str:
    return BASE_URL.rstrip("/") + UI_PATH


def click_reset(page: Page) -> None:
    """点击「重置」并接受 confirm 对话框。"""
    page.once("dialog", lambda d: d.accept())
    with page.expect_response(
        lambda r: r.request.method == "POST" and "/api/jobs/reset" in r.url
    ):
        page.locator("#btnReset").click()


@pytest.fixture(autouse=True)
def reset_before_each_test(page: Page) -> None:
    """每个测试前先打开页面并执行重置。"""
    # SSE 长连接会使 networkidle 无法达成，只等待 DOM 与关键控件
    page.goto(_ui_url(), wait_until="domcontentloaded")
    page.locator("#btnReset").wait_for(state="visible", timeout=15_000)
    click_reset(page)
    page.locator("#btnClone").wait_for(state="visible", timeout=10_000)


def test_after_reset_new_task_is_locked(page: Page) -> None:
    """重置后：必须先克隆，新建任务应禁用。"""
    run = page.locator("#btnRun")
    expect(run).to_be_disabled()
    msg = page.locator("#taskGateMsg")
    expect(msg).to_be_visible()
    expect(msg).to_contain_text("克隆")


def test_shallow_clone_somanyad_unlocks_new_task_and_gate_api(page: Page) -> None:
    """浅克隆 ruandao/somanyad 后，新建任务可用，且 task-gate 接口返回 clone_done。"""
    page.locator("#cloneUrl").fill(TEST_REPO)
    page.locator("#cloneDepth").fill("1")
    clone_btn = page.locator("#btnClone")
    expect(clone_btn).to_be_enabled()
    clone_btn.click()

    expect(page.locator("#btnClone")).to_be_enabled(timeout=300_000)
    err = page.locator("#cloneErr")
    expect(err).to_have_text("", timeout=10_000)

    expect(page.locator("#btnRun")).to_be_enabled(timeout=60_000)
    expect(page.locator("#taskGateMsg")).not_to_be_visible()

    res = page.evaluate(
        """async (token) => {
          const u = new URL('/api/requirements/task-gate', location.origin);
          u.searchParams.set('access_token', token);
          const r = await fetch(u.toString(), { headers: { 'X-Access-Token': token } });
          return r.json();
        }""",
        ACCESS_TOKEN,
    )
    assert res.get("clone_done") is True
