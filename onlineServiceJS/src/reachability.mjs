/**
 * 解析宿主机可达 IP（公网或本网段）与映射端口，向 SaaS 注册 CloudServerConfig（register-reachability）。
 */
import os from 'os';

import { appendOutboundReqLog, sanitizeUrlForOutboundLog } from './outboundReqLog.mjs';
import { postJson, rewriteDockerInternal, taskApiPrefix } from './saasTaskCloud.mjs';

/** IPv6 等非 IPv4 文本在 URL authority 中需方括号 */
function authorityHost(ip) {
  const s = String(ip || '').trim();
  if (!s) return '';
  if (s.startsWith('[')) return s;
  if (!s.includes(':')) return s;
  return `[${s}]`;
}

function buildHttpUrl(ip, port, pathname = '') {
  const h = authorityHost(ip);
  const p = Number(port);
  if (!h || !Number.isFinite(p) || p <= 0) return '';
  let suffix = pathname || '';
  if (suffix && !suffix.startsWith('/')) suffix = `/${suffix}`;
  return `http://${h}:${p}${suffix}`;
}

function normalizeUrlNoTrailingSlash(raw) {
  const u = new URL(raw);
  return u.href.replace(/\/$/, '');
}

function envBusinessApiEndpointRaw() {
  return String(process.env.BusinessApiEndPoint || process.env.BUSINESS_API_ENDPOINT || '').trim();
}

/**
 * 与 `bootstrap.mjs` 中换票用 `normalizeBusinessApiEndpointUrl` 对齐：编排常见
 * `http://<ip>/api` 无显式端口时，WHATWG URL 的 origin 会落在默认 80/443，与容器实际
 * `PORT` / `TRAE_HOST_HTTP_PORT`（默认 8765）不一致，导致 register-reachability 写入 DB 缺端口。
 */
function applyHostMappedPortIfIpLikeHost(u) {
  const host = String(u.hostname || '').trim();
  const looksLikeIp =
    /^\d{1,3}(\.\d{1,3}){3}$/.test(host) || host.includes(':') || host === 'localhost';
  if (!u.port && looksLikeIp) {
    u.port = String(hostMappedHttpPort());
  }
}

/**
 * 优先沿用换票阶段使用的 BUSINESS_API_ENDPOINT，避免注册可达地址与换票地址源不一致。
 * @returns {{ businessApiEndpoint: string, serverUrl: string, publicIp: string|null } | null}
 */
export function reachabilityFromBusinessEndpointEnv() {
  const raw = envBusinessApiEndpointRaw();
  if (!raw) return null;

  let u;
  try {
    u = new URL(rewriteDockerInternal(raw));
  } catch {
    appendOutboundReqLog('reachability: ignore invalid BUSINESS_API_ENDPOINT/BusinessApiEndPoint');
    return null;
  }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') {
    appendOutboundReqLog(`reachability: ignore non-http business endpoint protocol=${u.protocol}`);
    return null;
  }
  applyHostMappedPortIfIpLikeHost(u);
  const businessApiEndpoint = normalizeUrlNoTrailingSlash(u.href);
  const hostName = String(u.hostname || '').trim();
  const ipLikeHost =
    /^\d{1,3}(?:\.\d{1,3}){3}$/.test(hostName) || hostName.includes(':');
  const publicIp = ipLikeHost ? hostName : null;

  // business_api_endpoint 约定为 .../api；server_url 对应其上一级根（保留可能存在的前缀路径）。
  let pathNoTrailing = String(u.pathname || '/').replace(/\/+$/, '');
  if (!pathNoTrailing) pathNoTrailing = '/';
  if (pathNoTrailing.toLowerCase().endsWith('/api')) {
    pathNoTrailing = pathNoTrailing.slice(0, -4) || '/';
  }
  const serverUrl = normalizeUrlNoTrailingSlash(`${u.origin}${pathNoTrailing}`);

  return { businessApiEndpoint, serverUrl, publicIp };
}

const _IPV4_RE = /\b(\d{1,3}(?:\.\d{1,3}){3})\b/;

/**
 * 国内常用公网 IPv4 查询（避免依赖境外 api.ipify.org）。
 * @param {number} [timeoutMs]
 * @returns {Promise<string|null>}
 */
async function fetchPublicIpv4Domestic(timeoutMs = 4500) {
  async function query(url, parse) {
    const safe = sanitizeUrlForOutboundLog(url);
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), timeoutMs);
    const t0 = Date.now();
    try {
      const r = await fetch(url, { signal: ac.signal });
      const body = await r.text();
      appendOutboundReqLog(`reachability GET ${safe} -> HTTP ${r.status} ${Date.now() - t0}ms`);
      if (!r.ok) return null;
      return parse(body);
    } catch (e) {
      appendOutboundReqLog(
        `reachability GET ${safe} -> error ${String(e?.message || e).slice(0, 240)} ${Date.now() - t0}ms`,
      );
      return null;
    } finally {
      clearTimeout(timer);
    }
  }

  let ip = await query('https://4.ipw.cn', (t) => {
    const s = t.trim();
    if (/^\d{1,3}(?:\.\d{1,3}){3}$/.test(s)) return s;
    const m = s.match(_IPV4_RE);
    return m ? m[1] : null;
  });
  if (ip) return ip;

  ip = await query('https://myip.ipip.net', (text) => {
    const m =
      text.match(/(?:当前\s*IP|IP)[：:]\s*(\d{1,3}(?:\.\d{1,3}){3})/) || text.match(_IPV4_RE);
    return m ? m[1] : null;
  });
  return ip || null;
}

