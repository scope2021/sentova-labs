"""Paper replay CLI helper: run the engine, write artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from signed_flow.engine import ReplayResult, run_replay
from signed_flow.risk import RiskLimits


def write_paper_report(
    result: ReplayResult,
    path: Path,
    extra_notes: list[str] | None = None,
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    t0 = datetime.fromtimestamp(result.t0_ms / 1000, tz=timezone.utc)
    t1 = datetime.fromtimestamp(result.t1_ms / 1000, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    lim = result.limits
    span_h = (result.t1_ms - result.t0_ms) / 1000.0 / 3600.0
    extra = ""
    if extra_notes:
        extra = "\n".join(f"- {n}" for n in extra_notes) + "\n\n"

    killed = bool(result.kill_notes)
    pnl = result.net_pnl
    if pnl < 0:
        verdict = (
            "Net of taker fees and a conservative next-print fill model, "
            "this paper replay lost money. That is consistent with the research "
            "note: the OFI sign rule does not survive costs."
        )
    elif pnl == 0:
        verdict = (
            "Paper replay finished flat (no fills, or fills that netted to zero). "
            "That is not an edge."
        )
    else:
        verdict = (
            "Paper replay finished with positive net PnL on this sample. "
            "Treat it as a fill-model diagnostic, not a reason to size up: "
            "the research IC is weak and last-price crypto bars are a noisy mid."
        )
    if killed:
        verdict += (
            f" The daily-loss kill switch fired {result.n_kills} time(s) "
            "and flatten orders were sent to the paper broker."
        )

    kills = "none"
    if result.kill_notes:
        kills = "; ".join(result.kill_notes)

    md = f"""# Sentova Labs — paper replay

Generated: {now.strftime("%Y-%m-%d %H:%M UTC")}

## Verdict

{verdict}

This is **not** a live book, **not** mainnet, and **not** a money printer.
Fills are simulated against later public tape. Testnet is a separate adapter
pinned to `testnet.binance.vision` and is not used by this report.

## Data

| | |
|---|---|
| Symbol | `{result.symbol}` |
| Venue / source | `{result.source}` |
| Prints | {result.n_prints:,} |
| Sample (UTC) | {t0.strftime("%Y-%m-%d %H:%M")} → {t1.strftime("%Y-%m-%d %H:%M")} ({span_h:.2f} h) |
| Bar | {result.bar_seconds}s OFI, closed on the next print |
| Warmup prints | {result.warmup_prints:,} (threshold frozen after this prefix) |
| Bars closed | {result.n_bars:,} |
| \\|OFI\\| threshold | {result.threshold:.6g} (warmup {result.quantile:.0%} quantile) |
| Taker fee | {result.taker_fee_bps:.1f} bps **one-way** on every fill |
| Starting cash | {result.starting_cash:,.2f} |

{extra}## Fill model

1. Trades are replayed in exchange-time order.
2. OFI of bar *t* uses only prints in `[t, t+bar)`.
3. The existing sign rule: if `|OFI| ≥ threshold`, target `sign(OFI) × order_qty`, else flat.
4. The order is submitted at bar close with `not_before = bar_end`. The paper broker **refuses** any fill whose print time is earlier than that. Same-bar tape cannot fill the signal that bar produced.
5. Fills consume later prints at the **printed price** (taker). No mid, no queue, no maker rebate, no partial-spread fantasy.
6. Risk runs before every non-flatten submit. Daily-loss kill flattens via a reduce-only market order on subsequent (or current, for the kill itself) tape.

## Risk (all on)

| Limit | Demo default |
|---|---|
| max notional | {lim.max_notional:g} |
| max position | {lim.max_position:g} |
| max order qty | {lim.max_order_qty:g} |
| max order notional | {lim.max_order_notional:g} |
| max orders / window | {lim.max_orders_per_window} / {lim.order_window_ms/1000:.0f}s |
| daily loss kill | {lim.daily_loss:g} |

Kills this run: {kills}

## Results

| | |
|---|---|
| Signals | {result.n_signals:,} |
| Orders submitted | {result.n_orders:,} |
| Fills | {result.n_fills:,} |
| Cancels | {result.n_cancels:,} |
| Risk rejects | {result.n_rejects:,} |
| Risk clips | {result.n_clips:,} |
| Kill events | {result.n_kills:,} |
| Ending qty | {result.ending_qty:.8f} |
| Ending cash | {result.ending_cash:,.4f} |
| Ending equity | {result.ending_equity:,.4f} |
| Realized (gross of fees) | {result.realized:,.4f} |
| Unrealized | {result.unrealized:,.4f} |
| Fees paid | {result.fees:,.4f} |
| Net PnL | **{result.net_pnl:,.4f}** |
| Equity min / max | {result.min_equity:,.4f} / {result.max_equity:,.4f} |

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
"""
    path.write_text(md, encoding="utf-8")


def limits_from_args(args) -> RiskLimits:
    return RiskLimits(
        max_notional=float(args.max_notional),
        max_position=float(args.max_position),
        max_order_qty=float(args.max_order_qty),
        max_order_notional=float(args.max_order_notional),
        max_orders_per_window=int(args.max_orders_per_window),
        order_window_ms=int(args.order_window_ms),
        daily_loss=float(args.daily_loss),
    )


def run_and_write(
    trades,
    *,
    symbol: str,
    source: str,
    bar_seconds: int,
    taker_fee_bps: float,
    order_qty: float,
    starting_cash: float,
    quantile: float,
    warmup_frac: float,
    limits: RiskLimits,
    artifact_dir: Path,
    extra_notes: list[str] | None = None,
) -> ReplayResult:
    from signed_flow.plots import plot_equity_curve

    result = run_replay(
        trades,
        symbol=symbol,
        source=source,
        bar_seconds=bar_seconds,
        taker_fee_bps=taker_fee_bps,
        order_qty=order_qty,
        starting_cash=starting_cash,
        quantile=quantile,
        warmup_frac=warmup_frac,
        limits=limits,
    )
    artifact_dir = Path(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    report = artifact_dir / "paper_report.md"
    write_paper_report(result, report, extra_notes=extra_notes)
    plot_equity_curve(
        result.equity_t,
        result.equity_v,
        artifact_dir / "paper_equity.png",
        symbol=result.symbol,
        fee_bps=result.taker_fee_bps,
        net_pnl=result.net_pnl,
    )
    return result
