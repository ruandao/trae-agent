/**
 * 从 task2app（TaskApiEndPoint）用 GitHub OAuth refresh 换 access_token，再对层级内仓库 HTTPS 推送。
 */
import { postJson, taskApiPrefix } from './saasTaskCloud.mjs';
import { runLayerGithubOauthAccessPush } from './layerGitOauthPush.mjs';
import { collectOauthRepoWriteTargets } from './layerGitOauthFetchTokenFiles.mjs';
import fs from 'fs';
import path from 'path';
import { logsDir } from './paths.mjs';

function appendOauthRefreshPushLog(line) {
  try {
    const p = path.join(logsDir(), 'gitPush.log');
    fs.appendFileSync(p, `${new Date().toISOString()} | ${line}\n`);
  } catch {
    /* ignore */
  }
}

function oauthTokenFetchTimeoutSec() {
  const raw = String(process.env.TRAE_LAYER_GITHUB_OAUTH_FETCH_TIMEOUT_SEC || '').trim();
  const n = Number.parseInt(raw, 10);
  if (!Number.isFinite(n)) return 120;
  return Math.max(30, Math.min(300, n));
}

/** @returns {string[]} owner/repo slug（小写），来自层内 GitHub 远程 */
export function collectGithubRepoSlugsInLayer(layerId) {
  return collectOauthRepoWriteTargets(layerId)
    .map((x) => x.githubSlug)
    .filter(Boolean);
}

/** @returns {{ repoMatchKey: string, githubSlug: string }[]} */
export function collectOauthRepoKeysInLayer(layerId) {
  return collectOauthRepoWriteTargets(layerId).map((x) => ({
    repoMatchKey: x.repoMatchKey,
    githubSlug: x.githubSlug,
  }));
}

/**
 * @param {object} opts
 * @param {string} opts.layerId
 * @param {string} [opts.targetBranch]
 * @param {string} [opts.traceId] forwarded X-Trace-Id from inbound HTTP request
 */
export async function runLayerOauthRefreshPush(opts) {
  const layerId = String(opts.layerId || '').trim();
  const traceId = opts.traceId;
  if (!layerId) {
    appendOauthRefreshPushLog('oauth-refresh-push fail layer_id=invalid detail=layer_id 无效');
    return { httpStatus: 400, payload: { ok: false, detail: 'layer_id 无效' } };
  }
  appendOauthRefreshPushLog(`oauth-refresh-push begin layer_id=${layerId}`);

  let cloudPrefix;
  try {
    cloudPrefix = taskApiPrefix();
  } catch (e) {
    appendOauthRefreshPushLog(
      `oauth-refresh-push fail layer_id=${layerId} detail=TaskApiPrefix ${String(e?.message || e).slice(0, 240)}`,
    );
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
    appendOauthRefreshPushLog(`oauth-refresh-push fail layer_id=${layerId} detail=缺少容器 ACCESS_TOKEN`);
    return { httpStatus: 503, payload: { ok: false, detail: '缺少容器 ACCESS_TOKEN' } };
  }

  let targetBranch = String(opts.targetBranch || '').trim();
  if (!targetBranch) {
    try {
      const detail = await postJson(
        `${cloudPrefix.replace(/\/$/, '')}/server-container-token/task-detail/`,
        { access_token: accessToken },
        30,
        { traceId },
      );
      const tb = detail?.task?.target_branch;
      if (typeof tb === 'string' && tb.trim()) targetBranch = tb.trim();
    } catch (e) {
      appendOauthRefreshPushLog(
        `oauth-refresh-push fail layer_id=${layerId} detail=task-detail ${String(e?.message || e).slice(0, 240)}`,
      );
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
    appendOauthRefreshPushLog(`oauth-refresh-push fail layer_id=${layerId} detail=target_branch 缺失`);
    return { httpStatus: 400, payload: { ok: false, detail: 'target_branch 必填（任务未配置目标分支）' } };
  }

  const oauthTargets = collectOauthRepoWriteTargets(layerId);
  if (!oauthTargets.length) {
    appendOauthRefreshPushLog(`oauth-refresh-push fail layer_id=${layerId} detail=层内未发现 Git 远程仓库`);
    return {
      httpStatus: 400,
      payload: { ok: false, detail: '层内未发现 Git 远程仓库' },
    };
  }
  const repoMatchKeys = [...new Set(oauthTargets.map((x) => x.repoMatchKey))];
  const repoSlugs = [...new Set(oauthTargets.map((x) => x.githubSlug).filter(Boolean))];

  const oauthFetchTimeoutSec = oauthTokenFetchTimeoutSec();
  appendOauthRefreshPushLog(
    `oauth-refresh-push token-fetch layer_id=${layerId} repos=${repoSlugs.length} target_branch=${targetBranch.slice(0, 120)} timeout_sec=${oauthFetchTimeoutSec}`,
  );
  let tokenPayload;
  try {
    tokenPayload = await postJson(
      `${cloudPrefix.replace(/\/$/, '')}/server-container-token/layer-github-oauth-access-tokens/`,
      {
        access_token: accessToken,
        repo_match_keys: repoMatchKeys,
        repo_slugs: repoSlugs.length ? repoSlugs : undefined,
        target_branch: targetBranch,
      },
      oauthFetchTimeoutSec,
      { traceId },
    );
  } catch (e) {
    appendOauthRefreshPushLog(
      `oauth-refresh-push fail layer_id=${layerId} detail=layer-github-oauth-access-tokens ${String(e?.message || e).slice(0, 320)}`,
    );
    return {
      httpStatus: 502,
      payload: {
        ok: false,
        detail: `从 task2app 拉取 GitHub AccessToken 失败：${String(e?.message || e).slice(0, 500)}`,
      },
    };
  }

  const authByKey =
    tokenPayload && typeof tokenPayload.git_auth_by_repo_match_key === 'object'
      ? tokenPayload.git_auth_by_repo_match_key
      : null;
  const githubAuthByRepo =
    authByKey && Object.keys(authByKey).length
      ? authByKey
      : tokenPayload && typeof tokenPayload.github_auth_by_repo === 'object' && tokenPayload.github_auth_by_repo
        ? tokenPayload.github_auth_by_repo
        : null;
  if (!githubAuthByRepo || !Object.keys(githubAuthByRepo).length) {
    const partial = tokenPayload?.partial_error || tokenPayload?.detail;
    appendOauthRefreshPushLog(
      `oauth-refresh-push fail layer_id=${layerId} detail=no github_auth_by_repo partial=${String(partial || '').slice(0, 240)}`,
    );
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
  appendOauthRefreshPushLog(
    `oauth-refresh-push token-ready layer_id=${layerId} token_repos=${Object.keys(githubAuthByRepo).length}`,
  );

  try {
    const result = await runLayerGithubOauthAccessPush({
      layerId,
      targetBranch,
      accessTokenByRepoSlug: githubAuthByRepo,
      prBaseBranch: String(tokenPayload?.pr_base_branch || '').trim(),
      prTitle: String(tokenPayload?.pr_title || '').trim(),
      prBody: String(tokenPayload?.pr_body || '').trim(),
    });
    appendOauthRefreshPushLog(
      `oauth-refresh-push done layer_id=${layerId} http_status=${result.httpStatus} ok=${Boolean(result?.payload?.ok)}`,
    );
    return result;
  } catch (e) {
    appendOauthRefreshPushLog(
      `oauth-refresh-push fail layer_id=${layerId} detail=oauth-access-push ${String(e?.message || e).slice(0, 320)}`,
    );
    throw e;
  }
}
