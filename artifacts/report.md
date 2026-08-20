# Signed Flow — research note

Generated: 2026-08-20 15:38 UTC

## Verdict

After a conservative round-trip taker fee, the simple long/short is net negative on the locked test set at every horizon reported.

This is a research simulation on public tape. It does not place orders and
it does not claim a production edge.

## Data

| | |
|---|---|
| Symbol | `BTCUSDT` |
| Venue / source | `binance.us (cached)` |
| Trades | 80,000 |
| Sample (UTC) | 2026-07-28 05:58 → 2026-08-20 15:37 |
| Bar | 1s last-price bars, regular calendar grid |
| Bars (all / active) | 2,021,904 / 49,723 |
| Split | first 1,415,332 bars train (70%), last 606,572 test (30%) |
| |OFI| threshold | 0.00216 (train 70% quantile of active bars) |
| Fee | 4.0 bps round-trip, charged on every signal |

Signs follow the Binance `m` flag: `isBuyerMaker=true` means the buyer was
the maker, so the trade was **seller-initiated** (sign −1). Otherwise +1.
OFI is the sum of signed volume in the bar. Features at bar *t* use only
trades with exchange time in `[t, t+bar)`. The predicted variable is the
**forward** last-price return from the close of *t* to the close of *t+h*.

Last-price returns on short crypto bars are a noisy stand-in for mid
returns. Empty bars have OFI = 0 and a forward-filled last price (return 0
until the next trade). Information coefficients below are computed on test
bars that actually had trades.

- CLI: python -m signed_flow --symbol BTCUSDT --bars 1 --fee-bps 4.0 --max-trades 80000
- Tape span ≈ 561.64 hours; empty 1s bars are kept so horizon h is calendar time.
- api.binance.com was not used (restricted or failed); tape is from binance.us (cached).

## Method

1. Bucket public aggTrades into 1s bars.
2. OFI_t = Σ sign_i · qty_i over trades in the bar.
3. Forward return r_{t→t+h} = P_{t+h} / P_t − 1 for h ∈ {1, 5, 15}.
4. Spearman IC and hit rate on the **test** slice, active bars only.
5. Trading rule, parameters frozen from train: go +1 / −1 with sign(OFI)
   only when |OFI| ≥ train quantile; otherwise flat. Hold *h* bars.
6. Net = gross − 4.0 bps per signal (no maker rebate, no
   queue position, no fill model). Overlapping signals each pay the fee —
   this overstates costs on purpose.

Train is not used to pick horizons, features, or a “best” IC.

## Results (test)

| h | n | IC | t_naive | hit | n_traded | gross/bar | net/bar | gross/trade | net/trade |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 16,518 | -0.0069 | -0.89 | 0.398 | 4,998 | 1.6412e-07 | -3.1318e-06 | 1.9918e-05 | -3.8008e-04 |
| 5 | 16,517 | 0.0117 | 1.51 | 0.435 | 4,998 | 9.2506e-08 | -5.6668e-07 | 5.6133e-05 | -3.4387e-04 |
| 15 | 16,517 | 0.0370 | 4.76 | 0.467 | 4,998 | 5.0754e-08 | -1.6898e-07 | 9.2392e-05 | -3.0761e-04 |

`t_naive` is the textbook correlation t-stat. Overlapping horizons and
heteroskedastic crypto returns make it anti-conservative; treat it as a
label, not a p-value.

Cumulative P&L on the test set (horizon = 1 bar) is in `cum_pnl.png`.
OFI vs next-bar return is in `ofi_vs_ret.png`.

### Headline numbers (h = 1 bar)

- Spearman IC: **-0.0069** (n = 16,518)
- Hit rate: **0.398** (zeros dropped, n = 2,212)
- Gross mean per bar: **1.6412e-07**
- Net mean per bar: **-3.1318e-06**
- Gross mean per traded bar: **1.9918e-05**
- Net mean per traded bar: **-3.8008e-04**
- Cumulative gross (sum of overlapping 1-bar strategy returns): **0.099549**
- Cumulative net: **-1.899651**

## What this is not

- Not a Sharpe from a live book.
- Not evidence that aggressor flow is useless; L2 OFI, queue position,
  and maker/taker mix are different objects.
- Not a license to ignore latency, outages, or the fact that Binance last
  price is not a mid.

## Stdout table

```
Signed Flow — walk-forward test
symbol=BTCUSDT  bar=1s  fee=4.0 bps RT  venue=binance.us (cached)
trades=80,000  bars=2,021,904  active=49,723  train=1,415,332  test=606,572
sample: 2026-07-28 05:58 UTC → 2026-08-20 15:37 UTC
|OFI| threshold (train 70% pctile, active bars): 0.00216

   h        n       IC  t_naive     hit  n_traded    gross/bar      net/bar    gross/trd      net/trd
------------------------------------------------------------------------------------------------------------
   1    16518  -0.0069    -0.89   0.398      4998   1.6412e-07  -3.1318e-06   1.9918e-05  -3.8008e-04
   5    16517   0.0117     1.51   0.435      4998   9.2506e-08  -5.6668e-07   5.6133e-05  -3.4387e-04
  15    16517   0.0370     4.76   0.467      4998   5.0754e-08  -1.6898e-07   9.2392e-05  -3.0761e-04
------------------------------------------------------------------------------------------------------------
IC = Spearman(OFI, fwd last-price return) on test bars with trades.
hit = sign(OFI) vs sign(fwd), zeros dropped. t_naive assumes iid (it is not).
Strategy: long/short sign(OFI) if |OFI| >= threshold; hold h bars; 4.0 bps RT charged on every signal.
gross/bar and net/bar divide the holding-period mean by h and include flats.
```
