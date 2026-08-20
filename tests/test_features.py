"""Synthetic trades: signs, bar aggregation, and no lookahead."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from signed_flow.features import (
    add_signed_volume,
    forward_return,
    trade_sign,
    trades_to_bars,
)
from signed_flow.research import hit_rate, spearman, walk_forward_split


def _trades() -> pd.DataFrame:
    # Four trades over ~2.5 seconds.
    # t=1000,1500 → bar 1000; t=2000 → bar 2000; t=3500 → bar 3000.
    return pd.DataFrame(
        {
            "agg_id": [1, 2, 3, 4],
            "price": [100.0, 100.5, 101.0, 99.0],
            "qty": [1.0, 2.0, 1.0, 3.0],
            "time": [1000, 1500, 2000, 3500],
            "is_buyer_maker": [False, True, False, True],
        }
    )


def test_trade_sign_buyer_maker_convention() -> None:
    # buyer was maker → seller-initiated → -1
    s = trade_sign(np.array([True, False, True]))
    assert list(s) == [-1, 1, -1]


def test_signed_volume() -> None:
    df = add_signed_volume(_trades())
    # +1*1, -1*2, +1*1, -1*3
    np.testing.assert_allclose(df["signed_volume"], [1.0, -2.0, 1.0, -3.0])


def test_bar_aggregation_and_no_lookahead() -> None:
    bars = trades_to_bars(_trades(), bar_seconds=1, regular_grid=True)
    # bar starts: 1000, 2000, 3000 (regular grid fills nothing extra besides those)
    assert list(bars.index) == [1000, 2000, 3000]

    # bar 1000 uses only trades at 1000 and 1500, not the 101 print at 2000
    assert bars.loc[1000, "last_price"] == 100.5
    assert bars.loc[1000, "ofi"] == pytest.approx(-1.0)  # 1 - 2
    assert bars.loc[1000, "volume"] == pytest.approx(3.0)
    assert int(bars.loc[1000, "trade_count"]) == 2
    assert bars.loc[1000, "vwap"] == pytest.approx((100 * 1 + 100.5 * 2) / 3)

    assert bars.loc[2000, "last_price"] == 101.0
    assert bars.loc[2000, "ofi"] == pytest.approx(1.0)
    assert int(bars.loc[2000, "trade_count"]) == 1

    assert bars.loc[3000, "last_price"] == 99.0
    assert bars.loc[3000, "ofi"] == pytest.approx(-3.0)


def test_empty_bar_ffill_last_price_no_future_trade() -> None:
    # Gap: trades at 0ms and 2500ms → bars 0, 1000, 2000 with 1s grid.
    trades = pd.DataFrame(
        {
            "price": [10.0, 12.0],
            "qty": [1.0, 1.0],
            "time": [0, 2500],
            "is_buyer_maker": [False, False],
        }
    )
    bars = trades_to_bars(trades, bar_seconds=1, regular_grid=True)
    assert list(bars.index) == [0, 1000, 2000]
    # empty bar at 1000 must not see the t=2500 trade
    assert bars.loc[1000, "last_price"] == 10.0
    assert bars.loc[1000, "ofi"] == 0.0
    assert int(bars.loc[1000, "trade_count"]) == 0
    assert bars.loc[2000, "last_price"] == 12.0


def test_forward_return_alignment() -> None:
    px = pd.Series([100.0, 110.0, 100.0], index=[0, 1, 2])
    r1 = forward_return(px, 1)
    assert np.isnan(r1.iloc[-1])
    assert r1.iloc[0] == pytest.approx(0.10)
    assert r1.iloc[1] == pytest.approx(-10.0 / 110.0)
    r2 = forward_return(px, 2)
    assert r2.iloc[0] == pytest.approx(0.0)
    assert np.isnan(r2.iloc[1])


def test_spearman_monotone() -> None:
    x = np.array([1.0, 2.0, 3.0, 4.0])
    y = np.array([10.0, 20.0, 30.0, 40.0])
    assert spearman(x, y) == pytest.approx(1.0)
    assert spearman(x, -y) == pytest.approx(-1.0)


def test_hit_rate_skips_zeros() -> None:
    sig = np.array([1.0, -1.0, 0.0, 1.0])
    fwd = np.array([0.5, -0.2, 0.9, -0.1])
    hr, n = hit_rate(sig, fwd)
    # pairs: +/+, -/-, skip zero signal, +/-  → 2/3
    assert n == 3
    assert hr == pytest.approx(2.0 / 3.0)


def test_walk_forward_is_prefix() -> None:
    train, test = walk_forward_split(10)
    assert train.sum() == 7
    assert test.sum() == 3
    assert train[:7].all() and test[7:].all()
    assert not (train & test).any()
