"""task_api_bootstrap：host.docker.internal 仅在设置 DOCKER_HOST_GATEWAY_IP 时换为 IP。"""

from __future__ import annotations

from trae_agent_online.task_cloud_bootstrap import (
    extract_git_repo_urls,
    git_clone_remote_for_ssh_pem,
    rewrite_host_docker_internal_url,
)


def test_rewrite_host_docker_internal_noop() -> None:
    assert (
        rewrite_host_docker_internal_url("http://127.0.0.1:8765/api") == "http://127.0.0.1:8765/api"
    )


def test_rewrite_host_docker_internal_unchanged_without_gateway_env(monkeypatch) -> None:
    monkeypatch.delenv("DOCKER_HOST_GATEWAY_IP", raising=False)
    assert (
        rewrite_host_docker_internal_url("http://host.docker.internal:8765/api/foo")
        == "http://host.docker.internal:8765/api/foo"
    )


def test_rewrite_host_docker_internal_with_env_ip(monkeypatch) -> None:
    monkeypatch.setenv("DOCKER_HOST_GATEWAY_IP", "192.168.65.254")
    assert (
        rewrite_host_docker_internal_url("http://host.docker.internal:8765/api/foo")
        == "http://192.168.65.254:8765/api/foo"
    )


def test_rewrite_host_docker_internal_preserves_userinfo(monkeypatch) -> None:
    monkeypatch.setenv("DOCKER_HOST_GATEWAY_IP", "10.0.0.2")
    assert (
        rewrite_host_docker_internal_url("http://u:p@host.docker.internal:99/x")
        == "http://u:p@10.0.0.2:99/x"
    )


def test_extract_git_repo_urls_supports_project_repos_git_repos() -> None:
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
    assert extract_git_repo_urls(payload) == [
        "https://github.com/example/repo-1.git",
        "https://github.com/example/repo-2.git",
        "https://github.com/example/repo-3.git",
    ]


def test_extract_git_repo_urls_supports_task_parameters_git_repos() -> None:
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
    assert extract_git_repo_urls(payload) == [
        "https://github.com/example/repo-a.git",
        "https://github.com/example/repo-b.git",
    ]


def test_git_clone_remote_for_ssh_pem_https_github() -> None:
    assert (
        git_clone_remote_for_ssh_pem("https://github.com/ruandao/goPractice")
        == "git@github.com:ruandao/goPractice.git"
    )
    assert (
        git_clone_remote_for_ssh_pem("https://github.com/ruandao/goPractice.git")
        == "git@github.com:ruandao/goPractice.git"
    )
    assert (
        git_clone_remote_for_ssh_pem("https://www.github.com/org/repo/")
        == "git@github.com:org/repo.git"
    )


def test_git_clone_remote_for_ssh_pem_passthrough() -> None:
    ssh = "git@github.com:foo/bar.git"
    assert git_clone_remote_for_ssh_pem(ssh) == ssh
    assert git_clone_remote_for_ssh_pem("http://github.com/foo/bar") == "http://github.com/foo/bar"
