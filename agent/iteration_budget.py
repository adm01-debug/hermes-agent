"""Per-agent iteration budget — thread-safe consume/refund counter.

Extracted from ``run_agent.py``.  Each ``AIAgent`` instance (parent or
subagent) holds an :class:`IterationBudget`; the parent's cap comes from
``max_iterations`` (default 500), each subagent's cap comes from
``delegation.max_iterations`` (default 50).

``run_agent`` re-exports ``IterationBudget`` so existing
``from run_agent import IterationBudget`` imports keep working unchanged.
"""

from __future__ import annotations

import threading


class IterationBudget:
    """Thread-safe iteration counter for an agent.

    Each agent (parent or subagent) gets its own ``IterationBudget``.
    The parent's budget is capped at ``max_iterations`` (default 500).
    Each subagent gets an independent budget capped at
    ``delegation.max_iterations`` (default 50) — this means total
    iterations across parent + subagents can exceed the parent's cap.
    Users control the per-subagent limit via ``delegation.max_iterations``
    in config.yaml.

    ``execute_code`` (programmatic tool calling) iterations are refunded via
    :meth:`refund` so they don't eat into the budget.
    """

    def __init__(self, max_total: int):
        self.max_total = max_total
        self._used = 0
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """Try to consume one iteration.  Returns True if allowed."""
        with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    def refund(self) -> None:
        """Give back one iteration (e.g. for execute_code turns)."""
        with self._lock:
            if self._used > 0:
                self._used -= 1

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_total - self._used)


class CostBudget:
    """Thread-safe USD cost accumulator for an agent family.

    ``limit_usd <= 0`` disables the gate (default).  Mirrors
    :class:`IterationBudget`: one instance shared across parent +
    subagents, so the total estimated spend of the whole delegation
    tree counts against the same cap.  Calls with no known price
    (``amount_usd is None``) are ignored — the gate stays inert for
    unknown-pricing models (spec-p3b decision 4).
    """

    def __init__(self, cost_limit_usd: float = 0.0, **kwargs):
        # Accept the spec keyword (limit_usd) and the shorthand (limit)
        # as aliases; positional use is cost_limit_usd.
        if kwargs:
            alias = kwargs.pop("limit_usd", None)
            if alias is None:
                alias = kwargs.pop("limit", None)
            if alias is not None:
                cost_limit_usd = alias
            if kwargs:
                raise TypeError(
                    f"CostBudget() got unexpected keyword arguments: {sorted(kwargs)}"
                )
        self.limit_usd = float(cost_limit_usd or 0.0)
        self._used_usd = 0.0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.limit_usd > 0

    def record_cost(self, amount_usd: float) -> bool:
        """Add one call's estimated cost.  Returns True while within the
        limit (or gate off / amount None / non-numeric / <= 0 / NaN);
        False when the limit is now exceeded.  Never raises."""
        if not self.enabled or amount_usd is None:
            return True
        try:
            _amount = float(amount_usd)
        except (TypeError, ValueError):
            return True
        if _amount <= 0 or _amount != _amount:  # <= 0 ignored; NaN clamp
            return True
        with self._lock:
            # 6-decimal rounding keeps float drift from flipping the gate
            # on a boundary (e.g. 0.1 + 0.2 vs 0.3).
            self._used_usd = round(self._used_usd + _amount, 6)
            return self._used_usd <= self.limit_usd

    def add_estimated_cost(self, usd: float | None) -> bool:
        """Alias of :meth:`record_cost` (None or <= 0 ignored)."""
        return self.record_cost(usd)

    @property
    def exhausted(self) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            return self._used_usd > self.limit_usd

    @property
    def exceeded(self) -> bool:
        """Alias of :attr:`exhausted`."""
        return self.exhausted

    @property
    def used_usd(self) -> float:
        with self._lock:
            return self._used_usd

    @property
    def accumulated_usd(self) -> float:
        """Alias of :attr:`used_usd`."""
        return self.used_usd

    @property
    def remaining_usd(self) -> float:
        return max(0.0, round(self.limit_usd - self.used_usd, 6))


__all__ = ["IterationBudget", "CostBudget"]
