// @ts-check
/**
 * 校验：基于某可写层再次 POST /api/jobs 时，会按串行顺序删掉该层之后的层（与 UI 点层再「创建并执行」一致）。
 * 依赖：git + GitHub；启动 ./run.sh；BASE_URL 与 ACCESS_TOKEN 与页面一致。
 */
import { test, expect } from '@playwright/test';

const TOKEN = process.env.ACCESS_TOKEN || 'dev-local-token';
const SOMANYAD = 'https://github.com/ruandao/somanyad.git';

async function waitCloneCompleted(request, layerId) {
  await expect
    .poll(
      async () => {
        const r = await request.get(`/api/repos/clone-status/${encodeURIComponent(layerId)}`, {
          headers: { 'X-Access-Token': TOKEN },
        });
        expect(r.ok()).toBeTruthy();
        const b = await r.json();
        if (b.status === 'failed') throw new Error(b.detail || 'clone failed');
        return b.status === 'completed';
      },
      { timeout: 180_000 },
    )
    .toBe(true);
}

test.describe('串行尾部清理（叠层前）', () => {
  test.skip(!!process.env.SKIP_CLONE_E2E, 'SKIP_CLONE_E2E set');
  test.describe.configure({ timeout: 200_000 });

  test('同一 repo_layer 第二次创建任务时删除第一次产生的新层', async ({ request }) => {
    const er = await request.get('/api/layers/empty-root', {
      headers: { 'X-Access-Token': TOKEN },
    });
    expect(er.ok()).toBeTruthy();
    const { layer_id: parentId } = await er.json();

    const cr = await request.post('/api/repos/clone', {
      headers: { 'X-Access-Token': TOKEN, 'Content-Type': 'application/json' },
      data: {
        url: SOMANYAD,
        parent_layer_id: parentId,
        depth: 1,
        branch: 'master',
      },
    });
    expect(cr.status()).toBe(202);
    const { layer_id: rootLayerId } = await cr.json();
    await waitCloneCompleted(request, rootLayerId);

    const j1r = await request.post('/api/jobs', {
      headers: { 'X-Access-Token': TOKEN, 'Content-Type': 'application/json' },
      data: {
        command: 'echo serial_tail_purge_1',
        command_kind: 'shell',
        repo_layer_id: rootLayerId,
      },
    });
    expect(j1r.status(), await j1r.text()).toBe(201);
    const j1 = await j1r.json();
    const stackedLayer1 = j1.layer_id;
    expect(stackedLayer1).toBeTruthy();
    expect(stackedLayer1).not.toBe(rootLayerId);

    const lr1 = await request.get('/api/layers', { headers: { 'X-Access-Token': TOKEN } });
    expect(lr1.ok()).toBeTruthy();
    const ids1 = (await lr1.json()).layers.map((x) => x.layer_id);
    expect(ids1).toContain(stackedLayer1);

    const j2r = await request.post('/api/jobs', {
      headers: { 'X-Access-Token': TOKEN, 'Content-Type': 'application/json' },
      data: {
        command: 'echo serial_tail_purge_2',
        command_kind: 'shell',
        repo_layer_id: rootLayerId,
      },
    });
    expect(j2r.status(), await j2r.text()).toBe(201);
    const j2 = await j2r.json();
    expect(j2.layer_id).toBeTruthy();
    expect(j2.layer_id).not.toBe(stackedLayer1);

    const lr2 = await request.get('/api/layers', { headers: { 'X-Access-Token': TOKEN } });
    expect(lr2.ok()).toBeTruthy();
    const ids2 = (await lr2.json()).layers.map((x) => x.layer_id);
    expect(ids2).not.toContain(stackedLayer1);

    const jobsR = await request.get('/api/jobs', { headers: { 'X-Access-Token': TOKEN } });
    expect(jobsR.ok()).toBeTruthy();
    const jobs = (await jobsR.json()).jobs || [];
    expect(jobs.some((x) => x.id === j1.id)).toBe(false);
  });
});
