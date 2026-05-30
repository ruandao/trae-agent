"""任务 LLM 预算本地预检（读取 TASK_LLM_BUDGET_POLICY env）。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal


class TaskLlmBudgetExhaustedError(RuntimeError):
    """模型预算已用尽，拒绝新的 LLM 调用。"""


@dataclass(frozen=True)
class TaskLlmBudgetModelEntry:
    provider: str
    base_url: str
    model: str
    budget_limit: Decimal
    spent_amount: Decimal

    @property
    def exhausted(self) -> bool:
        if self.budget_limit <= 0:
            return False
        return self.spent_amount >= self.budget_limit


def _normalize_base_url(base_url: str) -> str:
    return str(base_url or "").strip().rstrip("/")


def load_task_llm_budget_policy() -> dict | None:
    raw = os.environ.get("TASK_LLM_BUDGET_POLICY", "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def find_budget_entry(
    *,
    provider: str,
    base_url: str | None,
    model: str,
) -> TaskLlmBudgetModelEntry | None:
    policy = load_task_llm_budget_policy()
    if not policy:
        return None
    models = policy.get("models")
    if not isinstance(models, list):
        return None
    normalized_base = _normalize_base_url(base_url or "")
    for item in models:
        if not isinstance(item, dict):
            continue
        if str(item.get("provider", "")).strip() != str(provider or "").strip():
            continue
        if _normalize_base_url(str(item.get("base_url", ""))) != normalized_base:
            continue
        if str(item.get("model", "")).strip() != str(model or "").strip():
            continue
        try:
            return TaskLlmBudgetModelEntry(
                provider=str(item.get("provider", "")).strip(),
                base_url=str(item.get("base_url", "")).strip(),
                model=str(item.get("model", "")).strip(),
                budget_limit=Decimal(str(item.get("budget_limit", "0"))),
                spent_amount=Decimal(str(item.get("spent_amount", "0"))),
            )
        except Exception:
            return None
    return None


def assert_llm_budget_available(
    *,
    provider: str,
    base_url: str | None,
    model: str,
) -> None:
    """LLM 调用前预检；无 policy 或未匹配模型时零开销放行。"""
    entry = find_budget_entry(provider=provider, base_url=base_url, model=model)
    if entry is None:
        return
    if entry.exhausted:
        raise TaskLlmBudgetExhaustedError(
            f"LLM 预算已用尽：{entry.provider}/{entry.model} "
            f"spent={entry.spent_amount} limit={entry.budget_limit} CNY"
        )
