"""Walk-forward OFI → forward-return research.

Train (first 70%) is used only to pick an |OFI| quantile threshold.
Test (last 30%) is never used for that choice. Costs are a conservative
round-trip taker fee charged on every signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from signed_flow.features import forward_return

HORIZONS = (1, 5, 15)
TRAIN_FRAC = 0.70
DEFAULT_QUANTILE = 0.70


def _rank(x: np.ndarray) -> np.ndarray:
    """Average ranks, 1..n. Ties share the mean rank (Spearman convention)."""
    return pd.Series(x).rank(method="average").to_numpy(dtype=np.float64)


def pearson(x: np.ndarray, y: np.ndarray) -> float:
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    if x.size < 3:
        return float("nan")
    x = x - x.mean()
    y = y - y.mean()
    denom = np.sqrt(np.dot(x, x) * np.dot(y, y))
    if denom == 0.0:
        return float("nan")
    return float(np.dot(x, y) / denom)


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    return pearson(_rank(x), _rank(y))


def hit_rate(signal: np.ndarray, fwd: np.ndarray) -> tuple[float, int]:
    """Fraction of times sign(signal) matches sign(fwd); zeros skipped."""
    s = np.sign(signal)
    r = np.sign(fwd)
    mask = (s != 0) & (r != 0) & np.isfinite(s) & np.isfinite(r)
    n = int(mask.sum())
    if n == 0:
        return float("nan"), 0
    return float(np.mean(s[mask] == r[mask])), n


def naive_tstat(corr: float, n: int) -> float:
    """IID t-stat for a correlation. Overlapping bars violate IID; diagnostic only."""
    if n <= 2 or not np.isfinite(corr) or abs(corr) >= 1.0:
        return float("nan")
    return float(corr * np.sqrt(n - 2) / np.sqrt(1.0 - corr * corr))


@dataclass
class HorizonResult:
    horizon: int
    n_obs: int
    ic: float
    ic_tstat: float
    hit_rate: float
    n_hit: int
    n_traded: int
    gross_mean_per_bar: float
    net_mean_per_bar: float
    gross_mean_per_trade: float
    net_mean_per_trade: float
    gross_cum: float
    net_cum: float


@dataclass
class ResearchResult:
    symbol: str
    bar_seconds: int
    fee_bps: float
    source: str
    n_trades: int
    n_bars: int
    n_active: int
    n_train: int
    n_test: int
    threshold: float
    quantile: float
    t0_ms: int
    t1_ms: int
    horizons: list[HorizonResult] = field(default_factory=list)
    # test-set series for plots (horizon=1)
    test_index: np.ndarray | None = None
    test_ofi: np.ndarray | None = None
    test_fwd1: np.ndarray | None = None
    test_gross_h1: np.ndarray | None = None
    test_net_h1: np.ndarray | None = None
    all_ofi: np.ndarray | None = None
    all_fwd1: np.ndarray | None = None


def walk_forward_split(n: int, train_frac: float = TRAIN_FRAC) -> tuple[np.ndarray, np.ndarray]:
    cut = int(n * train_frac)
    if cut < 1 or n - cut < 1:
        raise ValueError(f"not enough bars for a 70/30 split: n={n}")
    train = np.zeros(n, dtype=bool)
    test = np.zeros(n, dtype=bool)
    train[:cut] = True
    test[cut:] = True
    return train, test


def run_research(
    bars: pd.DataFrame,
    *,
    symbol: str,
    bar_seconds: int,
    fee_bps: float,
    source: str,
    n_trades: int,
    quantile: float = DEFAULT_QUANTILE,
    horizons: tuple[int, ...] = HORIZONS,
) -> ResearchResult:
    """Evaluate OFI vs forward last-price returns with a locked test set."""
    n = len(bars)
    train_mask, test_mask = walk_forward_split(n)
    ofi = bars["ofi"].to_numpy(dtype=np.float64)
    active = bars["volume"].to_numpy(dtype=np.float64) > 0
    train_active = train_mask & active
    abs_ofi_train = np.abs(ofi[train_active])
    if abs_ofi_train.size == 0:
        raise ValueError("train split has no active bars")
    threshold = float(np.quantile(abs_ofi_train, quantile))

    fee = float(fee_bps) * 1e-4  # bps round-trip → fraction

    result = ResearchResult(
        symbol=symbol,
        bar_seconds=bar_seconds,
        fee_bps=fee_bps,
        source=source,
        n_trades=n_trades,
        n_bars=n,
        n_active=int(active.sum()),
        n_train=int(train_mask.sum()),
        n_test=int(test_mask.sum()),
        threshold=threshold,
        quantile=quantile,
        t0_ms=int(bars.index[0]),
        t1_ms=int(bars.index[-1]),
    )

    px = bars["last_price"]
    for h in horizons:
        fwd = forward_return(px, h).to_numpy(dtype=np.float64)
        valid = np.isfinite(fwd) & np.isfinite(ofi)
        # IC / hit-rate: test + had flow this bar (the interesting question)
        eval_mask = test_mask & active & valid
        x = ofi[eval_mask]
        y = fwd[eval_mask]
        ic = spearman(x, y)
        hr, n_hit = hit_rate(x, y)

        # Strategy on test: trade only when |OFI| >= train threshold.
        trade = test_mask & valid & (np.abs(ofi) >= threshold) & (ofi != 0)
        pos = np.where(trade, np.sign(ofi), 0.0)
        gross = pos * fwd
        net = np.where(trade, pos * fwd - fee, 0.0)

        n_traded = int(trade.sum())
        n_test_valid = int((test_mask & valid).sum())
        if n_traded == 0 or n_test_valid == 0:
            hres = HorizonResult(
                horizon=h,
                n_obs=int(eval_mask.sum()),
                ic=ic,
                ic_tstat=naive_tstat(ic, int(eval_mask.sum())),
                hit_rate=hr,
                n_hit=n_hit,
                n_traded=0,
                gross_mean_per_bar=float("nan"),
                net_mean_per_bar=float("nan"),
                gross_mean_per_trade=float("nan"),
                net_mean_per_trade=float("nan"),
                gross_cum=0.0,
                net_cum=0.0,
            )
        else:
            # Mean per bar: average over all valid test bars (flats count as 0).
            # Per trade: average over bars we actually took a position.
            hres = HorizonResult(
                horizon=h,
                n_obs=int(eval_mask.sum()),
                ic=ic,
                ic_tstat=naive_tstat(ic, int(eval_mask.sum())),
                hit_rate=hr,
                n_hit=n_hit,
                n_traded=n_traded,
                gross_mean_per_bar=float(np.mean(gross[test_mask & valid]) / h),
                net_mean_per_bar=float(np.mean(net[test_mask & valid]) / h),
                gross_mean_per_trade=float(np.mean(gross[trade])),
                net_mean_per_trade=float(np.mean(net[trade])),
                gross_cum=float(np.sum(gross[test_mask & valid])),
                net_cum=float(np.sum(net[test_mask & valid])),
            )
        result.horizons.append(hres)

        if h == 1:
            t_idx = np.flatnonzero(test_mask & valid)
            result.test_index = bars.index.to_numpy()[t_idx]
            result.test_ofi = ofi[t_idx]
            result.test_fwd1 = fwd[t_idx]
            result.test_gross_h1 = gross[t_idx]
            result.test_net_h1 = net[t_idx]
            a_idx = np.flatnonzero(active & valid)
            result.all_ofi = ofi[a_idx]
            result.all_fwd1 = fwd[a_idx]

    return result


def format_table(result: ResearchResult) -> str:
    from datetime import datetime, timezone

    t0 = datetime.fromtimestamp(result.t0_ms / 1000, tz=timezone.utc)
    t1 = datetime.fromtimestamp(result.t1_ms / 1000, tz=timezone.utc)
    lines = [
        "Sentova Labs — walk-forward test",
        f"symbol={result.symbol}  bar={result.bar_seconds}s  "
        f"fee={result.fee_bps:.1f} bps RT  venue={result.source}",
        f"trades={result.n_trades:,}  bars={result.n_bars:,}  "
        f"active={result.n_active:,}  train={result.n_train:,}  test={result.n_test:,}",
        f"sample: {t0.strftime('%Y-%m-%d %H:%M')} UTC → {t1.strftime('%Y-%m-%d %H:%M')} UTC",
        f"|OFI| threshold (train {result.quantile:.0%} pctile, active bars): "
        f"{result.threshold:.6g}",
        "",
        f"{'h':>4} {'n':>8} {'IC':>8} {'t_naive':>8} {'hit':>7} "
        f"{'n_traded':>9} {'gross/bar':>12} {'net/bar':>12} "
        f"{'gross/trd':>12} {'net/trd':>12}",
        "-" * 108,
    ]
    for h in result.horizons:
        lines.append(
            f"{h.horizon:4d} {h.n_obs:8d} {h.ic:8.4f} {h.ic_tstat:8.2f} "
            f"{h.hit_rate:7.3f} {h.n_traded:9d} "
            f"{h.gross_mean_per_bar:12.4e} {h.net_mean_per_bar:12.4e} "
            f"{h.gross_mean_per_trade:12.4e} {h.net_mean_per_trade:12.4e}"
        )
    lines += [
        "-" * 108,
        "IC = Spearman(OFI, fwd last-price return) on test bars with trades.",
        "hit = sign(OFI) vs sign(fwd), zeros dropped. t_naive assumes iid (it is not).",
        f"Strategy: long/short sign(OFI) if |OFI| >= threshold; hold h bars; "
        f"{result.fee_bps:.1f} bps RT charged on every signal.",
        "gross/bar and net/bar divide the holding-period mean by h and include flats.",
    ]
    return "\n".join(lines)


def write_report(result: ResearchResult, path, extra_notes: list[str] | None = None) -> None:
    from datetime import datetime, timezone
    from pathlib import Path

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    t0 = datetime.fromtimestamp(result.t0_ms / 1000, tz=timezone.utc)
    t1 = datetime.fromtimestamp(result.t1_ms / 1000, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    synthetic = "SYNTHETIC" in result.source.upper()

    h1 = next(h for h in result.horizons if h.horizon == 1)
    net_neg = any(
        np.isfinite(h.net_mean_per_bar) and h.net_mean_per_bar < 0 for h in result.horizons
    )

    verdict_bits = []
    if synthetic:
        verdict_bits.append(
            "DATA IS SYNTHETIC. Do not treat these numbers as a market result."
        )
    if net_neg:
        verdict_bits.append(
            "After a conservative round-trip taker fee, the simple long/short "
            "is net negative on the locked test set at every horizon reported."
            if all(
                np.isfinite(h.net_mean_per_bar) and h.net_mean_per_bar < 0
                for h in result.horizons
            )
            else "After a conservative round-trip taker fee, net P&L is negative "
            "at one or more horizons on the locked test set."
        )
    else:
        verdict_bits.append(
            "Net of the assumed taker fee the simple rule is not obviously dead, "
            "but last-price 1s crypto bars are a noisy mid proxy and this is not "
            "a trading recommendation."
        )

    rows = []
    for h in result.horizons:
        rows.append(
            f"| {h.horizon} | {h.n_obs:,} | {h.ic:.4f} | {h.ic_tstat:.2f} | "
            f"{h.hit_rate:.3f} | {h.n_traded:,} | {h.gross_mean_per_bar:.4e} | "
            f"{h.net_mean_per_bar:.4e} | {h.gross_mean_per_trade:.4e} | "
            f"{h.net_mean_per_trade:.4e} |"
        )

    extra = ""
    if extra_notes:
        extra = "\n".join(f"- {n}" for n in extra_notes) + "\n\n"

    md = f"""# Sentova Labs — research note

