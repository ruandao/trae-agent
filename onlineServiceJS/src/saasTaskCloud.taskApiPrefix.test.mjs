// @ts-check
/** taskApiPrefix：任务详情页形态 TaskApiEndPoint 须能解析，否则换票不执行。 */
import { test } from 'node:test';
import assert from 'node:assert';
import { isRetryableGitCloneFailure, taskApiPrefix } from './saasTaskCloud.mjs';

function snapshotEnv(keys) {
  const out = {};
  for (const k of keys) {
    out[k] = process.env[k];
  }
  return out;
}

function restoreEnv(saved) {
  for (const k of Object.keys(saved)) {
    const v = saved[k];
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
}

const KEYS = [
  'TaskApiEndPoint',
  'TASK_API_ENDPOINT',
  'tenantId',
  'workspaceId',
  'taskId',
  'DOCKER_GATEWAY_HOSTNAME',
  'DOCKER_HOST_GATEWAY_IP',
];

test('taskApiPrefix：浏览器任务详情路径 /tenant/.../task-detail/{task}', () => {
  const saved = snapshotEnv(KEYS);
  try {
    delete process.env.tenantId;
    delete process.env.workspaceId;
    delete process.env.taskId;
    process.env.TaskApiEndPoint =
      'http://aidevpm.com/tenant/827923618468040704/workspace/827923618602258432/task-detail/840502733785767936/';
    assert.strictEqual(
      taskApiPrefix(),
      'http://aidevpm.com/api/tenant/827923618468040704/workspace/827923618602258432/task/840502733785767936/cloud'
    );
  } finally {
    restoreEnv(saved);
  }
});

test('taskApiPrefix：保留标准 /api/tenant/.../task/.../cloud 路径', () => {
  const saved = snapshotEnv(KEYS);
  try {
    delete process.env.tenantId;
    delete process.env.workspaceId;
    delete process.env.taskId;
    process.env.TaskApiEndPoint =
      'https://api.example.com/api/tenant/a/workspace/b/task/c/cloud';
    assert.strictEqual(
      taskApiPrefix(),
      'https://api.example.com/api/tenant/a/workspace/b/task/c/cloud'
    );
  } finally {
    restoreEnv(saved);
  }
});

test('taskApiPrefix：仅设 TASK_API_ENDPOINT（docker -e 常见写法）时与 TaskApiEndPoint 等价', () => {
  const saved = snapshotEnv(KEYS);
  try {
    delete process.env.TaskApiEndPoint;
    delete process.env.tenantId;
    delete process.env.workspaceId;
    delete process.env.taskId;
    process.env.TASK_API_ENDPOINT =
      'http://api.aidevpm.com/api/tenant/827923618468040704/workspace/827923618602258432/task/840502733785767936/cloud';
    assert.strictEqual(
      taskApiPrefix(),
      'http://api.aidevpm.com/api/tenant/827923618468040704/workspace/827923618602258432/task/840502733785767936/cloud'
    );
  } finally {
    restoreEnv(saved);
  }
});

test('taskApiPrefix：/api/.../task-detail/{id}', () => {
  const saved = snapshotEnv(KEYS);
  try {
    delete process.env.tenantId;
    delete process.env.workspaceId;
    delete process.env.taskId;
    process.env.TaskApiEndPoint =
      'https://api.example.com/api/tenant/x/workspace/y/task-detail/z/';
    assert.strictEqual(
      taskApiPrefix(),
      'https://api.example.com/api/tenant/x/workspace/y/task/z/cloud'
    );
  } finally {
    restoreEnv(saved);
  }
});

test('isRetryableGitCloneFailure: 识别 TLS/EOF 网络中断', () => {
  const err = new Error(
    'git exit 128: error: RPC failed; curl 56 GnuTLS recv error (-9): Error decoding the received TLS packet.\n' +
      'fatal: early EOF\nfatal: fetch-pack: invalid index-pack output'
  );
  assert.strictEqual(isRetryableGitCloneFailure(err), true);
});

test('isRetryableGitCloneFailure: 不误判认证失败', () => {
  const err = new Error('git exit 128: fatal: Authentication failed for https://example.com/repo.git');
  assert.strictEqual(isRetryableGitCloneFailure(err), false);
});
