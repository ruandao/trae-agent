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

import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, expect

BASE_URL = os.environ.get("TRAE_UI_BASE", "http://127.0.0.1:8765")
UI_PATH = os.environ.get("TRAE_UI_PATH", "/ui/dev-local-token")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "dev-local-token")
TEST_REPO = os.environ.get("TRAE_E2E_REPO", "https://github.com/ruandao/somanyad.git")


def _ui_url() -> str:
    return BASE_URL.rstrip("/") + UI_PATH


def click_reset(page: Page) -> None:
    """点击「重置」并接受 confirm 对话框。"""
    page.once("dialog", lambda d: d.accept())
    with page.expect_response(lambda r: r.request.method == "POST" and "/api/jobs/reset" in r.url):
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
def test_api_layers_excludes_empty_anchor_kind(page: Page) -> None:
    """GET /api/layers 不应列出 layer_meta.kind=empty 的锚点层（仅克隆 API 使用，避免重启堆积「无 git」行）。"""
    page.goto(_ui_url(), wait_until="domcontentloaded", timeout=25_000)
    page.locator("#btnRefresh").wait_for(state="visible", timeout=15_000)
    j = page.evaluate(
        """async (token) => {
          const u = new URL('/api/layers', location.origin);
          u.searchParams.set('access_token', token);
          const r = await fetch(u.toString(), { headers: { 'X-Access-Token': token } });
          return r.json();
        }""",
        ACCESS_TOKEN,
    )
    assert isinstance(j, dict)
    for x in j.get("layers") or []:
        mk = x.get("meta_kind")
        assert mk != "empty", f"不应暴露 empty 锚点层: {x}"


@pytest.mark.skip_reset
def test_localhost_hostname_ui_page_loads(page: Page) -> None:
    """run_local 默认端口下，浏览器使用 http://localhost（非仅 127.0.0.1）应能打开 /ui/<token>。"""
    port = os.environ.get("PORT", "8765")
    url = f"http://localhost:{port}/ui/{ACCESS_TOKEN}"
    page.goto(url, wait_until="domcontentloaded", timeout=25_000)
    page.locator("#btnRefresh").wait_for(state="visible", timeout=15_000)
    assert "Trae" in (page.title() or "")


@pytest.mark.skip_reset
def test_refresh_allows_networkidle_despite_sse(page: Page) -> None:
    """页面在 EventSource(SSE) 长连接下仍应可加载；Playwright 的 networkidle 会因长连接永不空闲而超时，故用 domcontentloaded。"""
    page.goto(_ui_url(), wait_until="domcontentloaded", timeout=25_000)
    page.locator("#btnRefresh").wait_for(state="visible", timeout=15_000)
    page.reload(wait_until="domcontentloaded", timeout=25_000)
    page.locator("#btnRefresh").wait_for(state="visible", timeout=15_000)


@pytest.mark.skip_reset
def test_refresh_job_steps_fetches_are_bounded(page: Page) -> None:
    """多任务时不应同时对 /api/jobs/<id>/steps 无界并行，否则同域连接池打满、DevTools 长期 pending。"""
    step_re = re.compile(r"/api/jobs/[^/?]+/steps(?:\?|$)")
    st: dict[str, int] = {"in_flight": 0, "max": 0}

    def _on_request(request) -> None:
        if request.method != "GET" or not step_re.search(request.url):
            return
        st["in_flight"] += 1
        st["max"] = max(st["max"], st["in_flight"])

    def _on_done(request) -> None:
        if request.method != "GET" or not step_re.search(request.url):
            return
        st["in_flight"] = max(0, st["in_flight"] - 1)

    page.on("request", _on_request)
    page.on("requestfinished", _on_done)
    page.on("requestfailed", _on_done)

    def _is_get_jobs_index(r) -> bool:
        return r.request.method == "GET" and urlparse(r.url).path.rstrip("/") == "/api/jobs"

    with page.expect_response(_is_get_jobs_index, timeout=20_000):
        page.goto(_ui_url(), wait_until="domcontentloaded", timeout=25_000)
    page.locator("#btnRefresh").wait_for(state="visible", timeout=15_000)
    for _ in range(150):
        page.wait_for_timeout(100)
        if st["in_flight"] == 0:
            page.wait_for_timeout(300)
            if st["in_flight"] == 0:
                break

    with page.expect_response(_is_get_jobs_index, timeout=20_000):
        page.reload(wait_until="domcontentloaded", timeout=25_000)
    page.locator("#btnRefresh").wait_for(state="visible", timeout=15_000)
    for _ in range(150):
        page.wait_for_timeout(100)
        if st["in_flight"] == 0:
            page.wait_for_timeout(300)
            if st["in_flight"] == 0:
                break

    if st["max"] == 0:
        pytest.skip("当前无任务或无 /steps 请求，跳过并发上界断言")
    assert st["max"] <= 5, f"同一时刻 GET /steps 峰值 {st['max']}，预期有界并发（≤5）"


