/**
 * Git 可执行文件：默认 `git`。若 PATH 上存在不兼容的包装脚本，可设 `TRAE_GIT_PATH=/usr/bin/git` 等绝对路径。
 */
export function gitCmd() {
  const g = String(process.env.TRAE_GIT_PATH || '').trim();
  return g || 'git';
}

/**
 * 与 `buildGitCloneArgs` 共用的行首参数：避免 Docker / bind mount 下「dubious ownership」导致 git 拒绝在挂载目录内建库。
 * @see https://git-scm.com/docs/git-config#Documentation/git-config.txt-safedirectory
 */
export function gitCloneConfigArgs() {
  return ['-c', 'safe.directory=*'];
}

/** 单引号包裹，便于日志里复制含空格的路径（POSIX 风格） */
function shellSingleQuoted(s) {
  return `'${String(s).replace(/'/g, `'\\''`)}'`;
}

/**
 * 单行描述与 `spawn(gitCmd(), args, { cwd, env })` 等价的调试信息：cwd、节选环境变量、git 可执行文件与参数。
 * 勿将 token、私钥 PEM 等传入 `envForLog`。
 *
 * @param {string} cwd
 * @param {string[]} args git 子命令参数（不含 git 可执行文件路径）
 * @param {Record<string, string|undefined>|null} [envForLog] 仅记录需要对照复现的 env 键
 * @returns {string}
 */
export function formatGitExecDebugLine(cwd, args, envForLog = null) {
  const parts = [];
  if (cwd) parts.push(`cwd=${shellSingleQuoted(cwd)}`);
  if (envForLog && typeof envForLog === 'object') {
    for (const [k, v] of Object.entries(envForLog)) {
      if (v === undefined || v === null || String(v) === '') continue;
      parts.push(`${k}=${shellSingleQuoted(String(v))}`);
    }
  }
  const exe = gitCmd();
  const fullArgv = [exe, ...args.map((a) => String(a))];
  parts.push(fullArgv.map(shellSingleQuoted).join(' '));
  return parts.join(' ');
}
