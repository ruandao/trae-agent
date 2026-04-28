import fs from 'fs';
import path from 'path';
import { execFileSync } from 'child_process';
import {
  layerPrimaryGitWorkdir,
  readLayerMeta,
  resolvedParentLayerId,
  listLayerRows,
  layerGitWorkdirRootsForFileListing,
} from './layerFs.mjs';
import { gitCmd } from './gitCmd.mjs';

const MAX_DIFF_ENTRIES = 4000;
const MAX_FILE_BYTES_FULL_COMPARE = 25 * 1024 * 1024;

function normalizeRel(p) {
  return String(p || '')
    .replace(/\\/g, '/')
    .replace(/^\/+|\/+$/g, '');
}

/** 排除 .git 目录及路径任一段为 .git 的条目（含子模块 worktree 的 `.git` 文件） */
function isGitInternalPath(relPosix) {
  const p = normalizeRel(relPosix);
  if (!p) return false;
  if (p === '.git' || p.startsWith('.git/')) return true;
  if (p.includes('/.git/')) return true;
  if (p.endsWith('/.git')) return true;
  return false;
}

function safeJoin(root, relPosix) {
  const clean = normalizeRel(relPosix);
  const segs = clean ? clean.split('/').filter(Boolean) : [];
  const joined = path.resolve(path.join(root, ...segs));
  const rootR = path.resolve(root);
  if (joined !== rootR && !joined.startsWith(rootR + path.sep)) {
    throw new Error('path outside layer root');
  }
  return joined;
}

/** @returns {Map<string, { t: 'f'; size: number; mtime: number } | { t: 'l'; tg: string }>} */
function collectIndex(absRoot) {
  const map = new Map();
  let n = 0;
  let truncated = false;

  function walk(abs, rel) {
    if (n >= MAX_DIFF_ENTRIES) {
      truncated = true;
      return;
    }
    let st;
    try {
      st = fs.lstatSync(abs);
    } catch {
      return;
    }
    const relN = normalizeRel(rel);
    if (isGitInternalPath(relN)) return;
    if (st.isSymbolicLink()) {
      map.set(relN, { t: 'l', tg: fs.readlinkSync(abs) });
      n++;
      return;
    }
    if (st.isDirectory()) {
      let ents;
      try {
        ents = fs.readdirSync(abs, { withFileTypes: true });
      } catch {
        return;
      }
      for (const e of ents) {
        if (e.name === '.git') continue;
        walk(path.join(abs, e.name), relN ? `${relN}/${e.name}` : e.name);
        if (n >= MAX_DIFF_ENTRIES) {
          truncated = true;
          return;
        }
      }
      return;
    }
    if (st.isFile()) {
      map.set(relN, { t: 'f', size: st.size, mtime: st.mtimeMs });
      n++;
    }
  }

  walk(absRoot, '');
  return { map, truncated };
}

function filesContentEqual(fpA, fpB) {
  const sa = fs.statSync(fpA);
  const sb = fs.statSync(fpB);
  if (sa.size !== sb.size) return false;
  if (sa.size > MAX_FILE_BYTES_FULL_COMPARE) {
    return sa.mtimeMs === sb.mtimeMs;
  }
  const a = fs.readFileSync(fpA);
  const b = fs.readFileSync(fpB);
  return Buffer.compare(a, b) === 0;
}

function compareIndices(parentRoot, childRoot, idxP, idxC) {
  const changes = [];
  const allKeys = new Set([...idxP.keys(), ...idxC.keys()]);
  for (const p of [...allKeys].sort()) {
    const a = idxP.get(p);
    const b = idxC.get(p);
    let absP;
    let absC;
    try {
      absP = a ? safeJoin(parentRoot, p) : null;
      absC = b ? safeJoin(childRoot, p) : null;
    } catch {
      continue;
    }
    if (a && !b) {
      changes.push({ path: p, kind: 'removed' });
      continue;
    }
    if (!a && b) {
      changes.push({ path: p, kind: 'added' });
      continue;
    }
    if (!a || !b) continue;
    if (a.t !== b.t) {
      changes.push({ path: p, kind: 'modified' });
      continue;
    }
    if (a.t === 'l') {
      if (a.tg !== b.tg) changes.push({ path: p, kind: 'modified' });
      continue;
    }
    if (a.t === 'f') {
      if (a.size !== b.size) {
        changes.push({ path: p, kind: 'modified' });
        continue;
      }
      try {
        if (!filesContentEqual(absP, absC)) changes.push({ path: p, kind: 'modified' });
      } catch {
        changes.push({ path: p, kind: 'modified' });
      }
    }
  }
  return changes;
}

