"""Wheatley Budget & Usage Shim (Maintained for backwards-compatibility; billing caps removed)"""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

from wheatley.audit import AuditLogger
from wheatley.config import BudgetConfig
from wheatley.llm.usage import UsageTracker

logger = logging.getLogger("wheatley.budget")


class BudgetTracker(UsageTracker):
    """Backwards-compatible tracker shim with artificial billing blocks and caps removed."""

    def __init__(self, config: Optional[BudgetConfig], audit: AuditLogger):
        super().__init__(audit)
        self.config = config or BudgetConfig()

    def calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Deprecated: returns 0.0 as billing calculations are removed."""
        return 0.0

    def get_daily_spend(self, date_str: Optional[str] = None) -> float:
        return 0.0

    def get_monthly_spend(self, month_str: Optional[str] = None) -> float:
        return 0.0

    def get_carried_over_bank(self) -> float:
        return 0.0

    def set_starting_bank(self, amount: float) -> None:
        pass

    def can_call_llm(self) -> Tuple[bool, str]:
        """Always returns True: artificial budget pauses and warnings have been removed."""
        return True, "OK"


__all__ = ["BudgetTracker", "UsageTracker"]
