"""Pre-trade risk. Tight demo defaults; every gate is on.

A kill from daily loss flattens via a reduce-only market order and rejects
new risk until the next UTC day. Flatten and reduce-only are not subject
to the rate limit or max-position clip (they shrink the book).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from signed_flow.orders import Order, Portfolio, Side


MS_PER_DAY = 86_400_000


@dataclass(frozen=True)
class RiskLimits:
    """All gates on. Numbers are demo-sized, not a live book."""

    max_notional: float = 200.0
    max_position: float = 0.002
    max_order_qty: float = 0.001
    max_order_notional: float = 80.0
    max_orders_per_window: int = 6
    order_window_ms: int = 60_000
    daily_loss: float = 8.0
    min_qty: float = 1e-6
    enabled: bool = True


DEMO_LIMITS = RiskLimits()


@dataclass
class RiskDecision:
    allowed: bool
    reason: str
    order: Order | None = None
    flatten: bool = False
    killed: bool = False

    @staticmethod
    def reject(reason: str, *, killed: bool = False, flatten: bool = False) -> RiskDecision:
        return RiskDecision(
            allowed=False, reason=reason, killed=killed, flatten=flatten
        )


@dataclass
class RiskGate:
    limits: RiskLimits = field(default_factory=RiskLimits)
    killed: bool = False
    kill_reason: str = ""
    killed_day: int | None = None
    _day: int | None = None
    _equity_at_day_open: float | None = None
    _submit_times: deque[int] = field(default_factory=deque)
    n_rejects: int = 0
    n_kills: int = 0
    n_clips: int = 0

    def on_mark(self, now_ms: int, portfolio: Portfolio) -> RiskDecision | None:
        """Update the UTC day and trip the daily-loss kill if needed.

        Returns a flatten decision when the gate just tripped and there is
        a position; otherwise None.
        """
        if not self.limits.enabled:
            return None
        day = now_ms // MS_PER_DAY
        if self._day is None:
            self._day = day
            if self._equity_at_day_open is None:
                self._equity_at_day_open = portfolio.starting_cash
        elif day != self._day:
            self._day = day
            self._equity_at_day_open = portfolio.equity
            if self.killed and self.killed_day is not None and day != self.killed_day:
                self.killed = False
                self.kill_reason = ""
                self.killed_day = None
        if self._equity_at_day_open is None:
            self._equity_at_day_open = portfolio.starting_cash
        if self.killed:
            if abs(portfolio.qty) > self.limits.min_qty:
                return RiskDecision.reject(
                    self.kill_reason or "killed", killed=True, flatten=True
                )
            return None
        day_pnl = portfolio.equity - self._equity_at_day_open
        if day_pnl <= -self.limits.daily_loss:
            self.killed = True
            self.kill_reason = (
                f"daily_loss {day_pnl:.4f} <= -{self.limits.daily_loss}"
            )
            self.killed_day = day
            self.n_kills += 1
            return RiskDecision.reject(
                self.kill_reason, killed=True, flatten=True
            )
        return None

    def evaluate(self, order: Order, portfolio: Portfolio, now_ms: int) -> RiskDecision:
        """Allow, clip, or reject. Call after on_mark for the same timestamp."""
        if not self.limits.enabled:
            return RiskDecision(allowed=True, reason="ok", order=order)

        if order.qty < self.limits.min_qty:
            self.n_rejects += 1
            return RiskDecision.reject("min_qty")

        reducing = _is_reducing(order, portfolio)

        if self.killed and not reducing:
            self.n_rejects += 1
            return RiskDecision.reject(
                self.kill_reason or "killed",
                killed=True,
                flatten=abs(portfolio.qty) > 0,
            )

        mark = portfolio.mark if portfolio.mark > 0.0 else 0.0
        qty = order.qty

        if qty > self.limits.max_order_qty + 1e-15:
            qty = self.limits.max_order_qty
            self.n_clips += 1

        if mark > 0.0:
            order_notional = qty * mark
            if order_notional > self.limits.max_order_notional:
                qty = self.limits.max_order_notional / mark
                self.n_clips += 1

        if not reducing and mark > 0.0:
            signed = qty if order.side is Side.BUY else -qty
            resulting = portfolio.qty + signed
            if abs(resulting) > self.limits.max_position + 1e-15:
                cap = self.limits.max_position
                if order.side is Side.BUY:
                    qty = max(0.0, cap - portfolio.qty)
                else:
                    qty = max(0.0, cap + portfolio.qty)
                self.n_clips += 1
                if qty < self.limits.min_qty:
                    self.n_rejects += 1
                    return RiskDecision.reject("max_position")
            resulting = portfolio.qty + (qty if order.side is Side.BUY else -qty)
            if abs(resulting) * mark > self.limits.max_notional + 1e-9:
                cap_qty = self.limits.max_notional / mark
                if order.side is Side.BUY:
                    qty = max(0.0, cap_qty - portfolio.qty)
                else:
                    qty = max(0.0, cap_qty + portfolio.qty)
                self.n_clips += 1
                if qty < self.limits.min_qty:
                    self.n_rejects += 1
                    return RiskDecision.reject("max_notional")

        if not reducing and not self._rate_ok(now_ms):
            self.n_rejects += 1
            return RiskDecision.reject("max_orders_per_window")

        if qty + 1e-15 < order.qty:
            order.qty = qty
        if order.qty < self.limits.min_qty:
            self.n_rejects += 1
            return RiskDecision.reject("clipped_to_zero")

        self._note_submit(now_ms)
        return RiskDecision(allowed=True, reason="ok", order=order, killed=self.killed)

    def _rate_ok(self, now_ms: int) -> bool:
        window = self.limits.order_window_ms
        q = self._submit_times
        while q and now_ms - q[0] >= window:
            q.popleft()
        return len(q) < self.limits.max_orders_per_window

    def _note_submit(self, now_ms: int) -> None:
        self._submit_times.append(now_ms)

    def flatten_order(
        self,
        *,
        symbol: str,
        portfolio: Portfolio,
        now_ms: int,
        client_id: str,
        not_before_ms: int,
    ) -> Order | None:
        qty = abs(portfolio.qty)
        if qty < self.limits.min_qty:
            return None
        side = Side.SELL if portfolio.qty > 0.0 else Side.BUY
        return Order(
            client_id=client_id,
            symbol=symbol,
            side=side,
            qty=qty,
            submitted_at=now_ms,
            not_before_ms=not_before_ms,
            reduce_only=True,
        )


def _is_reducing(order: Order, portfolio: Portfolio) -> bool:
    if order.reduce_only:
        return True
    if portfolio.qty == 0.0:
        return False
    signed = order.qty if order.side is Side.BUY else -order.qty
    return portfolio.qty * signed < 0.0