/** 与 {@link layerGitWorkdirRootsForFileListing} 中 relPrefix 对齐，在父层中找同一仓目录 */
function findParentWorkdirForChildPrefix(rootsP, relPrefix) {
  const key = relPrefix || '';
  const hit = rootsP.find((x) => (x.relPrefix || '') === key);
  if (hit) return hit.workdir;
  if (rootsP.length === 1 && !rootsP[0].relPrefix) return rootsP[0].workdir;
  return null;
}

/** @returns {{ staged: Set<string>, unstaged: Set<string> }} 路径为仓内 posix 相对路径，与 git name-only 一致 */
function gitStatusPathSets(workC) {
  const cwd = String(workC || '').trim();
  if (!cwd) return { staged: new Set(), unstaged: new Set() };
  const env = { ...process.env, GIT_TERMINAL_PROMPT: '0' };
  const runZ = (args) => {
    try {
      const out = execFileSync(gitCmd(), args, {
        cwd,
        encoding: 'utf8',
        env,
        maxBuffer: 32 * 1024 * 1024,
      });
      return String(out || '')
        .split('\0')
        .map((s) => normalizeRel(s))
        .filter(Boolean);
    } catch {
      return [];
    }
  };
  const staged = new Set(runZ(['diff', '--cached', '--name-only', '-z']));
  const unstagedTracked = new Set(runZ(['diff', '--name-only', '-z']));
  const untracked = new Set(runZ(['ls-files', '--others', '--exclude-standard', '-z']));
  const unstaged = new Set([...unstagedTracked, ...untracked]);
  return { staged, unstaged };
}

/**
 * 按子层各 git 仓库的暂存区 / 工作区状态标注变动（与 git status 两块区域对齐；同一文件可同时在两侧）。
 */
function enrichChangesWithGitStatus(parentId, lid, changes) {
  if (!changes.length) return changes;
  /** @type {Map<string, Array<{ i: number, inner: string }>>} */
  const byWorkdir = new Map();
  const fallbackIndices = [];

  for (let i = 0; i < changes.length; i++) {
    const resolved = resolvePairedWorkdirsForDiff(parentId, lid, changes[i].path);
    if (!resolved?.workC) {
      fallbackIndices.push(i);
      continue;
    }
    const innerN = normalizeRel(resolved.inner);
    const w = resolved.workC;
    if (!byWorkdir.has(w)) byWorkdir.set(w, []);
    byWorkdir.get(w).push({ i, inner: innerN });
  }

  const flagsByIndex = new Map();
  for (const i of fallbackIndices) {
    flagsByIndex.set(i, { git_staged: false, git_unstaged: true, git_layer_diff_only: false });
  }
  for (const [workC, items] of byWorkdir) {
    const { staged, unstaged } = gitStatusPathSets(workC);
    for (const { i, inner } of items) {
      const st = Boolean(inner && staged.has(inner));
      const un = Boolean(inner && unstaged.has(inner));
      if (st || un) {
        flagsByIndex.set(i, { git_staged: st, git_unstaged: un, git_layer_diff_only: false });
      } else {
        /** 仅相对父层目录对比有差异，Git 索引/工作区未列出（例如已与 HEAD 一致） */
        flagsByIndex.set(i, {
          git_staged: false,
          git_unstaged: false,
          git_layer_diff_only: true,
        });
      }
    }
  }

  return changes.map((ch, i) => {
    const f = flagsByIndex.get(i);
    if (f) return { ...ch, ...f };
    return { ...ch, git_staged: false, git_unstaged: true, git_layer_diff_only: false };
  });
}

function finalizeLayerParentDiffPayload(parentId, lid, payload) {
  const raw = Array.isArray(payload.changes) ? payload.changes : [];
  const filtered = raw.filter((ch) => !isGitInternalPath(ch.path || ''));
  const enriched = enrichChangesWithGitStatus(parentId, lid, filtered);
  return { ...payload, changes: enriched, same: enriched.length === 0 };
}

/**
 * 多仓时 rel 为「扁平路径」（含仓库目录前缀）；返回成对 workdir 与仓内相对路径，供 unified diff 与 getLayerParentDiffFiles 列表一致。
 * @returns {{ workP: string, workC: string, inner: string, label: string } | null}
 */
