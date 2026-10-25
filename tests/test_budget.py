"""Tests for Wheatley Budget & Usage Shim (Verifying No Billing Caps or Warnings)"""

from __future__ import annotations

import pytest
from wheatley.audit import AuditLogger
from wheatley.budget import BudgetTracker
from wheatley.config import BudgetConfig


@pytest.fixture
def budget_tracker(tmp_path):
    db_path = str(tmp_path / "test_budget.db")
    audit = AuditLogger(db_path)
    cfg = BudgetConfig(model_flash="gemini-3.8-flash")
    return BudgetTracker(cfg, audit)


def test_calculate_cost_noop(budget_tracker):
    # Cost calculations are deprecated and return 0.0
    cost = budget_tracker.calculate_cost(1_000_000, 1_000_000)
    assert cost == 0.0


def test_record_usage_and_tokens(budget_tracker):
    assert budget_tracker.get_daily_tokens() == (0, 0)
    assert budget_tracker.get_monthly_tokens() == (0, 0)

    budget_tracker.record_usage(
        model="gemini-3.8-flash",
        input_tokens=100_000,
        output_tokens=10_000,
        context="test_query",
    )

    inp, outp = budget_tracker.get_monthly_tokens()
    assert inp == 100_000
    assert outp == 10_000


def test_no_daily_cap_guardrail(budget_tracker):
    can_call, reason = budget_tracker.can_call_llm()
    assert can_call is True
    assert reason == "OK"

    # Even with massive usage, can_call_llm remains True (no billing pauses)
    budget_tracker.record_usage(
        model="gemini-3.8-flash",
        input_tokens=100_000_000,
        output_tokens=10_000_000,
        context="heavy_query",
    )

    can_call, reason = budget_tracker.can_call_llm()
    assert can_call is True
    assert reason == "OK"


def test_format_usage_markdown(budget_tracker):
    summary = budget_tracker.format_usage_markdown(provider="gemini", model="gemini-3.8-flash")
    assert "LLM Usage & Activity" in summary
    assert "gemini-3.8-flash" in summary
    assert "no artificial billing caps" in summary
    assert "PAUSED" not in summary
    assert "WARNING" not in summary
