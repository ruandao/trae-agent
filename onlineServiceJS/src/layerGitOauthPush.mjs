/**
 * 接口 A：接收 GitHub OAuth 每仓库 access_token 映射，对层级内各 Git 工作区执行 HTTPS push，
 * 并在给定 base 分支上尝试创建 Pull Request（GitHub REST API）。
 */
import fs from 'fs';
import path from 'path';
import os from 'os';
import { spawn, spawnSync } from 'child_process';
import { gitCmd, formatGitExecDebugLine } from './gitCmd.mjs';
import { layerGitWorkdirRootsForFileListing } from './layerFs.mjs';
import { appendOutboundReqLog, appendGitPushReqLog, sanitizeUrlForOutboundLog } from './outboundReqLog.mjs';

const GH_SLUG_RE = /github\.com[:/]([\w.-]+)\/([\w.-]+?)(?:\.git)?\/?$/i;

export function parseGithubOwnerRepoFromRemoteUrl(url) {
  const m = String(url || '').match(GH_SLUG_RE);
  if (!m) return null;
  const owner = String(m[1]).trim();
  let repo = String(m[2]).trim();
  if (repo.toLowerCase().endsWith('.git')) repo = repo.slice(0, -4);
  if (!owner || !repo) return null;
  return { owner, repo, slug: `${owner}/${repo}` };
}

function gitConfigGetRemoteOrigin(workdir) {
  try {
    const out = spawnSync(gitCmd(), ['config', '--get', 'remote.origin.url'], {
      cwd: workdir,
      encoding: 'utf8',
      env: { ...process.env, GIT_TERMINAL_PROMPT: '0' },
      maxBuffer: 1024 * 1024,
    });
    if (out.status !== 0) return '';
    return String(out.stdout || '')
      .trim()
      .split('\n')[0] || '';
  } catch {
    return '';
  }
}

function githubHttpsRemoteFromSlug(owner, repo) {
  return `https://github.com/${owner}/${repo}.git`;
}

function writeAskpassBundle(accessToken) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ghoauth_'));
  const tokenPath = path.join(dir, 'token');
  fs.writeFileSync(tokenPath, accessToken, { mode: 0o600 });
  const shPath = path.join(dir, 'askpass.sh');
  const tp = tokenPath.replace(/'/g, "'\\''");
  fs.writeFileSync(
    shPath,
    `#!/bin/sh
T=$(cat '${tp}')
case "$1" in
  *Username*) printf '%s\n' 'x-access-token' ;;
  *Password*) printf '%s\n' "$T" ;;
esac
`,
    { mode: 0o700 },
  );
  return {
    shPath,
    cleanup() {
      try {
        fs.rmSync(dir, { recursive: true, force: true });
      } catch {
        /* ignore */
      }
    },
  };
}

async function gitExecAsync(args, cwd, env = {}) {
  return new Promise((resolve, reject) => {
    const proc = spawn(gitCmd(), args, {
      cwd,
      env: { ...process.env, ...env, GIT_TERMINAL_PROMPT: '0' },
    });
    let out = '';
    let err = '';
    proc.stdout?.on('data', (c) => {
      out += c.toString();
    });
    proc.stderr?.on('data', (c) => {
      err += c.toString();
    });
    proc.on('error', reject);
    proc.on('close', (code) => {
      if (code === 0) resolve(out + err);
      else reject(new Error((err || out || `git exit ${code}`).slice(-4000)));
    });
  });
}

function normalizeBranchRef(name) {
  const b = String(name || '').trim();
  if (!b) return '';
  return b.startsWith('refs/') ? b : `refs/heads/${b}`;
}

function branchNameFromRef(ref) {
  const r = String(ref || '').trim();
  if (!r) return '';
  if (r.startsWith('refs/heads/')) return r.slice('refs/heads/'.length);
  return r;
}

