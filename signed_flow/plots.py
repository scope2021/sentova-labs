"""Tight matplotlib figures for the research note."""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from signed_flow.research import ResearchResult


def _style() -> None:
    plt.rcParams.update(
        {
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.alpha": 0.25,
            "grid.linewidth": 0.6,
            "font.size": 10,
            "axes.titlesize": 11,
            "axes.labelsize": 10,
            "legend.frameon": False,
            "savefig.dpi": 140,
            "savefig.bbox": "tight",
            "savefig.facecolor": "white",
        }
    )


def _downsample(n: int, max_points: int, rng: np.random.Generator) -> np.ndarray:
    if n <= max_points:
        return np.arange(n)
    return np.sort(rng.choice(n, size=max_points, replace=False))


def plot_ofi_vs_return(
    result: ResearchResult,
    path: Path,
    max_points: int = 8_000,
) -> None:
    if result.test_ofi is None or result.test_fwd1 is None:
        return
    rng = np.random.default_rng(0)
    # Test bars that had flow (empty calendar bars are a zero-zero cloud).
    mask = np.isfinite(result.test_ofi) & np.isfinite(result.test_fwd1)
    flow = mask & (result.test_ofi != 0)
    x = result.test_ofi[flow]
    y = result.test_fwd1[flow] * 1e4  # bps
    if x.size == 0:
        x = result.test_ofi[mask]
        y = result.test_fwd1[mask] * 1e4
    idx = _downsample(x.size, max_points, rng)
    fig, ax = plt.subplots(figsize=(6.2, 4.4))
    ax.scatter(x[idx], y[idx], s=6, alpha=0.25, c="#1f4e79", linewidths=0)
    ax.axhline(0, color="#666", lw=0.6)
    ax.axvline(0, color="#666", lw=0.6)
    ax.set_xlabel("OFI (signed volume in bar)")
    ax.set_ylabel("Next-bar last-price return (bps)")
    ax.set_title(f"{result.symbol}  ·  OFI vs next-bar return  ·  downsampled")
    fig.savefig(path)
    plt.close(fig)


def plot_cum_pnl(result: ResearchResult, path: Path) -> None:
    if result.test_gross_h1 is None or result.test_index is None:
        return
    t = pd.to_datetime(result.test_index, unit="ms", utc=True)
    gross = np.cumsum(result.test_gross_h1)
    net = np.cumsum(result.test_net_h1)
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    ax.plot(t, gross, color="#1f4e79", lw=1.2, label="gross")
    ax.plot(t, net, color="#9c2f2f", lw=1.2, label="net (taker RT fee)")
    ax.axhline(0, color="#666", lw=0.6)
    ax.set_xlabel("Test period (UTC)")
    ax.set_ylabel("Cumulative strategy return")
    ax.set_title(
        f"{result.symbol}  ·  test cum P&L  ·  h=1  ·  fee {result.fee_bps:.1f} bps RT"
    )
    ax.legend(loc="best")
    fig.autofmt_xdate()
    fig.savefig(path)
    plt.close(fig)


def plot_ofi_series(result: ResearchResult, path: Path, max_points: int = 12_000) -> None:
    if result.test_ofi is None or result.test_index is None:
        return
    t = pd.to_datetime(result.test_index, unit="ms", utc=True)
    ofi = result.test_ofi
    nz = ofi != 0
    t = t[nz]
    ofi = ofi[nz]
    if ofi.size > max_points:
        step = int(np.ceil(ofi.size / max_points))
        t = t[::step]
        ofi = ofi[::step]
    fig, ax = plt.subplots(figsize=(7.2, 3.4))
    ax.vlines(t, 0.0, ofi, color="#1f4e79", lw=0.6, alpha=0.75)
    ax.axhline(0, color="#666", lw=0.6)
    ax.axhline(result.threshold, color="#9c2f2f", lw=0.8, ls="--", label="± threshold")
    ax.axhline(-result.threshold, color="#9c2f2f", lw=0.8, ls="--")
    ax.set_xlabel("Test period (UTC)")
    ax.set_ylabel("OFI")
    ax.set_title(f"{result.symbol}  ·  test OFI  ·  {result.bar_seconds}s bars")
    ax.legend(loc="best")
    fig.autofmt_xdate()
    fig.savefig(path)
    plt.close(fig)


def write_all(result: ResearchResult, artifact_dir: Path) -> list[Path]:
    _style()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    paths = [
        artifact_dir / "ofi_vs_ret.png",
        artifact_dir / "cum_pnl.png",
        artifact_dir / "ofi_series.png",
    ]
    plot_ofi_vs_return(result, paths[0])
    plot_cum_pnl(result, paths[1])
    plot_ofi_series(result, paths[2])
    return paths


def plot_equity_curve(
    times_ms,
    equity,
    path: Path,
    *,
    symbol: str,
    fee_bps: float,
    net_pnl: float,
) -> None:
    """Paper-replay equity vs exchange time."""
    _style()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if times_ms is None or equity is None:
        return
    t = np.asarray(times_ms)
    y = np.asarray(equity, dtype=np.float64)
    if t.size == 0 or y.size == 0:
        return
    ts = pd.to_datetime(t, unit="ms", utc=True)
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    ax.plot(ts, y, color="#1f4e79", lw=1.2, label="equity")
    ax.axhline(y[0], color="#666", lw=0.6, ls="--", label="start")
    ax.set_xlabel("Replay (UTC)")
    ax.set_ylabel("Equity")
    sign = "+" if net_pnl >= 0 else ""
    ax.set_title(
        f"{symbol}  ·  paper equity  ·  taker {fee_bps:.1f} bps  ·  "
        f"net {sign}{net_pnl:.2f}"
    )
    ax.legend(loc="best")
    fig.autofmt_xdate()
    fig.savefig(path)
    plt.close(fig)
