// @ts-check
/**
 * Playwright api 项目：与 node:test 双保险，确保任务详情页形态 URL 能拼出任务云前缀（换票前提）。
 *   cd trae-agent/onlineServiceJS && npx playwright test --project=api e2e/task-api-prefix-spa-path.api.spec.mjs
 */
import { test, expect } from '@playwright/test';
import { taskApiPrefix } from '../src/saasTaskCloud.mjs';

const KEYS = ['TaskApiEndPoint', 'tenantId', 'workspaceId', 'taskId', 'DOCKER_HOST_GATEWAY_IP'];

function snapshotEnv() {
  const o = {};
  for (const k of KEYS) o[k] = process.env[k];
  return o;
}

function restoreEnv(saved) {
  for (const k of KEYS) {
    if (saved[k] === undefined) delete process.env[k];
    else process.env[k] = saved[k];
  }
}

test.describe.configure({ mode: 'serial' });

test.describe('taskApiPrefix 与任务详情页 URL 兼容', () => {
  test('无 tenant 环境变量时，从 /tenant/.../task-detail/ 解析', () => {
    const saved = snapshotEnv();
    try {
      delete process.env.tenantId;
      delete process.env.workspaceId;
      delete process.env.taskId;
      process.env.TaskApiEndPoint =
        'http://aidevpm.com/tenant/827923618468040704/workspace/827923618602258432/task-detail/840502733785767936/';
      expect(taskApiPrefix()).toBe(
        'http://aidevpm.com/api/tenant/827923618468040704/workspace/827923618602258432/task/840502733785767936/cloud'
      );
    } finally {
      restoreEnv(saved);
    }
  });

  test('环境变量优先于路径', () => {
    const saved = snapshotEnv();
    try {
      process.env.tenantId = 'env-tenant';
      process.env.workspaceId = 'env-ws';
      process.env.taskId = 'env-task';
      process.env.TaskApiEndPoint = 'https://x.com/tenant/a/workspace/b/task-detail/c/';
      expect(taskApiPrefix()).toBe('https://x.com/api/tenant/env-tenant/workspace/env-ws/task/env-task/cloud');
    } finally {
      restoreEnv(saved);
    }
  });
});
