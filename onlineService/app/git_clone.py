"""Clone a remote Git repository into a new writable layer directory."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .layers import create_root_layer, new_layer_id

PublishFn = Callable[[dict[str, Any]], Awaitable[None]]
from .paths import layers_root

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
    if ver_raw is None:
        ver = "HTTP/1.1"
    else:
        ver = ver_raw.strip()
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
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:
        await proc.wait()
    except OSError:
        pass


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
            await publish({"type": "repo_clone_output", "layer_id": layer_id, "chunk": text})


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
        try:
            os.close(fd)
        except OSError:
            pass
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

    If ``publish`` is set, streams chunks as ``{"type": "repo_clone_output", "layer_id", "chunk"}``.
    Also emits ``repo_clone_started`` / ``repo_clone_retry`` when applicable.

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
            lp = create_root_layer(layer_id)
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
                await publish(
                    {
                        "type": "repo_clone_started",
                        "layer_id": layer_id,
                        "attempt": attempt + 1,
                        "max_attempts": max_attempts,
                        "url": u,
                    }
                )

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(lp),
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
                    await publish(
                        {
                            "type": "repo_clone_output",
                            "layer_id": layer_id,
                            "chunk": "\n[git clone timed out]\n",
                        }
                    )
                _safe_rmtree_if_under(lp, root)
                if attempt < max_attempts - 1:
                    if publish:
                        await publish(
                            {
                                "type": "repo_clone_retry",
                                "layer_id": layer_id,
                                "attempt": attempt + 2,
                                "max_attempts": max_attempts,
                                "message": f"克隆超时，重试 {attempt + 2}/{max_attempts} …",
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
                    await publish(
                        {
                            "type": "repo_clone_output",
                            "layer_id": layer_id,
                            "chunk": "\n[git clone timed out]\n",
                        }
                    )
                _safe_rmtree_if_under(lp, root)
                if attempt < max_attempts - 1:
                    if publish:
                        await publish(
                            {
                                "type": "repo_clone_retry",
                                "layer_id": layer_id,
                                "attempt": attempt + 2,
                                "max_attempts": max_attempts,
                                "message": f"克隆超时，重试 {attempt + 2}/{max_attempts} …",
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
                    await publish(
                        {
                            "type": "repo_clone_output",
                            "layer_id": layer_id,
                            "chunk": "\n[git clone: process wait timed out]\n",
                        }
                    )
                _safe_rmtree_if_under(lp, root)
                if attempt < max_attempts - 1:
                    if publish:
                        await publish(
                            {
                                "type": "repo_clone_retry",
                                "layer_id": layer_id,
                                "attempt": attempt + 2,
                                "max_attempts": max_attempts,
                                "message": f"等待进程超时，重试 {attempt + 2}/{max_attempts} …",
                            }
                        )
                    await _sleep_before_retry(attempt)
                    continue
                return layer_id, lp, out, -1

            code = exit_code if isinstance(exit_code, int) else -1
            out = "".join(acc)

            if code == 0:
                return layer_id, lp, out, code

            _safe_rmtree_if_under(lp, root)
            if attempt < max_attempts - 1 and _looks_like_transient_fetch_error(out):
                if publish:
                    await publish(
                        {
                            "type": "repo_clone_retry",
                            "layer_id": layer_id,
                            "attempt": attempt + 2,
                            "max_attempts": max_attempts,
                            "message": f"网络错误，重试 {attempt + 2}/{max_attempts} …",
                        }
                    )
                await _sleep_before_retry(attempt)
                continue
            return layer_id, lp, out, code
        finally:
            if ipv4_curl_cfg is not None:
                try:
                    ipv4_curl_cfg.unlink(missing_ok=True)
                except OSError:
                    pass


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
