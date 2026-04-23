import fs from 'fs';
import path from 'path';
import {
  layerPrimaryGitWorkdir,
  readLayerMeta,
  resolvedParentLayerId,
  listLayerRows,
} from './layerFs.mjs';

const MAX_DIFF_ENTRIES = 4000;
const MAX_FILE_BYTES_FULL_COMPARE = 25 * 1024 * 1024;

function normalizeRel(p) {
  return String(p || '')
    .replace(/\\/g, '/')
    .replace(/^\/+|\/+$/g, '');
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

  const workP = layerPrimaryGitWorkdir(parentId);
  const workC = layerPrimaryGitWorkdir(lid);
  if (!workP || !workC) {
    return {
      layer_id: lid,
      parent_layer_id: parentId,
      same: false,
      changes: [],
      truncated: false,
      detail: '无法解析父层或当前层的 git 工作目录。',
    };
  }

  const cp = collectIndex(workP);
  const cc = collectIndex(workC);
  const truncated = cp.truncated || cc.truncated;
  const changes = compareIndices(workP, workC, cp.map, cc.map);
  const same = changes.length === 0;

  return {
    layer_id: lid,
    parent_layer_id: parentId,
    same,
    changes,
    truncated,
    detail: '',
  };
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

  const workP = layerPrimaryGitWorkdir(parentId);
  const workC = layerPrimaryGitWorkdir(lid);
  if (!workP || !workC) {
    return { ok: false, status: 400, body: { detail: '工作目录不可用' } };
  }

  let absP;
  let absC;
  try {
    absP = safeJoin(workP, rel);
    absC = safeJoin(workC, rel);
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
