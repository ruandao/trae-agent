// @ts-check
/**
 * 校验「原始控制台日志」iframe：流式刷新时不应在用户向上翻阅时强制滚回底部。
 * 依赖本机服务：BASE_URL=http://127.0.0.1:8765 npx playwright test --project=chromium e2e/job-raw-log-scroll.spec.mjs
 */
import { test, expect } from '@playwright/test';

const TOKEN = process.env.ACCESS_TOKEN || 'dev-local-token';

function tallSrcdoc(lineCount) {
  const lines = Array.from({ length: lineCount }, (_, i) => `LINE_${i}`).join('\n');
  return (
    '<!DOCTYPE html><html><head><meta charset="utf-8"><style>' +
      'html,body{margin:0;padding:8px;background:#161618;color:#e8e8e8;font:13px/1.45 monospace}' +
      'pre{margin:0;white-space:pre-wrap;word-break:break-word}' +
      '</style></head><body><pre>' +
      lines.replace(/</g, '&lt;') +
      '</pre></body></html>'
  );
}

test.describe('原始控制台日志 iframe 滚动', () => {
  test('向上翻阅时分片更新不应强制滚到底部；贴底时仍跟随底部', async ({ page, baseURL }) => {
    const origin = baseURL || 'http://127.0.0.1:8765';
    await page.goto(`${origin}/ui/${encodeURIComponent(TOKEN)}`);

    const result = await page.evaluate(({ tallA, tallB }) => {
      const W = window.__onlineServiceUiTest;
      if (!W || typeof W.setExecRichIframeSrcdocSticky !== 'function') {
        return { ok: false, reason: 'missing __onlineServiceUiTest' };
      }
      const host = document.createElement('div');
      host.innerHTML =
        '<iframe class="out exec-rich-frame job-raw-frame" data-job-id="e2e-scroll"></iframe>';
      document.body.appendChild(host);
      const fr = /** @type {HTMLIFrameElement} */ (host.querySelector('iframe'));
      if (!fr) return { ok: false, reason: 'no iframe' };

      W.setExecRichIframeSrcdocSticky(fr, tallA);
      let doc = fr.contentDocument;
      if (!doc) return { ok: false, reason: 'no contentDocument' };
      let el = doc.scrollingElement || doc.documentElement;
      el.scrollTop = 0;
      W.setExecRichIframeSrcdocSticky(fr, tallB);
      doc = fr.contentDocument;
      el = doc.scrollingElement || doc.documentElement;
      const scrolledUpStillNearTop = el.scrollTop < 120;

      W.setExecRichIframeSrcdocSticky(fr, tallA);
      doc = fr.contentDocument;
      el = doc.scrollingElement || doc.documentElement;
      el.scrollTop = el.scrollHeight;
      W.setExecRichIframeSrcdocSticky(fr, tallB);
      doc = fr.contentDocument;
      el = doc.scrollingElement || doc.documentElement;
      const stuckBottom =
        el.scrollHeight - el.clientHeight - el.scrollTop < 64;

      return { ok: true, scrolledUpStillNearTop, stuckBottom };
    }, { tallA: tallSrcdoc(80), tallB: tallSrcdoc(200) });

    expect(result.ok, result.reason || '').toBeTruthy();
    expect(result.scrolledUpStillNearTop).toBe(true);
    expect(result.stuckBottom).toBe(true);
  });
});
