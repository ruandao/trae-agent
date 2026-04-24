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
