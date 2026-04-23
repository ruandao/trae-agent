// @ts-check
/**
 * API 校验可不下载浏览器；若需跑依赖 page 的用例，再执行：
 *   npx playwright install chromium
 * UI 用例（clone-ui-queue）默认 channel: 'chrome'；无 Chrome 时再 npx playwright install chromium。
 */
export default {
  testDir: 'e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: 'list',
  use: {
    baseURL: process.env.BASE_URL || 'http://127.0.0.1:8765',
    trace: 'off',
  },
  projects: [
    {
      name: 'api',
      testIgnore: [
        '**/clone-ui-queue.spec.mjs',
        '**/branch-graph-cdp.spec.mjs',
        '**/writable-changes-ui.spec.mjs',
      ],
    },
    {
      name: 'chromium',
      testMatch: ['**/clone-ui-queue.spec.mjs', '**/writable-changes-ui.spec.mjs'],
      /* 优先本机 Chrome，避免 CI/弱网无法下载 Chromium bundle */
      use: { channel: 'chrome' },
    },
    {
      name: 'cdp9222',
      testMatch: '**/branch-graph-cdp.spec.mjs',
      /* 浏览器由外部 Chrome --remote-debugging-port=9222 提供；无需 channel */
      use: {},
    },
  ],
};
