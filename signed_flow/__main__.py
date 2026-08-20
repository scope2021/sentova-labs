"""CLI: python -m signed_flow [--symbol BTCUSDT] [--bars 1] [--fee-bps 4] [--max-trades 80000]"""

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


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="signed_flow",
        description=(
            "Order-flow imbalance research lab on public Binance aggTrades. "
            "Research and simulation only — no orders are placed."
        ),
    )
    p.add_argument("--symbol", default="BTCUSDT", help="Binance symbol (default BTCUSDT)")
    p.add_argument(
        "--bars",
        type=int,
        default=1,
        metavar="SEC",
        help="Bar length in seconds (default 1)",
    )
    p.add_argument(
        "--fee-bps",
        type=float,
        default=4.0,
        dest="fee_bps",
        help="Round-trip taker fee in basis points (default 4 = 0.0004)",
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
        help="Train |OFI| quantile used as the trade filter (default 0.70)",
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore data/ cache and re-download",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = _project_root()
    data_dir = root / "data"
    artifact_dir = root / "artifacts"

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


if __name__ == "__main__":
    sys.exit(main())
