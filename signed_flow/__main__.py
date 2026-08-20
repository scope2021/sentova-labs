"""CLI: python -m signed_flow [--symbol BTCUSDT] ...
        python -m signed_flow paper [--symbol BTCUSDT] ...
"""

from __future__ import annotations

import argparse
import os

os.environ.setdefault("MPLBACKEND", "Agg")
import sys
from pathlib import Path

from signed_flow.binance import load_or_fetch
from signed_flow.features import trades_to_bars
from signed_flow.plots import write_all
from signed_flow.research import format_table, run_research, write_report
from signed_flow.risk import DEMO_LIMITS


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _add_tape_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--symbol", default="BTCUSDT", help="Binance symbol (default BTCUSDT)")
    p.add_argument(
        "--bars",
        type=int,
        default=1,
        metavar="SEC",
        help="Bar length in seconds (default 1)",
    )
    p.add_argument(
        "--max-trades",
        type=int,
        default=80_000,
        dest="max_trades",
        help="Max aggTrades to download / keep (default 80000)",
    )
    p.add_argument(
        "--quantile",
        type=float,
        default=0.70,
        help="Train / warmup |OFI| quantile used as the trade filter (default 0.70)",
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore data/ cache and re-download",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="sentova",
        description=(
            "Sentova Labs — order-flow imbalance research on public Binance aggTrades. "
            "Research and simulation only — no live mainnet orders."
        ),
        epilog="Paper replay: python -m signed_flow paper --help",
    )
    _add_tape_args(p)
    p.add_argument(
        "--fee-bps",
        type=float,
        default=4.0,
        dest="fee_bps",
        help="Round-trip taker fee in basis points (default 4 = 0.0004)",
    )
    return p.parse_args(argv)


def parse_paper_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="sentova paper",
        description=(
            "Sentova Labs — paper replay through the event-driven engine. "
            "Conservative next-print fills, taker fees, risk, kill switch. "
            "Not a money printer. Not mainnet."
        ),
    )
    _add_tape_args(p)
    p.add_argument(
        "--fee-bps",
        type=float,
        default=2.0,
        dest="fee_bps",
        help="One-way taker fee in basis points (default 2; round-trip ≈ 4)",
    )
    p.add_argument(
        "--order-qty",
        type=float,
        default=0.001,
        dest="order_qty",
        help="Target |position| when the OFI sign rule fires (default 0.001)",
    )
    p.add_argument(
        "--starting-cash",
        type=float,
        default=10_000.0,
        dest="starting_cash",
        help="Paper cash (default 10000)",
    )
    p.add_argument(
        "--warmup",
        type=float,
        default=0.30,
        help="Prefix fraction of prints used only to freeze |OFI| threshold (default 0.30)",
    )
    p.add_argument("--max-notional", type=float, default=DEMO_LIMITS.max_notional)
    p.add_argument("--max-position", type=float, default=DEMO_LIMITS.max_position)
    p.add_argument("--max-order-qty", type=float, default=DEMO_LIMITS.max_order_qty)
    p.add_argument(
        "--max-order-notional", type=float, default=DEMO_LIMITS.max_order_notional
    )
    p.add_argument(
        "--max-orders-per-window",
        type=int,
        default=DEMO_LIMITS.max_orders_per_window,
    )
    p.add_argument(
        "--order-window-ms", type=int, default=DEMO_LIMITS.order_window_ms
    )
    p.add_argument("--daily-loss", type=float, default=DEMO_LIMITS.daily_loss)
    return p.parse_args(argv)


def _load_tape(args, data_dir: Path):
    tape = load_or_fetch(
        symbol=args.symbol,
        max_trades=args.max_trades,
        data_dir=data_dir,
        refresh=args.refresh,
    )
    trades = tape.trades
    t0 = int(trades["time"].min())
    t1 = int(trades["time"].max())
    span_h = (t1 - t0) / 1000.0 / 3600.0
    print(
        f"tape: {len(trades):,} trades  {tape.symbol}  source={tape.source}  "
        f"span={span_h:.2f}h"
    )
    return tape, span_h


