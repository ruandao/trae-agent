import { test, expect } from '@playwright/test';

test.describe('生成提交日志按钮', () => {
  test('点击生成提交日志按钮显示 AI 总结', async ({ page, request }) => {
    const baseUrl = process.env.BASE_URL || 'http://127.0.0.1:8765';
    const token = process.env.ACCESS_TOKEN || 'dev-local-token';

    await page.goto(`${baseUrl}/ui/${token}`);

    await page.waitForSelector('[data-testid="layer-list"]');

    const writableLayers = await page.evaluate(() => {
      const layers = [];
      document.querySelectorAll('[data-layer-id]').forEach(el => {
        const id = el.getAttribute('data-layer-id');
        const writable = el.querySelector('[title="可写"]') !== null;
        if (writable) {
          layers.push(id);
        }
      });
      return layers;
    });

    if (writableLayers.length === 0) {
      test.skip('没有可写层可用');
      return;
    }

    let targetLayerId = null;
    let diffFiles = [];

    for (const layerId of writableLayers) {
      try {
        const response = await request.get(`${baseUrl}/api/layers/${layerId}/diff/parent/files`);
        if (response.ok()) {
          const data = await response.json();
          if (data.changes && data.changes.length > 0) {
            targetLayerId = layerId;
            diffFiles = data.changes;
            break;
          }
        }
      } catch (e) {
        console.log(`检查层 ${layerId} 失败:`, e);
      }
    }

    if (!targetLayerId) {
      test.skip('没有找到与上层有差异的可写层');
      return;
    }

    await page.click(`[data-layer-id="${targetLayerId}"]`);

    await page.waitForSelector('button:has-text("生成提交日志")');

    const generateBtn = page.locator('button:has-text("生成提交日志")');
    await generateBtn.click();

    await page.waitForSelector('pre', { state: 'visible' });

    await page.waitForFunction(() => {
      const pre = document.querySelector('pre');
      return pre && !pre.textContent.includes('生成中…');
    });

    const execPre = page.locator('pre');
    const content = await execPre.textContent();

    console.log('生成的总结内容:', content);

    expect(content).not.toBe('生成失败：无返回总结');
    expect(content).not.toBe('未找到与上层可写层有差异的文件');
    expect(content).not.toBe('（当前工作区与 HEAD 无差异）');

    if (diffFiles.some(c => c.kind === 'removed')) {
      const removedFiles = diffFiles.filter(c => c.kind === 'removed').map(c => c.path);
      expect(content).toContain('删除');
      removedFiles.forEach(file => {
        expect(content).toContain(file.split('/').pop());
      });
    }

    if (diffFiles.some(c => c.kind === 'added')) {
      expect(content).toContain('新增');
    }

    if (diffFiles.some(c => c.kind === 'modified')) {
      expect(content).toContain('修改');
    }
  });
});