def _inject_job_style_probe(page: Page, pre_text: str, data_id: str) -> None:
    """向 main 追加固定容器，避免 loadJobs 清空 #jobs 时冲掉测试注入的节点。"""
    page.evaluate(
        """({ text, dataId }) => {
          let host = document.getElementById('e2eStyleProbe');
          if (!host) {
            host = document.createElement('div');
            host.id = 'e2eStyleProbe';
            const main = document.querySelector('main');
            if (main) main.appendChild(host);
            else document.body.appendChild(host);
          }
          host.innerHTML =
            '<div class="job-card" data-id="' + dataId + '"><pre class="out"></pre></div>';
          host.querySelector('pre.out').textContent = text;
        }""",
        {"text": pre_text, "dataId": data_id},
    )


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
    _inject_job_style_probe(page, block, "e2e-batch-sim")
    pre = page.locator('#e2eStyleProbe .job-card[data-id="e2e-batch-sim"] pre.out')
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
    _inject_job_style_probe(page, long_line, "e2e-wide-log")
    pre = page.locator('#e2eStyleProbe .job-card[data-id="e2e-wide-log"] pre.out')
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
    """重置后：必须先克隆，新建任务区显示门控提示（指令仅通过 zTree 操作栏提交）。"""
    msg = page.locator("#taskGateMsg")
    expect(msg).to_be_visible()
    expect(msg).to_contain_text("克隆")


def test_clone_layer_serial_has_no_duplicate_clone_job_row(page: Page) -> None:
    """克隆完成后：串行列表中仅可写层行展示克隆层，不另挂 command_kind=clone 的任务行。"""
    page.locator("#cloneUrl").fill(TEST_REPO)
    page.locator("#cloneDepth").fill("1")
    page.locator("#btnClone").click()

    expect(page.locator("#btnClone")).to_be_enabled(timeout=300_000)
    expect(page.locator("#cloneErr")).to_have_text("", timeout=10_000)

    page.locator("#btnRefresh").click()
    page.wait_for_function(
        """() => document.getElementById('layer_serial_graph') !== null""",
        timeout=60_000,
    )

    probe = page.evaluate(
        """async () => {
      const token = typeof ACCESS_TOKEN !== 'undefined' ? ACCESS_TOKEN : '';
      const pa = await fetch(
        '/api/project/active?access_token=' + encodeURIComponent(token),
        { headers: { 'X-Access-Token': token } },
      ).then((r) => r.json());
      const tip = pa.active_tip_layer_id;
      if (!tip) return { ok: false, reason: 'no active_tip_layer_id' };
      const host = document.getElementById('layer_serial_graph');
      if (!host) return { ok: false, reason: 'no layer_serial_graph' };
      const nid = '__layer__:' + tip;
      const layerBtn = Array.from(host.querySelectorAll('button.layer-serial-row')).find(
        (b) => b.getAttribute('data-mind-node-id') === nid
      );
      if (!layerBtn) return { ok: false, reason: 'no serial row for tip layer', tip };
      const jobRows = host.querySelectorAll('button.layer-serial-row.layer-serial-job');
      return { ok: jobRows.length === 0, tip, nJobRows: jobRows.length };
    }"""
    )
    assert probe.get("ok") is True, probe


