// @ts-check
/**
 * 使用本机 Chrome 远程调试端口连接（CDP），便于边调试边看纵轨分支图。
 *
 * 1) 关闭正在运行的 Chrome 后执行（macOS 示例）：
 *    /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222
 * 2) 保持 onlineServiceJS 在 8765 运行，然后：
 *    BASE_URL=http://127.0.0.1:8765 npm run test:e2e:cdp
 *
 * 可通过 PW_CDP_URL 覆盖（默认 http://127.0.0.1:9222）。
 */
import { test, expect, chromium } from '@playwright/test';

const TOKEN = process.env.ACCESS_TOKEN || 'dev-local-token';
const BASE = process.env.BASE_URL || 'http://127.0.0.1:8765';
const CDP_URL = process.env.PW_CDP_URL || 'http://127.0.0.1:9222';

test.describe('SourceTree 风纵轨分支图（CDP 9222）', () => {
  test.describe.configure({ mode: 'serial', timeout: 60_000 });

  test('页面含 layer-branch-graph，有层时可见 branch-rail', async () => {
    let browser;
    try {
      browser = await chromium.connectOverCDP(CDP_URL);
    } catch (e) {
      test.skip(true, `无法连接 ${CDP_URL}：请先启动 Chrome --remote-debugging-port=9222（${e.message || e}）`);
      return;
    }

    const ctx = browser.contexts()[0];
    if (!ctx) {
      test.skip(true, 'CDP 浏览器无默认 context');
      return;
    }

    const page = await ctx.newPage();
    try {
      await page.goto(`${BASE.replace(/\/$/, '')}/ui/${encodeURIComponent(TOKEN)}`, {
        waitUntil: 'domcontentloaded',
      });

      const noLayers = page.getByText('暂无可写层');
      if (await noLayers.isVisible().catch(() => false)) {
        test.info().annotations.push({ type: 'note', description: '当前无层，跳过轨线数量断言' });
        return;
      }

      const graph = page.getByTestId('layer-branch-graph');
      await expect(graph).toBeVisible({ timeout: 20_000 });

      const rails = page.getByTestId('branch-rail');
      await expect(rails.first()).toBeVisible({ timeout: 15_000 });
      expect(await rails.count()).toBeGreaterThan(0);

      const nodes = page.getByTestId('branch-node');
      expect(await nodes.count()).toBe(await rails.count());
    } finally {
      await page.close();
    }
  });
});