function resolvePairedWorkdirsForDiff(parentId, lid, rel) {
  const rootsC = layerGitWorkdirRootsForFileListing(lid);
  const rootsP = layerGitWorkdirRootsForFileListing(parentId);
  if (!rootsC.length) {
    const wP = layerPrimaryGitWorkdir(parentId);
    const wC = layerPrimaryGitWorkdir(lid);
    if (!wP || !wC) return null;
    return { workP: wP, workC: wC, inner: rel, label: rel };
  }
  for (const rC of rootsC) {
    const pre = (rC.relPrefix || '').trim();
    if (!pre) continue;
    if (rel === pre || rel.startsWith(`${pre}/`)) {
      const inner = rel === pre ? '' : rel.slice(pre.length + 1);
      const wP = findParentWorkdirForChildPrefix(rootsP, rC.relPrefix);
      if (rC.workdir && wP) {
        return { workP: wP, workC: rC.workdir, inner, label: rel };
      }
    }
  }
  if (rootsC.length === 1) {
    const wP = findParentWorkdirForChildPrefix(rootsP, rootsC[0].relPrefix) || layerPrimaryGitWorkdir(parentId);
    const wC = rootsC[0].workdir;
    if (wP && wC) return { workP: wP, workC: wC, inner: rel, label: rel };
  }
  const wP = layerPrimaryGitWorkdir(parentId);
  const wC = layerPrimaryGitWorkdir(lid);
  if (!wP || !wC) return null;
  return { workP: wP, workC: wC, inner: rel, label: rel };
}

/**
 * 将子层中每个 git 工作根与父层对应根成对对比，路径与「扁平文件列表 / files/*」一致（多仓带仓库目录前缀）。
 */
export function getLayerParentDiffFiles(layerId) {
  const lid = String(layerId || '').trim();
  const known = new Set(listLayerRows().map((r) => r.layer_id));
  if (!known.has(lid)) {
    return {
      layer_id: lid,
      parent_layer_id: null,
      same: false,
      changes: [],
      truncated: false,
      detail: '层不存在或不在可写层列表中。',
    };
  }

  const meta = readLayerMeta(lid);
  let parentId =
    meta?.parent_layer_id && known.has(meta.parent_layer_id) ? meta.parent_layer_id : null;
  if (!parentId) parentId = resolvedParentLayerId(lid, known, null);
  if (!parentId || !known.has(parentId)) {
    return {
      layer_id: lid,
      parent_layer_id: null,
      same: false,
      changes: [],
      truncated: false,
      detail:
        '当前层无可用父层对比：需要 layer_meta.json 中的 parent_layer_id，或工作区目录树可解析到相邻父层。',
    };
  }

  const rootsC = layerGitWorkdirRootsForFileListing(lid);
  const rootsP = layerGitWorkdirRootsForFileListing(parentId);
  if (!rootsC.length || !rootsP.length) {
    const workP0 = layerPrimaryGitWorkdir(parentId);
    const workC0 = layerPrimaryGitWorkdir(lid);
    if (!workP0 || !workC0) {
      return {
        layer_id: lid,
        parent_layer_id: parentId,
        same: false,
        changes: [],
        truncated: false,
        detail: '无法解析父层或当前层的 git 工作目录。',
      };
    }
    const cp = collectIndex(workP0);
    const cc = collectIndex(workC0);
    const truncated = cp.truncated || cc.truncated;
    const changes = compareIndices(workP0, workC0, cp.map, cc.map);
    return finalizeLayerParentDiffPayload(parentId, lid, {
      layer_id: lid,
      parent_layer_id: parentId,
      same: changes.length === 0,
      changes,
      truncated,
      detail: '',
    });
  }

  const allChanges = [];
  let anyTruncated = false;
  let comparedPairs = 0;
  for (const rC of rootsC) {
    const workC = rC.workdir;
    const workP = findParentWorkdirForChildPrefix(rootsP, rC.relPrefix);
    if (!workC || !workP) continue;
    comparedPairs += 1;
    const cp = collectIndex(workP);
    const cc = collectIndex(workC);
    anyTruncated = anyTruncated || cp.truncated || cc.truncated;
    const part = compareIndices(workP, workC, cp.map, cc.map);
    const pre = (rC.relPrefix || '').trim();
    for (const ch of part) {
      const rel = ch.path || '';
      const pathOut = pre && rel ? `${pre}/${rel}` : pre || rel;
      if (!pathOut) continue;
      allChanges.push({ path: pathOut, kind: ch.kind });
    }
  }

  if (comparedPairs === 0) {
    const workP0 = layerPrimaryGitWorkdir(parentId);
    const workC0 = layerPrimaryGitWorkdir(lid);
    if (!workP0 || !workC0) {
      return {
        layer_id: lid,
        parent_layer_id: parentId,
        same: false,
        changes: [],
        truncated: false,
        detail: '无法解析父层或当前层的 git 工作目录。',
      };
    }
    const cp = collectIndex(workP0);
    const cc = collectIndex(workC0);
    anyTruncated = anyTruncated || cp.truncated || cc.truncated;
    allChanges.push(...compareIndices(workP0, workC0, cp.map, cc.map));
  }

  allChanges.sort((a, b) => String(a.path).localeCompare(String(b.path)));
  const same = allChanges.length === 0;
  return finalizeLayerParentDiffPayload(parentId, lid, {
    layer_id: lid,
    parent_layer_id: parentId,
    same,
    changes: allChanges,
    truncated: anyTruncated,
    detail: '',
  });
}

