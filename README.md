# Sentova Labs

Order-flow research. Walk-forward, after costs.

A small, reproducible study of whether **aggressive order flow** on a crypto
spot tape predicts **short-horizon last-price returns**, after a conservative
taker fee. It is a research lab, not a strategy pitch. If the signal dies
after costs, that is the result.

## What this is

Public Binance aggregate trades are signed by the `isBuyerMaker` flag,
bucketed into calendar bars, and reduced to a single number per bar: an
order-flow imbalance (OFI) proxy equal to signed volume. That number is
correlated with the *forward* last-price return, then run through a
walk-forward long/short with a fee. One command downloads (or caches) the
tape, prints a table, and writes `artifacts/report.md` plus a few plots.
No API keys. No orders. No live trading.

## Hypothesis

Cont, Kukanov, and Stoikov (and the market-microstructure literature
behind them) treat incoming aggressive flow as information about the
short-run path of the mid: buy-initiated volume lifts the quote, sell-
initiated volume hits it. The testable claim here is narrower and worse
instrumented.

On a 1-second crypto bar, using **last trade price** rather than a
true mid, does `sign(volume_buy − volume_sell)` line up with the next
few bars of return, and does a dead-simple long/short survive a 4 bp
round-trip taker fee?

It should not be surprising if the answer is “weakly, then no.” Last
price is a noisy mid proxy; 1s crypto bars mix queue dynamics, latency,
and self-trading; and 4 bp is a lot of edge to find in a second.

## Data

- Venue: Binance public REST `GET /api/v3/aggTrades` (no key). The
  client tries `api.binance.com`, then `api.binance.us`. If both fail,
  a synthetic tape is generated and the report is marked **SYNTHETIC**.
- Default symbol: `BTCUSDT`. Default sample: ~80k most-recent aggTrades,
  cached under `data/`.
- Sign convention: `m = true` (buyer was the maker) ⇒ the seller was
  the aggressor ⇒ sign **−1**. Otherwise **+1**. Signed volume is
  `sign · qty`. This is the usual Lee–Ready / trade-flag convention,
  not a guessed tick rule.
- The public endpoint returns a short recent window, not years of
  history. Sample length in clock time depends on how busy the venue
  is; Binance.US is much thinner than Binance.com. The report always
  prints the UTC span.

## Method

1. Bucket trades into calendar bars of `--bars` seconds (default 1s).
   Empty bars are kept, with OFI = 0 and last price forward-filled, so
   a horizon of *h* bars is *h* seconds of clock time.
2. **No lookahead.** Features at bar *t* use only trades with exchange
   time in `[t, t+bar)`. The label is the forward last-price return
   `P_{t+h}/P_t − 1`, not the contemporaneous bar return.
3. On the **test** slice, report Spearman IC of OFI vs that forward
   return (active bars only), and a hit rate of `sign(OFI)` vs
   `sign(return)` with zeros dropped.
4. Walk-forward: first 70% of bars are train, last 30% are test. Train
   is used for **one** number: the 70% quantile of `|OFI|` on active
   train bars. Test is never used to pick a threshold, a horizon, or a
   feature.
5. Trading rule: if `|OFI|` ≥ that threshold, hold `sign(OFI)` for *h*
   bars; else flat. Gross mean return per bar is the holding-period
   P&L divided by *h*, including flats. Net subtracts `--fee-bps`
   (default 4) as a round-trip fraction on **every** signal. That fee
   assumption is intentionally harsh (overlapping signals each pay).

Horizons: 1, 5, 15 bars.

## Results

The numbers live in `artifacts/report.md`, which the CLI overwrites on
every run. Read that file, not this paragraph. Things to look at:

- Spearman IC and hit rate at h = 1, 5, 15 on the **test** slice.
- Gross vs net mean return per bar. If net is negative, the fee ate
  the signal; that is the finding, not a bug.
- `artifacts/cum_pnl.png` — test cumulative P&L, gross vs net, h = 1.
- `artifacts/ofi_vs_ret.png` — scatter of OFI vs next-bar return.
- Sample length and whether the source is `binance.com`, `binance.us`,
  or `SYNTHETIC`. A synthetic run is not a market result.

<!-- RESULTS -->

## Results (latest run)

Run date: **20 Aug 2026, 21:07 IST** (15:37 UTC).  
Symbol `BTCUSDT`. **Live public tape from `api.binance.us`** (80,000
aggTrades, 28 Jul 2026 05:58 UTC → 20 Aug 2026 15:37 UTC, ~562 hours).
`api.binance.com` returned HTTP 451 (geo-restricted from this host);
nothing here is synthetic.

| | |
|---|---|
| Trades / 1s bars / active bars | 80,000 / 2,021,904 / 49,723 |
| Walk-forward | first 70% of bars train, last 30% test |
| Train abs(OFI) 70% quantile (active) | 0.00216 |
| Fee | 4.0 bps round-trip on every signal |

Test-set numbers (active bars for IC/hit; strategy filtered by the
frozen train threshold):

| h | n | Spearman IC | hit rate | n traded | gross / bar | net / bar |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 16,518 | −0.0069 | 0.398 | 4,998 | +1.64e−07 | **−3.13e−06** |
| 5 | 16,517 | +0.0117 | 0.435 | 4,998 | +9.25e−08 | **−5.67e−07** |
| 15 | 16,517 | +0.0370 | 0.467 | 4,998 | +5.08e−08 | **−1.69e−07** |

At h = 1 the gross mean per traded bar is about **+0.20 bp**; the 4 bp
round-trip fee leaves about **−3.8 bp** per trade. Cumulative test P&L
at h = 1 is +0.10 gross and **−1.90 net**.

**The signal does not survive costs.** Hit rate on unfiltered active
bars is below a coin flip at every horizon (most 1s last-price moves
after a print are zero and are dropped). Gross P&L of the *thresholded*
rule is microscopically positive because the wins are slightly larger
than the losses; it is nowhere near 4 bp. Last-price 1s bars on a thin
US-venue tape are a noisy mid proxy, and this is not a trading
recommendation. Full table, caveats, and figures: `artifacts/report.md`.

## How to run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python -m signed_flow --help
python -m signed_flow --symbol BTCUSDT --bars 1 --fee-bps 4 --max-trades 80000

pytest -q
```

Reruns use `data/*_aggtrades.csv.gz` unless you pass `--refresh`.
Plots and the research note land in `artifacts/`.

## What would make a desk take this seriously next

- **L2 book OFI** (Cont–Kukanov–Stoikov depth imbalance, or queue
  depletion at the touch), not last-trade signed volume.
- A real **mid**, not last price; timestamp alignment to the book.
- **Maker** fees / rebates and a fill model, not a blanket 4 bp taker
  round-trip on every bar.
- Queue position, cancel/replace, and toxicity filters.
- Multi-venue (lead/lag between Binance, Coinbase, CME basis).
- Months of data, not tens of thousands of recent aggTrades; a proper
  event study around inventory and news.
- Cross-sectional names, not one symbol.

Until those are in the notebook, this is a clean measurement of a weak
proxy, not a reason to bid for flow.
