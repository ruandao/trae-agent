import fs from 'fs';
import path from 'path';
import { spawn } from 'node:child_process';

import { appendCloneLayerLog } from './bootstrap.mjs';
import { completeExecStream } from './execStream.mjs';
import { broadcast } from './sseHub.mjs';

/** @type {Map<string, { status: string, queue_position?: number, detail?: string }>} */
const opState = new Map();

/** @type {object[]} */
const queue = [];
let draining = false;

function cleanupEphemeral(dir) {
  if (!dir) return;
  try {
    fs.rmSync(dir, { recursive: true, force: true });
  } catch {
    /* ignore */
  }
}

function renumberQueued() {
  for (let i = 0; i < queue.length; i++) {
    const t = queue[i];
    opState.set(t.lid, { status: 'queued', queue_position: i });
  }
}

export function getCloneOpStatus(layerId) {
  return opState.get(layerId) || null;
}

/**
 * @param {object} task
 * @param {string} task.lid
 * @param {string} task.root
 * @param {string} task.cloneCwd
 * @param {string | null} task.parentLayerId
 * @param {string[]} task.gitArgs
 * @param {NodeJS.ProcessEnv} task.env
 * @param {string | null} task.ephemeralKeyDir
 * @param {string} [task.titleUrl]
 * @returns {number} queue_position 0-based
 */
export function enqueueClone(task) {
  queue.push(task);
  renumberQueued();
  const pos = opState.get(task.lid).queue_position;
  void drainLoop();
  return pos;
}

async function drainLoop() {
  if (draining) return;
  draining = true;
  try {
    while (queue.length > 0) {
      const next = queue.shift();
      renumberQueued();
      await executeCloneTask(next);
    }
  } finally {
    draining = false;
    if (queue.length > 0) {
      void drainLoop();
    }
  }
}

function executeCloneTask(task) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (inner) => {
      if (settled) return;
      settled = true;
      inner();
      resolve();
    };

    const { lid, root, cloneCwd, parentLayerId, gitArgs, env, ephemeralKeyDir, titleUrl } = task;

    opState.set(lid, { status: 'running' });
    broadcast({
      type: 'repo_clone_started',
      layer_id: lid,
      title: `开始克隆 · ${titleUrl || 'repository'}`,
    });
    appendCloneLayerLog(lid, `[clone] ${gitArgs.join(' ')}\n`);

    const proc = spawn('git', gitArgs, {
      cwd: cloneCwd,
      env: { ...process.env, ...env, GIT_TERMINAL_PROMPT: '0' },
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    let outAcc = '';
    const onData = (chunk) => {
      const s = chunk.toString();
      outAcc += s;
      appendCloneLayerLog(lid, s);
    };
    proc.stdout?.on('data', onData);
    proc.stderr?.on('data', onData);

    proc.on('error', (e) => {
      finish(() => {
        cleanupEphemeral(ephemeralKeyDir);
        try {
          fs.rmSync(root, { recursive: true, force: true });
        } catch {
          /* ignore */
        }
        const msg = e.message || String(e);
        opState.set(lid, { status: 'failed', detail: msg });
        appendCloneLayerLog(lid, `\n[错误] ${msg}\n`);
        completeExecStream('clone', lid);
        broadcast({
          type: 'repo_clone_finished',
          layer_id: lid,
          title: `克隆失败: ${msg}`,
          status: 'error',
        });
      });
    });

    proc.on('close', (code) => {
      finish(() => {
        cleanupEphemeral(ephemeralKeyDir);
        if (code === 0) {
          try {
            const metaPayload = {
              version: 1,
              kind: 'clone',
              parent_layer_id: parentLayerId || null,
            };
            if (titleUrl) metaPayload.clone_url = String(titleUrl).trim();
            fs.writeFileSync(
              path.join(root, 'layer_meta.json'),
              JSON.stringify(metaPayload, null, 2),
              'utf8'
            );
          } catch {
            /* ignore */
          }
          opState.set(lid, { status: 'completed' });
          completeExecStream('clone', lid);
          broadcast({ type: 'repo_clone_finished', layer_id: lid, title: '克隆成功', status: 'ok' });
          broadcast({ type: 'repo_cloned', layer_id: lid, title: '仓库已就绪' });
          return;
        }
        const tail = (outAcc || `git exit ${code}`).slice(-4000);
        try {
          fs.rmSync(root, { recursive: true, force: true });
        } catch {
          /* ignore */
        }
        opState.set(lid, { status: 'failed', detail: tail });
        appendCloneLayerLog(lid, `\n[错误] git exit ${code}\n${tail}\n`);
        completeExecStream('clone', lid);
        broadcast({
          type: 'repo_clone_finished',
          layer_id: lid,
          title: `克隆失败 (exit ${code})`,
          status: 'error',
        });
      });
    });
  });
}
