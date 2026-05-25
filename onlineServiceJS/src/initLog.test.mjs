// @ts-check
import { test } from 'node:test';
import assert from 'node:assert';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import {
  buildInitLogEnvSnapshot,
  buildInitLogRecord,
  appendInitLogBestEffort,
} from './initLog.mjs';

test('默认全量采集环境变量：空键忽略，值转字符串', () => {
  const envMapping = {
    A: 123,
    B: false,
    C: null,
    D: undefined,
    '': 'should-be-ignored',
    '   ': 'also-ignored',
  };
  const got = buildInitLogEnvSnapshot(envMapping, undefined);

  assert.deepStrictEqual(got, {
    A: '123',
    B: 'false',
    C: 'null',
    D: 'undefined',
  });
});

test('配置 INIT_LOG_ENV_KEYS 时仅保留白名单键（逗号分隔并 trim）', () => {
  const envMapping = {
    A: 'va',
    B: 2,
    C: true,
    D: 'drop',
    '': 'drop-empty',
  };
  const got = buildInitLogEnvSnapshot(envMapping, ' A, B , , C  ');

  assert.deepStrictEqual(got, {
    A: 'va',
    B: '2',
    C: 'true',
  });
});

test('buildInitLogRecord 生成 JSON 行格式，包含 ts/event/pid/port/env', () => {
  const now = new Date('2026-05-25T00:00:00.000Z');
  const line = buildInitLogRecord({
    pid: 321,
    port: 8787,
    envMapping: { A: 1 },
    now,
    rawPolicy: '',
  });

  assert.ok(typeof line === 'string');
  assert.ok(line.endsWith('\n'));

  const payload = JSON.parse(line);
  assert.deepStrictEqual(payload, {
    ts: now.toISOString(),
    event: 'onlineServiceJS.init',
    pid: 321,
    port: '8787',
    env: { A: '1' },
  });
});

test('appendInitLogBestEffort 追加写入 ONLINE_PROJECT_STATE_ROOT/logs/init.log', () => {
  const prevRoot = process.env.ONLINE_PROJECT_STATE_ROOT;
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'online-service-initlog-'));
  process.env.ONLINE_PROJECT_STATE_ROOT = tempRoot;

  try {
    appendInitLogBestEffort({
      pid: 7,
      port: 9000,
      envMapping: { K: 'v' },
      now: new Date('2026-05-25T00:00:01.000Z'),
      rawPolicy: '',
    });
    appendInitLogBestEffort({
      pid: 7,
      port: 9001,
      envMapping: { K: 'v2' },
      now: new Date('2026-05-25T00:00:02.000Z'),
      rawPolicy: '',
    });

    const filePath = path.join(tempRoot, 'logs', 'init.log');
    const content = fs.readFileSync(filePath, 'utf8');
    const lines = content.trimEnd().split('\n');

    assert.strictEqual(lines.length, 2);
    assert.deepStrictEqual(JSON.parse(lines[0]), {
      ts: '2026-05-25T00:00:01.000Z',
      event: 'onlineServiceJS.init',
      pid: 7,
      port: '9000',
      env: { K: 'v' },
    });
    assert.deepStrictEqual(JSON.parse(lines[1]), {
      ts: '2026-05-25T00:00:02.000Z',
      event: 'onlineServiceJS.init',
      pid: 7,
      port: '9001',
      env: { K: 'v2' },
    });
  } finally {
    if (prevRoot === undefined) delete process.env.ONLINE_PROJECT_STATE_ROOT;
    else process.env.ONLINE_PROJECT_STATE_ROOT = prevRoot;

    fs.rmSync(tempRoot, { recursive: true, force: true });
  }
});

test('appendInitLogBestEffort 写入失败时返回错误状态', () => {
  const result = appendInitLogBestEffort(
    {
      pid: 7,
      port: 9000,
      envMapping: { K: 'v' },
      now: new Date('2026-05-25T00:00:01.000Z'),
      rawPolicy: '',
    },
    {
      writeFile: () => {
        throw new Error('disk full');
      },
    }
  );
  assert.equal(result.ok, false);
  assert.match(String(result.error), /disk full/);
});
