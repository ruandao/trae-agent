/**
 * 从 task2app（TaskApiEndPoint）用 GitHub OAuth refresh 换 access_token，再对层级内仓库 HTTPS 推送。
 */
import { postJson, taskApiPrefix } from './saasTaskCloud.mjs';
import { layerGitWorkdirRootsForFileListing } from './layerFs.mjs';
import { parseGithubOwnerRepoFromRemoteUrl, runLayerGithubOauthAccessPush } from './layerGitOauthPush.mjs';
import { spawnSync } from 'child_process';
import fs from 'fs';
import path from 'path';
import { gitCmd } from './gitCmd.mjs';

function gitConfigGetRemoteOrigin(workdir) {
  try {
    const out = spawnSync(gitCmd(), ['config', '--get', 'remote.origin.url'], {
      cwd: workdir,
      encoding: 'utf8',
      env: { ...process.env, GIT_TERMINAL_PROMPT: '0' },
      maxBuffer: 1024 * 1024,
    });
    if (out.status !== 0) return '';
    return (
      String(out.stdout || '')
        .trim()
        .split('\n')[0] || ''
    );
  } catch {
    return '';
  }
}

/** @returns {string[]} owner/repo slug（小写），来自层内 GitHub 远程 */
export function collectGithubRepoSlugsInLayer(layerId) {
  const roots = layerGitWorkdirRootsForFileListing(layerId);
  const slugs = new Set();
  for (const row of roots) {
    try {
      if (!fs.existsSync(path.join(row.workdir, '.git'))) continue;
    } catch {
      continue;
    }
    const origin = gitConfigGetRemoteOrigin(row.workdir);
    const info = parseGithubOwnerRepoFromRemoteUrl(origin);
    if (info?.slug) slugs.add(String(info.slug).toLowerCase());
  }
  return [...slugs];
}

/**
 * @param {object} opts
 * @param {string} opts.layerId
 * @param {string} [opts.targetBranch]
 */
export async function runLayerOauthRefreshPush(opts) {
  const layerId = String(opts.layerId || '').trim();
  if (!layerId) {
    return { httpStatus: 400, payload: { ok: false, detail: 'layer_id 无效' } };
  }

  let cloudPrefix;
  try {
    cloudPrefix = taskApiPrefix();
  } catch (e) {
    return {
      httpStatus: 503,
      payload: {
        ok: false,
        detail: `未配置 TaskApiEndPoint/TASK_API_ENDPOINT：${String(e?.message || e)}`,
      },
    };
  }

  const accessToken = String(process.env.ACCESS_TOKEN || '').trim();
  if (!accessToken) {
    return { httpStatus: 503, payload: { ok: false, detail: '缺少容器 ACCESS_TOKEN' } };
  }

  let targetBranch = String(opts.targetBranch || '').trim();
  if (!targetBranch) {
    try {
      const detail = await postJson(
        `${cloudPrefix.replace(/\/$/, '')}/server-container-token/task-detail/`,
        { access_token: accessToken },
        30,
      );
      const tb = detail?.task?.target_branch;
      if (typeof tb === 'string' && tb.trim()) targetBranch = tb.trim();
    } catch (e) {
      return {
        httpStatus: 502,
        payload: {
          ok: false,
          detail: `拉取任务详情失败：${String(e?.message || e).slice(0, 500)}`,
        },
      };
    }
  }
  if (!targetBranch) {
    return { httpStatus: 400, payload: { ok: false, detail: 'target_branch 必填（任务未配置目标分支）' } };
  }

  const repoSlugs = collectGithubRepoSlugsInLayer(layerId);
  if (!repoSlugs.length) {
    return {
      httpStatus: 400,
      payload: { ok: false, detail: '层内未发现 github.com 远程仓库' },
    };
  }

  let tokenPayload;
  try {
    tokenPayload = await postJson(
      `${cloudPrefix.replace(/\/$/, '')}/server-container-token/layer-github-oauth-access-tokens/`,
      {
        access_token: accessToken,
        repo_slugs: repoSlugs,
        target_branch: targetBranch,
      },
      60,
    );
  } catch (e) {
    return {
      httpStatus: 502,
      payload: {
        ok: false,
        detail: `从 task2app 拉取 GitHub AccessToken 失败：${String(e?.message || e).slice(0, 500)}`,
      },
    };
  }

  const githubAuthByRepo =
    tokenPayload && typeof tokenPayload.github_auth_by_repo === 'object' && tokenPayload.github_auth_by_repo
      ? tokenPayload.github_auth_by_repo
      : null;
  if (!githubAuthByRepo || !Object.keys(githubAuthByRepo).length) {
    const partial = tokenPayload?.partial_error || tokenPayload?.detail;
    return {
      httpStatus: 409,
      payload: {
        ok: false,
        detail: partial
          ? String(partial)
          : 'task2app 未返回可用的 github_auth_by_repo',
        repo_slugs: repoSlugs,
      },
    };
  }

  return runLayerGithubOauthAccessPush({
    layerId,
    targetBranch,
    accessTokenByRepoSlug: githubAuthByRepo,
    prBaseBranch: String(tokenPayload?.pr_base_branch || '').trim(),
    prTitle: String(tokenPayload?.pr_title || '').trim(),
    prBody: String(tokenPayload?.pr_body || '').trim(),
  });
}