/**
 * 不使用 127.0.0.1 作为隐式回退；无法解析时抛错，由调用方退出进程。
 * @returns {Promise<string>}
 */
async function resolveReachableIp() {
  const fromEnv = String(process.env.TRAE_PUBLIC_IP || process.env.PUBLIC_IP || '').trim();
  if (fromEnv) return fromEnv;

  const wan = await fetchPublicIpv4Domestic();
  if (wan) return wan;

  const nets = os.networkInterfaces();
  for (const name of Object.keys(nets)) {
    for (const net of nets[name] || []) {
      if (net && net.family === 'IPv4' && !net.internal) {
        return net.address;
      }
    }
  }

  throw new Error(
    '无法解析可达 IP（已禁用回退 127.0.0.1）：请设置环境变量 TRAE_PUBLIC_IP 或 PUBLIC_IP，' +
      '或确保本机能访问国内公网 IP 查询接口（https://4.ipw.cn / https://myip.ipip.net）且存在非回环 IPv4 网卡地址'
  );
}

/** 宿主机可达 HTTP 端口：publish 映射优先 TRAE_HOST_HTTP_PORT，否则容器 PORT，默认与 server.listen 一致 8765 */
export function hostMappedHttpPort() {
  const explicit = String(process.env.TRAE_HOST_HTTP_PORT || '').trim();
  if (explicit) {
    const n = parseInt(explicit, 10);
    if (Number.isFinite(n) && n > 0) return n;
  }
  return parseInt(process.env.PORT || '8765', 10) || 8765;
}

function hostMappedVscodePort() {
  const explicit = String(process.env.TRAE_HOST_VSCODE_PORT || '').trim();
  if (explicit) {
    const n = parseInt(explicit, 10);
    if (Number.isFinite(n) && n > 0) return n;
  }
  // onlineService-entrypoint.sh：CODE_SERVER_ENABLED 时 code-server 默认监听容器内 8888；宿主机常见 8888:8888 映射
  const cs = String(process.env.CODE_SERVER_ENABLED || '').trim().toLowerCase();
  if (['1', 'true', 'yes', 'on'].includes(cs)) {
    const inner = parseInt(String(process.env.CODE_SERVER_BIND_PORT || '8888').trim(), 10);
    return Number.isFinite(inner) && inner > 0 ? inner : 8888;
  }
  return null;
}

/**
 * 向 SaaS 注册 `server_url` / `business_api_endpoint`（register-reachability）。
 * 由 `server.mjs` 在 HTTP listen 成功后尽快调用，早于 `runBootstrapAfterListen`（克隆与 YAML），
 * 以便任务详情页拉层图与心跳不被长时间 git clone 阻塞。
 *
 * @param {{ skipped?: boolean, prefix?: string, newAccess?: string, timeout?: number }} ctx
 */
export async function registerReachabilityAfterBootstrap(ctx) {
  if (!ctx || ctx.skipped || !ctx.prefix) return;
  if (['1', 'true', 'yes', 'on'].includes(String(process.env.TRAE_SKIP_REACHABILITY_REGISTER || '').toLowerCase())) {
    appendOutboundReqLog('reachability: skip TRAE_SKIP_REACHABILITY_REGISTER');
    return;
  }

  const token = String(process.env.ACCESS_TOKEN || ctx.newAccess || '').trim();
  if (!token) {
    appendOutboundReqLog('reachability: skip (no ACCESS_TOKEN)');
    return;
  }

  let prefix = ctx.prefix;
  try {
    prefix = taskApiPrefix();
  } catch {
    /* use ctx.prefix */
  }

  const timeoutSec = Math.max(1, ctx.timeout || parseFloat(process.env.TASK_API_BOOTSTRAP_TIMEOUT_SEC || '5') || 5);

  const fromBusiness = reachabilityFromBusinessEndpointEnv();
  const pip = fromBusiness ? fromBusiness.publicIp : await resolveReachableIp();
  const httpPort = hostMappedHttpPort();
  const vscodePort = hostMappedVscodePort();

  const serverUrl = fromBusiness?.serverUrl || buildHttpUrl(pip, httpPort);
  const biz = fromBusiness?.businessApiEndpoint || buildHttpUrl(pip, httpPort, '/api');
  const vscodeUrl = vscodePort != null && pip ? buildHttpUrl(pip, vscodePort, '/') : '';

  const body = {
    access_token: token,
    server_url: serverUrl,
    business_api_endpoint: biz,
  };
  if (pip) body.public_ip = pip;
  if (vscodeUrl) body.container_vscode_url = vscodeUrl;

  await postJson(
    `${prefix.replace(/\/$/, '')}/server-container-token/register-reachability/`,
    body,
    timeoutSec
  );
  const pipText = pip || '(none)';
  console.log(
    `[onlineServiceJS] 已向 SaaS 注册可达地址 public_ip=${pipText} server_url=${serverUrl} business_api_endpoint=${biz}` +
      (vscodeUrl ? ` vscode=${vscodeUrl}` : '')
  );
  appendOutboundReqLog(
    `reachability: registered public_ip=${pipText} server_url=${serverUrl} business_api_endpoint=${biz}`
  );
}
