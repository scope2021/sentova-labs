"""No lookahead, kill-switch flatten, engine path on a short tape."""

from __future__ import annotations

import pandas as pd
import pytest

from signed_flow.brokers import PaperBroker
from signed_flow.engine import TradingEngine, run_replay
from signed_flow.orders import Portfolio, Side
from signed_flow.risk import RiskGate, RiskLimits


def _tape(rows: list[tuple]) -> pd.DataFrame:
    # rows: (agg_id, price, qty, time, is_buyer_maker)
    return pd.DataFrame(
        rows,
        columns=["agg_id", "price", "qty", "time", "is_buyer_maker"],
    )


def test_signal_at_t_cannot_fill_at_t() -> None:
    """Bar [0, 1000) is all 100 prints (buy aggressor → +OFI).

    The 110 print at t=1000 is the next bar. Fill must be 110, never 100.
    """
    trades = _tape(
        [
            (1, 100.0, 2.0, 100, False),
            (2, 100.0, 2.0, 400, False),
            (3, 100.0, 2.0, 800, False),
            (4, 110.0, 5.0, 1000, False),
            (5, 111.0, 5.0, 1500, False),
        ]
    )
    limits = RiskLimits(
        max_notional=10_000.0,
        max_position=10.0,
        max_order_qty=10.0,
        max_order_notional=10_000.0,
        max_orders_per_window=50,
        order_window_ms=60_000,
        daily_loss=1_000_000.0,
        min_qty=1e-9,
    )
    port = Portfolio(symbol="BTCUSDT", cash=10_000.0)
    broker = PaperBroker(portfolio=port, taker_fee_bps=0.0)
    gate = RiskGate(limits=limits)
    engine = TradingEngine(
        symbol="BTCUSDT",
        broker=broker,
        risk=gate,
        bar_seconds=1,
        order_qty=1.0,
        quantile=0.0,
        warmup_frac=0.0,
        warmup_prints=1,
    )
    result = engine.replay(trades, source="test")
    assert result.n_fills >= 1
    first = broker.history[0] if broker.history else broker.open_orders[0]
    # The first filled/partial order must not have used a t<1000 print.
    assert first.fills, "expected at least one fill"
    for fill in first.fills:
        assert fill.print_time >= 1000
        assert fill.price >= 110.0
    # Same-bar 100s never became a fill price.
    assert all(f.price != 100.0 for f in first.fills)


def test_kill_switch_flattens() -> None:
    """Buy 1 at 100, mark crashes to 80 → daily_loss 5 trips, flatten to 0."""
    trades = _tape(
        [
            (1, 100.0, 3.0, 100, False),
            (2, 100.0, 3.0, 200, False),
            (3, 100.0, 3.0, 1100, False),  # fill the long
            (4, 80.0, 3.0, 2100, True),  # mark crash; flatten should take this
            (5, 80.0, 3.0, 3100, True),
            (6, 80.0, 3.0, 4100, True),
            (7, 79.0, 3.0, 5100, True),
        ]
    )
    limits = RiskLimits(
        max_notional=50_000.0,
        max_position=5.0,
        max_order_qty=5.0,
        max_order_notional=50_000.0,
        max_orders_per_window=50,
        order_window_ms=60_000,
        daily_loss=5.0,
        min_qty=1e-9,
    )
    result = run_replay(
        trades,
        symbol="BTCUSDT",
        source="test",
        bar_seconds=1,
        taker_fee_bps=0.0,
        order_qty=1.0,
        starting_cash=10_000.0,
        quantile=0.0,
        warmup_frac=0.0,
        limits=limits,
    )
    # Force warmup_prints: run_replay with warmup_frac 0 still sets warmup via
    # max(1, int(n * 0)) = 1. Good — first print warms up, later bars trade.
    assert result.n_kills >= 1
    assert abs(result.ending_qty) < 1e-9
    assert any("daily_loss" in n for n in result.kill_notes)


def test_killed_rejects_new_entries() -> None:
    port = Portfolio(symbol="BTCUSDT", cash=1_000.0)
    port.set_mark(100.0)
    port.apply_fill(Side.BUY, 1.0, 100.0, fee=0.0)
    port.set_mark(50.0)  # unrealized -50
    gate = RiskGate(
        limits=RiskLimits(
            max_notional=1_000_000.0,
            max_position=10.0,
            max_order_qty=10.0,
            max_order_notional=1_000_000.0,
            max_orders_per_window=50,
            daily_loss=10.0,
        )
    )
    trip = gate.on_mark(now_ms=5_000, portfolio=port)
    assert trip is not None
    assert trip.killed and trip.flatten
    from signed_flow.orders import Order

    entry = Order(
        client_id="x",
        symbol="BTCUSDT",
        side=Side.BUY,
        qty=1.0,
        submitted_at=5_000,
        not_before_ms=5_000,
    )
    decision = gate.evaluate(entry, port, 5_000)
    assert not decision.allowed
    flat = gate.flatten_order(
        symbol="BTCUSDT",
        portfolio=port,
        now_ms=5_000,
        client_id="flat",
        not_before_ms=5_000,
    )
    assert flat is not None
    assert flat.reduce_only
    assert flat.side is Side.SELL
    assert flat.qty == pytest.approx(1.0)
    ok = gate.evaluate(flat, port, 5_000)
    assert ok.allowed


def test_replay_is_deterministic() -> None:
    trades = _tape(
        [
            (i, 100.0 + (i % 3), 1.0, 100 * i, bool(i % 2))
            for i in range(1, 40)
        ]
    )
    limits = RiskLimits(
        max_notional=50_000.0,
        max_position=5.0,
        max_order_qty=1.0,
        max_order_notional=50_000.0,
        max_orders_per_window=20,
        daily_loss=1_000.0,
    )
    a = run_replay(
        trades, symbol="BTCUSDT", bar_seconds=1, order_qty=0.5,
        starting_cash=10_000.0, quantile=0.0, warmup_frac=0.2, limits=limits,
        taker_fee_bps=2.0,
    )
    b = run_replay(
        trades, symbol="BTCUSDT", bar_seconds=1, order_qty=0.5,
        starting_cash=10_000.0, quantile=0.0, warmup_frac=0.2, limits=limits,
        taker_fee_bps=2.0,
    )
    assert a.n_fills == b.n_fills
    assert a.net_pnl == pytest.approx(b.net_pnl)
    assert a.ending_qty == pytest.approx(b.ending_qty)
