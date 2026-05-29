/**
 * 从 task2app 拉取 Git OAuth access_token，并按仓库写入 <repo>/.task2app_access_token。
 */
import fs from 'fs';
import path from 'path';
import { spawnSync } from 'child_process';
import { postJson, taskApiPrefix } from './saasTaskCloud.mjs';
import { layerGitWorkdirRootsForFileListing } from './layerFs.mjs';
import { parseGithubOwnerRepoFromRemoteUrl } from './layerGitOauthPush.mjs';
import { repoMatchKeyFromUrl } from './repoMatchKey.mjs';
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

export function collectOauthRepoWriteTargets(layerId) {
  const roots = layerGitWorkdirRootsForFileListing(layerId);
  const targets = [];
  for (const row of roots) {
    try {
      if (!fs.existsSync(path.join(row.workdir, '.git'))) continue;
    } catch {
      continue;
    }
    const origin = gitConfigGetRemoteOrigin(row.workdir);
    const repoMatchKey = repoMatchKeyFromUrl(origin);
    if (!repoMatchKey) continue;
    const slugInfo = parseGithubOwnerRepoFromRemoteUrl(origin);
    targets.push({
      workdir: row.workdir,
      relPrefix: row.relPrefix || '',
      originUrl: origin,
      repoMatchKey,
      githubSlug: slugInfo?.slug ? String(slugInfo.slug).toLowerCase() : '',
    });
  }
  return targets;
}

/** @deprecated 使用 collectOauthRepoWriteTargets */
export function collectGithubRepoWriteTargets(layerId) {
  return collectOauthRepoWriteTargets(layerId).filter((x) => x.githubSlug);
}

function authTokensByRepoMatchKeyFromPayload(tokenPayload) {
  const byKey =
    tokenPayload && typeof tokenPayload.git_auth_by_repo_match_key === 'object'
      ? tokenPayload.git_auth_by_repo_match_key
      : null;
  if (byKey && Object.keys(byKey).length) return byKey;
  const legacy =
    tokenPayload && typeof tokenPayload.github_auth_by_repo === 'object'
      ? tokenPayload.github_auth_by_repo
      : null;
  return legacy && Object.keys(legacy).length ? legacy : null;
}

/**
 * @param {object} opts
 * @param {string} opts.layerId
 * @param {string} [opts.targetBranch]
 * @param {string} [opts.traceId] forwarded X-Trace-Id from inbound HTTP request
 */
export async function runLayerOauthFetchTokenFiles(opts) {
  const layerId = String(opts?.layerId || '').trim();
  const traceId = opts?.traceId;
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

  const targets = collectOauthRepoWriteTargets(layerId);
  if (!targets.length) {
    return { httpStatus: 400, payload: { ok: false, detail: '层内未发现 Git 远程仓库' } };
  }
  const repoMatchKeys = [...new Set(targets.map((x) => x.repoMatchKey))];
  const repoSlugs = [...new Set(targets.map((x) => x.githubSlug).filter(Boolean))];
  const oauthFetchTimeoutSec = oauthTokenFetchTimeoutSec();

  let tokenPayload;
  try {
    const body = {
      access_token: accessToken,
      repo_match_keys: repoMatchKeys,
    };
    if (repoSlugs.length) body.repo_slugs = repoSlugs;
    if (targetBranch) body.target_branch = targetBranch;
    tokenPayload = await postJson(
      `${cloudPrefix.replace(/\/$/, '')}/server-container-token/layer-github-oauth-access-tokens/`,
      body,
      oauthFetchTimeoutSec,
      { traceId },
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
        detail: `从 task2app 拉取 Git OAuth AccessToken 失败：${errMsg.slice(0, 500)}`,
        error_code: structured?.error_code,
        failed_stage: structured?.failed_stage,
        retryable: structured?.retryable,
        detail_safe: structured?.detail_safe,
      },
    };
  }

  const authByRepo = authTokensByRepoMatchKeyFromPayload(tokenPayload);
  if (!authByRepo || !Object.keys(authByRepo).length) {
    const partial = tokenPayload?.partial_error || tokenPayload?.detail;
    return {
      httpStatus: 409,
      payload: {
        ok: false,
        detail: partial ? String(partial) : 'task2app 未返回可用的 OAuth access_token',
      },
    };
  }

  const repos = [];
  let writeOkCount = 0;
  for (const target of targets) {
    const token = String(
      authByRepo[target.repoMatchKey] ||
        (target.githubSlug ? authByRepo[target.githubSlug] : '') ||
        '',
    ).trim();
    const filePath = path.join(target.workdir, '.task2app_access_token');
    const repoLabel = target.repoMatchKey || target.githubSlug || target.relPrefix || '?';
    if (!token) {
      repos.push({
        repo_match_key: target.repoMatchKey,
        github_slug: target.githubSlug || undefined,
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
        repo_match_key: target.repoMatchKey,
        github_slug: target.githubSlug || undefined,
        rel_prefix: target.relPrefix,
        token_file_path: filePath,
        write_ok: true,
        repo_label: repoLabel,
      });
    } catch (e) {
      repos.push({
        repo_match_key: target.repoMatchKey,
        github_slug: target.githubSlug || undefined,
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
