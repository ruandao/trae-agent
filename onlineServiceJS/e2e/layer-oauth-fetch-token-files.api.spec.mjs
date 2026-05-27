// @ts-check
/**
 * API：8765 oauth-fetch-token-files 须在合理时间内返回（不 pending）。
 *
 * ACCESS_TOKEN=DbiXfR54... BASE_URL=http://127.0.0.1:8765 \
 *   npx playwright test --project=api e2e/layer-oauth-fetch-token-files.api.spec.mjs
 */
import { test, expect } from '@playwright/test';

const TOKEN =
  process.env.ACCESS_TOKEN ||
  'DbiXfR54HqiD-Oe0Wqmq1xCDptuPM3fxhmMhfdYpXdKTUGHDwIJ5DqC3_Riry3C-';

async function pickGitLayer(request, origin) {
  const lr = await request.get(`${origin}/api/layers`, {
    headers: { 'X-Access-Token': TOKEN },
  });
  expect(lr.ok()).toBeTruthy();
  const layers = (await lr.json()).layers || [];
  const gitLayer = layers.find(
    (x) => x && x.git_worktree_dirty !== null && x.git_remote && x.git_remote.is_git,
  );
  expect(gitLayer, '需要至少一个含 git 仓库的可写层').toBeTruthy();
  return gitLayer;
}

test.describe('8765 oauth-fetch API', () => {
  test('GET /ui/:token 返回 HTML 壳', async ({ request, baseURL }) => {
    const origin = baseURL || 'http://127.0.0.1:8765';
    const res = await request.get(`${origin}/ui/${encodeURIComponent(TOKEN)}`);
    expect(res.ok()).toBeTruthy();
    const html = await res.text();
    expect(html).toMatch(/拉取AccessToken|layer-git-oauth-fetch-token-files|ACCESS_TOKEN/i);
  });

  test('POST oauth-fetch-token-files 在 30s 内返回 200', async ({ request, baseURL }) => {
    const origin = baseURL || 'http://127.0.0.1:8765';
    const gitLayer = await pickGitLayer(request, origin);
    const started = Date.now();
    const resp = await request.post(
      `${origin}/api/layers/${encodeURIComponent(gitLayer.layer_id)}/git/oauth-fetch-token-files`,
      {
        headers: {
          'X-Access-Token': TOKEN,
          'Content-Type': 'application/json',
        },
        data: {},
        timeout: 30_000,
      },
    );
    const elapsed = Date.now() - started;
    expect(resp.status(), await resp.text()).toBe(200);
    expect(elapsed).toBeLessThan(30_000);
    const body = await resp.json();
    expect(body.ok).toBe(true);
    expect(body.token_files.some((x) => x && x.write_ok)).toBeTruthy();
  });
});