async function createGithubPullRequest({ owner, repo, head, base, accessToken, title, bodyText }) {
  const apiUrl = `https://api.github.com/repos/${owner}/${repo}/pulls`;
  const safeUrl = sanitizeUrlForOutboundLog(apiUrl);
  const t0 = Date.now();
  let r;
  try {
    r = await fetch(apiUrl, {
      method: 'POST',
      headers: {
        Accept: 'application/vnd.github+json',
        Authorization: `Bearer ${accessToken}`,
        'X-GitHub-Api-Version': '2022-11-28',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        title: title || 'Pull request',
        head,
        base,
        body: bodyText || '',
      }),
    });
  } catch (e) {
    appendOutboundReqLog(
      `github-api POST ${safeUrl} -> error ${String(e?.message || e).slice(0, 400)} ${Date.now() - t0}ms`,
    );
    throw e;
  }
  const text = await r.text();
  appendOutboundReqLog(`github-api POST ${safeUrl} -> HTTP ${r.status} ${Date.now() - t0}ms`);
  let json = null;
  try {
    json = JSON.parse(text);
  } catch {
    /* ignore */
  }
  return {
    ok: r.status === 201,
    status: r.status,
    json,
    text: text.slice(0, 2000),
  };
}

/**
 * @param {object} opts
 * @param {string} opts.layerId
 * @param {string} opts.targetBranch - 推送到远端的分支名（与现有 /git/push 一致）
 * @param {Record<string,string>} [opts.accessTokenByRepoSlug] - 每仓库 token，键为 owner/repo（小写）
 * @param {string} [opts.prBaseBranch] - 若提供则对每个 GitHub 仓尝试创建 PR：base ← 该值，head ← targetBranch
 * @param {string} [opts.prTitle]
 * @param {string} [opts.prBody]
 */
