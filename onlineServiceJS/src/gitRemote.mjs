/**
 * 将 HTTPS Git 远端转换为 SSH 形式（git@host:owner/repo.git）。
 * 解析失败时返回原始输入，避免误改。
 */
export function gitSshFromHttps(url) {
  try {
    const u = new URL(url);
    let host = u.hostname.toLowerCase();
    if (host === 'www.github.com') host = 'github.com';
    let pth = u.pathname.replace(/^\//, '').replace(/\.git$/i, '');
    if (!host || !pth || pth.includes('..')) return url;
    return `git@${host}:${pth}.git`;
  } catch {
    return url;
  }
}

/**
 * push 场景下优先把 GitHub 远端转成 SSH，避免 HTTPS 在无交互终端下弹凭据提示。
 * 非 GitHub 远端返回 `origin`，保持现有行为。
 */
export function gitPushRemoteArgFromOrigin(originUrl) {
  const raw = String(originUrl || '').trim();
  if (!raw) return 'origin';
  if (/^git@github\.com:/i.test(raw)) return raw;
  try {
    if (/^ssh:\/\//i.test(raw)) {
      const u = new URL(raw);
      const host = String(u.hostname || '').toLowerCase();
      let pth = String(u.pathname || '').replace(/^\/+/, '').replace(/\.git$/i, '');
      if ((host === 'github.com' || host === 'www.github.com') && pth && !pth.includes('..')) {
        return `git@github.com:${pth}.git`;
      }
      return 'origin';
    }
  } catch {
    return 'origin';
  }
  if (/^https?:\/\//i.test(raw)) {
    const ssh = gitSshFromHttps(raw);
    return /^git@github\.com:/i.test(ssh) ? ssh : 'origin';
  }
  return 'origin';
}
