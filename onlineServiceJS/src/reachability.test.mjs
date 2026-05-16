// @ts-check
import { test } from 'node:test';
import assert from 'node:assert';

import { reachabilityFromBusinessEndpointEnv } from './reachability.mjs';

const KEYS = [
  'BusinessApiEndPoint',
  'BUSINESS_API_ENDPOINT',
  'DOCKER_GATEWAY_HOSTNAME',
  'DOCKER_HOST_GATEWAY_IP',
  'PORT',
  'TRAE_HOST_HTTP_PORT',
];

function snapshotEnv(keys) {
  const out = {};
  for (const k of keys) out[k] = process.env[k];
  return out;
}

function restoreEnv(saved) {
  for (const k of Object.keys(saved)) {
    const v = saved[k];
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
}

test('reachabilityFromBusinessEndpointEnv：域名 BUSINESS_API_ENDPOINT 直接用于注册', () => {
  const saved = snapshotEnv(KEYS);
  try {
    process.env.BUSINESS_API_ENDPOINT = 'https://3009-47-86-27-42.ngrok-free.app/api';
    delete process.env.BusinessApiEndPoint;
    const got = reachabilityFromBusinessEndpointEnv();
    assert.deepStrictEqual(got, {
      businessApiEndpoint: 'https://3009-47-86-27-42.ngrok-free.app/api',
      serverUrl: 'https://3009-47-86-27-42.ngrok-free.app',
      publicIp: null,
    });
  } finally {
    restoreEnv(saved);
  }
});

test('reachabilityFromBusinessEndpointEnv：IP BUSINESS_API_ENDPOINT 可提取 public_ip', () => {
  const saved = snapshotEnv(KEYS);
  try {
    process.env.BusinessApiEndPoint = 'http://203.0.113.8:8765/api';
    delete process.env.BUSINESS_API_ENDPOINT;
    const got = reachabilityFromBusinessEndpointEnv();
    assert.deepStrictEqual(got, {
      businessApiEndpoint: 'http://203.0.113.8:8765/api',
      serverUrl: 'http://203.0.113.8:8765',
      publicIp: '203.0.113.8',
    });
  } finally {
    restoreEnv(saved);
  }
});

test('reachabilityFromBusinessEndpointEnv：IP 无显式端口时补 hostMappedHttpPort（与换票规范化一致）', () => {
  const saved = snapshotEnv(KEYS);
  try {
    delete process.env.TRAE_HOST_HTTP_PORT;
    process.env.PORT = '37521';
    process.env.BUSINESS_API_ENDPOINT = 'http://203.0.113.9/api';
    delete process.env.BusinessApiEndPoint;
    const got = reachabilityFromBusinessEndpointEnv();
    assert.deepStrictEqual(got, {
      businessApiEndpoint: 'http://203.0.113.9:37521/api',
      serverUrl: 'http://203.0.113.9:37521',
      publicIp: '203.0.113.9',
    });
  } finally {
    restoreEnv(saved);
  }
});

test('reachabilityFromBusinessEndpointEnv：域名仍写 :8765 且 TRAE_HOST_HTTP_PORT 为映射口时改用映射口', () => {
  const saved = snapshotEnv(KEYS);
  try {
    process.env.TRAE_HOST_HTTP_PORT = '49152';
    process.env.PORT = '8765';
    process.env.BUSINESS_API_ENDPOINT = 'http://debug.aidevpm.com:8765/api';
    delete process.env.BusinessApiEndPoint;
    const got = reachabilityFromBusinessEndpointEnv();
    assert.deepStrictEqual(got, {
      businessApiEndpoint: 'http://debug.aidevpm.com:49152/api',
      serverUrl: 'http://debug.aidevpm.com:49152',
      publicIp: null,
    });
  } finally {
    restoreEnv(saved);
  }
});
