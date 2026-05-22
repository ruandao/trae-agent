/**
 * 从 task2app 拉取 GitHub OAuth access_token，并按仓库写入 <repo>/.task2app_access_token。
 */
import fs from 'fs';
import path from 'path';
import { spawnSync } from 'child_process';
import { postJson, taskApiPrefix } from './saasTaskCloud.mjs';
import { layerGitWorkdirRootsForFileListing } from './layerFs.mjs';
import { parseGithubOwnerRepoFromRemoteUrl } from './layerGitOauthPush.mjs';
import { gitCmd } from './gitCmd.mjs';
import { logsDir } from './paths.mjs';

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

function appendTokenRefreshLog(line) {
  try {
    const p = path.join(logsDir(), 'tokenRefresh.log');
    fs.appendFileSync(p, `${new Date().toISOString()} | ${line}\n`);
  } catch {
    /* ignore */
  }
}

function oauthTokenFetchTimeoutSec() {
  const raw = String(process.env.TRAE_LAYER_GITHUB_OAUTH_FETCH_TIMEOUT_SEC || '').trim();
  const configured = Number.parseInt(raw, 10);
  const rawMin = String(process.env.TRAE_LAYER_GITHUB_OAUTH_FETCH_TIMEOUT_MIN_SEC || '').trim();
  const configuredMin = Number.parseInt(rawMin, 10);
  const minTimeoutSec = Number.isFinite(configuredMin) ? Math.max(1, Math.min(300, configuredMin)) : 30;
  if (!Number.isFinite(configured)) return 120;
  return Math.max(minTimeoutSec, Math.min(300, configured));
}

function parseStructuredErrorFromMessage(message) {
  const text = String(message || '').trim();
  const jsonStart = text.indexOf('{');
  if (jsonStart < 0) return null;
  const candidate = text.slice(jsonStart);
  try {
    const parsed = JSON.parse(candidate);
    if (!parsed || typeof parsed !== 'object') return null;
    return parsed;
  } catch {
    return null;
  }
}

function defaultStructuredErrorForMessage(message) {
  const text = String(message || '').toLowerCase();
  if (text.includes('aborted') || text.includes('timeout') || text.includes('timed out')) {
    return {
      error_code: 'UPSTREAM_GITOAUTH_TIMEOUT',
      failed_stage: 'gitoauth_summary',
      retryable: true,
      detail_safe: '访问 gitOauth 超时，请稍后重试',
    };
  }
  return null;
}

function pickStructuredErrorFields(value) {
  if (!value || typeof value !== 'object') return null;
  const errorCode = String(value.error_code || '').trim();
  const failedStage = String(value.failed_stage || '').trim();
  if (!errorCode && !failedStage) return null;
  const picked = {
    error_code: errorCode || undefined,
    failed_stage: failedStage || undefined,
    retryable: typeof value.retryable === 'boolean' ? value.retryable : undefined,
    detail_safe: value.detail_safe ? String(value.detail_safe) : undefined,
  };
  return picked;
}

function collectGithubRepoWriteTargets(layerId) {
  const roots = layerGitWorkdirRootsForFileListing(layerId);
  const targets = [];
  for (const row of roots) {
    try {
      if (!fs.existsSync(path.join(row.workdir, '.git'))) continue;
    } catch {
      continue;
    }
    const origin = gitConfigGetRemoteOrigin(row.workdir);
    const info = parseGithubOwnerRepoFromRemoteUrl(origin);
    if (!info?.slug) continue;
    targets.push({
      workdir: row.workdir,
      relPrefix: row.relPrefix || '',
      slug: String(info.slug).toLowerCase(),
    });
  }
  return targets;
}

/**
 * @param {object} opts
 * @param {string} opts.layerId
 * @param {string} [opts.targetBranch]
 */
export async function runLayerOauthFetchTokenFiles(opts) {
  const layerId = String(opts?.layerId || '').trim();
  if (!layerId) {
    return { httpStatus: 400, payload: { ok: false, detail: 'layer_id 无效' } };
  }
  appendTokenRefreshLog(`oauth-fetch-token-files begin layer_id=${layerId}`);

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

  const targetBranch = String(opts?.targetBranch || '').trim();

  const targets = collectGithubRepoWriteTargets(layerId);
  if (!targets.length) {
    return { httpStatus: 400, payload: { ok: false, detail: '层内未发现 github.com 远程仓库' } };
  }
  const repoSlugs = [...new Set(targets.map((x) => x.slug))];
  const oauthFetchTimeoutSec = oauthTokenFetchTimeoutSec();

  let tokenPayload;
  try {
    const body = {
      access_token: accessToken,
      repo_slugs: repoSlugs,
    };
    if (targetBranch) body.target_branch = targetBranch;
    tokenPayload = await postJson(
      `${cloudPrefix.replace(/\/$/, '')}/server-container-token/layer-github-oauth-access-tokens/`,
      body,
      oauthFetchTimeoutSec,
    );
  } catch (e) {
    const errMsg = String(e?.message || e);
    const fromStructuredPayload = pickStructuredErrorFields(e?.structuredPayload);
    const parsed = pickStructuredErrorFields(parseStructuredErrorFromMessage(errMsg));
    const fallback = defaultStructuredErrorForMessage(errMsg);
    const structured = fromStructuredPayload || parsed || fallback;
    return {
      httpStatus: 502,
      payload: {
        ok: false,
        detail: `从 task2app 拉取 GitHub AccessToken 失败：${errMsg.slice(0, 500)}`,
        error_code: structured?.error_code,
        failed_stage: structured?.failed_stage,
        retryable: structured?.retryable,
        detail_safe: structured?.detail_safe,
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
        detail: partial ? String(partial) : 'task2app 未返回可用的 github_auth_by_repo',
      },
    };
  }

  const repos = [];
  let writeOkCount = 0;
  for (const target of targets) {
    const token = String(githubAuthByRepo[target.slug] || '').trim();
    const filePath = path.join(target.workdir, '.task2app_access_token');
    if (!token) {
      repos.push({
        github_slug: target.slug,
        rel_prefix: target.relPrefix,
        token_file_path: filePath,
        write_ok: false,
        detail: '该仓库未返回 access_token',
      });
      continue;
    }
    try {
      fs.writeFileSync(filePath, `${token}\n`, { mode: 0o600 });
      writeOkCount += 1;
      repos.push({
        github_slug: target.slug,
        rel_prefix: target.relPrefix,
        token_file_path: filePath,
        write_ok: true,
      });
    } catch (e) {
      repos.push({
        github_slug: target.slug,
        rel_prefix: target.relPrefix,
        token_file_path: filePath,
        write_ok: false,
        detail: `写入失败：${String(e?.message || e).slice(0, 200)}`,
      });
    }
  }

  const failed = repos.length - writeOkCount;
  appendTokenRefreshLog(
    `oauth-fetch-token-files done layer_id=${layerId} write_ok=${writeOkCount} failed=${failed} repos=${repos.length}`,
  );
  return {
    httpStatus: 200,
    payload: {
      ok: true,
      summary: `已写入 ${writeOkCount}/${repos.length} 个仓库 token 文件`,
      token_files: repos,
      partial_error: failed > 0 ? `有 ${failed} 个仓库写入失败或缺少 token` : undefined,
    },
  };
}
