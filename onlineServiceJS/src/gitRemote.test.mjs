// @ts-check
import { test } from 'node:test';
import assert from 'node:assert';
import { gitSshFromHttps, gitPushRemoteArgFromOrigin } from './gitRemote.mjs';

test('gitSshFromHttps: github https 转 ssh', () => {
  assert.equal(gitSshFromHttps('https://github.com/AAAA/BBBB'), 'git@github.com:AAAA/BBBB.git');
  assert.equal(gitSshFromHttps('https://github.com/AAAA/BBBB.git'), 'git@github.com:AAAA/BBBB.git');
  assert.equal(gitSshFromHttps('https://www.github.com/AAAA/BBBB'), 'git@github.com:AAAA/BBBB.git');
});

test('gitPushRemoteArgFromOrigin: github https 优先 ssh remote', () => {
  assert.equal(
    gitPushRemoteArgFromOrigin('https://github.com/AAAA/BBBB.git'),
    'git@github.com:AAAA/BBBB.git',
  );
  assert.equal(
    gitPushRemoteArgFromOrigin('https://my-user@github.com/AAAA/BBBB'),
    'git@github.com:AAAA/BBBB.git',
  );
});

test('gitPushRemoteArgFromOrigin: github ssh 输入保持可推送', () => {
  assert.equal(gitPushRemoteArgFromOrigin('git@github.com:AAAA/BBBB.git'), 'git@github.com:AAAA/BBBB.git');
  assert.equal(
    gitPushRemoteArgFromOrigin('ssh://git@github.com/AAAA/BBBB.git'),
    'git@github.com:AAAA/BBBB.git',
  );
});

test('gitPushRemoteArgFromOrigin: 非 github 维持 origin', () => {
  assert.equal(gitPushRemoteArgFromOrigin('https://gitlab.com/AAAA/BBBB.git'), 'origin');
  assert.equal(gitPushRemoteArgFromOrigin(''), 'origin');
});
