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


def test_extract_git_repo_urls_supports_project_repos_git_repos() -> None:
    from onlineService.app.task_api_bootstrap import _extract_git_repo_urls

    payload = {
        "project_repos": [
            {
                "project_id": "p1",
                "project_name": "repo-1",
                "git_repos": [
                    "https://github.com/example/repo-1.git",
                    "https://github.com/example/repo-2.git",
                ],
            },
            {
                "project_id": "p2",
                "project_name": "repo-2",
                "git_repos": ["https://github.com/example/repo-3.git"],
            },
        ]
    }
    assert _extract_git_repo_urls(payload) == [
        "https://github.com/example/repo-1.git",
        "https://github.com/example/repo-2.git",
        "https://github.com/example/repo-3.git",
    ]


def test_extract_git_repo_urls_supports_task_parameters_git_repos() -> None:
    from onlineService.app.task_api_bootstrap import _extract_git_repo_urls

    payload = {
        "task": {
            "parameters": {
                "git_repos": [
                    "https://github.com/example/repo-a.git",
                    {"git_repo": "https://github.com/example/repo-b.git"},
                ]
            }
        }
    }
    assert _extract_git_repo_urls(payload) == [
        "https://github.com/example/repo-a.git",
        "https://github.com/example/repo-b.git",
    ]


def test_git_clone_remote_for_ssh_pem_https_github() -> None:
    from onlineService.app.task_api_bootstrap import _git_clone_remote_for_ssh_pem

    assert (
        _git_clone_remote_for_ssh_pem("https://github.com/ruandao/goPractice")
        == "git@github.com:ruandao/goPractice.git"
    )
    assert (
        _git_clone_remote_for_ssh_pem("https://github.com/ruandao/goPractice.git")
        == "git@github.com:ruandao/goPractice.git"
    )
    assert (
        _git_clone_remote_for_ssh_pem("https://www.github.com/org/repo/")
        == "git@github.com:org/repo.git"
    )


def test_git_clone_remote_for_ssh_pem_passthrough() -> None:
    from onlineService.app.task_api_bootstrap import _git_clone_remote_for_ssh_pem

    ssh = "git@github.com:foo/bar.git"
    assert _git_clone_remote_for_ssh_pem(ssh) == ssh
    assert _git_clone_remote_for_ssh_pem("http://github.com/foo/bar") == "http://github.com/foo/bar"