def test_recreate_from_upper_layer_clears_descendant_layers(page: Page) -> None:
    """选中克隆层（该层有代表任务）后两次「创建并执行」：中间子层应被替换而非累加。

    回归：仅用 repo_layer_id 清理时，UI 走 parent_job_id 路径会跳过清理，可写层数递增。
    """
    page.locator("#cloneUrl").fill(TEST_REPO)
    page.locator("#cloneDepth").fill("1")
    page.locator("#btnClone").click()

    expect(page.locator("#btnClone")).to_be_enabled(timeout=300_000)
    expect(page.locator("#cloneErr")).to_have_text("", timeout=10_000)

    page.wait_for_function(
        """() => document.querySelector('#layerRelationActions textarea') !== null""",
        timeout=120_000,
    )

    tip = page.evaluate(
        """async (token) => {
          const u = new URL('/api/project/active', location.origin);
          u.searchParams.set('access_token', token);
          const r = await fetch(u.toString(), { headers: { 'X-Access-Token': token } });
          const j = await r.json();
          return j.active_tip_layer_id;
        }""",
        ACCESS_TOKEN,
    )
    assert isinstance(tip, str) and len(tip) > 4

    mind_id = "__layer__:" + tip
    page.locator(f'button.layer-serial-row[data-mind-node-id="{mind_id}"]').click()
    cmd_ta = page.locator("#layerRelationActions textarea[data-testid=layer-new-job-command]")
    cmd_ta.wait_for(state="visible", timeout=15_000)
    cmd_ta.fill("echo e2e-upper-sweep-1")
    page.locator("#layerRelationActions select[data-testid=layer-new-job-kind]").select_option(
        "shell"
    )

    with page.expect_response(
        lambda r: r.request.method == "POST"
        and "/api/jobs" in r.url
        and "/redo" not in r.url
        and "/interrupt" not in r.url
        and "/reset" not in r.url,
        timeout=60_000,
    ) as post_job:
        page.locator("#layerRelationActions").get_by_role("button", name="创建并执行").click()
    assert post_job.value.ok, post_job.value.text()

    page.wait_for_function(
        """async (token) => {
          const u = new URL('/api/jobs', location.origin);
          u.searchParams.set('access_token', token);
          const r = await fetch(u.toString(), { headers: { 'X-Access-Token': token } });
          const j = await r.json();
          const jobs = j.jobs || [];
          const shell = jobs.filter((x) =>
            x.command_kind === 'shell' && String(x.command || '').indexOf('e2e-upper-sweep-1') >= 0
          );
          return shell.length >= 1 && shell[0].status === 'completed';
        }""",
        ACCESS_TOKEN,
        timeout=120_000,
    )

    n1 = page.evaluate(
        """async (token) => {
          const u = new URL('/api/layers', location.origin);
          u.searchParams.set('access_token', token);
          const r = await fetch(u.toString(), { headers: { 'X-Access-Token': token } });
          const j = await r.json();
          return (j.layers || []).length;
        }""",
        ACCESS_TOKEN,
    )

    page.locator(f'button.layer-serial-row[data-mind-node-id="{mind_id}"]').click()
    cmd_ta2 = page.locator("#layerRelationActions textarea[data-testid=layer-new-job-command]")
    cmd_ta2.wait_for(state="visible", timeout=15_000)
    cmd_ta2.fill("echo e2e-upper-sweep-2")
    page.locator("#layerRelationActions select[data-testid=layer-new-job-kind]").select_option(
        "shell"
    )

    with page.expect_response(
        lambda r: r.request.method == "POST"
        and "/api/jobs" in r.url
        and "/redo" not in r.url
        and "/interrupt" not in r.url
        and "/reset" not in r.url,
        timeout=60_000,
    ) as post_job2:
        page.locator("#layerRelationActions").get_by_role("button", name="创建并执行").click()
    assert post_job2.value.ok, post_job2.value.text()

    page.wait_for_function(
        """async (token) => {
          const u = new URL('/api/jobs', location.origin);
          u.searchParams.set('access_token', token);
          const r = await fetch(u.toString(), { headers: { 'X-Access-Token': token } });
          const j = await r.json();
          const jobs = j.jobs || [];
          const shell = jobs.filter((x) =>
            x.command_kind === 'shell' && String(x.command || '').indexOf('e2e-upper-sweep-2') >= 0
          );
          return shell.length >= 1 && shell[0].status === 'completed';
        }""",
        ACCESS_TOKEN,
        timeout=120_000,
    )

    n2 = page.evaluate(
        """async (token) => {
          const u = new URL('/api/layers', location.origin);
          u.searchParams.set('access_token', token);
          const r = await fetch(u.toString(), { headers: { 'X-Access-Token': token } });
          const j = await r.json();
          return (j.layers || []).length;
        }""",
        ACCESS_TOKEN,
    )

    assert n1 == n2, f"第二次从上层创建后层数应不变（替换子层），得到 n1={n1} n2={n2}"


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


