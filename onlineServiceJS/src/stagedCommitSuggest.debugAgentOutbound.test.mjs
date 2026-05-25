import test, { mock } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';

import { suggestStagedCommitMessage } from './stagedCommitSuggest.mjs';

const ENV_KEYS = [
  'DEBUG_AGENT',
  'ONLINE_PROJECT_STATE_ROOT',
  'TRAE_STAGED_COMMIT_LLM_BASE_URL',
  'TRAE_STAGED_COMMIT_LLM_API_KEY',
  'TRAE_STAGED_COMMIT_LLM_MODEL',
  'TRAE_STAGED_COMMIT_LLM_DISABLE',
];

function snapshotEnv() {
  const out = {};
  for (const k of ENV_KEYS) out[k] = process.env[k];
  return out;
}

function restoreEnv(saved) {
  for (const [k, v] of Object.entries(saved)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
}

function fakeGitExec(args) {
  const key = args.join(' ');
  if (key === 'diff --cached') {
    return Promise.resolve('diff --git a/a.txt b/a.txt\n+hello\n');
  }
  if (key === 'diff --cached --stat') {
    return Promise.resolve(' a.txt | 1 +\n 1 file changed, 1 insertion(+)');
  }
  if (key === 'diff --cached -z --name-only') {
    return Promise.resolve('a.txt\0');
  }
  return Promise.resolve('');
}

test('suggestStagedCommitMessage 在 DEBUG_AGENT=true 时记录外部 LLM 请求与响应完整字段', async () => {
  const saved = snapshotEnv();
  const stateRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'staged-commit-debug-'));
  const fetchMock = mock.method(globalThis, 'fetch', async () => {
    return new Response(
      JSON.stringify({
        choices: [{ message: { content: '修复提交说明生成链路' } }],
      }),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json', 'X-Trace-Resp': 'ok' },
      },
    );
  });
  try {
    process.env.DEBUG_AGENT = 'true';
    process.env.ONLINE_PROJECT_STATE_ROOT = stateRoot;
    process.env.TRAE_STAGED_COMMIT_LLM_BASE_URL = 'https://llm.example/v1';
    process.env.TRAE_STAGED_COMMIT_LLM_API_KEY = 'sk-test-key';
    process.env.TRAE_STAGED_COMMIT_LLM_MODEL = 'test-model';
    delete process.env.TRAE_STAGED_COMMIT_LLM_DISABLE;

    const msg = await suggestStagedCommitMessage(fakeGitExec, '/tmp/repo');
    assert.equal(msg, '修复提交说明生成链路');

    const logPath = path.join(stateRoot, 'reqLogs', 'outbound.log');
    const content = fs.readFileSync(logPath, 'utf8');
    assert.match(
      content,
      /DEBUG_AGENT outbound request method=POST url=https:\/\/llm\.example\/v1\/chat\/completions headers=\{"Content-Type":"application\/json","Authorization":"Bearer sk-test-key"\} body=\{.+\}/,
    );
    assert.match(
      content,
      /DEBUG_AGENT outbound response method=POST url=https:\/\/llm\.example\/v1\/chat\/completions status=200 headers=\{.+\} body=\{"choices":\[\{"message":\{"content":"修复提交说明生成链路"\}\}\]\}/,
    );
  } finally {
    fetchMock.mock.restore();
    restoreEnv(saved);
    fs.rmSync(stateRoot, { recursive: true, force: true });
  }
});

test('suggestStagedCommitMessage 在 DEBUG_AGENT=false 时不记录外部 LLM 调试字段', async () => {
  const saved = snapshotEnv();
  const stateRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'staged-commit-no-debug-'));
  const fetchMock = mock.method(globalThis, 'fetch', async () => {
    return new Response(
      JSON.stringify({
        choices: [{ message: { content: '更新提交信息生成提示词' } }],
      }),
      {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      },
    );
  });
  try {
    process.env.DEBUG_AGENT = 'false';
    process.env.ONLINE_PROJECT_STATE_ROOT = stateRoot;
    process.env.TRAE_STAGED_COMMIT_LLM_BASE_URL = 'https://llm.example/v1';
    process.env.TRAE_STAGED_COMMIT_LLM_API_KEY = 'sk-test-key';
    process.env.TRAE_STAGED_COMMIT_LLM_MODEL = 'test-model';
    delete process.env.TRAE_STAGED_COMMIT_LLM_DISABLE;

    await suggestStagedCommitMessage(fakeGitExec, '/tmp/repo');
    const logPath = path.join(stateRoot, 'reqLogs', 'outbound.log');
    const content = fs.readFileSync(logPath, 'utf8');
    assert.doesNotMatch(content, /DEBUG_AGENT outbound request method=POST/);
    assert.doesNotMatch(content, /DEBUG_AGENT outbound response method=POST/);
  } finally {
    fetchMock.mock.restore();
    restoreEnv(saved);
    fs.rmSync(stateRoot, { recursive: true, force: true });
  }
});
