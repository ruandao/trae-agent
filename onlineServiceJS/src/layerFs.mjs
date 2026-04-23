import fs from 'fs';
import path from 'path';
import crypto from 'crypto';
import { spawnSync } from 'node:child_process';
import { layersRoot } from './paths.mjs';

export const LAYER_ID_RE = /^(\d{8}_\d{6})_([0-9a-fA-F]+)$/;

const SKIP_NAMES = new Set(['__pycache__', '.DS_Store', '.git']);
const SKIP_RECURSIVE = new Set(['__pycache__', '.DS_Store']);

export function newLayerId() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  const ts = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}_${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
  const suf = crypto.randomBytes(3).toString('hex');
  return `${ts}_${suf}`;
}

export function layerPath(layerId) {
  return path.join(layersRoot(), layerId);
}

export function readLayerMeta(layerId) {
  const p = path.join(layerPath(layerId), 'layer_meta.json');
  if (!fs.existsSync(p)) return null;
  try {
    const raw = JSON.parse(fs.readFileSync(p, 'utf8'));
    const kind = raw.kind;
    if (!['clone', 'job', 'empty'].includes(kind)) return null;
    let parent = raw.parent_layer_id ? String(raw.parent_layer_id).trim() : null;
    if (parent && !LAYER_ID_RE.test(parent)) parent = null;
    let clone_url = null;
    if (raw.clone_url != null && String(raw.clone_url).trim()) {
      clone_url = String(raw.clone_url).trim();
    }
    return { version: Number(raw.version || 1), kind, parent_layer_id: parent, clone_url };
  } catch {
    return null;
  }
}

export function writeLayerMeta(layerId, kind, parentLayerId = null) {
  const root = layerPath(layerId);
  fs.mkdirSync(root, { recursive: true });
  const payload = { version: 1, kind, parent_layer_id: parentLayerId };
  fs.writeFileSync(path.join(root, 'layer_meta.json'), JSON.stringify(payload, null, 2), 'utf8');
}

export function createEmptyLayer(layerId) {
  const p = layerPath(layerId);
  if (fs.existsSync(p)) fs.rmSync(p, { recursive: true, force: true });
  fs.mkdirSync(p, { recursive: true });
  writeLayerMeta(layerId, 'empty', null);
  return p;
}

export function createRootLayer(layerId) {
  const p = layerPath(layerId);
  fs.mkdirSync(p, { recursive: true });
  return p;
}

function dirHasGit(p) {
  const g = path.join(p, '.git');
  try {
    return fs.existsSync(g);
  } catch {
    return false;
  }
}

/** 层内用于 git / 任务的工作目录（与 Python layer_fs 语义接近） */
export function layerPrimaryGitWorkdir(layerId) {
  const root = layerPath(layerId);
  if (!fs.existsSync(root)) return null;
  if (dirHasGit(root)) return root;
  const base = path.join(root, 'base');
  if (fs.existsSync(base) && dirHasGit(base)) return base;
  try {
    const subs = fs.readdirSync(root, { withFileTypes: true });
    for (const ent of subs) {
      if (!ent.isDirectory() || SKIP_NAMES.has(ent.name)) continue;
      const c = path.join(root, ent.name);
      if (dirHasGit(c)) return c;
    }
  } catch {
    /* ignore */
  }
  return root;
}

/**
 * true：存在未提交/未暂存变更；false：工作区干净；null：非 git 或检测失败（与 Python layer_git.git_worktree_dirty 对齐）。
 */
export function gitWorktreeDirty(layerId) {
  if (!layerId || !LAYER_ID_RE.test(String(layerId))) return null;
  const work = layerPrimaryGitWorkdir(layerId);
  if (!work) return null;
  try {
    const r = spawnSync('git', ['status', '--porcelain'], {
      cwd: work,
      encoding: 'utf8',
      maxBuffer: 10 * 1024 * 1024,
      env: { ...process.env, GIT_TERMINAL_PROMPT: '0' },
      timeout: 60_000,
    });
    if (r.error || r.status !== 0) return null;
    return (r.stdout || '').trim().length > 0;
  } catch {
    return null;
  }
}

export function layerRootOrChildHasGit(layerDir) {
  try {
    for (const base of [layerDir, path.join(layerDir, 'base')]) {
      if (!fs.existsSync(base) || !fs.statSync(base).isDirectory()) continue;
      if (dirHasGit(base)) return true;
      const subs = fs.readdirSync(base, { withFileTypes: true });
      for (const ent of subs) {
        if (!ent.isDirectory() || SKIP_NAMES.has(ent.name)) continue;
        if (dirHasGit(path.join(base, ent.name))) return true;
      }
    }
  } catch {
    return false;
  }
  return false;
}

