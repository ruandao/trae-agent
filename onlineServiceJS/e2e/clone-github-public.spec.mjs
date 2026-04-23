// @ts-check
/**
 * 需本机 git + 网络访问 GitHub。启动：./run.sh
 *   BASE_URL=http://127.0.0.1:8765 npm run test:e2e
 * 跳过：SKIP_CLONE_E2E=1
 */
import { test, expect } from '@playwright/test';

const TOKEN = process.env.ACCESS_TOKEN || 'dev-local-token';
const SOMANYAD = 'https://github.com/ruandao/somanyad.git';

/**
 * @param {import('@playwright/test').APIRequestContext} request
 * @param {string} layerId
 */
async function waitCloneCompleted(request, layerId) {
  await expect
    .poll(
      async () => {
        const r = await request.get(`/api/repos/clone-status/${encodeURIComponent(layerId)}`, {
          headers: { 'X-Access-Token': TOKEN },
        });
        expect(r.ok()).toBeTruthy();
        const b = await r.json();
        if (b.status === 'failed') {
          throw new Error(b.detail || 'clone failed');
        }
        return b.status === 'completed';
      },
      { timeout: 180_000 },
    )
    .toBe(true);
}

test.describe('clone public GitHub repo', () => {
  test.skip(!!process.env.SKIP_CLONE_E2E, 'SKIP_CLONE_E2E set');

  test.describe.configure({ timeout: 180_000 });

  test('POST /api/repos/clone ruandao/somanyad — 无效 PEM 不误转 SSH', async ({ request }) => {
    const er = await request.get('/api/layers/empty-root', {
      headers: { 'X-Access-Token': TOKEN },
    });
    expect(er.ok()).toBeTruthy();
    const { layer_id: parentId } = await er.json();

    const res = await request.post('/api/repos/clone', {
      headers: { 'X-Access-Token': TOKEN, 'Content-Type': 'application/json' },
      data: {
        url: SOMANYAD,
        parent_layer_id: parentId,
        depth: 1,
        branch: 'master',
        // 模拟页面/localStorage 残留：非 PEM 不得触发 HTTPS→git@ 与 GIT_SSH_COMMAND
        ephemeral_ssh_private_key: 'not-a-real-key',
      },
    });

    const body = await res.text();
    expect(res.status(), body).toBe(202);
    expect(res.ok(), body).toBeTruthy();
    const j = JSON.parse(body);
    expect(j.accepted).toBe(true);
    expect(j.layer_id).toBeTruthy();
    expect(j.layer_path).toBeTruthy();
    await waitCloneCompleted(request, j.layer_id);
  });

  test('POST /api/repos/clone 异步入队：202 早于克隆完成', async ({ request }) => {
    const er = await request.get('/api/layers/empty-root', {
      headers: { 'X-Access-Token': TOKEN },
    });
    expect(er.ok()).toBeTruthy();
    const { layer_id: parentId } = await er.json();

    const t0 = Date.now();
    const res = await request.post('/api/repos/clone', {
      headers: { 'X-Access-Token': TOKEN, 'Content-Type': 'application/json' },
      data: {
        url: SOMANYAD,
        parent_layer_id: parentId,
        depth: 1,
        branch: 'master',
      },
    });
    const elapsed = Date.now() - t0;
    const body = await res.text();
    expect(res.status(), body).toBe(202);
    expect(elapsed, 'HTTP 应在 git 完成前返回').toBeLessThan(8000);
    const j = JSON.parse(body);
    expect(j.accepted).toBe(true);
    expect(j.queue_position).toBe(0);
    await waitCloneCompleted(request, j.layer_id);
  });

  test('克隆后 GET /api/layers 中该层 git_worktree_dirty 为 false（非 null，避免 UI 误判无 git）', async ({ request }) => {
    const er = await request.get('/api/layers/empty-root', {
      headers: { 'X-Access-Token': TOKEN },
    });
    expect(er.ok()).toBeTruthy();
    const { layer_id: parentId } = await er.json();

    const res = await request.post('/api/repos/clone', {
      headers: { 'X-Access-Token': TOKEN, 'Content-Type': 'application/json' },
      data: {
        url: SOMANYAD,
        parent_layer_id: parentId,
        depth: 1,
        branch: 'master',
      },
    });
    const body = await res.text();
    expect(res.status(), body).toBe(202);
    const { layer_id: newLayerId } = JSON.parse(body);
    await waitCloneCompleted(request, newLayerId);

    const lr = await request.get('/api/layers', {
      headers: { 'X-Access-Token': TOKEN },
    });
    expect(lr.ok()).toBeTruthy();
    const { layers } = await lr.json();
    const row = layers.find((x) => x.layer_id === newLayerId);
    expect(row, `layer ${newLayerId} in /api/layers`).toBeTruthy();
    expect(row.git_worktree_dirty).toBe(false);
    expect(row.command, '层节点应对外展示克隆指令而非 idle_done').toMatch(/git clone|somanyad/i);

    const man = await request.get(
      `/api/exec-streams/clone/${encodeURIComponent(newLayerId)}/manifest`,
      { headers: { 'X-Access-Token': TOKEN } },
    );
    expect(man.ok()).toBeTruthy();
    const mj = await man.json();
    expect(Array.isArray(mj.segments)).toBeTruthy();
    expect(mj.segments.length).toBeGreaterThan(0);
    expect(mj.complete).toBe(true);
    const seg0 = await request.get(
      `/api/exec-streams/clone/${encodeURIComponent(newLayerId)}/segments/0`,
      { headers: { 'X-Access-Token': TOKEN } },
    );
    expect(seg0.ok()).toBeTruthy();
    const sj = await seg0.json();
    expect(sj.text).toBeDefined();
  });
});
