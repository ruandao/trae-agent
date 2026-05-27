// @ts-check
/**
 * UI：8765 页面点击「拉取AccessToken」不 pending。
 *
 * ACCESS_TOKEN=DbiXfR54... BASE_URL=http://127.0.0.1:8765 \
 *   npx playwright test --project=chromium e2e/layer-oauth-fetch-token-files-ui.spec.mjs
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

test('页面点击「拉取AccessToken」在 30s 内完成', async ({ page, request, baseURL }) => {
  test.setTimeout(60_000);
  const origin = baseURL || 'http://127.0.0.1:8765';
  const gitLayer = await pickGitLayer(request, origin);

  page.on('dialog', async (dialog) => {
    expect(dialog.message()).not.toMatch(/失败|fetch failed|aborted|timeout/i);
    await dialog.accept();
  });

  await page.goto(`${origin}/ui/${encodeURIComponent(TOKEN)}`);
  await expect(page.getByTestId('layer-branch-graph')).toBeVisible({ timeout: 20_000 });

  const layerBtn = page
    .locator('button.layer-serial-row')
    .filter({ hasText: gitLayer.layer_id })
    .first();
  await expect(layerBtn).toBeVisible({ timeout: 20_000 });
  await layerBtn.click();

  const fetchBtn = page.getByTestId('layer-git-oauth-fetch-token-files');
  await expect(fetchBtn).toBeVisible({ timeout: 10_000 });

  const responsePromise = page.waitForResponse(
    (resp) =>
      resp.url().includes('/git/oauth-fetch-token-files') && resp.request().method() === 'POST',
    { timeout: 30_000 },
  );

  await fetchBtn.click();
  const response = await responsePromise;
  expect(response.status()).toBe(200);
  const payload = await response.json();
  expect(payload.ok).toBe(true);

  await expect(fetchBtn).toHaveText('拉取AccessToken', { timeout: 15_000 });
});