def test_clone_sets_online_project_symlink_active_tip(page: Page) -> None:
    """克隆成功后：onlineProject 应符号链接到 onlineService/layers 下对应 tip，/api/project/active 可解析。"""
    page.locator("#cloneUrl").fill(TEST_REPO)
    page.locator("#cloneDepth").fill("1")
    page.locator("#btnClone").click()

    expect(page.locator("#btnClone")).to_be_enabled(timeout=300_000)
    expect(page.locator("#cloneErr")).to_have_text("", timeout=10_000)

    res = page.evaluate(
        """async (token) => {
          const u = new URL('/api/project/active', location.origin);
          u.searchParams.set('access_token', token);
          const r = await fetch(u.toString(), { headers: { 'X-Access-Token': token } });
          return r.json();
        }""",
        ACCESS_TOKEN,
    )
    assert res.get("is_symlink") is True
    tip = res.get("active_tip_layer_id")
    assert isinstance(tip, str) and len(tip) > 8
    rp = res.get("resolved_path") or ""
    assert "onlineService" in rp or "layers" in rp or "materialized" in rp or "runtime" in rp


def test_redo_button(page: Page) -> None:
    """克隆后提交任务；必要时先中断，再点「重新执行」应 POST /redo 成功并回到 pending/running。"""
    page.locator("#cloneUrl").fill(TEST_REPO)
    page.locator("#cloneDepth").fill("1")
    page.locator("#btnClone").click()

    expect(page.locator("#btnClone")).to_be_enabled(timeout=300_000)
    expect(page.locator("#cloneErr")).to_have_text("", timeout=10_000)
    page.wait_for_function(
        """() => document.querySelector('#layerRelationActions textarea') !== null""",
        timeout=120_000,
    )

    # trae-cli 任务依赖 service_config.yaml + .venv；烟测用 shell。须长时间 sleep 以便进入 running 后中断，
    # 避免任务瞬间 completed 时 HEAD 与基线不一致导致 redo 被 git 锁拒绝。
    actions = page.locator("#layerRelationActions")
    actions.locator("textarea").fill("sleep 25; echo 'e2e：重新执行烟测'")
    actions.locator("select").select_option("shell")
    with page.expect_response(
        lambda r: r.request.method == "POST"
        and "/api/jobs" in r.url
        and "/redo" not in r.url
        and "/interrupt" not in r.url
        and "/reset" not in r.url,
        timeout=60_000,
    ) as post_job:
        actions.get_by_role("button", name="创建并执行").click()
    assert post_job.value.ok, post_job.value.text()

    card = page.locator(".job-card").first
    expect(card).to_be_visible(timeout=60_000)

    # 等到进入 running（sleep 任务会保持一段时间）
    page.wait_for_function(
        """() => {
          const st = document.querySelector('.job-card .status');
          if (!st) return false;
          return /\\brunning\\b/.test(st.className || '');
        }""",
        timeout=120_000,
    )

    with page.expect_response(lambda r: r.request.method == "POST" and "/interrupt" in r.url):
        card.locator("[data-interrupt]").click()
    expect(card.locator(".status.interrupted")).to_be_visible(timeout=120_000)

    redo = card.locator("[data-redo]")
    expect(redo).to_be_visible()
    with page.expect_response(
        lambda r: r.request.method == "POST" and "/redo" in r.url
    ) as redo_info:
        redo.click()
    assert redo_info.value.ok, redo_info.value.text()

    expect(card.locator(".status.pending, .status.running")).to_be_visible(timeout=60_000)