export function listLayerRows() {
  const root = layersRoot();
  if (!fs.existsSync(root)) return [];
  const out = [];
  for (const name of fs.readdirSync(root)) {
    if (!LAYER_ID_RE.test(name)) continue;
    const p = path.join(root, name);
    if (!fs.statSync(p).isDirectory()) continue;
    const m = name.match(LAYER_ID_RE);
    let createdAt = null;
    if (m) {
      const ts = m[1].replace('_', '');
      if (ts.length === 14) {
        const y = ts.slice(0, 4);
        const mo = ts.slice(4, 6);
        const da = ts.slice(6, 8);
        const h = ts.slice(8, 10);
        const mi = ts.slice(10, 12);
        const s = ts.slice(12, 14);
        createdAt = `${y}-${mo}-${da}T${h}:${mi}:${s}`;
      }
    }
    out.push({ layer_id: name, created_at: createdAt });
  }
  out.sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
  return out;
}

export function anyLayerHasGitRepo() {
  for (const row of listLayerRows()) {
    if (layerRootOrChildHasGit(layerPath(row.layer_id))) return true;
  }
  return false;
}

function copyEntry(src, dest) {
  const st = fs.lstatSync(src);
  if (st.isDirectory()) {
    fs.mkdirSync(dest, { recursive: true });
    for (const ent of fs.readdirSync(src, { withFileTypes: true })) {
      if (SKIP_RECURSIVE.has(ent.name)) continue;
      copyEntry(path.join(src, ent.name), path.join(dest, ent.name));
    }
  } else if (st.isSymbolicLink()) {
    fs.symlinkSync(fs.readlinkSync(src), dest);
  } else {
    fs.copyFileSync(src, dest);
  }
}

export function createStackedLayer(childId, parentLayerId) {
  const parentDir = layerPath(parentLayerId);
  const childDir = layerPath(childId);
  if (!fs.existsSync(parentDir)) throw new Error(`parent layer not found: ${parentLayerId}`);
  if (fs.existsSync(childDir)) fs.rmSync(childDir, { recursive: true, force: true });
  fs.mkdirSync(childDir, { recursive: true });
  for (const ent of fs.readdirSync(parentDir, { withFileTypes: true })) {
    if (ent.name === '.git' || SKIP_NAMES.has(ent.name)) continue;
    copyEntry(path.join(parentDir, ent.name), path.join(childDir, ent.name));
  }
  const pg = path.join(parentDir, '.git');
  const cg = path.join(childDir, '.git');
  if (fs.existsSync(pg)) {
    try {
      if (fs.existsSync(cg)) fs.rmSync(cg, { recursive: true, force: true });
      const rel = path.join('..', parentLayerId, '.git');
      fs.symlinkSync(rel, cg, 'dir');
    } catch {
      /* ignore symlink failure on Windows rare */
    }
  }
  writeLayerMeta(childId, 'job', parentLayerId);
  return childDir;
}

export function directChildLayerIds(baseLayerId) {
  const out = [];
  for (const row of listLayerRows()) {
    if (row.layer_id === baseLayerId) continue;
    const m = readLayerMeta(row.layer_id);
    if (m && m.parent_layer_id === baseLayerId) out.push(row.layer_id);
  }
  return out;
}

export function deleteLayerTree(layerId) {
  const p = layerPath(layerId);
  if (fs.existsSync(p)) fs.rmSync(p, { recursive: true, force: true });
}

export function repoDirNameFromUrl(url) {
  try {
    const u = new URL(url);
    let base = u.pathname.split('/').filter(Boolean).pop() || 'repo';
    if (base.endsWith('.git')) base = base.slice(0, -4);
    return base.replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^[-._]+|[-._]+$/g, '') || 'repo';
  } catch {
    return 'repo';
  }
}

export function resolvedParentLayerId(layerId, knownIds, jobs) {
  const meta = readLayerMeta(layerId);
  if (meta?.parent_layer_id && knownIds.has(meta.parent_layer_id)) return meta.parent_layer_id;
  const work = layerPrimaryGitWorkdir(layerId);
  if (!work) return null;
  let cur = path.dirname(work);
  const root = layersRoot();
  while (cur && cur.startsWith(root)) {
    const name = path.basename(cur);
    if (knownIds.has(name) && name !== layerId) return name;
    const parentDir = path.dirname(cur);
    if (parentDir === cur) break;
    cur = parentDir;
  }
  return null;
}
