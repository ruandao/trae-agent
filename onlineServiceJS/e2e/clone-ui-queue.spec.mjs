// @ts-check
/**
 * 浏览器校验：202 异步入队后按钮恢复；克隆输出区与右侧 SSE 日志有进度事件。
 *   npx playwright install chromium
 *   BASE_URL=http://127.0.0.1:8765 npm run test:e2e:browser
 */
import { test, expect } from '@playwright/test';

const TOKEN = process.env.ACCESS_TOKEN || 'dev-local-token';
const SOMANYAD = 'https://github.com/ruandao/somanyad.git';

test.describe('clone UI async queue', () => {
  test.skip(!!process.env.SKIP_CLONE_E2E, 'SKIP_CLONE_E2E set');

  test.describe.configure({ timeout: 180_000 });

  test('克隆按钮在入队后恢复可点；cloneOut 与 sseLog 出现进度', async ({ page }) => {
    await page.goto(`/ui/${TOKEN}`);
    await page.getByTestId('clone-url').fill(SOMANYAD);
    const depth = page.locator('#cloneDepth');
    if (await depth.count()) await depth.fill('1');
    const branch = page.locator('#cloneBranch');
    if (await branch.count()) await branch.fill('master');

    const btn = page.getByTestId('btn-clone');
    await btn.click();
    await expect(btn).toBeEnabled({ timeout: 15_000 });
    const cloneFrame = page.frameLocator('#cloneOutFrame');
    await expect(page.locator('#cloneOutBanner')).toContainText(/队列/, { timeout: 15_000 });
    await expect(cloneFrame.locator('body')).toContainText(/clone|git|Receiving|objects/i, {
      timeout: 120_000,
    });
    await expect(page.locator('#sseLog')).toContainText(/repo_clone_started|开始克隆/i, {
      timeout: 120_000,
    });
  });
});
