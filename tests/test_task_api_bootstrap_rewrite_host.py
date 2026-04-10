"""task_api_bootstrap：host.docker.internal 仅在设置 DOCKER_HOST_GATEWAY_IP 时换为 IP。"""

from __future__ import annotations


def test_rewrite_host_docker_internal_noop() -> None:
    from onlineService.app.task_api_bootstrap import _rewrite_host_docker_internal_url

    assert (
        _rewrite_host_docker_internal_url("http://127.0.0.1:8765/api")
        == "http://127.0.0.1:8765/api"
    )


def test_rewrite_host_docker_internal_unchanged_without_gateway_env(monkeypatch) -> None:
    from onlineService.app.task_api_bootstrap import _rewrite_host_docker_internal_url

    monkeypatch.delenv("DOCKER_HOST_GATEWAY_IP", raising=False)
    assert (
        _rewrite_host_docker_internal_url("http://host.docker.internal:8765/api/foo")
        == "http://host.docker.internal:8765/api/foo"
    )


def test_rewrite_host_docker_internal_with_env_ip(monkeypatch) -> None:
    from onlineService.app.task_api_bootstrap import _rewrite_host_docker_internal_url

    monkeypatch.setenv("DOCKER_HOST_GATEWAY_IP", "192.168.65.254")
    assert (
        _rewrite_host_docker_internal_url("http://host.docker.internal:8765/api/foo")
        == "http://192.168.65.254:8765/api/foo"
    )


def test_rewrite_host_docker_internal_preserves_userinfo(monkeypatch) -> None:
    from onlineService.app.task_api_bootstrap import _rewrite_host_docker_internal_url

    monkeypatch.setenv("DOCKER_HOST_GATEWAY_IP", "10.0.0.2")
    assert (
        _rewrite_host_docker_internal_url("http://u:p@host.docker.internal:99/x")
        == "http://u:p@10.0.0.2:99/x"
    )
