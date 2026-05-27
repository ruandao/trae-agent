// @ts-check
/**
 * 回归：task-detail 下发的 repo_clone_credentials 若与 git_repos 仅 host 不同（同 path），
 * bootstrap 克隆仍应命中凭据，不再出现：
 *   fatal: could not read Username for 'http://localhost:8012': terminal prompts disabled
 *
 * 运行：
 *   cd trae-agent/onlineServiceJS
 *   npx playwright test --project=api e2e/bootstrap-clone-host-alias-credential.api.spec.mjs
 */
import fs from 'fs';
import os from 'os';
import path from 'path';
import http from 'http';
import { spawn } from 'child_process';
import { fileURLToPath } from 'url';
import { test, expect } from '@playwright/test';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SERVICE_ROOT = path.resolve(__dirname, '..');
const ACCESS = 'e2e-bootstrap-access-token';

function pickFreeListenPort() {
  return new Promise((resolve, reject) => {
    const s = http.createServer();
    s.on('error', reject);
    s.listen(0, '127.0.0.1', () => {
      const addr = s.address();
      const p = typeof addr === 'object' && addr ? addr.port : 0;
      s.close(() => resolve(p));
    });
  });
}

function waitForHttpOk(url, headers, timeoutMs) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    const tick = () => {
      if (Date.now() - start > timeoutMs) {
        reject(new Error(`timeout waiting for ${url}`));
        return;
      }
      fetch(url, { headers })
        .then((r) => {
          if (r.ok) resolve();
          else setTimeout(tick, 120);
        })
        .catch(() => setTimeout(tick, 120));
    };
    tick();
  });
}

function waitFor(cond, timeoutMs, msg) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    const tick = () => {
      if (cond()) {
        resolve();
        return;
      }
      if (Date.now() - start > timeoutMs) {
        reject(new Error(msg));
        return;
      }
      setTimeout(tick, 120);
    };
    tick();
  });
}

/**
 * @returns {Promise<{ server: import('http').Server, port: number, receivedPaths: string[] }>}
 */
function startMockTaskCloud() {
  const receivedPaths = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const url = String(req.url || '');
      receivedPaths.push(url);
      res.setHeader('Content-Type', 'application/json; charset=utf-8');
      if (url.includes('/server-container-token/task-detail/')) {
        res.end(
          JSON.stringify({
            project_repos: [
              {
                project_id: 'p1',
                project_name: 'somanyad',
                git_repos: ['http://localhost:8012/demo/repo-a.git'],
              },
            ],
          }),
        );
        return;
      }
      if (url.includes('/server-container-token/repo-clone-credentials/')) {
        res.end(
          JSON.stringify({
            repo_clone_credentials: {
              'http://gitlab.aidevpm.com/demo/repo-a.git': {
                ephemeral_oauth_access_token: 'mock-gitlab-token-123',
                provider: 'gitlab',
                git_http_username: 'oauth2',
              },
            },
          }),
        );
        return;
      }
      if (url.includes('/server-container-token/feature-params-yaml/')) {
        res.end(JSON.stringify({ yaml: 'app:\n  name: e2e\n' }));
        return;
      }
      if (url.includes('/server-container-token/git-clone-progress/')) {
        res.end(JSON.stringify({ ok: true }));
        return;
      }
      if (url.includes('/server-container-token/heartbeat/')) {
        res.end(JSON.stringify({ ok: true }));
        return;
      }
      res.end('{}');
    });
  });
  return new Promise((resolve, reject) => {
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const addr = server.address();
      const port = typeof addr === 'object' && addr ? addr.port : 0;
      resolve({ server, port, receivedPaths });
    });
  });
}

test.describe.configure({ mode: 'serial', timeout: 120_000 });

