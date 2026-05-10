/**
 * 解析宿主机可达 IP（公网或本网段）与映射端口，向 SaaS 注册 CloudServerConfig（register-reachability）。
 */
import fs from 'fs';
import os from 'os';
import path from 'path';

import { reqLogsDir } from './paths.mjs';
import { postJson, taskApiPrefix } from './saasTaskCloud.mjs';

function logOutbound(line) {
  try {
    const f = path.join(reqLogsDir(), 'outbound.log');
    fs.appendFileSync(f, `${new Date().toISOString()} | ${line}\n`);
  } catch {
    /* ignore */
  }
}

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

const _IPV4_RE = /\b(\d{1,3}(?:\.\d{1,3}){3})\b/;

/**
 * 国内常用公网 IPv4 查询（避免依赖境外 api.ipify.org）。
 * @param {number} [timeoutMs]
 * @returns {Promise<string|null>}
 */
async function fetchPublicIpv4Domestic(timeoutMs = 4500) {
  async function query(url, parse) {
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), timeoutMs);
    try {
      const r = await fetch(url, { signal: ac.signal });
      if (!r.ok) return null;
      const body = await r.text();
      return parse(body);
    } catch {
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
 * @param {{ skipped?: boolean, prefix?: string, newAccess?: string, timeout?: number }} ctx
 */
export async function registerReachabilityAfterBootstrap(ctx) {
  if (!ctx || ctx.skipped || !ctx.prefix) return;
  if (['1', 'true', 'yes', 'on'].includes(String(process.env.TRAE_SKIP_REACHABILITY_REGISTER || '').toLowerCase())) {
    logOutbound('reachability: skip TRAE_SKIP_REACHABILITY_REGISTER');
    return;
  }

  const token = String(process.env.ACCESS_TOKEN || ctx.newAccess || '').trim();
  if (!token) {
    logOutbound('reachability: skip (no ACCESS_TOKEN)');
    return;
  }

  let prefix = ctx.prefix;
  try {
    prefix = taskApiPrefix();
  } catch {
    /* use ctx.prefix */
  }

  const timeoutSec = Math.max(1, ctx.timeout || parseFloat(process.env.TASK_API_BOOTSTRAP_TIMEOUT_SEC || '5') || 5);

  const pip = await resolveReachableIp();
  const httpPort = hostMappedHttpPort();
  const vscodePort = hostMappedVscodePort();

  const serverUrl = buildHttpUrl(pip, httpPort);
  const biz = buildHttpUrl(pip, httpPort, '/api');
  const vscodeUrl = vscodePort != null ? buildHttpUrl(pip, vscodePort, '/') : '';

  const body = {
    access_token: token,
    public_ip: pip,
    server_url: serverUrl,
    business_api_endpoint: biz,
  };
  if (vscodeUrl) body.container_vscode_url = vscodeUrl;

  await postJson(
    `${prefix.replace(/\/$/, '')}/server-container-token/register-reachability/`,
    body,
    timeoutSec
  );
  console.log(
    `[onlineServiceJS] 已向 SaaS 注册可达地址 public_ip=${pip} server_url=${serverUrl}` +
      (vscodeUrl ? ` vscode=${vscodeUrl}` : '')
  );
  logOutbound(`reachability: registered public_ip=${pip} server_url=${serverUrl}`);
}