export async function runLayerGithubOauthAccessPush(opts) {
  const layerId = String(opts.layerId || '').trim();
  const targetBranch = String(opts.targetBranch || '').trim();
  const rawAccessTokenByRepoSlug =
    opts && typeof opts.accessTokenByRepoSlug === 'object' && opts.accessTokenByRepoSlug
      ? opts.accessTokenByRepoSlug
      : null;
  const accessTokenByRepoSlug = {};
  if (rawAccessTokenByRepoSlug) {
    for (const [rawSlug, rawToken] of Object.entries(rawAccessTokenByRepoSlug)) {
      const slug = String(rawSlug || '').trim().toLowerCase();
      const tok = String(rawToken || '').trim();
      if (!slug || !tok) continue;
      accessTokenByRepoSlug[slug] = tok;
    }
  }
  const prBaseBranch = String(opts.prBaseBranch || '').trim();
  const prTitle = String(opts.prTitle || '').trim() || 'Pull request';
  const prBody = String(opts.prBody || '').trim();

  if (!layerId) {
    return { httpStatus: 400, payload: { ok: false, detail: 'layer_id 无效' } };
  }
  if (!targetBranch) {
    return { httpStatus: 400, payload: { ok: false, detail: 'target_branch 必填' } };
  }
  if (!Object.keys(accessTokenByRepoSlug).length) {
    return { httpStatus: 400, payload: { ok: false, detail: 'github_auth_by_repo 必填' } };
  }

  const roots = layerGitWorkdirRootsForFileListing(layerId);
  const gitRoots = roots.filter((row) => {
    try {
      return fs.existsSync(path.join(row.workdir, '.git'));
    } catch {
      return false;
    }
  });
  if (!gitRoots.length) {
    appendGitPushReqLog(`oauth layer_id=${layerId} fail reason=no_git`);
    return { httpStatus: 400, payload: { ok: false, detail: 'no git' } };
  }

  const dstRef = normalizeBranchRef(targetBranch);
  const headName = branchNameFromRef(dstRef);
  const baseName = prBaseBranch ? branchNameFromRef(normalizeBranchRef(prBaseBranch)) : '';

  appendGitPushReqLog(`oauth layer_id=${layerId} begin repos=${gitRoots.length} dst=${dstRef}`);

  /** @type {object[]} */
  const repos = [];

  const askpassByToken = new Map();
  const askpassForToken = (token) => {
    const key = String(token || '');
    const hit = askpassByToken.get(key);
    if (hit) return hit;
    const bundle = writeAskpassBundle(key);
    askpassByToken.set(key, bundle);
    return bundle;
  };

  try {
    for (const row of gitRoots) {
      const originUrl = gitConfigGetRemoteOrigin(row.workdir);
      const slugInfo = parseGithubOwnerRepoFromRemoteUrl(originUrl);
      const item = {
        rel_prefix: row.relPrefix || '',
        origin_url: originUrl ? 'set' : '',
        push_ok: false,
        pr: null,
        pr_error: null,
      };
      if (!slugInfo) {
        item.detail = 'remote 非 github.com，已跳过 OAuth 推送';
        appendGitPushReqLog(
          `oauth layer_id=${layerId} rel_prefix=${String(row.relPrefix || '').slice(0, 160)} skip=non_github_remote`,
        );
        repos.push(item);
        continue;
      }
      const httpsRemote = githubHttpsRemoteFromSlug(slugInfo.owner, slugInfo.repo);
      item.github_slug = slugInfo.slug;
      const repoToken = accessTokenByRepoSlug[String(slugInfo.slug || '').toLowerCase()];
      if (!repoToken) {
        item.detail = '该仓库未找到可用的 GitHub OAuth access_token';
        appendGitPushReqLog(
          `oauth layer_id=${layerId} slug=${slugInfo.slug} rel_prefix=${String(row.relPrefix || '').slice(0, 160)} token=missing`,
        );
        repos.push(item);
        return {
          httpStatus: 400,
          payload: {
            ok: false,
            detail: `推送失败（${slugInfo.slug}）：${item.detail}`,
            github_oauth_multirepo: { repos },
          },
        };
      }
      const ask = askpassForToken(repoToken);
      const pushEnv = {
        ...process.env,
        GIT_TERMINAL_PROMPT: '0',
        GIT_ASKPASS: ask.shPath,
        GIT_ASKPASS_ALWAYS: '1',
      };
      const pushArgs = ['push', httpsRemote, `HEAD:${dstRef}`];
      const cmdLine = formatGitExecDebugLine(row.workdir, pushArgs, {
        GIT_ASKPASS: ask.shPath,
        GIT_ASKPASS_ALWAYS: '1',
      });
      appendGitPushReqLog(
        `oauth layer_id=${layerId} slug=${slugInfo.slug} rel_prefix=${String(row.relPrefix || '').slice(0, 160)} run ${cmdLine}`,
      );
      try {
        await gitExecAsync(pushArgs, row.workdir, pushEnv);
        item.push_ok = true;
        appendGitPushReqLog(
          `oauth layer_id=${layerId} slug=${slugInfo.slug} rel_prefix=${String(row.relPrefix || '').slice(0, 160)} git_push ok`,
        );
      } catch (e) {
        item.detail = String(e.message || e);
        appendGitPushReqLog(
          `oauth layer_id=${layerId} slug=${slugInfo.slug} rel_prefix=${String(row.relPrefix || '').slice(0, 160)} git_push fail cmd=${cmdLine} err=${String(e.message || e).slice(0, 800)}`,
        );
        repos.push(item);
        return {
          httpStatus: 400,
          payload: {
            ok: false,
            detail: `推送失败（${slugInfo.slug}）：${item.detail}`,
            github_oauth_multirepo: { repos },
          },
        };
      }

      if (baseName && headName && baseName !== headName) {
        const prRes = await createGithubPullRequest({
          owner: slugInfo.owner,
          repo: slugInfo.repo,
          head: headName,
          base: baseName,
          accessToken: repoToken,
          title: prTitle,
          bodyText: prBody,
        });
        if (prRes.ok && prRes.json) {
          item.pr = {
            html_url: prRes.json.html_url || '',
            number: prRes.json.number,
            state: prRes.json.state,
          };
        } else {
          item.pr_error = prRes.text || `http_${prRes.status}`;
        }
      }
      repos.push(item);
    }
  } finally {
    for (const ask of askpassByToken.values()) {
      try {
        ask.cleanup();
      } catch {
        /* ignore */
      }
    }
  }

  const anyPushed = repos.some((r) => r.push_ok);
  if (!anyPushed) {
    appendGitPushReqLog(`oauth layer_id=${layerId} fail reason=no_successful_push`);
    return {
      httpStatus: 400,
      payload: {
        ok: false,
        detail: '层内未发现可供 OAuth 推送的 github.com 远程仓库',
        github_oauth_multirepo: { repos },
      },
    };
  }
  appendGitPushReqLog(`oauth layer_id=${layerId} done ok repos=${repos.length}`);
  return {
    httpStatus: 200,
    payload: {
      ok: true,
      github_oauth_multirepo: { repos },
    },
  };
}
