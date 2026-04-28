// @ts-check
import { test } from 'node:test';
import assert from 'node:assert';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {
  layerGitWorkdirRootsForFileListing,
  listFlatRelativeFilesForLayer,
  resolveLayerGitLogContext,
} from './layerFs.mjs';

test('layerGitWorkdirRootsForFileListing: 并列多仓返回多个根且带前缀', () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'layer-fs-test-'));
  const layers = path.join(tmp, 'layers');
  const lid = '20260427_154500_deadbe';
  const layerDir = path.join(layers, lid);
  fs.mkdirSync(path.join(layerDir, 'goPractice', '.git'), { recursive: true });
  fs.mkdirSync(path.join(layerDir, 'zOther', '.git'), { recursive: true });

  const prev = process.env.ONLINE_PROJECT_LAYERS;
  process.env.ONLINE_PROJECT_LAYERS = layers;
  try {
    const roots = layerGitWorkdirRootsForFileListing(lid);
    assert.equal(roots.length, 2);
    assert.equal(roots[0].relPrefix, 'goPractice');
    assert.equal(roots[1].relPrefix, 'zOther');
  } finally {
    if (prev === undefined) delete process.env.ONLINE_PROJECT_LAYERS;
    else process.env.ONLINE_PROJECT_LAYERS = prev;
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test('listFlatRelativeFilesForLayer: 合并多仓相对路径', () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'layer-fs-test-'));
  const layers = path.join(tmp, 'layers');
  const lid = '20260427_154501_c0ffee';
  const layerDir = path.join(layers, lid);
  fs.mkdirSync(path.join(layerDir, 'repoA', '.git'), { recursive: true });
  fs.writeFileSync(path.join(layerDir, 'repoA', 'x.md'), 'x');
  fs.mkdirSync(path.join(layerDir, 'repoB', '.git'), { recursive: true });
  fs.writeFileSync(path.join(layerDir, 'repoB', 'y.md'), 'y');

  const prev = process.env.ONLINE_PROJECT_LAYERS;
  process.env.ONLINE_PROJECT_LAYERS = layers;
  try {
    const files = listFlatRelativeFilesForLayer(lid, 100);
    assert.ok(files.includes('repoA/x.md'));
    assert.ok(files.includes('repoB/y.md'));
  } finally {
    if (prev === undefined) delete process.env.ONLINE_PROJECT_LAYERS;
    else process.env.ONLINE_PROJECT_LAYERS = prev;
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test('resolveLayerGitLogContext: 多仓时按路径前缀选对应 workdir 与 pathspec', () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'layer-gitlog-ctx-'));
  const layers = path.join(tmp, 'layers');
  const lid = '20260427_160000_abcdef';
  const layerDir = path.join(layers, lid);
  fs.mkdirSync(path.join(layerDir, 'zFirst', '.git'), { recursive: true });
  fs.mkdirSync(path.join(layerDir, 'somanyad-emailD', '.git'), { recursive: true });
  const prev = process.env.ONLINE_PROJECT_LAYERS;
  process.env.ONLINE_PROJECT_LAYERS = layers;
  try {
    const a = resolveLayerGitLogContext(lid, 'somanyad-emailD');
    assert(a);
    assert.equal(a.pathspec, null);
    assert(a.work.endsWith(`${path.sep}somanyad-emailD`));
    const b = resolveLayerGitLogContext(lid, 'somanyad-emailD/src');
    assert(b);
    assert.equal(b.pathspec, 'src');
    assert(b.work.endsWith(`${path.sep}somanyad-emailD`));
    const c = resolveLayerGitLogContext(lid, 'somanyad-emailD/hello_world/Cargo.lock');
    assert(c);
    assert.equal(c.pathspec, 'hello_world/Cargo.lock');
    assert(c.work.endsWith(`${path.sep}somanyad-emailD`));
  } finally {
    if (prev === undefined) delete process.env.ONLINE_PROJECT_LAYERS;
    else process.env.ONLINE_PROJECT_LAYERS = prev;
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});
