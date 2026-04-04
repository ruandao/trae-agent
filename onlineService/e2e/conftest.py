"""Playwright fixtures for onlineService E2E."""

from __future__ import annotations

import os
import urllib.error
import urllib.parse
import urllib.request

import pytest
from playwright.sync_api import Browser, Page, Playwright, sync_playwright

ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "dev-local-token")
TRAE_UI_BASE = os.environ.get("TRAE_UI_BASE", "http://127.0.0.1:8765")


@pytest.fixture(scope="module", autouse=True)
def require_task_gate_endpoint() -> None:
    """若服务为旧版本无 task-gate 路由，跳过本文件全部用例并提示重启。"""
    url = (
        f"{TRAE_UI_BASE.rstrip('/')}/api/requirements/task-gate"
        f"?access_token={urllib.parse.quote(ACCESS_TOKEN)}"
    )
    req = urllib.request.Request(
        url,
        headers={"X-Access-Token": ACCESS_TOKEN},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status != 200:
                pytest.skip(f"task-gate 返回 HTTP {resp.status}，请用当前仓库代码重启服务")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            pytest.skip(
                "GET /api/requirements/task-gate 为 404：请重启 uvicorn 以加载最新 onlineService。"
            )
        raise
    except OSError as e:
        pytest.skip(f"无法连接 {TRAE_UI_BASE}：{e}")


@pytest.fixture(scope="session")
def playwright_instance() -> Playwright:
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(playwright_instance: Playwright) -> Browser:
    headless = os.environ.get("PLAYWRIGHT_HEADLESS", "1") != "0"
    b = playwright_instance.chromium.launch(headless=headless)
    yield b
    b.close()


@pytest.fixture
def page(browser: Browser) -> Page:
    ctx = browser.new_context()
    p = ctx.new_page()
    p.set_default_navigation_timeout(60_000)
    p.set_default_timeout(60_000)
    yield p
    ctx.close()
