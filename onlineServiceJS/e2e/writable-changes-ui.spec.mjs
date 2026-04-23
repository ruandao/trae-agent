// @ts-check
/**
 * 依赖本机已启动服务：ACCESS_TOKEN 与页面一致；浏览器：npx playwright install chromium
 * BASE_URL=http://127.0.0.1:8765 npx playwright test --project=chromium e2e/writable-changes-ui.spec.mjs
 */
import { test, expect } from '@playwright/test';

const TOKEN = process.env.ACCESS_TOKEN || 'dev-local-token';

test.describe('可写层变动浏览 UI', () => {
  test('API diff 有结构且页面列表区有说明或路径行', async ({ page, request, baseURL }) => {
    const origin = baseURL || 'http://127.0.0.1:8765';
    const lr = await request.get(`${origin}/api/layers`, {
      headers: { 'X-Access-Token': TOKEN },
    });
    expect(lr.ok()).toBeTruthy();
    const lj = await lr.json();
    const layers = lj.layers || [];
    test.skip(layers.length === 0, '当前无本地可写层，跳过');

    const pick =
      layers.find((x) => x.parent_layer_id) ||
      layers.find((x) => x.layer_id) ||
      layers[0];

    const dr = await request.get(
      `${origin}/api/layers/${encodeURIComponent(pick.layer_id)}/diff/parent/files`,
      { headers: { 'X-Access-Token': TOKEN } },
    );
    expect(dr.ok()).toBeTruthy();
    const dj = await dr.json();
    expect(dj).toHaveProperty('layer_id');
    expect(dj.layer_id).toBe(pick.layer_id);
    expect(Array.isArray(dj.changes)).toBeTruthy();
    const apiHasExplanation =
      dj.parent_layer_id != null ||
      dj.same === true ||
      (typeof dj.detail === 'string' && dj.detail.length > 0);
    expect(apiHasExplanation).toBeTruthy();

    await page.goto(`${origin}/ui/${encodeURIComponent(TOKEN)}`);
    await page.getByRole('heading', { name: '可写层变动浏览' }).waitFor({ state: 'visible' });
    await page.locator('#writableChangesLayer').selectOption(pick.layer_id);

    const list = page.locator('#writableChangesList');
    await expect(list).not.toContainText('加载中…', { timeout: 20000 });
    await expect(list).not.toContainText('请先选择任务层');

    const rows = list.locator('.writable-change-row');
    const rowCount = await rows.count();
    const bodyText = (await list.innerText()).trim();

    expect(rowCount > 0 || bodyText.length > 0).toBeTruthy();
    expect(bodyText).toMatch(
      /一致|无可用父层|未解析|差异|路径|增|删|改|对比|父层|当前层|条路径/,
    );
  });
});
