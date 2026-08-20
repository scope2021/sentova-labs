"""Order state machine and inventory."""

from __future__ import annotations

import pytest

from signed_flow.orders import (
    Fill,
    IllegalTransition,
    Order,
    OrderStatus,
    Portfolio,
    Side,
    legal_targets,
)


def _order(**kwargs) -> Order:
    base = dict(
        client_id="sf-1",
        symbol="BTCUSDT",
        side=Side.BUY,
        qty=1.0,
        submitted_at=1_000,
        not_before_ms=1_000,
    )
    base.update(kwargs)
    return Order(**base)


def _fill(qty: float, price: float = 100.0, time: int = 1_500, fee: float = 0.02) -> Fill:
    return Fill(
        order_id="sf-1",
        time=time,
        price=price,
        qty=qty,
        fee=fee,
        print_time=time,
        agg_id=1,
    )


def test_new_to_ack_to_filled() -> None:
    o = _order()
    assert o.status is OrderStatus.NEW
    o.ack()
    assert o.status is OrderStatus.ACK
    o.apply_fill(_fill(1.0))
    assert o.status is OrderStatus.FILLED
    assert o.remaining == 0.0
    assert o.avg_fill_px == pytest.approx(100.0)


def test_partial_then_filled() -> None:
    o = _order(qty=2.0)
    o.ack()
    o.apply_fill(_fill(0.5, time=1_200))
    assert o.status is OrderStatus.PARTIAL
    o.apply_fill(_fill(1.5, time=1_300, fee=0.03))
    assert o.status is OrderStatus.FILLED
    assert o.filled_qty == pytest.approx(2.0)


def test_new_to_rejected_is_terminal() -> None:
    o = _order()
    o.reject("max_position")
    assert o.status is OrderStatus.REJECTED
    with pytest.raises(IllegalTransition):
        o.ack()
    with pytest.raises(IllegalTransition):
        o.apply_fill(_fill(1.0))
    with pytest.raises(IllegalTransition):
        o.cancel()


def test_cancel_from_ack_not_from_filled() -> None:
    o = _order()
    o.ack()
    o.cancel("user")
    assert o.status is OrderStatus.CANCELED
    with pytest.raises(IllegalTransition):
        o.apply_fill(_fill(1.0))

    o2 = _order(client_id="sf-2")
    o2.ack()
    o2.apply_fill(_fill(1.0))
    with pytest.raises(IllegalTransition):
        o2.cancel("too-late")


def test_fill_before_ack_illegal() -> None:
    o = _order()
    with pytest.raises(IllegalTransition):
        o.apply_fill(_fill(1.0))


def test_lookahead_fill_rejected_on_order() -> None:
    o = _order(not_before_ms=1_000)
    o.ack()
    with pytest.raises(ValueError, match="lookahead"):
        o.apply_fill(_fill(1.0, time=999))


def test_legal_targets_table() -> None:
    assert OrderStatus.ACK in legal_targets(OrderStatus.NEW)
    assert OrderStatus.REJECTED in legal_targets(OrderStatus.NEW)
    assert OrderStatus.FILLED not in legal_targets(OrderStatus.NEW)
    assert not legal_targets(OrderStatus.FILLED)
    assert not legal_targets(OrderStatus.REJECTED)


def test_portfolio_long_round_trip() -> None:
    p = Portfolio(symbol="BTCUSDT", cash=10_000.0)
    p.set_mark(100.0)
    p.apply_fill(Side.BUY, 2.0, 100.0, fee=0.4)
    assert p.qty == pytest.approx(2.0)
    assert p.avg_px == pytest.approx(100.0)
    assert p.cash == pytest.approx(10_000.0 - 200.0 - 0.4)
    p.set_mark(110.0)
    assert p.unrealized == pytest.approx(20.0)
    p.apply_fill(Side.SELL, 2.0, 110.0, fee=0.44)
    assert p.qty == pytest.approx(0.0)
    assert p.realized == pytest.approx(20.0)
    assert p.net_pnl == pytest.approx(20.0 - 0.4 - 0.44)


def test_portfolio_short_and_flip() -> None:
    p = Portfolio(symbol="BTCUSDT", cash=10_000.0)
    p.apply_fill(Side.SELL, 1.0, 100.0, fee=0.2)
    assert p.qty == pytest.approx(-1.0)
    p.apply_fill(Side.BUY, 1.5, 90.0, fee=0.27)
    # cover 1 at 90 → realized +10; leftover +0.5 long at 90
    assert p.qty == pytest.approx(0.5)
    assert p.avg_px == pytest.approx(90.0)
    assert p.realized == pytest.approx(10.0)