@pytest.mark.skip_reset
def test_serial_layer_rows_unique_and_match_deduped_api(page: Page) -> None:
    """串行列表中层行与 API 去重后层数一致；所有 data-mind-node-id 唯一，且无重复 __layer__ 前缀。"""
    page.goto(_ui_url(), wait_until="domcontentloaded", timeout=25_000)
    page.locator("#btnRefresh").wait_for(state="visible", timeout=15_000)
    page.locator("#btnRefresh").click()
    page.locator("#layerRelationTree").wait_for(state="visible", timeout=10_000)
    page.wait_for_function(
        """() => {
          const host = document.getElementById('layerRelationTree');
          if (!host) return false;
          const t = host.textContent || '';
          if (t.includes('暂无可写层')) return true;
          return document.getElementById('layer_serial_graph') !== null;
        }""",
        timeout=25_000,
    )
    result = page.evaluate(
        """async () => {
      const token = typeof ACCESS_TOKEN !== 'undefined' ? ACCESS_TOKEN : '';
      const host = document.getElementById('layerRelationTree');
      const hint = (host && host.textContent) || '';
      if (hint.includes('暂无可写层')) {
        return { skip: true, reason: 'no writable layers in this environment' };
      }
      const graph = document.getElementById('layer_serial_graph');
      if (!graph) {
        return { ok: false, reason: 'missing #layer_serial_graph though layers expected' };
      }
      const btns = Array.from(graph.querySelectorAll('button.layer-serial-row'));
      const ids = btns.map((b) => b.getAttribute('data-mind-node-id')).filter(Boolean);
      const idSet = new Set(ids);
      if (ids.length !== idSet.size) {
        return { ok: false, reason: 'duplicate mind node id', n: ids.length, uniq: idSet.size };
      }
      const PREFIX = '__layer__:';
      const layerStrip = ids
        .map((id) => String(id))
        .filter((id) => id.indexOf(PREFIX) === 0)
        .map((id) => id.slice(PREFIX.length));
      const layerSet = new Set(layerStrip);
      if (layerStrip.length !== layerSet.size) {
        return { ok: false, reason: 'duplicate layer row for same layer_id', layerStrip };
      }
      let r;
      try {
        r = await fetch(
          '/api/layers?access_token=' + encodeURIComponent(token),
          { headers: { 'X-Access-Token': token } },
        );
      } catch (e) {
        return { ok: false, reason: 'fetch /api/layers failed', err: String(e) };
      }
      if (!r.ok) {
        return { ok: false, reason: 'GET /api/layers ' + r.status };
      }
      const j = await r.json();
      const raw = j.layers || [];
      const by = new Map();
      for (const l of raw) {
        const id = l && l.layer_id;
        if (!id) continue;
        if (!by.has(id)) by.set(id, Object.assign({}, l));
        else {
          const cur = by.get(id);
          for (const k of Object.keys(l)) {
            const v = l[k];
            if (v !== undefined && v !== null && v !== '') cur[k] = v;
          }
        }
      }
      if (by.size !== layerStrip.length) {
        return {
          ok: false,
          reason: 'deduped API layer count !== serial layer rows',
          apiDeduped: by.size,
          serialLayerRows: layerStrip.length,
        };
      }
      return { ok: true, apiDeduped: by.size, serialRows: btns.length };
    }"""
    )
    if result.get("skip"):
        pytest.skip(str(result.get("reason", "skip")))
    assert result.get("ok") is True, result


_STEP_DIR_RE = re.compile(r"^step_(\d+)$")


@pytest.mark.skip_reset
def test_overlay_job_steps_on_disk_appear_in_task_card(page: Page) -> None:
    """Overlay 结束后 agent 步骤在 diff/.trae_agent_json；任务卡应加载到与磁盘 step 目录数一致的 UI。"""
    repo_root = Path(__file__).resolve().parents[2]
    state_path = repo_root / "onlineProject_state" / "runtime" / "jobs_state.json"
    if not state_path.is_file():
        pytest.skip("本地无 onlineProject_state/runtime/jobs_state.json")
    jobs = (json.loads(state_path.read_text(encoding="utf-8")).get("jobs")) or []
    sample_job_id = os.environ.get(
        "TRAE_E2E_FIXTURE_JOB_ID", "fc51b93c-a499-419f-a064-685be1857b45"
    ).strip()
    target = next((j for j in jobs if j.get("id") == sample_job_id), None)
    if not target:
        pytest.skip(
            f"jobs_state 中无 job id {sample_job_id}（可设 TRAE_E2E_FIXTURE_JOB_ID 或先跑过 overlay 任务）"
        )
    assert target is not None  # narrow for mypy（pytest.skip 在部分 stub 下非 NoReturn）
    layer_path = Path(str(target.get("layer_path") or ""))
    diff_agent_root = layer_path / "diff" / ".trae_agent_json" / sample_job_id
    if not diff_agent_root.is_dir():
        pytest.skip(f"非本场景：缺少 {diff_agent_root}")
    step_dirs = [p for p in diff_agent_root.iterdir() if p.is_dir() and _STEP_DIR_RE.match(p.name)]
    min_steps = len(step_dirs)
    assert min_steps >= 1

    page.goto(_ui_url(), wait_until="domcontentloaded")
    page.locator("#btnRefresh").wait_for(state="visible", timeout=15_000)
    card = page.locator(f'.job-card[data-id="{sample_job_id}"]')
    card.wait_for(state="visible", timeout=15_000)
    page.wait_for_function(
        """(jobId) => {
          const box = document.querySelector('.job-steps[data-job-id="' + jobId + '"]');
          if (!box) return false;
          return box.querySelectorAll('details.job-step-accordion').length >= 1;
        }""",
        arg=sample_job_id,
        timeout=25_000,
    )
    ui_count = card.locator("details.job-step-accordion").count()
    assert ui_count >= min_steps