def run_research_cli(args: argparse.Namespace) -> int:
    root = _project_root()
    data_dir = root / "data"
    artifact_dir = root / "artifacts"
    tape, span_h = _load_tape(args, data_dir)
    trades = tape.trades

    print(f"bucketing into {args.bars}s bars ...")
    bars = trades_to_bars(trades, bar_seconds=args.bars, regular_grid=True)
    print(f"bars: {len(bars):,}  active={(bars['volume'] > 0).sum():,}")

    result = run_research(
        bars,
        symbol=tape.symbol,
        bar_seconds=args.bars,
        fee_bps=args.fee_bps,
        source=tape.source,
        n_trades=len(trades),
        quantile=args.quantile,
    )
    table = format_table(result)
    print()
    print(table)
    print()

    notes = [
        f"CLI: python -m signed_flow --symbol {args.symbol} --bars {args.bars} "
        f"--fee-bps {args.fee_bps} --max-trades {args.max_trades}",
        f"Tape span ≈ {span_h:.2f} hours; empty {args.bars}s bars are kept so "
        f"horizon h is calendar time.",
    ]
    if "binance.com" not in tape.source and "SYNTHETIC" not in tape.source:
        notes.append(
            "api.binance.com was not used (restricted or failed); "
            f"tape is from {tape.source}."
        )
    if "SYNTHETIC" in tape.source.upper():
        notes.append(
            "SYNTHETIC fallback: no planted forward-looking alpha; "
            "signs are mildly autocorrelated with contemporaneous impact only."
        )

    report_path = artifact_dir / "report.md"
    write_report(result, report_path, extra_notes=notes)
    paths = write_all(result, artifact_dir)
    print(f"wrote {report_path}")
    for pth in paths:
        print(f"wrote {pth}")
    return 0


def run_paper_cli(args: argparse.Namespace) -> int:
    from signed_flow.paper import limits_from_args, run_and_write

    root = _project_root()
    tape, span_h = _load_tape(args, root / "data")
    notes = [
        f"CLI: python -m signed_flow paper --symbol {args.symbol} --bars {args.bars} "
        f"--fee-bps {args.fee_bps} --order-qty {args.order_qty} "
        f"--max-trades {args.max_trades}",
        f"Tape span ≈ {span_h:.2f} hours. Fill model: next print after bar close, "
        f"taker {args.fee_bps:.1f} bps one-way.",
        "Sentova Labs paper engine — not live mainnet, not a money printer.",
    ]
    if "SYNTHETIC" in tape.source.upper():
        notes.append("SYNTHETIC tape: not a market result.")
    print(
        f"paper replay: bar={args.bars}s  taker={args.fee_bps:.1f}bps  "
        f"qty={args.order_qty}  warmup={args.warmup:.0%}"
    )
    result = run_and_write(
        tape.trades,
        symbol=tape.symbol,
        source=tape.source,
        bar_seconds=args.bars,
        taker_fee_bps=args.fee_bps,
        order_qty=args.order_qty,
        starting_cash=args.starting_cash,
        quantile=args.quantile,
        warmup_frac=args.warmup,
        limits=limits_from_args(args),
        artifact_dir=root / "artifacts",
        extra_notes=notes,
    )
    print(
        f"fills={result.n_fills:,}  rejects={result.n_rejects:,}  "
        f"kills={result.n_kills:,}  net={result.net_pnl:,.4f}  "
        f"equity={result.ending_equity:,.4f}"
    )
    print(f"wrote {root / 'artifacts' / 'paper_report.md'}")
    print(f"wrote {root / 'artifacts' / 'paper_equity.png'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "paper":
        return run_paper_cli(parse_paper_args(argv[1:]))
    if argv and argv[0] == "research":
        argv = argv[1:]
    return run_research_cli(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
