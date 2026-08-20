"""Orders, fills, and inventory.

State machine: new → ack | rejected; ack → partial | filled | canceled;
partial → partial | filled | canceled. Terminal: filled, rejected, canceled.

PnL is mark-to-market vs last print. Fees are taker, charged on notional.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def signed(self) -> int:
        return 1 if self is Side.BUY else -1


class OrderStatus(str, Enum):
    NEW = "new"
    ACK = "ack"
    REJECTED = "rejected"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELED = "canceled"


_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.NEW: frozenset({OrderStatus.ACK, OrderStatus.REJECTED}),
    OrderStatus.ACK: frozenset(
        {OrderStatus.PARTIAL, OrderStatus.FILLED, OrderStatus.CANCELED}
    ),
    OrderStatus.PARTIAL: frozenset(
        {OrderStatus.PARTIAL, OrderStatus.FILLED, OrderStatus.CANCELED}
    ),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELED: frozenset(),
}

TERMINAL = frozenset(
    {OrderStatus.REJECTED, OrderStatus.FILLED, OrderStatus.CANCELED}
)


class IllegalTransition(ValueError):
    """Raised when an order is driven into a state the machine forbids."""


@dataclass(frozen=True)
class Fill:
    order_id: str
    time: int
    price: float
    qty: float
    fee: float
    print_time: int
    agg_id: int


@dataclass
class Order:
    """Working order. Mutable only through the state-machine methods."""

    client_id: str
    symbol: str
    side: Side
    qty: float
    submitted_at: int
    not_before_ms: int
    status: OrderStatus = OrderStatus.NEW
    filled_qty: float = 0.0
    filled_notional: float = 0.0
    fees: float = 0.0
    reject_reason: str = ""
    reduce_only: bool = False
    signal_bar_start: int | None = None
    fills: list[Fill] = field(default_factory=list)
    cancel_reason: str = ""

    def __post_init__(self) -> None:
        if self.qty <= 0.0:
            raise ValueError("order qty must be positive")
        if self.not_before_ms < 0:
            raise ValueError("not_before_ms must be >= 0")

    @property
    def remaining(self) -> float:
        return max(0.0, self.qty - self.filled_qty)

    @property
    def avg_fill_px(self) -> float:
        if self.filled_qty <= 0.0:
            return 0.0
        return self.filled_notional / self.filled_qty

    @property
    def is_open(self) -> bool:
        return self.status in {OrderStatus.NEW, OrderStatus.ACK, OrderStatus.PARTIAL}

    def _move(self, new_status: OrderStatus) -> None:
        allowed = _TRANSITIONS[self.status]
        if new_status not in allowed:
            raise IllegalTransition(
                f"{self.client_id}: {self.status.value} → {new_status.value} is illegal"
            )
        self.status = new_status

    def ack(self, at: int | None = None) -> None:
        del at
        self._move(OrderStatus.ACK)

    def reject(self, reason: str) -> None:
        self.reject_reason = reason
        self._move(OrderStatus.REJECTED)

    def cancel(self, reason: str = "cancel") -> None:
        if self.status in TERMINAL:
            raise IllegalTransition(
                f"{self.client_id}: cannot cancel from {self.status.value}"
            )
        self.cancel_reason = reason
        self._move(OrderStatus.CANCELED)

    def apply_fill(self, fill: Fill, *, complete_eps: float = 1e-12) -> None:
        if self.status is OrderStatus.NEW:
            raise IllegalTransition(
                f"{self.client_id}: fill before ack is illegal"
            )
        if self.status in {OrderStatus.REJECTED, OrderStatus.FILLED, OrderStatus.CANCELED}:
            raise IllegalTransition(
                f"{self.client_id}: fill from {self.status.value} is illegal"
            )
        if fill.qty <= 0.0 or fill.price <= 0.0:
            raise ValueError("fill qty and price must be positive")
        if fill.print_time < self.not_before_ms:
            raise ValueError(
                f"{self.client_id}: fill print_time {fill.print_time} < "
                f"{self.not_before_ms} not_before (lookahead)"
            )
        remaining = self.remaining
        if fill.qty - remaining > 1e-9:
            raise ValueError(
                f"{self.client_id}: fill qty {fill.qty} exceeds remaining {remaining}"
            )
        self.filled_qty += fill.qty
        self.filled_notional += fill.qty * fill.price
        self.fees += fill.fee
        self.fills.append(fill)
        if self.remaining <= complete_eps:
            self.filled_qty = self.qty
            self._move(OrderStatus.FILLED)
        else:
            self._move(OrderStatus.PARTIAL)


@dataclass
class Portfolio:
    """Single-name inventory. qty > 0 long, qty < 0 short."""

    symbol: str
    cash: float
    qty: float = 0.0
    avg_px: float = 0.0
    realized: float = 0.0
    fees: float = 0.0
    mark: float = 0.0
    starting_cash: float = 0.0

    def __post_init__(self) -> None:
        if self.starting_cash == 0.0:
            self.starting_cash = self.cash

    @property
    def unrealized(self) -> float:
        if self.qty == 0.0 or self.mark <= 0.0:
            return 0.0
        return self.qty * (self.mark - self.avg_px)

    @property
    def equity(self) -> float:
        mark = self.mark if self.mark > 0.0 else self.avg_px
        return self.cash + self.qty * mark

    @property
    def notional(self) -> float:
        px = self.mark if self.mark > 0.0 else self.avg_px
        return abs(self.qty) * px

    @property
    def net_pnl(self) -> float:
        return self.equity - self.starting_cash

    def set_mark(self, price: float) -> None:
        if price > 0.0:
            self.mark = float(price)

    def apply_fill(self, side: Side, qty: float, price: float, fee: float) -> None:
        """Update inventory for a taker fill. qty is always positive."""
        if qty <= 0.0 or price <= 0.0:
            raise ValueError("fill qty and price must be positive")
        signed = qty if side is Side.BUY else -qty
        self.cash -= signed * price + fee
        self.fees += fee

        old = self.qty
        if old == 0.0 or old * signed > 0.0:
            new_qty = old + signed
            abs_old = abs(old)
            self.avg_px = (abs_old * self.avg_px + qty * price) / abs(new_qty)
            self.qty = new_qty
            return

        closed = min(abs(old), qty)
        if old > 0.0:
            self.realized += (price - self.avg_px) * closed
        else:
            self.realized += (self.avg_px - price) * closed

        new_qty = old + signed
        if new_qty == 0.0:
            self.qty = 0.0
            self.avg_px = 0.0
        elif old * new_qty < 0.0:
            self.qty = new_qty
            self.avg_px = price
        else:
            self.qty = new_qty


def legal_targets(status: OrderStatus) -> frozenset[OrderStatus]:
    return _TRANSITIONS[status]


def replay_fills(order: Order, fills: Iterable[Fill]) -> Order:
    """Helper for tests: ack then apply fills in order."""
    if order.status is OrderStatus.NEW:
        order.ack()
    for fill in fills:
        order.apply_fill(fill)
    return order
