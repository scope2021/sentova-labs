"""Event-driven paper engine: tape → features → OFI sign rule → risk → broker.

Deterministic. A bar-t signal is submitted at bar close and may fill only
on prints with exchange time >= bar end. No mid, no maker, no lookahead.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from signed_flow.brokers import PaperBroker
from signed_flow.features import trade_sign
from signed_flow.orders import Order, Portfolio, Side
from signed_flow.risk import DEMO_LIMITS, RiskDecision, RiskGate, RiskLimits


@dataclass
class ReplayResult:
    symbol: str
    source: str
    bar_seconds: int
    taker_fee_bps: float
    quantile: float
    threshold: float
    warmup_prints: int
    n_prints: int
    n_bars: int
    n_signals: int
    n_orders: int
    n_rejects: int
    n_fills: int
    n_cancels: int
    n_kills: int
    n_clips: int
    starting_cash: float
    ending_equity: float
    ending_cash: float
    ending_qty: float
    realized: float
    unrealized: float
    fees: float
    net_pnl: float
    max_equity: float
    min_equity: float
    t0_ms: int
    t1_ms: int
    limits: RiskLimits
    kill_notes: list[str]
    equity_t: np.ndarray
    equity_v: np.ndarray


@dataclass
class TradingEngine:
    symbol: str
    broker: PaperBroker
    risk: RiskGate
    bar_seconds: int = 1
    order_qty: float = 0.001
    quantile: float = 0.70
    warmup_frac: float = 0.30
    warmup_prints: int = 0

    _bar_ms: int = field(init=False, default=1000)
    _open_bar_start: int | None = field(init=False, default=None)
    _bar_ofi: float = field(init=False, default=0.0)
    _bar_volume: float = field(init=False, default=0.0)
    _bar_last: float = field(init=False, default=0.0)
    _n_prints: int = field(init=False, default=0)
    _n_bars: int = field(init=False, default=0)
    _n_signals: int = field(init=False, default=0)
    _n_orders: int = field(init=False, default=0)
    _seq: int = field(init=False, default=0)
    _threshold: float | None = field(init=False, default=None)
    _abs_ofi: list[float] = field(init=False, default_factory=list)
    _equity_t: list[int] = field(init=False, default_factory=list)
    _equity_v: list[float] = field(init=False, default_factory=list)
    _eq_max: float = field(init=False, default=0.0)
    _eq_min: float = field(init=False, default=0.0)
    _kill_notes: list[str] = field(init=False, default_factory=list)
    _flatten_id: str | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        if self.bar_seconds < 1:
            raise ValueError("bar_seconds must be >= 1")
        self._bar_ms = int(self.bar_seconds) * 1000
        cash = self.broker.portfolio.starting_cash
        self._eq_max = cash
        self._eq_min = cash

    def _next_id(self, tag: str) -> str:
        self._seq += 1
        return f"sf-{tag}-{self._seq}"

    def _sample_equity(self, now_ms: int, force: bool = True) -> None:
        eq = self.broker.portfolio.equity
        if eq > self._eq_max:
            self._eq_max = eq
        if eq < self._eq_min:
            self._eq_min = eq
        if not force:
            return
        self._equity_t.append(now_ms)
        self._equity_v.append(eq)

    def on_print(
        self,
        *,
        time: int,
        price: float,
        qty: float,
        agg_id: int,
        is_buyer_maker: bool,
    ) -> None:
        port = self.broker.portfolio
        port.set_mark(price)

        if self._open_bar_start is not None:
            bar_end = self._open_bar_start + self._bar_ms
            if time >= bar_end:
                self._close_bar(bar_end)

        self._maybe_kill(time)
        self.broker.on_print(time=time, price=price, qty=qty, agg_id=agg_id)
        self._maybe_kill(time)
        self._accumulate(time, price, qty, is_buyer_maker)

        self._n_prints += 1
        if self._threshold is None and self.warmup_prints > 0:
            if self._n_prints >= self.warmup_prints:
                self._freeze_threshold()
        self._sample_equity(time, force=self._n_prints == 1 or self._n_prints % 32 == 0)

    def on_end(self) -> None:
        if self._open_bar_start is not None:
            bar_end = self._open_bar_start + self._bar_ms
            self._close_bar(bar_end)
        self.broker.cancel_all("end_of_tape")
        last_t = self._equity_t[-1] if self._equity_t else 0
        self._sample_equity(last_t, force=True)

    def replay(self, trades: pd.DataFrame, *, source: str = "") -> ReplayResult:
        if trades.empty:
            raise ValueError("no trades")
        n = int(len(trades))
        if self.warmup_prints <= 0:
            self.warmup_prints = max(1, int(n * self.warmup_frac))
        t0 = int(trades["time"].iloc[0])
        t1 = int(trades["time"].iloc[-1])
        times = trades["time"].to_numpy(dtype=np.int64)
        prices = trades["price"].to_numpy(dtype=np.float64)
        qtys = trades["qty"].to_numpy(dtype=np.float64)
        agg_ids = trades["agg_id"].to_numpy(dtype=np.int64)
        makers = trades["is_buyer_maker"].to_numpy(dtype=bool)
        for i in range(n):
            self.on_print(
                time=int(times[i]),
                price=float(prices[i]),
                qty=float(qtys[i]),
                agg_id=int(agg_ids[i]),
                is_buyer_maker=bool(makers[i]),
            )
        self.on_end()
        port = self.broker.portfolio
        thr = float("nan") if self._threshold is None else float(self._threshold)
        return ReplayResult(
            symbol=self.symbol,
            source=source,
            bar_seconds=self.bar_seconds,
            taker_fee_bps=self.broker.taker_fee_bps,
            quantile=self.quantile,
            threshold=thr,
            warmup_prints=self.warmup_prints,
            n_prints=self._n_prints,
            n_bars=self._n_bars,
            n_signals=self._n_signals,
            n_orders=self._n_orders,
            n_rejects=self.risk.n_rejects,
            n_fills=self.broker.n_fills,
            n_cancels=self.broker.n_cancels,
            n_kills=self.risk.n_kills,
            n_clips=self.risk.n_clips,
            starting_cash=port.starting_cash,
            ending_equity=port.equity,
            ending_cash=port.cash,
            ending_qty=port.qty,
            realized=port.realized,
            unrealized=port.unrealized,
            fees=port.fees,
            net_pnl=port.net_pnl,
            max_equity=self._eq_max,
            min_equity=self._eq_min,
            t0_ms=t0,
            t1_ms=t1,
            limits=self.risk.limits,
            kill_notes=list(self._kill_notes),
            equity_t=np.asarray(self._equity_t, dtype=np.int64),
            equity_v=np.asarray(self._equity_v, dtype=np.float64),
        )

    def _accumulate(
        self, time: int, price: float, qty: float, is_buyer_maker: bool
    ) -> None:
        bar_start = (time // self._bar_ms) * self._bar_ms
        if self._open_bar_start is None:
            self._open_bar_start = int(bar_start)
            self._bar_ofi = 0.0
            self._bar_volume = 0.0
        signed = float(trade_sign(np.array([is_buyer_maker], dtype=bool))[0])
        self._bar_ofi += signed * qty
        self._bar_volume += qty
        self._bar_last = price

    def _close_bar(self, bar_end: int) -> None:
        ofi = self._bar_ofi
        volume = self._bar_volume
        bar_start = self._open_bar_start
        self._open_bar_start = None
        self._bar_ofi = 0.0
        self._bar_volume = 0.0
        self._n_bars += 1
        if volume > 0.0 and self._threshold is None:
            self._abs_ofi.append(abs(ofi))
        if self._threshold is None and self._n_prints >= self.warmup_prints:
            self._freeze_threshold()
        if self._threshold is None:
            return
        if volume <= 0.0:
            target = 0.0
        elif abs(ofi) >= self._threshold and ofi != 0.0:
            target = float(np.sign(ofi)) * self.order_qty
            self._n_signals += 1
        else:
            target = 0.0
        self._rebalance(
            target=target,
            now_ms=bar_end,
            not_before_ms=bar_end,
            signal_bar_start=bar_start,
        )

    def _freeze_threshold(self) -> None:
        if not self._abs_ofi:
            return
        self._threshold = float(np.quantile(np.asarray(self._abs_ofi), self.quantile))

    def _maybe_kill(self, now_ms: int) -> None:
        decision = self.risk.on_mark(now_ms, self.broker.portfolio)
        if decision is None:
            return
        if decision.killed and decision.flatten:
            self._enter_flatten(now_ms, decision.reason)

    def _enter_flatten(self, now_ms: int, reason: str) -> None:
        if reason not in self._kill_notes:
            self._kill_notes.append(reason)
        for order in list(self.broker.open_orders):
            if order.is_open and not order.reduce_only:
                self.broker.cancel(order.client_id, "kill")
        if self._flatten_working():
            return
        flat = self.risk.flatten_order(
            symbol=self.symbol,
            portfolio=self.broker.portfolio,
            now_ms=now_ms,
            client_id=self._next_id("flat"),
            not_before_ms=now_ms,
        )
        if flat is None:
            return
        self._n_orders += 1
        self._flatten_id = flat.client_id
        self.broker.submit(flat)

    def _flatten_working(self) -> bool:
        if self._flatten_id is None:
            return False
        for order in self.broker.open_orders:
            if order.client_id == self._flatten_id and order.is_open:
                return True
        return False

    def _rebalance(
        self,
        *,
        target: float,
        now_ms: int,
        not_before_ms: int,
        signal_bar_start: int | None,
    ) -> None:
        port = self.broker.portfolio
        if self.risk.killed:
            self._enter_flatten(now_ms, self.risk.kill_reason or "killed")
            return
        for order in list(self.broker.open_orders):
            if order.is_open and not order.reduce_only:
                self.broker.cancel(order.client_id, "replace")
        delta = target - port.qty
        min_qty = self.risk.limits.min_qty
        if abs(delta) < min_qty:
            return
        side = Side.BUY if delta > 0.0 else Side.SELL
        order = Order(
            client_id=self._next_id("mkt"),
            symbol=self.symbol,
            side=side,
            qty=abs(delta),
            submitted_at=now_ms,
            not_before_ms=not_before_ms,
            reduce_only=bool(target == 0.0),
            signal_bar_start=signal_bar_start,
        )
        decision: RiskDecision = self.risk.evaluate(order, port, now_ms)
        if not decision.allowed:
            if decision.flatten:
                self._enter_flatten(now_ms, decision.reason)
            return
        assert decision.order is not None
        self._n_orders += 1
        self.broker.submit(decision.order)


def run_replay(
    trades: pd.DataFrame,
    *,
    symbol: str,
    source: str = "",
    bar_seconds: int = 1,
    taker_fee_bps: float = 2.0,
    order_qty: float = 0.001,
    starting_cash: float = 10_000.0,
    quantile: float = 0.70,
    warmup_frac: float = 0.30,
    limits: RiskLimits | None = None,
) -> ReplayResult:
    port = Portfolio(symbol=symbol, cash=starting_cash)
    broker = PaperBroker(portfolio=port, taker_fee_bps=taker_fee_bps)
    gate = RiskGate(limits=limits or DEMO_LIMITS)
    engine = TradingEngine(
        symbol=symbol,
        broker=broker,
        risk=gate,
        bar_seconds=bar_seconds,
        order_qty=order_qty,
        quantile=quantile,
        warmup_frac=warmup_frac,
    )
    return engine.replay(trades, source=source)
