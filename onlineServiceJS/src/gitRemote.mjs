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
 * 计算 push 的 remote 参数。
 * - 非 GitHub 远端返回 `origin`，保持现有行为。
 * - GitHub HTTPS 仅在明确要求时转为 SSH（例如已注入临时私钥），
 *   避免容器“仅有 HTTPS 凭据”时被强行切到 SSH 后失败。
 */
export function gitPushRemoteArgFromOrigin(originUrl, opts = {}) {
  const preferGithubSsh = opts && opts.preferGithubSsh === true;
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
    if (!preferGithubSsh) return 'origin';
    const ssh = gitSshFromHttps(raw);
    return /^git@github\.com:/i.test(ssh) ? ssh : 'origin';
  }
  return 'origin';
}
