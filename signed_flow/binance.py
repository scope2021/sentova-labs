"""Public Binance REST aggTrade downloader (no API keys).

Tries api.binance.com first, then api.binance.us. Caches to data/ so
reruns do not hit the network. If both venues fail, a synthetic tape
is generated and clearly marked as such.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

VENUES: tuple[str, ...] = (
    "https://api.binance.com",
    "https://api.binance.us",
)
AGG_PATH = "/api/v3/aggTrades"
PAGE_LIMIT = 1000
WEIGHT_SLEEP_S = 0.08  # ~12 req/s; well under 1200 req/min
MAX_RETRIES = 5
TIMEOUT_S = 20

COLUMNS = ["agg_id", "price", "qty", "time", "is_buyer_maker"]


@dataclass(frozen=True)
class Tape:
    """Fetched or cached aggregate trades plus provenance."""

    trades: pd.DataFrame
    symbol: str
    source: str  # e.g. "binance.com", "binance.us", "SYNTHETIC"
    fetched_at: str
    cache_path: Path | None


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": "signed-flow-research/0.1 (academic; public REST)",
        }
    )
    return s


def _cache_paths(data_dir: Path, symbol: str) -> tuple[Path, Path]:
    stem = data_dir / f"{symbol.upper()}_aggtrades"
    return Path(str(stem) + ".csv.gz"), Path(str(stem) + ".meta.json")


def _save_cache(tape: Tape, data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    csv_path, meta_path = _cache_paths(data_dir, tape.symbol)
    tape.trades.to_csv(csv_path, index=False, compression="gzip")
    meta = {
        "symbol": tape.symbol,
        "source": tape.source,
        "fetched_at": tape.fetched_at,
        "n_trades": int(len(tape.trades)),
        "time_min": int(tape.trades["time"].min()),
        "time_max": int(tape.trades["time"].max()),
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def _load_cache(data_dir: Path, symbol: str, max_trades: int) -> Tape | None:
    csv_path, meta_path = _cache_paths(data_dir, symbol)
    if not csv_path.exists():
        return None
    df = pd.read_csv(csv_path, compression="gzip")
    for col in COLUMNS:
        if col not in df.columns:
            return None
    df = df.sort_values("agg_id").reset_index(drop=True)
    if len(df) < max(1000, max_trades // 5):
        return None
    if len(df) > max_trades:
        df = df.iloc[-max_trades:].reset_index(drop=True)
    source = "cache"
    fetched_at = "unknown"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            source = str(meta.get("source", "cache"))
            fetched_at = str(meta.get("fetched_at", "unknown"))
        except json.JSONDecodeError:
            pass
    return Tape(
        trades=_normalize(df),
        symbol=symbol.upper(),
        source=f"{source} (cached)",
        fetched_at=fetched_at,
        cache_path=csv_path,
    )


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(
        {
            "agg_id": pd.to_numeric(df["agg_id"], errors="coerce").astype("int64"),
            "price": pd.to_numeric(df["price"], errors="coerce").astype("float64"),
            "qty": pd.to_numeric(df["qty"], errors="coerce").astype("float64"),
            "time": pd.to_numeric(df["time"], errors="coerce").astype("int64"),
            "is_buyer_maker": df["is_buyer_maker"].astype(bool),
        }
    )
    out = out.dropna().sort_values(["time", "agg_id"]).reset_index(drop=True)
    return out


def _parse_page(raw: list[dict]) -> pd.DataFrame:
    if not raw:
        return pd.DataFrame(columns=COLUMNS)
    return pd.DataFrame(
        {
            "agg_id": [int(r["a"]) for r in raw],
            "price": [float(r["p"]) for r in raw],
            "qty": [float(r["q"]) for r in raw],
            "time": [int(r["T"]) for r in raw],
            "is_buyer_maker": [bool(r["m"]) for r in raw],
        }
    )


def _get_json(
    sess: requests.Session, url: str, params: dict, attempt: int = 0
) -> tuple[int, object]:
    try:
        resp = sess.get(url, params=params, timeout=TIMEOUT_S)
    except requests.RequestException as exc:
        if attempt + 1 >= MAX_RETRIES:
            raise
        time.sleep(min(8.0, 0.5 * 2**attempt))
        return _get_json(sess, url, params, attempt + 1)
    if resp.status_code in {429, 418, 500, 502, 503, 504}:
        if attempt + 1 >= MAX_RETRIES:
            resp.raise_for_status()
        retry_after = resp.headers.get("Retry-After")
        wait = float(retry_after) if retry_after else min(8.0, 0.5 * 2**attempt)
        time.sleep(wait)
        return _get_json(sess, url, params, attempt + 1)
    return resp.status_code, _safe_json(resp)


def _safe_json(resp: requests.Response) -> object:
    try:
        return resp.json()
    except ValueError:
        return {"msg": resp.text[:300], "code": resp.status_code}


def _host_label(base: str) -> str:
    if "binance.us" in base:
        return "binance.us"
    if "binance.com" in base:
        return "binance.com"
    return base


def _latest_id(sess: requests.Session, base: str, symbol: str) -> tuple[int, int] | None:
    code, body = _get_json(
        sess, base + AGG_PATH, {"symbol": symbol, "limit": 1}
    )
    if code != 200 or not isinstance(body, list) or not body:
        return None
    row = body[0]
    return int(row["a"]), int(row["T"])


def _fetch_from_venue(
    sess: requests.Session,
    base: str,
    symbol: str,
    max_trades: int,
) -> pd.DataFrame | None:
    latest = _latest_id(sess, base, symbol)
    if latest is None:
        return None
    last_id, _ = latest
    start_id = max(0, last_id - max_trades + 1)
    pages: list[pd.DataFrame] = []
    got = 0
    cursor = start_id
    print(
        f"  fetching {symbol} from {_host_label(base)} "
        f"(fromId={cursor}, target={max_trades:,} trades) ..."
    )
    while got < max_trades:
        code, body = _get_json(
            sess,
            base + AGG_PATH,
            {"symbol": symbol, "fromId": cursor, "limit": PAGE_LIMIT},
        )
        if code == 451 or code == 403:
            return None
        if code != 200:
            msg = body if isinstance(body, dict) else {"body": str(body)[:200]}
            print(f"  {_host_label(base)} HTTP {code}: {msg}")
            if not pages:
                return None
            break
        if not isinstance(body, list) or not body:
            break
        page = _parse_page(body)
        pages.append(page)
        got += len(page)
        next_id = int(page["agg_id"].iloc[-1]) + 1
        if next_id <= cursor:
            break
        cursor = next_id
        if len(page) < PAGE_LIMIT:
            break
        time.sleep(WEIGHT_SLEEP_S)
    if not pages:
        return None
    df = pd.concat(pages, ignore_index=True)
    df = df.drop_duplicates(subset=["agg_id"]).sort_values("agg_id")
    if len(df) > max_trades:
        df = df.iloc[-max_trades:]
    return df.reset_index(drop=True)


def build_synthetic_tape(
    n_trades: int = 80_000,
    seed: int = 7,
    start_ms: int | None = None,
) -> pd.DataFrame:
    """A realistic-looking tape, not a live market.

    Signs are mildly autocorrelated (bursts of taker flow). Prices are a
    random walk plus a small contemporaneous impact term. There is no
    planted forward-looking alpha.
    """
    rng = np.random.default_rng(seed)
    n = int(n_trades)
    if start_ms is None:
        start_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - n * 400
    # Irregular arrivals, ~2-4 trades per second on average with gaps.
    dt_ms = rng.exponential(scale=350.0, size=n).clip(1, 15_000)
    time_ms = start_ms + np.cumsum(dt_ms).astype(np.int64)

    # AR(1) latent aggressor pressure → trade signs.
    ar = np.empty(n, dtype=np.float64)
    ar[0] = rng.normal()
    eps = rng.normal(size=n)
    for i in range(1, n):
        ar[i] = 0.35 * ar[i - 1] + eps[i]
    sign = np.where(ar >= 0, 1.0, -1.0)
    is_buyer_maker = sign < 0  # seller-initiated iff buyer was maker

    qty = rng.lognormal(mean=-2.2, sigma=0.9, size=n).astype(np.float64)
    qty = np.clip(qty, 1e-4, 5.0)

    # Mid random walk + tiny contemporaneous impact (not tradable).
    log_mid = np.cumsum(rng.normal(0.0, 1.2e-4, size=n))
    log_mid += 0.015 * np.cumsum(sign * qty) / (np.arange(n) + 50.0)
    price = 65_000.0 * np.exp(log_mid - log_mid[0])

    return pd.DataFrame(
        {
            "agg_id": np.arange(1, n + 1, dtype=np.int64),
            "price": price,
            "qty": qty,
            "time": time_ms,
            "is_buyer_maker": is_buyer_maker,
        }
    )


def load_or_fetch(
    symbol: str = "BTCUSDT",
    max_trades: int = 80_000,
    data_dir: Path | str = "data",
    refresh: bool = False,
) -> Tape:
    """Load cached trades or download from a public Binance REST venue."""
    symbol = symbol.upper()
    data_dir = Path(data_dir)
    if not refresh:
        cached = _load_cache(data_dir, symbol, max_trades)
        if cached is not None:
            print(
                f"cache hit: {len(cached.trades):,} trades "
                f"from {cached.source} ({cached.cache_path})"
            )
            return cached

    sess = _session()
    last_error = None
    for base in VENUES:
        label = _host_label(base)
        try:
            print(f"trying {label} ...")
            df = _fetch_from_venue(sess, base, symbol, max_trades)
        except requests.RequestException as exc:
            last_error = exc
            print(f"  {label} failed: {exc}")
            continue
        if df is None or df.empty:
            print(f"  {label}: no usable tape")
            continue
        tape = Tape(
            trades=_normalize(df),
            symbol=symbol,
            source=label,
            fetched_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            cache_path=None,
        )
        _save_cache(tape, data_dir)
        csv_path, _ = _cache_paths(data_dir, symbol)
        print(f"  wrote {len(tape.trades):,} trades → {csv_path}")
        return Tape(
            trades=tape.trades,
            symbol=tape.symbol,
            source=tape.source,
            fetched_at=tape.fetched_at,
            cache_path=csv_path,
        )

    print(
        "WARNING: public Binance REST unavailable; "
        "falling back to SYNTHETIC tape (not live market data)."
    )
    if last_error is not None:
        print(f"  last error: {last_error}")
    df = build_synthetic_tape(n_trades=max_trades)
    tape = Tape(
        trades=_normalize(df),
        symbol=symbol,
        source="SYNTHETIC",
        fetched_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        cache_path=None,
    )
    _save_cache(tape, data_dir)
    return tape
