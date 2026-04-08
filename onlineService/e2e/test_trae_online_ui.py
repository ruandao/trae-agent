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

「重新执行」用例（test_redo_button）会提交真实 trae-cli 任务；若长时间处于 running，
会在最多约 3 分钟内尝试「中断」后再点「重新执行」，总时长可能达数分钟，视本机 LLM 与任务而定。
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
def reset_before_each_test(request: pytest.FixtureRequest, page: Page) -> None:
    """每个测试前先打开页面并执行重置。"""
    if request.node.get_closest_marker("skip_reset"):
        return
    # SSE 长连接会使 networkidle 无法达成，只等待 DOM 与关键控件
    page.goto(_ui_url(), wait_until="domcontentloaded")
    page.locator("#btnReset").wait_for(state="visible", timeout=15_000)
    click_reset(page)
    page.locator("#btnClone").wait_for(state="visible", timeout=10_000)


@pytest.mark.skip_reset
def test_refresh_allows_networkidle_despite_sse(page: Page) -> None:
    """首屏 fetch 结束后再建 SSE；避免与长连接并存导致 wait_until=networkidle 永不达成（刷新一直转圈）。"""
    page.goto(_ui_url(), wait_until="networkidle", timeout=25_000)
    page.locator("#btnRefresh").wait_for(state="visible", timeout=15_000)
    page.reload(wait_until="networkidle", timeout=25_000)
    page.locator("#btnRefresh").wait_for(state="visible", timeout=15_000)


@pytest.mark.skip_reset
def test_event_source_patch_and_large_chunk_like_batched_sse(page: Page) -> None:
    """在首屏加载前包装 EventSource；统计轻量 ``job_output`` SSE（无 chunk，前端按 job_id 拉取）。"""
    page.add_init_script(
        """
        window.__traeJobOutputChunks = 0;
        const RawES = window.EventSource;
        window.EventSource = function (url, cfg) {
          const es = new RawES(url, cfg);
          es.addEventListener('message', (ev) => {
            try {
              const o = JSON.parse(ev.data);
              if (o.type === 'job_output' && o.job_id) window.__traeJobOutputChunks += 1;
            } catch (e) {}
          });
          return es;
        };
        """
    )
    page.goto(_ui_url(), wait_until="domcontentloaded")
    page.locator("#btnRefresh").wait_for(state="visible", timeout=15_000)
    page.wait_for_function("() => typeof window.__traeJobOutputChunks === 'number'", timeout=8_000)
    wide_row = "│ Status │ " + ("█" * 180) + " │ TAIL │"
    block = "\n".join(f"{wide_row}  line {i}" for i in range(80))
    page.evaluate(
        """(text) => {
          const box = document.getElementById('jobs');
          const div = document.createElement('div');
          div.className = 'job-card';
          div.setAttribute('data-id', 'e2e-batch-sim');
          div.innerHTML = '<pre class="out"></pre>';
          div.querySelector('pre').textContent = text;
          box.insertBefore(div, box.firstChild);
        }""",
        block,
    )
    pre = page.locator('.job-card[data-id="e2e-batch-sim"] pre.out')
    info = pre.evaluate(
        """(el) => ({
          len: el.textContent.length,
          scrollWidth: el.scrollWidth,
          clientWidth: el.clientWidth,
          whiteSpace: getComputedStyle(el).whiteSpace,
        })"""
    )
    assert info["len"] > 12_000
    assert info["whiteSpace"] == "pre"
    assert info["scrollWidth"] > info["clientWidth"]
    n = page.evaluate("() => window.__traeJobOutputChunks")
    assert isinstance(n, int)


def test_job_log_wide_rich_table_scrolls_horizontally(page: Page) -> None:
    """任务列表 pre 对超长行不强制换行断字，应出现横向滚动条以查看完整 Rich 表格。"""
    page.set_viewport_size({"width": 420, "height": 700})
    page.goto(_ui_url(), wait_until="domcontentloaded")
    page.locator("#btnRefresh").wait_for(state="visible", timeout=15_000)
    long_line = "│ Status │ " + ("█" * 220) + " │ RIGHT_TAIL │"
    page.evaluate(
        """(line) => {
          const box = document.getElementById('jobs');
          const div = document.createElement('div');
          div.className = 'job-card';
          div.setAttribute('data-id', 'e2e-wide-log');
          div.innerHTML = '<pre class="out"></pre>';
          div.querySelector('pre').textContent = line;
          box.insertBefore(div, box.firstChild);
        }""",
        long_line,
    )
    pre = page.locator(".job-card pre.out").first
    info = pre.evaluate(
        """(el) => {
          const cs = getComputedStyle(el);
          return {
            scrollWidth: el.scrollWidth,
            clientWidth: el.clientWidth,
            whiteSpace: cs.whiteSpace,
            wordBreak: cs.wordBreak,
          };
        }"""
    )
    assert info["whiteSpace"] == "pre"
    assert info["wordBreak"] == "normal"
    assert info["scrollWidth"] > info["clientWidth"]


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


def test_redo_button(page: Page) -> None:
    """克隆后提交任务；必要时先中断，再点「重新执行」应 POST /redo 成功并回到 pending/running。"""
    page.locator("#cloneUrl").fill(TEST_REPO)
    page.locator("#cloneDepth").fill("1")
    page.locator("#btnClone").click()

    expect(page.locator("#btnClone")).to_be_enabled(timeout=300_000)
    expect(page.locator("#cloneErr")).to_have_text("", timeout=10_000)
    expect(page.locator("#btnRun")).to_be_enabled(timeout=60_000)

    page.locator("#cmd").fill("e2e：重新执行烟测")
    page.locator("#btnRun").click()

    card = page.locator(".job-card").first
    expect(card).to_be_visible(timeout=30_000)

    # 等到非 pending（running 或已结束），最长约 5 分钟
    page.wait_for_function(
        """() => {
          const st = document.querySelector('.job-card .status');
          if (!st) return false;
          const c = st.className || '';
          return /\\brunning\\b|\\bcompleted\\b|\\bfailed\\b|\\binterrupted\\b/.test(c);
        }""",
        timeout=300_000,
    )

    status = card.locator(".status").first
    cls = (status.get_attribute("class") or "").lower()
    if "running" in cls:
        with page.expect_response(
            lambda r: r.request.method == "POST" and "/interrupt" in r.url
        ):
            card.locator("[data-interrupt]").click()
        expect(card.locator(".status.interrupted")).to_be_visible(timeout=120_000)

    redo = card.locator("[data-redo]")
    expect(redo).to_be_visible()
    with page.expect_response(
        lambda r: r.request.method == "POST" and "/redo" in r.url
    ) as redo_info:
        redo.click()
    assert redo_info.value.ok, redo_info.value.text()

    expect(
        card.locator(".status.pending, .status.running")
    ).to_be_visible(timeout=60_000)
