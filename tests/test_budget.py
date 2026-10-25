"""Tests for Wheatley Budget Guardrails & Credit Ledger"""

from __future__ import annotations

import pytest
from wheatley.audit import AuditLogger
from wheatley.budget import BudgetTracker
from wheatley.config import BudgetConfig


@pytest.fixture
def budget_tracker(tmp_path):
    db_path = str(tmp_path / "test_budget.db")
    audit = AuditLogger(db_path)
    cfg = BudgetConfig(
        monthly_grant_usd=10.0,
        starting_bank_usd=0.0,
        warn_threshold_usd=6.0,
        pause_threshold_usd=9.50,
        daily_cap_usd=2.0,
        model_flash="gemini-3.8-flash",
        input_cost_per_million=0.15,
        output_cost_per_million=0.60,
    )
    return BudgetTracker(cfg, audit)


def test_calculate_cost(budget_tracker):
    # 1M input ($0.15) + 1M output ($0.60) = $0.75
    cost = budget_tracker.calculate_cost(1_000_000, 1_000_000)
    assert cost == 0.75

    # 10k input + 1k output
    cost_small = budget_tracker.calculate_cost(10_000, 1_000)
    # (10000 / 1e6 * 0.15) = 0.0015
    # (1000 / 1e6 * 0.60) = 0.0006
    # Total = 0.0021
    assert cost_small == 0.0021


def test_record_usage_and_spend(budget_tracker):
    assert budget_tracker.get_daily_spend() == 0.0
    assert budget_tracker.get_monthly_spend() == 0.0

    cost = budget_tracker.record_usage(
        model="gemini-3.8-flash",
        input_tokens=100_000,
        output_tokens=10_000,
        context="test_query",
    )
    assert cost > 0.0
    assert budget_tracker.get_daily_spend() == round(cost, 4)
    assert budget_tracker.get_monthly_spend() == round(cost, 4)

    inp, outp = budget_tracker.get_monthly_tokens()
    assert inp == 100_000
    assert outp == 10_000


def test_daily_cap_guardrail(budget_tracker):
    can_call, reason = budget_tracker.can_call_llm()
    assert can_call is True

    # Record usage exceeding daily cap ($2.00)
    # 2.0 / 0.75 * 1M = ~2.7M tokens
    budget_tracker.record_usage(
        model="gemini-3.8-flash",
        input_tokens=10_000_000,
        output_tokens=1_000_000,
        context="heavy_query",
    )
    assert budget_tracker.get_daily_spend() >= 2.0

    can_call, reason = budget_tracker.can_call_llm()
    assert can_call is False
    assert "Daily AI budget cap reached" in reason


def test_pause_threshold_guardrail(budget_tracker):
    # Set daily cap higher so pause threshold is tested
    budget_tracker.config.daily_cap_usd = 20.0
    budget_tracker.config.pause_threshold_usd = 5.0

    can_call, reason = budget_tracker.can_call_llm()
    assert can_call is True

    # Record spend over $5.00
    budget_tracker.record_usage(
        model="gemini-3.8-flash",
        input_tokens=40_000_000,
        output_tokens=2_000_000,
    )
    assert budget_tracker.get_monthly_spend() >= 5.0

    can_call, reason = budget_tracker.can_call_llm()
    assert can_call is False
    assert "Monthly AI budget limit reached" in reason


def test_carried_over_bank(budget_tracker):
    assert budget_tracker.get_carried_over_bank() == 0.0
    budget_tracker.set_starting_bank(15.50)
    assert budget_tracker.get_carried_over_bank() == 15.50

    summary = budget_tracker.format_usage_markdown()
    assert "$15.50" in summary
    assert "gemini-3.8-flash" in summary