function simpleUnifiedDiff(parentText, childText, relLabel) {
  const as = parentText.split('\n');
  const bs = childText.split('\n');
  let out = `--- ${relLabel} (parent)\n+++ ${relLabel} (child)\n`;
  const max = Math.max(as.length, bs.length);
  let any = false;
  for (let i = 0; i < max; i++) {
    const x = as[i];
    const y = bs[i];
    if (x === y) continue;
    any = true;
    if (x !== undefined) out += `-${x}\n`;
    if (y !== undefined) out += `+${y}\n`;
  }
  return any ? out : '';
}

export function getLayerParentUnifiedDiff(layerId, relPathRaw) {
  const rel = normalizeRel(relPathRaw);
  if (!rel) {
    return { ok: false, status: 400, body: { detail: 'query path required' } };
  }

  const lid = String(layerId || '').trim();
  const known = new Set(listLayerRows().map((r) => r.layer_id));
  const metaL = readLayerMeta(lid);
  let parentId =
    metaL?.parent_layer_id && known.has(metaL.parent_layer_id) ? metaL.parent_layer_id : null;
  if (!parentId) parentId = resolvedParentLayerId(lid, known, null);
  if (!parentId || !known.has(parentId)) {
    return { ok: false, status: 400, body: { detail: '无父层，无法 diff' } };
  }

  const resolved = resolvePairedWorkdirsForDiff(parentId, lid, rel);
  if (!resolved) {
    return { ok: false, status: 400, body: { detail: '工作目录不可用' } };
  }
  const { workP, workC, inner, label: relLabel } = resolved;
  const pathInRepo = (inner || '').trim();

  let absP;
  let absC;
  try {
    absP = pathInRepo ? safeJoin(workP, pathInRepo) : workP;
    absC = pathInRepo ? safeJoin(workC, pathInRepo) : workC;
  } catch (e) {
    return { ok: false, status: 400, body: { detail: String(e.message || e) } };
  }

  const exP = fs.existsSync(absP);
  const exC = fs.existsSync(absC);
  if (!exP && !exC) {
    return { ok: false, status: 404, body: { detail: 'path not found on parent or child' } };
  }

  try {
    const stC = exC ? fs.lstatSync(absC) : null;
    const stP = exP ? fs.lstatSync(absP) : null;

    if (stC?.isDirectory() || stP?.isDirectory()) {
      return { ok: true, body: { same: true, diff: '（目录条目，无逐行 unified diff）' } };
    }

    if (stC?.isSymbolicLink() || stP?.isSymbolicLink()) {
      const tC = stC?.isSymbolicLink() ? fs.readlinkSync(absC) : '';
      const tP = stP?.isSymbolicLink() ? fs.readlinkSync(absP) : '';
      const same = tC === tP;
      return {
        ok: true,
        body: {
          same,
          diff: same ? '' : `--- ${rel} (parent link)\n+++ ${rel} (child link)\n-${tP}\n+${tC}\n`,
        },
      };
    }

    if (exP && exC && stP.isFile() && stC.isFile()) {
      const bufP = fs.readFileSync(absP);
      const bufC = fs.readFileSync(absC);
      if (Buffer.compare(bufP, bufC) === 0) {
        return { ok: true, body: { same: true, diff: '' } };
      }

      const joined = Buffer.concat([bufP, bufC]);
      if (joined.includes(0)) {
        return {
          ok: true,
          body: { same: false, diff: '（二进制文件差异，略去逐字节展示）', truncated: false },
        };
      }

      const textP = bufP.toString('utf8');
      const textC = bufC.toString('utf8');
      const diff = simpleUnifiedDiff(textP, textC, rel);
      return { ok: true, body: { same: false, diff: diff || '（文本有差异）', truncated: false } };
    }

    if (exP && !exC && stP.isFile()) {
      const textP = fs.readFileSync(absP, 'utf8');
      const diff = simpleUnifiedDiff(textP, '', rel);
      return { ok: true, body: { same: false, diff: diff || '（父层有、子层无）', truncated: false } };
    }
    if (!exP && exC && stC.isFile()) {
      const textC = fs.readFileSync(absC, 'utf8');
      const diff = simpleUnifiedDiff('', textC, rel);
      return { ok: true, body: { same: false, diff: diff || '（子层新增文件）', truncated: false } };
    }

    return { ok: false, status: 400, body: { detail: '无法对该路径生成 diff' } };
  } catch (e) {
    return { ok: false, status: 400, body: { detail: String(e.message || e) } };
  }
}
