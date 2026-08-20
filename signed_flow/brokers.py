"""Brokers: in-process paper matcher and Binance spot testnet.

HARD RULE: the live adapter talks only to https://testnet.binance.vision.
Mainnet hosts, withdrawals, and transfers are refused in code, not policy.
Keys (BINANCE_TESTNET_KEY / BINANCE_TESTNET_SECRET) are optional: HMAC
signing is unit-tested without a network; submit without keys raises.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode, urlparse

import requests

from signed_flow.orders import Fill, Order, OrderStatus, Portfolio, Side

TESTNET_HOST = "testnet.binance.vision"
TESTNET_BASE_URL = "https://testnet.binance.vision"
KEY_ENV = "BINANCE_TESTNET_KEY"
SECRET_ENV = "BINANCE_TESTNET_SECRET"

_FORBIDDEN_PATH_BITS = (
    "withdraw",
    "transfer",
    "sapi/",
    "wapi/",
    "futures",
    "capital",
    "asset/dust",
    "universalTransfer",
    "margin/transfer",
)


class MainnetRefusedError(ValueError):
    """Raised when a base URL is not the spot testnet host."""


class BrokerError(RuntimeError):
    """Broker refused or could not complete a request."""


def assert_spot_testnet(base_url: str) -> str:
    """Accept only https://testnet.binance.vision. Refuse everything else."""
    raw = (base_url or "").strip()
    if not raw:
        raise MainnetRefusedError(
            "empty base_url; only https://testnet.binance.vision is allowed"
        )
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    host = (parsed.hostname or "").lower()
    scheme = (parsed.scheme or "https").lower()
    if scheme != "https":
        raise MainnetRefusedError(
            f"refusing base_url {base_url!r}: https required (got {scheme!r})"
        )
    if host != TESTNET_HOST:
        raise MainnetRefusedError(
            f"refusing base_url {base_url!r}: host {host!r} is not "
            f"{TESTNET_HOST}. Mainnet (api.binance.com and friends) is disabled."
        )
    return TESTNET_BASE_URL


def assert_allowed_path(path: str) -> None:
    lowered = path.lower()
    for bit in _FORBIDDEN_PATH_BITS:
        if bit.lower() in lowered:
            raise BrokerError(
                f"refusing path {path!r}: withdrawals/transfers/non-spot are disabled"
            )
    if not path.startswith("/api/v3/"):
        raise BrokerError(
            f"refusing path {path!r}: only /api/v3/ spot testnet routes are enabled"
        )


