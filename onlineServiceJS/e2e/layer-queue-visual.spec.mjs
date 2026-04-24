// @ts-check
/**
 * 串行图：运行中任务轨点转圈（.st-row-running）、队列占位行虚线（.is-layer-queue + .zt-virtual）。
 * 从仓库 static/index.html 注入页面，避免本机已跑服务返回旧版 HTML；API 由 route mock。
 *
 * BASE_URL=http://127.0.0.1:8765 npx playwright test --project=chromium e2e/layer-queue-visual.spec.mjs
 */
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { test, expect } from '@playwright/test';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const INDEX_HTML_PATH = path.join(__dirname, '../static/index.html');

const TOKEN = process.env.ACCESS_TOKEN || 'dev-local-token';

const MOCK_JOB = {
  id: 'cccccccc-1111-2222-3333-444444444444',
  layer_id: 'L_queue_visual',
  status: 'running',
  command: 'mock active trae command',
  command_kind: 'trae',
  created_at: '2026-04-23T12:00:00.000Z',
  parent_job_id: null,
  repo_layer_id: null,
  exit_code: null,
  output: '',
  git_branch: null,
  git_destructive_locked: false,
};

const MOCK_LAYERS_BODY = {
  layers: [
    {
      layer_id: 'L_queue_visual',
      created_at: '2026-04-20T10:00:00.000Z',
      command: 'git clone https://example.com/repo.git',
      parent_layer_id: null,
      job_id: MOCK_JOB.id,
      job_status: 'running',
      queue_depth: 2,
      queue_items: [
        { position: 0, command_kind: 'trae', command_preview: 'queued trae instruction one' },
        { position: 1, command_kind: 'shell', command_preview: 'echo queued shell two' },
      ],
      mind_state: 'running',
      git_worktree_dirty: false,
      meta_kind: null,
    },
  ],
  layers_root: '/tmp/mock_layers',
  bootstrap_layer_id: null,
};

async function stubUiAndApi(page) {
  await page.route('**/ui/**', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.continue();
      return;
    }
    let html = fs.readFileSync(INDEX_HTML_PATH, 'utf8');
    html = html.replace('__ACCESS_TOKEN_JSON__', JSON.stringify(TOKEN));
    await route.fulfill({
      status: 200,
      contentType: 'text/html; charset=utf-8',
      body: html,
    });
  });
  await page.route('**/api/requirements/task-gate**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ clone_done: true }),
    });
  });
  await page.route('**/api/repos/bootstrap-clone-log**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ layer_id: '', text: '' }),
    });
  });
  await page.route('**/api/layers**', async (route) => {
    const u = new URL(route.request().url());
    if (route.request().method() !== 'GET') {
      await route.continue();
      return;
    }
    if (u.pathname.includes('/children')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ entries: [] }),
      });
      return;
    }
    if (u.pathname === '/api/layers') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(MOCK_LAYERS_BODY),
      });
      return;
    }
    await route.continue();
  });
  await page.route('**/api/jobs**', async (route) => {
    const u = new URL(route.request().url());
    if (route.request().method() !== 'GET' || u.pathname !== '/api/jobs') {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ jobs: [MOCK_JOB] }),
    });
  });
}

test.describe('可写层串行图 · 队列可视化', () => {
  test('运行中显示转圈轨点，队列行虚线样式', async ({ page, baseURL }) => {
    const origin = baseURL || 'http://127.0.0.1:8765';
    await stubUiAndApi(page);
    await page.goto(`${origin}/ui/${encodeURIComponent(TOKEN)}`);

    const graph = page.locator('[data-testid="layer-branch-graph"]');
    await expect(graph).toBeVisible({ timeout: 30000 });

    const runningWrap = graph.locator('.layer-serial-row-wrap.st-row-running');
    await expect(runningWrap).toHaveCount(1, { timeout: 15000 });
    await expect(runningWrap).toHaveAttribute('data-layer-serial-kind', 'running');

    const queueWraps = graph.locator('.layer-serial-row-wrap.is-layer-queue');
    await expect(queueWraps).toHaveCount(2);
    await expect(queueWraps.first()).toHaveAttribute('data-layer-serial-kind', 'queue');

    const dashedButtons = graph.locator('button.layer-serial-row.zt-virtual');
    await expect(dashedButtons).toHaveCount(2);

    const spinNode = runningWrap.locator('[data-testid="branch-node"]');
    await expect(spinNode).toBeVisible();
    const anim = await spinNode.evaluate((el) => getComputedStyle(el).animationName);
    expect(anim === 'none' || String(anim).includes('st-node-spin')).toBeTruthy();
  });
});
