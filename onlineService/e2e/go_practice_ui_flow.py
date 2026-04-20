#!/usr/bin/env python3
"""
Playwright：在本地 Trae Online UI 上完成「SSH 克隆 → trae 任务 → 提交 → 推送」全流程。

前置：
  - 已启动 ./onlineService/run_local.sh（默认 ACCESS_TOKEN=dev-local-token，端口 8765）
  - 已安装：pip install -r onlineService/e2e/requirements-e2e.txt && playwright install chromium
  - 本机 tmp 下存在可用的 GitHub SSH 私钥（默认路径见 --ssh-key-path）
  - trae-cli / LLM 配置可用，否则「用js 写一个杨辉三角」任务会失败

示例（仓库根目录执行）：

  python onlineService/e2e/go_practice_ui_flow.py

  python onlineService/e2e/go_practice_ui_flow.py --base-url http://127.0.0.1:8765 --reset

环境变量（可选）：
  TRAE_UI_BASE、ACCESS_TOKEN、TRAE_DEMO_SSH_KEY、TRAE_DEMO_REPO_SSH（默认克隆 URL）、TRAE_JOB_TIMEOUT_MS
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright


def _is_get_jobs(r) -> bool:
    return r.request.method == "GET" and urlparse(r.url).path.rstrip("/") == "/api/jobs"


def _refresh_jobs_and_wait_layer_in_ztree(page, layer_id: str) -> None:
    """刷新任务列表，直到串行可写层列表出现对应层行（异步渲染）。"""
    lid = str(layer_id or "").strip()
    if not lid:
        raise ValueError("empty layer_id")
    last_err: str | None = None
    for _attempt in range(5):
        with page.expect_response(_is_get_jobs, timeout=45_000):
            page.locator("#btnRefresh").click()
        page.wait_for_timeout(600)
        try:
            page.wait_for_function(
                """(lid) => {
                  const host = document.getElementById('layer_serial_graph');
                  if (!host || !lid) return false;
                  const nid = '__layer__:' + lid;
                  return Array.from(host.querySelectorAll('button.layer-serial-row')).some(
                    (b) => b.getAttribute('data-mind-node-id') === nid
                  );
                }""",
                arg=lid,
                timeout=25_000,
            )
            return
        except Exception as e:
            last_err = str(e)
            continue
    raise TimeoutError(f"串行列表未在多次刷新后出现层节点 {layer_id!r}；最后错误: {last_err}")


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_UI = (
    os.environ.get("TRAE_UI_BASE", "http://localhost:8765").rstrip("/")
    + "/ui/"
    + os.environ.get("ACCESS_TOKEN", "dev-local-token")
)
DEFAULT_KEY = Path(
    os.environ.get("TRAE_DEMO_SSH_KEY", str(REPO_ROOT / "tmp/ljy080829@gmail.com.github.key"))
)
DEFAULT_REPO = os.environ.get("TRAE_DEMO_REPO_SSH", "git@github.com:ruandao/goPractice.git")
DEFAULT_CMD = os.environ.get("TRAE_DEMO_TASK_CMD", "用js 写一个杨辉三角")
JOB_TIMEOUT_MS = int(os.environ.get("TRAE_JOB_TIMEOUT_MS", str(900_000)))


def _poll_latest_non_clone_job(page, token: str) -> dict | None:
    data = page.evaluate(
        """async (token) => {
          const u = new URL('/api/jobs', location.origin);
          u.searchParams.set('access_token', token);
          const r = await fetch(u.toString(), { headers: { 'X-Access-Token': token } });
          return await r.json();
        }""",
        token,
    )
    jobs = data.get("jobs") or []
    for j in reversed(jobs):
        if j.get("command_kind") != "clone":
            return j
    return None


def _wait_job_terminal(page, token: str, *, timeout_ms: int) -> dict:
    deadline = time.monotonic() + timeout_ms / 1000.0
    last: dict | None = None
    while time.monotonic() < deadline:
        last = _poll_latest_non_clone_job(page, token)
        if last and last.get("status") in ("completed", "failed", "interrupted"):
            return last
        page.wait_for_timeout(800)
    raise TimeoutError(f"任务在 {timeout_ms}ms 内未完成，最后快照: {last!r}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default=DEFAULT_UI, help="完整 UI 地址（含 /ui/<token>）")
    p.add_argument("--token", default=os.environ.get("ACCESS_TOKEN", "dev-local-token"))
    p.add_argument("--ssh-key-path", type=Path, default=DEFAULT_KEY, help="服务器本机 SSH 私钥路径")
    p.add_argument(
        "--repo-url",
        default=DEFAULT_REPO,
        help="克隆 URL（git@ 或 https://）",
    )
    p.add_argument(
        "--https-public-clone",
        action="store_true",
        help="不传 ssh_identity_file，用于公共仓库 HTTPS 烟测（推送仍可能因无 token 失败）",
    )
    p.add_argument("--task-command", default=DEFAULT_CMD)
    p.add_argument(
        "--command-kind",
        choices=("trae", "shell"),
        default="trae",
        help="新建任务的 command_kind；烟测可用 shell 避免调用 trae-cli/LLM",
    )
    p.add_argument("--reset", action="store_true", help="开始前点击「重置」")
    p.add_argument("--job-timeout-ms", type=int, default=JOB_TIMEOUT_MS)
    args = p.parse_args()

    key_path = args.ssh_key_path.expanduser().resolve()
    key_pem: str | None = None
    if not args.https_public_clone:
        if not key_path.is_file():
            print(f"错误：找不到 SSH 私钥文件：{key_path}", file=sys.stderr)
            return 2
        key_pem = key_path.read_text(encoding="utf-8", errors="replace")
        if "BEGIN" not in key_pem or "PRIVATE KEY" not in key_pem:
            print(f"警告：{key_path} 似乎不是 PEM 私钥，仍尝试继续。", file=sys.stderr)

    ui = args.base_url.strip()
    origin = ui.split("/ui/", 1)[0] if "/ui/" in ui else ui.rsplit("/", 1)[0]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("dialog", lambda d: d.accept())

        page.goto(ui, wait_until="domcontentloaded", timeout=60_000)
        page.locator("#btnClone").wait_for(state="visible", timeout=30_000)

        if args.reset:
            with page.expect_response(
                lambda r: r.request.method == "POST" and "/api/jobs/reset" in r.url,
                timeout=120_000,
            ):
                page.locator("#btnReset").click()
            page.locator("#btnClone").wait_for(state="visible", timeout=30_000)

        page.get_by_test_id("clone-url").fill(args.repo_url)
        page.get_by_test_id("clone-ssh-key-path").fill(
            "" if args.https_public_clone else str(key_path)
        )
        page.locator("#cloneDepth").fill("1")
        page.get_by_test_id("btn-clone").click()
        try:
            expect(page.get_by_test_id("btn-clone")).to_be_enabled(timeout=300_000)
        except AssertionError:
            print("克隆长时间未完成。cloneOut / cloneErr：", file=sys.stderr)
            print((page.locator("#cloneOut").inner_text() or "")[:4000], file=sys.stderr)
            print((page.locator("#cloneErr").inner_text() or ""), file=sys.stderr)
            browser.close()
            return 3
        err = page.locator("#cloneErr")
        if err.text_content(timeout=5_000).strip():
            print("克隆失败：" + err.inner_text(), file=sys.stderr)
            browser.close()
            return 3

        page.locator('[data-testid="layer-new-job-command"]').wait_for(
            state="visible", timeout=120_000
        )
        bar = page.locator("#layerRelationActions")
        bar.locator('[data-testid="layer-new-job-command"]').fill(args.task_command)
        bar.locator('[data-testid="layer-new-job-kind"]').select_option(args.command_kind)

        def _is_jobs_create(r) -> bool:
            if r.request.method != "POST":
                return False
            path = urlparse(r.url).path.rstrip("/")
            return path == "/api/jobs"

        btn_run = bar.get_by_test_id("layer-create-and-run")
        btn_run.scroll_into_view_if_needed()
        with page.expect_response(_is_jobs_create, timeout=120_000) as resp_info:
            btn_run.click()
        post = resp_info.value
        if not post.ok:
            print("创建任务失败：", post.status, post.text(), file=sys.stderr)
            browser.close()
            return 4

        job = _wait_job_terminal(page, args.token, timeout_ms=args.job_timeout_ms)
        if job.get("status") != "completed":
            print("任务未成功完成：", json.dumps(job, ensure_ascii=False), file=sys.stderr)
            browser.close()
            return 5

        layer_id = str(job.get("layer_id") or "").strip()
        if not layer_id:
            print("任务记录缺少 layer_id", file=sys.stderr)
            browser.close()
            return 6

        # 任务结束后串行列表可能尚未含新子层行：刷新并轮询直至可点选
        _refresh_jobs_and_wait_layer_in_ztree(page, layer_id)

        ok = page.evaluate("(lid) => window.__traeE2e_selectLayerNode(lid)", layer_id)
        if not ok:
            print(
                f"无法在串行列表选中层 {layer_id}（__traeE2e_selectLayerNode 返回 false）",
                file=sys.stderr,
            )
            browser.close()
            return 7

        page.get_by_test_id("layer-git-commit").click()
        expect(page.get_by_test_id("layer-git-commit")).to_be_enabled(timeout=120_000)

        push_btn = page.get_by_test_id("layer-git-push")
        if not push_btn.count():
            print(
                "未显示「推送」按钮（可能无 upstream 或非 SSH/HTTPS 远程）；跳过推送。",
                file=sys.stderr,
            )
            browser.close()
            return 0

        push_body: dict = {}
        if key_pem:
            push_body["ephemeral_ssh_private_key"] = key_pem
        resp = page.request.post(
            f"{origin}/api/layers/{layer_id}/git/push",
            params={"access_token": args.token},
            headers={
                "X-Access-Token": args.token,
                "Content-Type": "application/json",
            },
            data=json.dumps(push_body if push_body else {}),
            timeout=120_000,
        )
        if not resp.ok:
            print("API 推送失败：", resp.status, resp.text(), file=sys.stderr)
            browser.close()
            return 8

        print(resp.text())
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
