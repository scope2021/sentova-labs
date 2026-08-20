"""Trade signs, signed volume, and calendar-time OFI bars.

No lookahead: the bar that starts at time t uses only trades with
exchange timestamp in [t, t + bar_length). Forward returns are computed
from last prices of later bars.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

MS = np.int64(1000)


def trade_sign(is_buyer_maker: pd.Series | np.ndarray) -> np.ndarray:
    """Seller-initiated (buyer is maker) → -1; buyer-initiated → +1."""
    maker = np.asarray(is_buyer_maker, dtype=bool)
    return np.where(maker, np.int8(-1), np.int8(1))


def add_signed_volume(trades: pd.DataFrame) -> pd.DataFrame:
    """Attach sign and signed_volume columns. Does not modify time order."""
    out = trades.copy()
    out["sign"] = trade_sign(out["is_buyer_maker"])
    out["signed_volume"] = out["sign"].astype("float64") * out["qty"].astype("float64")
    return out


def trades_to_bars(
    trades: pd.DataFrame,
    bar_seconds: int = 1,
    regular_grid: bool = True,
) -> pd.DataFrame:
    """Bucket aggTrades into calendar bars.

    Parameters
    ----------
    trades
        Must contain price, qty, time (ms), is_buyer_maker.
    bar_seconds
        Bar width in seconds. Default 1.
    regular_grid
        If True, insert empty bars between the first and last trade so
        a horizon of h bars is h * bar_seconds of calendar time. Empty
        bars have OFI = 0, volume = 0, last_price forward-filled.

    Returns
    -------
    DataFrame indexed by bar_start (ms since epoch) with columns:
    ofi, volume, trade_count, last_price, vwap, ret.
    ``ret`` is the last-price return of *this* bar vs the previous bar
    (not a forward return).
    """
    if bar_seconds < 1:
        raise ValueError("bar_seconds must be >= 1")
    if trades.empty:
        raise ValueError("no trades")

    df = add_signed_volume(trades)
    bar_ms = np.int64(bar_seconds) * MS
    ts = df["time"].to_numpy(dtype=np.int64)
    bar_id = (ts // bar_ms) * bar_ms

    px = df["price"].to_numpy(dtype=np.float64)
    qty = df["qty"].to_numpy(dtype=np.float64)
    signed = df["signed_volume"].to_numpy(dtype=np.float64)

    tmp = pd.DataFrame(
        {
            "bar_start": bar_id,
            "ofi": signed,
            "volume": qty,
            "notional": px * qty,
            "last_price": px,
            "one": np.ones(len(df), dtype=np.int64),
        }
    )
    grouped = tmp.groupby("bar_start", sort=True)
    bars = pd.DataFrame(
        {
            "ofi": grouped["ofi"].sum(),
            "volume": grouped["volume"].sum(),
            "trade_count": grouped["one"].sum(),
            "last_price": grouped["last_price"].last(),
            "notional": grouped["notional"].sum(),
        }
    )
    bars["vwap"] = bars["notional"] / bars["volume"].replace(0, np.nan)
    bars = bars.drop(columns=["notional"])

    if regular_grid:
        start = int(bars.index.min())
        end = int(bars.index.max())
        full = np.arange(start, end + bar_ms, bar_ms, dtype=np.int64)
        bars = bars.reindex(full)
        bars["ofi"] = bars["ofi"].fillna(0.0)
        bars["volume"] = bars["volume"].fillna(0.0)
        bars["trade_count"] = bars["trade_count"].fillna(0)
        bars["last_price"] = bars["last_price"].ffill()
        bars["vwap"] = bars["vwap"].ffill()

    bars["trade_count"] = bars["trade_count"].astype(np.int64)
    bars["ret"] = bars["last_price"].pct_change()
    bars.index.name = "bar_start"
    return bars


def forward_return(last_price: pd.Series, horizon: int) -> pd.Series:
    """Return from bar t close to bar t+horizon close. No lookahead into t."""
    if horizon < 1:
        raise ValueError("horizon must be >= 1")
    px = last_price.astype("float64")
    return px.shift(-horizon) / px - 1.0