def sign_query(params: dict[str, Any], secret: str) -> str:
    """Binance HMAC-SHA256 over the URL-encoded query string (insertion order)."""
    query = urlencode(params, doseq=True)
    return hmac.new(
        secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def encode_signed(params: dict[str, Any], secret: str) -> tuple[str, str]:
    """Return (query_without_sig, hex_signature)."""
    query = urlencode(params, doseq=True)
    signature = hmac.new(
        secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return query, signature


@dataclass(frozen=True)
class SignedRequest:
    method: str
    url: str
    query: str
    signature: str
    headers: dict[str, str]


class Broker(Protocol):
    portfolio: Portfolio

    def submit(self, order: Order) -> Order: ...
    def cancel(self, order_id: str, reason: str = "cancel") -> Order | None: ...
    def on_print(
        self,
        *,
        time: int,
        price: float,
        qty: float,
        agg_id: int,
    ) -> list[Fill]: ...


@dataclass
class PaperBroker:
    """In-process taker: ACK on submit, fill from later tape prints only."""

    portfolio: Portfolio
    taker_fee_bps: float = 2.0
    open_orders: list[Order] = field(default_factory=list)
    history: list[Order] = field(default_factory=list)
    n_acks: int = 0
    n_rejects: int = 0
    n_fills: int = 0
    n_cancels: int = 0

    def submit(self, order: Order) -> Order:
        if order.status is not OrderStatus.NEW:
            raise BrokerError(
                f"submit expects NEW, got {order.status.value}"
            )
        if order.qty <= 0.0:
            order.reject("invalid_qty")
            self.n_rejects += 1
            self.history.append(order)
            return order
        order.ack()
        self.n_acks += 1
        self.open_orders.append(order)
        return order

    def cancel(self, order_id: str, reason: str = "cancel") -> Order | None:
        for i, order in enumerate(self.open_orders):
            if order.client_id == order_id and order.is_open:
                order.cancel(reason)
                self.n_cancels += 1
                self.history.append(order)
                del self.open_orders[i]
                return order
        return None

    def cancel_all(self, reason: str = "cancel_all") -> list[Order]:
        canceled: list[Order] = []
        still: list[Order] = []
        for order in self.open_orders:
            if order.is_open:
                order.cancel(reason)
                self.n_cancels += 1
                self.history.append(order)
                canceled.append(order)
            else:
                still.append(order)
        self.open_orders = still
        return canceled

    def on_print(
        self,
        *,
        time: int,
        price: float,
        qty: float,
        agg_id: int,
    ) -> list[Fill]:
        """Match resting market orders against this print. FIFO by submit."""
        self.portfolio.set_mark(price)
        remaining_liq = float(qty)
        fills: list[Fill] = []
        still: list[Order] = []
        fee_rate = self.taker_fee_bps * 1e-4
        for order in self.open_orders:
            if remaining_liq <= 0.0:
                still.append(order)
                continue
            if not order.is_open:
                self.history.append(order)
                continue
            if time < order.not_before_ms:
                still.append(order)
                continue
            take = min(order.remaining, remaining_liq)
            if take <= 0.0:
                still.append(order)
                continue
            fee = take * price * fee_rate
            fill = Fill(
                order_id=order.client_id,
                time=time,
                price=price,
                qty=take,
                fee=fee,
                print_time=time,
                agg_id=agg_id,
            )
            order.apply_fill(fill)
            self.portfolio.apply_fill(order.side, take, price, fee)
            self.n_fills += 1
            fills.append(fill)
            remaining_liq -= take
            if order.is_open:
                still.append(order)
            else:
                self.history.append(order)
        self.open_orders = still
        return fills


@dataclass
class BinanceTestnetBroker:
    """Spot REST adapter pinned to testnet.binance.vision. No withdrawals."""

    api_key: str = ""
    api_secret: str = ""
    base_url: str = TESTNET_BASE_URL
    portfolio: Portfolio = field(
        default_factory=lambda: Portfolio(symbol="BTCUSDT", cash=0.0)
    )
    recv_window: int = 5_000
    timeout_s: float = 15.0
    session: requests.Session | None = None

    def __post_init__(self) -> None:
        self.base_url = assert_spot_testnet(self.base_url)
        if not self.api_key:
            self.api_key = os.environ.get(KEY_ENV, "")
        if not self.api_secret:
            self.api_secret = os.environ.get(SECRET_ENV, "")
        if self.session is None:
            self.session = requests.Session()
            self.session.headers.update(
                {
                    "Accept": "application/json",
                    "User-Agent": "sentova-labs/0.2 (testnet-only)",
                }
            )

    def require_keys(self) -> None:
        if not self.api_key or not self.api_secret:
            raise BrokerError(
                f"missing {KEY_ENV} / {SECRET_ENV}; "
                "signing helpers work without keys, live submit does not"
            )

    def build_signed_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        timestamp_ms: int | None = None,
    ) -> SignedRequest:
        self.base_url = assert_spot_testnet(self.base_url)
        assert_allowed_path(path)
        payload: dict[str, Any] = dict(params or {})
        payload["timestamp"] = (
            int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
        )
        payload["recvWindow"] = int(self.recv_window)
        # Stable insertion order for HMAC: timestamp last-ish is fine;
        # we sign exactly the urlencoded payload as built.
        query, signature = encode_signed(payload, self.api_secret or "")
        url = self.base_url + path
        headers = {"X-MBX-APIKEY": self.api_key}
        return SignedRequest(
            method=method.upper(),
            url=url,
            query=query,
            signature=signature,
            headers=headers,
        )

    def _signed_request(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        self.require_keys()
        req = self.build_signed_request(method, path, params)
        full_query = req.query + "&signature=" + req.signature
        sess = self.session
        assert sess is not None
        resp = sess.request(
            req.method,
            req.url + "?" + full_query,
            timeout=self.timeout_s,
            headers=req.headers,
        )
        if resp.status_code >= 400:
            raise BrokerError(f"testnet HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise BrokerError(f"testnet non-JSON: {resp.text[:200]}") from exc

    def place_market_order(self, symbol: str, side: Side, qty: float) -> Any:
        params = {
            "symbol": symbol.upper(),
            "side": side.value,
            "type": "MARKET",
            "quantity": f"{qty:.8f}".rstrip("0").rstrip("."),
        }
        return self._signed_request("POST", "/api/v3/order", params)

    def cancel_order(self, symbol: str, client_id: str) -> Any:
        params = {"symbol": symbol.upper(), "origClientOrderId": client_id}
        return self._signed_request("DELETE", "/api/v3/order", params)

    def account(self) -> Any:
        return self._signed_request("GET", "/api/v3/account", {})

    def submit(self, order: Order) -> Order:
        """Live testnet submit. Paper replay does not use this path."""
        try:
            self.place_market_order(order.symbol, order.side, order.qty)
        except BrokerError as exc:
            order.reject(str(exc))
            return order
        order.ack()
        return order

    def cancel(self, order_id: str, reason: str = "cancel") -> Order | None:
        del reason
        raise BrokerError(
            f"use cancel_order(symbol, client_id) on testnet (got {order_id})"
        )

    def on_print(
        self,
        *,
        time: int,
        price: float,
        qty: float,
        agg_id: int,
    ) -> list[Fill]:
        del time, price, qty, agg_id
        return []
