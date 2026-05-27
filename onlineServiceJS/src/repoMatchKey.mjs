/**
 * 与任务详情前端 ``gitCloneRefMatchKey`` / Django ``repo_match_key_from_url`` 对齐。
 */
export function repoMatchKeyFromUrl(u) {
  const raw = String(u || '').trim();
  if (!raw) return '';
  if (/^git@/i.test(raw)) {
    const m = raw.match(/^git@([^:]+):(.+?)(?:\.git)?\/?$/i);
    if (m) {
      const host = String(m[1]).toLowerCase();
      let p = String(m[2] || '')
        .replace(/\\/g, '/')
        .replace(/\/+$/, '')
        .replace(/\.git$/i, '');
      return `${host}/${p}`.toLowerCase();
    }
  }
  try {
    const x = new URL(raw);
    let pth = (x.pathname || '/').replace(/\/+$/, '').replace(/\.git$/i, '');
    if (pth.startsWith('/')) pth = pth.slice(1);
    return `${x.host.toLowerCase()}/${pth}`.toLowerCase();
  } catch {
    return raw
      .toLowerCase()
      .replace(/\/+$/, '')
      .replace(/\.git$/i, '');
  }
}
