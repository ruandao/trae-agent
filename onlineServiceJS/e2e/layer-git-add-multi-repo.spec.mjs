// @ts-check
/**
 * 核验：多并列仓时 ``POST /api/layers/:id/git/add`` 应在正确仓库目录执行 git add
 *（与 ``resolveLayerGitLogContext`` / 扁平路径前缀一致），而非仅用 ``layerPrimaryGitWorkdir`` 主仓。
 *
 * 运行（在 onlineServiceJS 目录）：
 *   npx playwright test e2e/layer-git-add-multi-repo.spec.mjs --project=api
 */
import { test, expect } from '@playwright/test';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import net from 'node:net';
import { fileURLToPath } from 'node:url';

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.listen(0, '127.0.0.1', () => {
      const addr = s.address();
      const p = typeof addr === 'object' && addr ? addr.port : 0;
      s.close(() => resolve(p));
    });
    s.on('error', reject);
  });
}

function initRepoWithFile(repoDir, relFile, content) {
  const abs = path.join(repoDir, relFile);
  fs.mkdirSync(path.dirname(abs), { recursive: true });
  fs.writeFileSync(abs, content, 'utf8');
  const r0 = spawnSync('git', ['-c', 'init.defaultBranch=main', 'init'], { cwd: repoDir, encoding: 'utf8' });
  if (r0.status !== 0) throw new Error(r0.stderr || r0.stdout);
  spawnSync('git', ['config', 'user.email', 'e2e@test.local'], { cwd: repoDir });
  spawnSync('git', ['config', 'user.name', 'e2e'], { cwd: repoDir });
  spawnSync('git', ['add', '.'], { cwd: repoDir });
  const r1 = spawnSync('git', ['commit', '-m', 'init'], { cwd: repoDir, encoding: 'utf8' });
  if (r1.status !== 0) throw new Error(r1.stderr || r1.stdout);
}

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const serviceDir = path.resolve(__dirname, '..');

test.describe('POST /api/layers/:layer_id/git/add 多仓并列', () => {
  test('扁平路径应解析到非主仓时 git add 成功', async ({ request }) => {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'osj-gadd-'));
    const stateRoot = path.join(tmp, 'oproj_state');
    const layersRootDir = path.join(stateRoot, 'layers');
    const lid = '20260427_180000_9f8e7d';
    const layerDir = path.join(layersRootDir, lid);
    // 主仓为字典序首（goPractice）；另一仓为 somanyad-emailD（与现场路径一致）
    const goP = path.join(layerDir, 'goPractice');
    const sEmail = path.join(layerDir, 'somanyad-emailD');
    initRepoWithFile(goP, 'a.txt', 'a');
    initRepoWithFile(sEmail, 'x.txt', 'x');
    // 非主仓内未跟踪文件，模拟 Cargo.lock
    const cargoRel = 'hello_world/Cargo.lock';
    fs.mkdirSync(path.join(sEmail, 'hello_world'), { recursive: true });
    fs.writeFileSync(path.join(sEmail, cargoRel), '[placeholder]\n', 'utf8');

    const port = await freePort();
    const token = 'pw-git-add-multi-token';
    const proc = spawn(process.execPath, ['src/server.mjs'], {
      cwd: serviceDir,
      env: {
        ...process.env,
        PORT: String(port),
        ONLINE_PROJECT_STATE_ROOT: stateRoot,
        ACCESS_TOKEN: token,
        CODE_SERVER_ENABLED: '0',
        TRAE_STAGED_COMMIT_LLM_DISABLE: '1',
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    const errBuf = [];
    proc.stderr?.on('data', (c) => errBuf.push(String(c)));

    await new Promise((resolve, reject) => {
      const t = setTimeout(() => {
        reject(new Error(`server start timeout. stderr: ${errBuf.join('').slice(-2000)}`));
      }, 45000);
      const onData = (c) => {
        if (String(c).includes('server listening')) {
          clearTimeout(t);
          proc.stdout?.off('data', onData);
          resolve(undefined);
        }
      };
      proc.stdout?.on('data', onData);
      proc.on('error', (e) => {
        clearTimeout(t);
        reject(e);
      });
    });

    const flat = `somanyad-emailD/${cargoRel}`;

    try {
      const res = await request.post(
        `http://127.0.0.1:${port}/api/layers/${encodeURIComponent(lid)}/git/add`,
        {
          data: { path: flat },
          headers: { 'X-Access-Token': token, 'Content-Type': 'application/json' },
        },
      );
      expect(res.ok(), await res.text()).toBeTruthy();
      const body = await res.json();
      expect(body.ok).toBe(true);
      expect(typeof body.suggested_commit_message).toBe('string');
      expect(body.suggested_commit_message.length).toBeGreaterThan(0);

      const st = spawnSync('git', ['diff', '--cached', '--name-only', '--', cargoRel], {
        cwd: sEmail,
        encoding: 'utf8',
      });
      expect(st.status).toBe(0);
      expect((st.stdout || '').trim()).toBe(cargoRel);

      const un = await request.post(
        `http://127.0.0.1:${port}/api/layers/${encodeURIComponent(lid)}/git/unstage`,
        {
          data: { path: flat },
          headers: { 'X-Access-Token': token, 'Content-Type': 'application/json' },
        },
      );
      expect(un.ok(), await un.text()).toBeTruthy();
      expect((await un.json()).ok).toBe(true);
      const st2 = spawnSync('git', ['diff', '--cached', '--name-only', '--', cargoRel], {
        cwd: sEmail,
        encoding: 'utf8',
      });
      expect(st2.status).toBe(0);
      expect((st2.stdout || '').trim()).toBe('');
    } finally {
      proc.kill('SIGTERM');
      await new Promise((r) => setTimeout(r, 400));
      fs.rmSync(tmp, { recursive: true, force: true });
    }
  });
});