Generated: {now.strftime("%Y-%m-%d %H:%M UTC")}

## Verdict

{' '.join(verdict_bits)}

This is a research simulation on public tape. It does not place orders and
it does not claim a production edge.

## Data

| | |
|---|---|
| Symbol | `{result.symbol}` |
| Venue / source | `{result.source}` |
| Trades | {result.n_trades:,} |
| Sample (UTC) | {t0.strftime("%Y-%m-%d %H:%M")} → {t1.strftime("%Y-%m-%d %H:%M")} |
| Bar | {result.bar_seconds}s last-price bars, regular calendar grid |
| Bars (all / active) | {result.n_bars:,} / {result.n_active:,} |
| Split | first {result.n_train:,} bars train (70%), last {result.n_test:,} test (30%) |
| |OFI| threshold | {result.threshold:.6g} (train {result.quantile:.0%} quantile of active bars) |
| Fee | {result.fee_bps:.1f} bps round-trip, charged on every signal |

Signs follow the Binance `m` flag: `isBuyerMaker=true` means the buyer was
the maker, so the trade was **seller-initiated** (sign −1). Otherwise +1.
OFI is the sum of signed volume in the bar. Features at bar *t* use only
trades with exchange time in `[t, t+bar)`. The predicted variable is the
**forward** last-price return from the close of *t* to the close of *t+h*.

