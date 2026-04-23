// @ts-check
/**
 * 仅用 Playwright「请求」客户端，不启动浏览器（无需 playwright install chromium）。
 * 启动服务：PORT=9876 ./run.sh（若 8765 被占用可换端口）
 * 运行：BASE_URL=http://127.0.0.1:9876 npm run test:e2e
 */
import { test, expect } from '@playwright/test';

const TOKEN = process.env.ACCESS_TOKEN || 'dev-local-token';

test.describe('onlineServiceJS HTTP', () => {
  test('GET /skill.md returns markdown', async ({ request }) => {
    const res = await request.get('/skill.md');
    expect(res.ok()).toBeTruthy();
    const ct = res.headers()['content-type'] || '';
    expect(ct).toMatch(/markdown|text\/plain/i);
    const text = await res.text();
    expect(text).toContain('onlineServiceJS');
  });

  test('GET /api/requirements/task-gate with token', async ({ request }) => {
    const res = await request.get('/api/requirements/task-gate', {
      headers: { 'X-Access-Token': TOKEN },
    });
    expect(res.ok()).toBeTruthy();
    const j = await res.json();
    expect(j).toHaveProperty('clone_done');
    expect(typeof j.clone_done).toBe('boolean');
  });

  test('GET /api/layers/empty-root with token', async ({ request }) => {
    const res = await request.get('/api/layers/empty-root', {
      headers: { 'X-Access-Token': TOKEN },
    });
    expect(res.ok()).toBeTruthy();
    const j = await res.json();
    expect(j).toHaveProperty('layer_id');
    expect(String(j.layer_id).length).toBeGreaterThan(0);
  });

  test('GET /api/ui/agent-render-hints 表驱动呈现声明', async ({ request }) => {
    const res = await request.get('/api/ui/agent-render-hints', {
      headers: { 'X-Access-Token': TOKEN },
    });
    expect(res.ok()).toBeTruthy();
    const j = await res.json();
    expect(j.version).toBe(1);
    expect(Array.isArray(j.step_rows)).toBeTruthy();
    expect(j.step_rows.length).toBeGreaterThan(0);
    expect(j.tool_expansion).toBeTruthy();
    expect(j.tool_expansion.per_call).toBeTruthy();
    expect(Array.isArray(j.tail_rows)).toBeTruthy();
    expect(j.rich_text_editor).toBeTruthy();
    expect(j.rich_text_editor.html_allowlist).toBeTruthy();
    expect(Array.isArray(j.rich_text_editor.html_allowlist.tags)).toBeTruthy();
    expect(j.rich_text_editor.html_allowlist.tags).toContain('p');
    expect(j.presentation_modes.rich_iframe.editor_html_contract).toBe('rich_text_editor');
  });
});

test.describe('onlineServiceJS UI HTML', () => {
  test('GET /ui/:token returns HTML shell', async ({ request }) => {
    const res = await request.get(`/ui/${TOKEN}`);
    expect(res.ok()).toBeTruthy();
    const ct = res.headers()['content-type'] || '';
    expect(ct).toMatch(/html/i);
    const html = await res.text();
    expect(html.length).toBeGreaterThan(50);
    expect(html).toMatch(/onlineServiceJS|克隆|SSE|配置|layer|DOCTYPE/i);
  });
});
