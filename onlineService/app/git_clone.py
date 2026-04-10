"""Clone a remote Git repository into a new writable layer directory."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .layers import create_clone_layer, new_layer_id
from .paths import layers_root

PublishFn = Callable[[dict[str, Any]], Awaitable[None]]

_clone_log_lock = threading.Lock()
_clone_logs: dict[str, str] = {}

try:
    _MAX_CLONE_LOG_CHARS = max(10_000, int(os.environ.get("TRAE_CLONE_LOG_CAP", "2000000")))
except ValueError:
    _MAX_CLONE_LOG_CHARS = 2_000_000


async def clear_clone_layer_log(layer_id: str) -> None:
    with _clone_log_lock:
        _clone_logs.pop(layer_id, None)


async def get_clone_layer_log_text(layer_id: str) -> str:
    with _clone_log_lock:
        return _clone_logs.get(layer_id, "")


async def append_clone_layer_log(layer_id: str, text: str) -> None:
    if not text:
        return
    with _clone_log_lock:
        cur = _clone_logs.get(layer_id, "") + text
        if len(cur) > _MAX_CLONE_LOG_CHARS:
            cur = "\n…(日志过长，仅保留末尾)\n" + cur[-_MAX_CLONE_LOG_CHARS:]
        _clone_logs[layer_id] = cur


def _clone_title_url(u: str, max_len: int = 72) -> str:
    s = u.strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


_MAX_URL_LEN = 4096
_MAX_BRANCH_LEN = 256
_DEFAULT_TIMEOUT = int(os.environ.get("GIT_CLONE_TIMEOUT_SEC", "1800"))


def _max_clone_attempts() -> int:
    try:
        return max(1, int(os.environ.get("GIT_CLONE_MAX_RETRIES", "3")))
    except ValueError:
        return 3


# 大仓库 HTTPS 易出现 curl 18 / early EOF；增大 postBuffer、默认 HTTP/1.1 可缓解
_DEFAULT_POST_BUFFER = int(os.environ.get("GIT_HTTP_POST_BUFFER", "524288000"))


def _git_config_prefix() -> list[str]:
    """传给 ``git -c …`` 的选项，降低大仓库 HTTPS 克隆中途断开的概率。"""
    prefix: list[str] = ["git"]
    lo = 1_048_576
    hi = 2_147_483_648
    try:
        pb = _DEFAULT_POST_BUFFER
        if lo <= pb <= hi:
            prefix.extend(["-c", f"http.postBuffer={pb}"])
    except (TypeError, ValueError):
        pass
    # 空字符串表示不强制版本（需 HTTP/2 时可设 GIT_HTTP_VERSION=""）
    ver_raw = os.environ.get("GIT_HTTP_VERSION")
    ver = "HTTP/1.1" if ver_raw is None else ver_raw.strip()
    if ver:
        prefix.extend(["-c", f"http.version={ver}"])
    for key, env_key, default in (
        ("http.lowSpeedLimit", "GIT_HTTP_LOW_SPEED_LIMIT", "0"),
        ("http.lowSpeedTime", "GIT_HTTP_LOW_SPEED_TIME", "0"),
    ):
        raw = os.environ.get(env_key, default).strip()
        try:
            n = int(raw)
            if n >= 0:
                prefix.extend(["-c", f"{key}={n}"])
        except ValueError:
            prefix.extend(["-c", f"{key}={default}"])
    return prefix


async def _kill_git_proc(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(OSError):
        await proc.wait()


async def _drain_git_stdout(
    proc: asyncio.subprocess.Process,
    *,
    deadline: float,
    layer_id: str,
    publish: PublishFn | None,
    acc: list[str],
) -> None:
    """Read combined stdout/stderr until EOF, bounded by ``deadline`` (monotonic)."""
    assert proc.stdout is not None
    loop = asyncio.get_running_loop()
    last_publish_at = 0.0
    publish_interval_sec = 0.25
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError
        try:
            chunk = await asyncio.wait_for(
                proc.stdout.read(8192),
                timeout=max(remaining, 0.01),
            )
        except asyncio.TimeoutError:
            raise TimeoutError from None
        if not chunk:
            break
        text = chunk.decode(errors="replace")
        acc.append(text)
        if publish:
            await append_clone_layer_log(layer_id, text)
            now = loop.time()
            if (now - last_publish_at) >= publish_interval_sec:
                await publish(
                    {
                        "type": "repo_clone_delta",
                        "layer_id": layer_id,
                        "title": "克隆输出更新",
                    }
                )
                last_publish_at = now


def _looks_like_transient_fetch_error(output: str) -> bool:
    """HTTPS / LibreSSL 下偶发 SSL_ERROR_SYSCALL、握手被掐断等，与网络瞬断类似，应重试。"""
    needles = (
        "RPC failed",
        "curl 18",
        "Transferred a partial file",
        "early EOF",
        "unexpected disconnect",
        "invalid index-pack",
        "Connection reset",
        "Connection timed out",
        "Empty reply from server",
        "SSL_ERROR_SYSCALL",
        "LibreSSL SSL_connect",
        "OpenSSL SSL_read",
        "OpenSSL SSL_connect",
        "SSL routines:",
        "gnutls_handshake",
        "Could not resolve host",
    )
    return any(n in output for n in needles)


def _retry_backoff_sec(attempt_index: int) -> float:
    """第 attempt_index 次失败后、下一次尝试前的等待秒数（指数退避，上限可配）。"""
    try:
        cap = float(os.environ.get("GIT_CLONE_RETRY_BACKOFF_CAP_SEC", "30"))
    except ValueError:
        cap = 30.0
    try:
        base = float(os.environ.get("GIT_CLONE_RETRY_BACKOFF_BASE_SEC", "1.5"))
    except ValueError:
        base = 1.5
    delay = base * (2**attempt_index)
    return min(delay, max(0.0, cap))


async def _sleep_before_retry(attempt_index: int) -> None:
    sec = _retry_backoff_sec(attempt_index)
    if sec > 0:
        await asyncio.sleep(sec)


# Git 默认识别为 40 位 SHA-1；启用 sha256 对象格式时为 64 位十六进制
_SHA_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$", re.IGNORECASE)


def _git_verify_timeout_sec() -> float:
    raw = os.environ.get("GIT_CLONE_VERIFY_TIMEOUT_SEC", "").strip()
    if not raw:
        return 120.0
    try:
        return max(5.0, float(raw))
    except ValueError:
        return 120.0


def verify_git_clone_workspace(
    repo_root: Path,
    *,
    env: dict[str, str] | None = None,
) -> str | None:
    """确认目录为 Git 工作区且 HEAD 指向有效提交（下载/对象库完整性的基本验证）。

    若环境变量 ``GIT_CLONE_SKIP_POST_VERIFY`` 为真则跳过并返回 ``None``。
    否则返回 ``HEAD`` 的完整对象名（十六进制小写，常见为 40 位 SHA-1）。
    """
    flag = os.environ.get("GIT_CLONE_SKIP_POST_VERIFY", "").strip().lower()
    if flag in ("1", "true", "yes"):
        return None
    rr = repo_root.resolve()
    if not rr.is_dir():
        raise RuntimeError(f"克隆目录不存在或不是目录: {rr}")
    e = dict(env or os.environ)
    e.setdefault("GIT_TERMINAL_PROMPT", "0")
    timeout = _git_verify_timeout_sec()

    def _run_git(args: list[str]) -> str:
        r = subprocess.run(
            ["git", "-C", str(rr), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=e,
        )
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()
            raise RuntimeError(f"git {' '.join(args)} 失败 (exit={r.returncode}): {err}")
        return (r.stdout or "").strip()

    inside = _run_git(["rev-parse", "--is-inside-work-tree"])
    if inside != "true":
        raise RuntimeError(f"非 Git 工作区 (rev-parse --is-inside-work-tree → {inside!r})")
    head = _run_git(["rev-parse", "--verify", "HEAD"])
    if not _SHA_RE.match(head):
        raise RuntimeError(f"HEAD 解析结果异常（非预期对象名格式）: {head!r}")
    kind = _run_git(["cat-file", "-t", "HEAD"])
    if kind != "commit":
        raise RuntimeError(f"HEAD 不是 commit 对象（cat-file -t → {kind!r}）")
    return head.lower()


def _clone_subprocess_env() -> dict[str, str]:
    """子进程环境：默认继承；可选去掉代理（部分环境 HTTPS 代理会导致 GitHub SSL 握手失败）。"""
    e = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    flag = os.environ.get("GIT_CLONE_UNSET_PROXY", "").strip().lower()
    if flag in ("1", "true", "yes"):
        for k in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            e.pop(k, None)
    return e


def _make_ipv4_curl_config_file() -> Path | None:
    """返回含 ``ipv4`` 的 curl 配置文件路径，供 ``git -c http.curlConfig=…`` 使用；调用方负责删除。"""
    flag = os.environ.get("GIT_HTTP_IPV4", "").strip().lower()
    if flag not in ("1", "true", "yes"):
        return None
    fd, path = tempfile.mkstemp(prefix="git-curl-ipv4-", suffix=".cfg", text=True)
    try:
        with os.fdopen(fd, "w") as f:
            f.write("ipv4\n")
    except OSError:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise
    return Path(path)


def _validate_url(url: str) -> str:
    u = url.strip()
    if not u or len(u) > _MAX_URL_LEN:
        raise ValueError("invalid or empty url")
    if u.startswith("-") or "\n" in u or "\0" in u:
        raise ValueError("invalid url")
    low = u.lower()
    if low.startswith("file:") or low.startswith("ext::") or low.startswith("ssh://-"):
        raise ValueError("unsupported url")
    if u.startswith(("https://", "http://", "git://", "ssh://")):
        p = urlparse(u)
        if not p.netloc and u.startswith(("http://", "https://")):
            raise ValueError("invalid http(s) url")
        return u
    if u.startswith("git@") and ":" in u[4:]:
        return u
    raise ValueError("unsupported url; use https, http, git, ssh, or git@host:path")


def _validate_branch(branch: str | None) -> str | None:
    if branch is None:
        return None
    b = branch.strip()
    if not b:
        return None
    if len(b) > _MAX_BRANCH_LEN:
        raise ValueError("branch name too long")
    if ".." in b or "\n" in b or "\0" in b or b.startswith("/"):
        raise ValueError("invalid branch name")
    return b


def _validate_depth(depth: int | None) -> int | None:
    if depth is None:
        return None
    if depth < 1 or depth > 10_000:
        raise ValueError("depth must be between 1 and 10000")
    return depth


async def clone_into_new_layer(
    url: str,
    *,
    branch: str | None = None,
    depth: int | None = None,
    timeout_sec: int | None = None,
    publish: PublishFn | None = None,
) -> tuple[str, Path, str, int]:
    """Create a new layer and run ``git clone`` into it.

    If ``publish`` is set, appends to an in-memory log and emits lightweight
    ``{"type": "repo_clone_delta", "layer_id", "title"}`` for UI polling.
    Also emits lightweight ``repo_clone_started`` / ``repo_clone_retry`` / ``repo_clone_delta`` when applicable.

    Returns ``(layer_id, resolved_layer_path, combined_output, exit_code)``.
    On non-zero exit, the layer directory is removed if it exists under ``layers_root``.
    """
    if shutil.which("git") is None:
        raise RuntimeError("git executable not found on PATH")

    u = _validate_url(url)
    br = _validate_branch(branch)
    dep = _validate_depth(depth)
    tout = timeout_sec if timeout_sec is not None else _DEFAULT_TIMEOUT
    max_attempts = _max_clone_attempts()

    root = layers_root().resolve()
    cfg = _git_config_prefix()

    for attempt in range(max_attempts):
        ipv4_curl_cfg: Path | None = None
        try:
            ipv4_curl_cfg = _make_ipv4_curl_config_file()
            layer_id = new_layer_id()
            lp = create_clone_layer(layer_id)
            try:
                lp_resolved = lp.resolve()
                if root not in lp_resolved.parents and lp_resolved != root:
                    raise RuntimeError("layer path outside layers root")
            except Exception:
                _safe_rmtree(lp)
                raise

            cmd: list[str] = list(cfg)
            if ipv4_curl_cfg is not None:
                cmd.extend(["-c", f"http.curlConfig={ipv4_curl_cfg}"])
            cmd.extend(["clone", "--progress"])
            if dep is not None:
                cmd.extend(["--depth", str(dep)])
            if br is not None:
                cmd.extend(["-b", br])
            cmd.extend([u, "."])

            if publish:
                await clear_clone_layer_log(layer_id)
                await publish(
                    {
                        "type": "repo_clone_started",
                        "layer_id": layer_id,
                        "title": "开始克隆 ({}/{}) · {}".format(
                            attempt + 1, max_attempts, _clone_title_url(u)
                        ),
                    }
                )

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(lp / "base"),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=_clone_subprocess_env(),
            )
            loop = asyncio.get_running_loop()
            deadline = loop.time() + float(tout)
            acc: list[str] = []
            try:
                await _drain_git_stdout(
                    proc, deadline=deadline, layer_id=layer_id, publish=publish, acc=acc
                )
            except TimeoutError:
                await _kill_git_proc(proc)
                out = "".join(acc) + "\n[git clone timed out]\n"
                if publish:
                    await append_clone_layer_log(layer_id, "\n[git clone timed out]\n")
                    await publish(
                        {
                            "type": "repo_clone_delta",
                            "layer_id": layer_id,
                            "title": "克隆超时",
                        }
                    )
                _safe_rmtree_if_under(lp, root)
                if attempt < max_attempts - 1:
                    if publish:
                        await publish(
                            {
                                "type": "repo_clone_retry",
                                "layer_id": layer_id,
                                "title": f"克隆超时，重试 {attempt + 2}/{max_attempts} …",
                            }
                        )
                    await _sleep_before_retry(attempt)
                    continue
                return layer_id, lp, out, -1

            wleft = deadline - loop.time()
            if wleft <= 0:
                await _kill_git_proc(proc)
                out = "".join(acc) + "\n[git clone timed out]\n"
                if publish:
                    await append_clone_layer_log(layer_id, "\n[git clone timed out]\n")
                    await publish(
                        {
                            "type": "repo_clone_delta",
                            "layer_id": layer_id,
                            "title": "克隆超时",
                        }
                    )
                _safe_rmtree_if_under(lp, root)
                if attempt < max_attempts - 1:
                    if publish:
                        await publish(
                            {
                                "type": "repo_clone_retry",
                                "layer_id": layer_id,
                                "title": f"克隆超时，重试 {attempt + 2}/{max_attempts} …",
                            }
                        )
                    await _sleep_before_retry(attempt)
                    continue
                return layer_id, lp, out, -1

            try:
                exit_code = await asyncio.wait_for(proc.wait(), timeout=wleft)
            except TimeoutError:
                await _kill_git_proc(proc)
                out = "".join(acc) + "\n[git clone: process wait timed out]\n"
                if publish:
                    await append_clone_layer_log(
                        layer_id, "\n[git clone: process wait timed out]\n"
                    )
                    await publish(
                        {
                            "type": "repo_clone_delta",
                            "layer_id": layer_id,
                            "title": "克隆进程等待超时",
                        }
                    )
                _safe_rmtree_if_under(lp, root)
                if attempt < max_attempts - 1:
                    if publish:
                        await publish(
                            {
                                "type": "repo_clone_retry",
                                "layer_id": layer_id,
                                "title": f"等待进程超时，重试 {attempt + 2}/{max_attempts} …",
                            }
                        )
                    await _sleep_before_retry(attempt)
                    continue
                return layer_id, lp, out, -1

            code = exit_code if isinstance(exit_code, int) else -1
            out = "".join(acc)

            if code == 0:
                venv = _clone_subprocess_env()
                try:
                    head_sha = await asyncio.to_thread(
                        verify_git_clone_workspace,
                        lp / "base",
                        env=venv,
                    )
                except RuntimeError as ver_e:
                    out = out + f"\n[clone-verify] {ver_e}\n"
                    _safe_rmtree_if_under(lp, root)
                    if attempt < max_attempts - 1:
                        if publish:
                            await append_clone_layer_log(
                                layer_id,
                                f"\n[克隆后校验失败，将重试] {ver_e}\n",
                            )
                            await publish(
                                {
                                    "type": "repo_clone_retry",
                                    "layer_id": layer_id,
                                    "title": f"校验失败，重试 {attempt + 2}/{max_attempts} …",
                                }
                            )
                        await _sleep_before_retry(attempt)
                        continue
                    return layer_id, lp, out, -1
                if head_sha and publish:
                    await append_clone_layer_log(
                        layer_id,
                        f"\n[校验] 拉取验证通过 HEAD={head_sha[:12]}\n",
                    )
                return layer_id, lp, out, code

            _safe_rmtree_if_under(lp, root)
            if attempt < max_attempts - 1 and _looks_like_transient_fetch_error(out):
                if publish:
                    await publish(
                        {
                            "type": "repo_clone_retry",
                            "layer_id": layer_id,
                            "title": f"网络错误，重试 {attempt + 2}/{max_attempts} …",
                        }
                    )
                await _sleep_before_retry(attempt)
                continue
            return layer_id, lp, out, code
        finally:
            if ipv4_curl_cfg is not None:
                with contextlib.suppress(OSError):
                    ipv4_curl_cfg.unlink(missing_ok=True)


def _safe_rmtree(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _safe_rmtree_if_under(path: Path, layers_root_resolved: Path) -> None:
    try:
        resolved = path.resolve()
        if layers_root_resolved in resolved.parents or resolved == layers_root_resolved:
            _safe_rmtree(path)
    except OSError:
        pass