Last-price returns on short crypto bars are a noisy stand-in for mid
returns. Empty bars have OFI = 0 and a forward-filled last price (return 0
until the next trade). Information coefficients below are computed on test
bars that actually had trades.

{extra}## Method

1. Bucket public aggTrades into {result.bar_seconds}s bars.
2. OFI_t = Σ sign_i · qty_i over trades in the bar.
3. Forward return r_{{t→t+h}} = P_{{t+h}} / P_t − 1 for h ∈ {{1, 5, 15}}.
4. Spearman IC and hit rate on the **test** slice, active bars only.
5. Trading rule, parameters frozen from train: go +1 / −1 with sign(OFI)
   only when |OFI| ≥ train quantile; otherwise flat. Hold *h* bars.
6. Net = gross − {result.fee_bps:.1f} bps per signal (no maker rebate, no
   queue position, no fill model). Overlapping signals each pay the fee —
   this overstates costs on purpose.

Train is not used to pick horizons, features, or a “best” IC.

## Results (test)

| h | n | IC | t_naive | hit | n_traded | gross/bar | net/bar | gross/trade | net/trade |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

`t_naive` is the textbook correlation t-stat. Overlapping horizons and
heteroskedastic crypto returns make it anti-conservative; treat it as a
label, not a p-value.

Cumulative P&L on the test set (horizon = 1 bar) is in `cum_pnl.png`.
OFI vs next-bar return is in `ofi_vs_ret.png`.

### Headline numbers (h = 1 bar)

- Spearman IC: **{h1.ic:.4f}** (n = {h1.n_obs:,})
- Hit rate: **{h1.hit_rate:.3f}** (zeros dropped, n = {h1.n_hit:,})
- Gross mean per bar: **{h1.gross_mean_per_bar:.4e}**
- Net mean per bar: **{h1.net_mean_per_bar:.4e}**
- Gross mean per traded bar: **{h1.gross_mean_per_trade:.4e}**
- Net mean per traded bar: **{h1.net_mean_per_trade:.4e}**
- Cumulative gross (sum of overlapping 1-bar strategy returns): **{h1.gross_cum:.6f}**
- Cumulative net: **{h1.net_cum:.6f}**

## What this is not

- Not a Sharpe from a live book.
- Not evidence that aggressor flow is useless; L2 OFI, queue position,
  and maker/taker mix are different objects.
- Not a license to ignore latency, outages, or the fact that Binance last
  price is not a mid.

## Stdout table

```
{format_table(result)}
```
"""
    path.write_text(md, encoding="utf-8")
