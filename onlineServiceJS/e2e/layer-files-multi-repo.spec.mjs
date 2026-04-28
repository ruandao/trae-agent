// @ts-check
/**
 * 核验：同一层下并列多个 git 克隆目录时，GET /api/layers/:id/files 应列出全部仓库内文件（带仓库名前缀）。
 * 依赖：本机 Node；会临时 spawn onlineServiceJS（独立 PORT）。
 *
 * 运行（在 onlineServiceJS 目录）：
 *   npx playwright test e2e/layer-files-multi-repo.spec.mjs --project=api
 */
import { test, expect } from '@playwright/test';
import { spawn } from 'node:child_process';
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

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const serviceDir = path.resolve(__dirname, '..');

test.describe('GET /api/layers/:layer_id/files 多仓并列', () => {
  test('返回全部并列克隆仓的相对路径', async ({ request }) => {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'osj-lfiles-'));
    const stateRoot = path.join(tmp, 'oproj_state');
    const layersRootDir = path.join(stateRoot, 'layers');
    const lid = '20260427_153000_a1b2c3';
    fs.mkdirSync(path.join(layersRootDir, lid, 'goPractice', '.git'), { recursive: true });
    fs.writeFileSync(path.join(layersRootDir, lid, 'goPractice', 'a.txt'), '1');
    fs.mkdirSync(path.join(layersRootDir, lid, 'otherRepo', '.git'), { recursive: true });
    fs.mkdirSync(path.join(layersRootDir, lid, 'otherRepo', 'sub'), { recursive: true });
    fs.writeFileSync(path.join(layersRootDir, lid, 'otherRepo', 'sub', 'b.txt'), '2');

    const port = await freePort();
    const token = 'pw-multi-repo-files-token';
    const proc = spawn(process.execPath, ['src/server.mjs'], {
      cwd: serviceDir,
      env: {
        ...process.env,
        PORT: String(port),
        ONLINE_PROJECT_STATE_ROOT: stateRoot,
        ACCESS_TOKEN: token,
        CODE_SERVER_ENABLED: '0',
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

    try {
      const res = await request.get(
        `http://127.0.0.1:${port}/api/layers/${encodeURIComponent(lid)}/files`,
        { headers: { 'X-Access-Token': token } },
      );
      expect(res.ok(), await res.text()).toBeTruthy();
      const j = await res.json();
      const files = Array.isArray(j.files) ? j.files : [];
      expect(files).toContain('goPractice/a.txt');
      expect(files).toContain('otherRepo/sub/b.txt');

      const f1 = await request.get(
        `http://127.0.0.1:${port}/api/layers/${encodeURIComponent(lid)}/files/goPractice/a.txt`,
        { headers: { 'X-Access-Token': token } },
      );
      expect(f1.ok(), await f1.text()).toBeTruthy();
      const j1 = await f1.json();
      expect(j1.content).toBe('1');
      expect(j1.path).toBe('goPractice/a.txt');

      const f2 = await request.get(
        `http://127.0.0.1:${port}/api/layers/${encodeURIComponent(lid)}/files/otherRepo/sub/b.txt`,
        { headers: { 'X-Access-Token': token } },
      );
      expect(f2.ok(), await f2.text()).toBeTruthy();
      expect((await f2.json()).content).toBe('2');
    } finally {
      proc.kill('SIGTERM');
      await new Promise((r) => setTimeout(r, 400));
      fs.rmSync(tmp, { recursive: true, force: true });
    }
  });
});
