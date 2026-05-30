"""task_llm_budget 预检单元测试。"""

import json

import pytest

from trae_agent.utils.task_llm_budget import (
    TaskLlmBudgetExhaustedError,
    assert_llm_budget_available,
    find_budget_entry,
)


@pytest.fixture
def budget_policy_env(monkeypatch):
    policy = {
        "currency": "CNY",
        "models": [
            {
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4.1",
                "budget_limit": "50.00",
                "spent_amount": "12.30",
            }
        ],
    }
    monkeypatch.setenv("TASK_LLM_BUDGET_POLICY", json.dumps(policy))
    return policy


def test_find_budget_entry_matches_model(budget_policy_env):
    entry = find_budget_entry(
        provider="openai",
        base_url="https://api.openai.com/v1/",
        model="gpt-4.1",
    )
    assert entry is not None
    assert str(entry.budget_limit) == "50.00"
    assert entry.exhausted is False


def test_assert_llm_budget_available_raises_when_exhausted(monkeypatch):
    policy = {
        "currency": "CNY",
        "models": [
            {
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4.1",
                "budget_limit": "10",
                "spent_amount": "10",
            }
        ],
    }
    monkeypatch.setenv("TASK_LLM_BUDGET_POLICY", json.dumps(policy))
    with pytest.raises(TaskLlmBudgetExhaustedError):
        assert_llm_budget_available(
            provider="openai",
            base_url="https://api.openai.com/v1",
            model="gpt-4.1",
        )


def test_no_policy_is_noop(monkeypatch):
    monkeypatch.delenv("TASK_LLM_BUDGET_POLICY", raising=False)
    assert_llm_budget_available(
        provider="openai", base_url="https://api.openai.com/v1", model="gpt-4.1"
    )
