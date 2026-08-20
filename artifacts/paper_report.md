# Sentova Labs — paper replay

Generated: 2026-08-20 16:10 UTC

## Verdict

Net of taker fees and a conservative next-print fill model, this paper replay lost money. That is consistent with the research note: the OFI sign rule does not survive costs. The daily-loss kill switch fired 5 time(s) and flatten orders were sent to the paper broker.

This is **not** a live book, **not** mainnet, and **not** a money printer.
Fills are simulated against later public tape. Testnet is a separate adapter
pinned to `testnet.binance.vision` and is not used by this report.

## Data

| | |
|---|---|
| Symbol | `BTCUSDT` |
| Venue / source | `binance.us (cached)` |
| Prints | 80,000 |
| Sample (UTC) | 2026-07-28 05:58 → 2026-08-20 15:37 (561.64 h) |
| Bar | 1s OFI, closed on the next print |
| Warmup prints | 24,000 (threshold frozen after this prefix) |
| Bars closed | 49,723 |
| \|OFI\| threshold | 0.00319 (warmup 70% quantile) |
| Taker fee | 2.0 bps **one-way** on every fill |
| Starting cash | 10,000.00 |

- CLI: python -m signed_flow paper --symbol BTCUSDT --bars 1 --fee-bps 2.0 --order-qty 0.001 --max-trades 80000
- Tape span ≈ 561.64 hours. Fill model: next print after bar close, taker 2.0 bps one-way.
- Sentova Labs paper engine — not live mainnet, not a money printer.

## Fill model

1. Trades are replayed in exchange-time order.
2. OFI of bar *t* uses only prints in `[t, t+bar)`.
3. The existing sign rule: if `|OFI| ≥ threshold`, target `sign(OFI) × order_qty`, else flat.
4. The order is submitted at bar close with `not_before = bar_end`. The paper broker **refuses** any fill whose print time is earlier than that. Same-bar tape cannot fill the signal that bar produced.
5. Fills consume later prints at the **printed price** (taker). No mid, no queue, no maker rebate, no partial-spread fantasy.
6. Risk runs before every non-flatten submit. Daily-loss kill flattens via a reduce-only market order on subsequent (or current, for the kill itself) tape.

## Risk (all on)

| Limit | Demo default |
|---|---|
| max notional | 200 |
| max position | 0.002 |
| max order qty | 0.001 |
| max order notional | 80 |
| max orders / window | 6 / 60s |
| daily loss kill | 8 |

Kills this run: daily_loss -8.5640 <= -8.0; daily_loss -8.1172 <= -8.0; daily_loss -8.0164 <= -8.0; daily_loss -8.0094 <= -8.0; daily_loss -8.0065 <= -8.0

## Results

| | |
|---|---|
| Signals | 8,942 |
| Orders submitted | 14,346 |
| Fills | 21,622 |
| Cancels | 2,008 |
| Risk rejects | 347 |
| Risk clips | 3,532 |
| Kill events | 5 |
| Ending qty | 0.00000000 |
| Ending cash | 9,885.0507 |
| Ending equity | 9,885.0507 |
| Realized (gross of fees) | 7.1584 |
| Unrealized | 0.0000 |
| Fees paid | 122.1077 |
| Net PnL | **-114.9493** |
| Equity min / max | 9,885.0485 / 10,000.0000 |

Equity curve: `paper_equity.png`.

## What a desk would still need

- A real L2 fill model (touch size, queue position, cancel/replace).
- Latency budget and matching-engine ack timeouts.
- Reconcile vs exchange drops, partials, and self-trades.
- Human-ack kill, pager, and a written flatten playbook.
- Per-name and gross book limits that survive a restart.
- This OFI last-trade proxy is **not** that book — the research note already showed the signal dying after a 4 bp round-trip.

## What this is not

- Not mainnet. The testnet adapter will refuse `api.binance.com`.
- Not a withdrawal client. Those paths are compile-time absent.
- Not advice to trade the OFI sign rule with real money.