test('bootstrap clone should use credential when host differs but path matches', async ({ request }) => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'osjs-bootstrap-alias-'));
  const fakeBin = path.join(tmp, 'bin');
  const gitLogPath = path.join(tmp, 'fake-git.log');
  fs.mkdirSync(fakeBin, { recursive: true });
  fs.writeFileSync(
    path.join(fakeBin, 'git'),
    `#!/usr/bin/env bash
set -e
echo "cmd:$*" >> "$FAKE_GIT_LOG"
remote=""
dest=""
for arg in "$@"; do
  case "$arg" in
    http://*|https://*) remote="$arg" ;;
  esac
done
for last; do dest="$last"; done
if [[ " $* " == *" clone "* ]]; then
  echo "clone_remote:$remote" >> "$FAKE_GIT_LOG"
  echo "git_http_username:\${GIT_HTTP_USERNAME:-}" >> "$FAKE_GIT_LOG"
  echo "git_http_password_len:\${#GIT_HTTP_PASSWORD}" >> "$FAKE_GIT_LOG"
  if [[ "$remote" == "http://localhost:8012/demo/repo-a.git" ]] && [[ -z "\${GIT_HTTP_PASSWORD:-}" ]]; then
    echo "fatal: could not read Username for 'http://localhost:8012': terminal prompts disabled" >&2
    exit 128
  fi
  mkdir -p "$dest/.git"
  exit 0
fi
exit 0
`,
    { mode: 0o755 },
  );

  const mock = await startMockTaskCloud();
  const listenPort = await pickFreeListenPort();
  const stdoutChunks = [];
  const stderrChunks = [];

  const childEnv = {
    ...process.env,
    NODEJS_WATCH: '0',
    TRAE_ONLINE_JS_DOCKER: '0',
    TRAE_USE_OVERLAY_STACK: '0',
    TRAE_SKIP_CONTAINER_TOKEN_EXCHANGE: '1',
    PORT: String(listenPort),
    ONLINE_PROJECT_STATE_ROOT: tmp,
    ONLINE_PROJECT_LAYERS: path.join(tmp, 'layers'),
    REPO_ROOT: tmp,
    ACCESS_TOKEN: ACCESS,
    BusinessApiEndPoint: `http://127.0.0.1:${listenPort}/api`,
    TaskApiEndPoint: `http://127.0.0.1:${mock.port}`,
    tenantId: '827923618468040704',
    workspaceId: '827923618602258432',
    taskId: '846269443533955072',
    NO_PROXY: '*',
    TASK_API_BOOTSTRAP_STRICT_STARTUP: '0',
    PATH: `${fakeBin}:${process.env.PATH || ''}`,
    FAKE_GIT_LOG: gitLogPath,
  };

  const proc = spawn(process.execPath, ['src/server.mjs'], {
    cwd: SERVICE_ROOT,
    env: childEnv,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  proc.stdout?.on('data', (c) => stdoutChunks.push(c.toString()));
  proc.stderr?.on('data', (c) => stderrChunks.push(c.toString()));

  try {
    await waitForHttpOk(
      `http://127.0.0.1:${listenPort}/api/requirements/task-gate`,
      { 'X-Access-Token': ACCESS },
      30_000,
    );

    await waitFor(
      () => stdoutChunks.join('').includes('任务引导完成（详情已拉取、克隆与配置已就绪）。'),
      35_000,
      'bootstrap did not complete successfully',
    );

    const gate = await request.get(`http://127.0.0.1:${listenPort}/api/requirements/task-gate`, {
      headers: { 'X-Access-Token': ACCESS },
    });
    expect(gate.ok()).toBeTruthy();

    const out = `${stdoutChunks.join('')}\n${stderrChunks.join('')}`;
    expect(out).not.toContain("could not read Username for 'http://localhost:8012'");
    expect(out).not.toContain('bootstrap (post-listen) error');

    const fakeLog = fs.readFileSync(gitLogPath, 'utf8');
    expect(fakeLog).toContain('clone_remote:http://localhost:8012/demo/repo-a.git');
    expect(fakeLog).toContain('git_http_username:oauth2');
    expect(fakeLog).toMatch(/git_http_password_len:[1-9]\d*/);

    expect(
      mock.receivedPaths.some((p) => p.includes('/server-container-token/task-detail/')),
      'mock task-detail endpoint should be called',
    ).toBe(true);
  } finally {
    proc.kill('SIGTERM');
    await new Promise((r) => setTimeout(r, 300));
    try {
      proc.kill('SIGKILL');
    } catch {
      /* ignore */
    }
    await new Promise((resolve) => mock.server.close(resolve));
    try {
      fs.rmSync(tmp, { recursive: true, force: true });
    } catch {
      /* ignore */
    }
  }
});
