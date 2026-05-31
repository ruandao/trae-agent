// @ts-check
/**
 * 回归：jobsRuntime 使用 bootstrapCloneLayerId 时必须已 import，避免 ReferenceError → 502。
 */
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';
import test from 'node:test';
import { spawnSync } from 'child_process';

const BASE_LAYER_ID = '20260531_120000_a1b2c3';
const CHILD_LAYER_ID = '20260531_120100_c4d5e6';

function seedLayer(layersDir, layerId, parentLayerId = null) {
  const dir = path.join(layersDir, layerId);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(
    path.join(dir, 'layer_meta.json'),
    JSON.stringify({ version: 1, kind: 'job', parent_layer_id: parentLayerId }, null, 2),
    'utf8',
  );
  const gitInit = spawnSync('git', ['init'], { cwd: dir, encoding: 'utf8' });
  assert.equal(gitInit.status, 0, gitInit.stderr || 'git init failed');
}

test('jobsRuntime bootstrapCloneLayerId binding (mirror + delete)', async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'osjs-bind-'));
  const layersDir = path.join(tmp, 'layers');
  fs.mkdirSync(layersDir, { recursive: true });
  const backup = {
    ONLINE_PROJECT_STATE_ROOT: process.env.ONLINE_PROJECT_STATE_ROOT,
    ONLINE_PROJECT_LAYERS: process.env.ONLINE_PROJECT_LAYERS,
    REPO_ROOT: process.env.REPO_ROOT,
    TaskApiEndPoint: process.env.TaskApiEndPoint,
    ACCESS_TOKEN: process.env.ACCESS_TOKEN,
  };
  process.env.ONLINE_PROJECT_STATE_ROOT = tmp;
  process.env.ONLINE_PROJECT_LAYERS = layersDir;
  process.env.REPO_ROOT = tmp;
  delete process.env.TaskApiEndPoint;
  delete process.env.ACCESS_TOKEN;

  try {
    const { buildLayersSnapshot, mirrorLayerGraphToTaskCloudSSE, deleteLayerAndMirrorToSaas } =
      await import('./jobsRuntime.mjs');
    const { bootstrapCloneLayerId } = await import('./bootstrap.mjs');

    const snap = buildLayersSnapshot(bootstrapCloneLayerId);
    assert.ok(Array.isArray(snap.layers));
    assert.ok(Array.isArray(snap.jobs));

    await mirrorLayerGraphToTaskCloudSSE();

    seedLayer(layersDir, BASE_LAYER_ID, null);
    seedLayer(layersDir, CHILD_LAYER_ID, BASE_LAYER_ID);
    await deleteLayerAndMirrorToSaas(CHILD_LAYER_ID);
    assert.equal(fs.existsSync(path.join(layersDir, CHILD_LAYER_ID)), false);
    assert.equal(fs.existsSync(path.join(layersDir, BASE_LAYER_ID)), true);
  } finally {
    for (const [k, v] of Object.entries(backup)) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
    try {
      fs.rmSync(tmp, { recursive: true, force: true });
    } catch {
      /* ignore */
    }
  }
});
