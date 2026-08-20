"""Testnet URL guard, HMAC signing, forbidden paths. No network."""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlencode

import pytest

from signed_flow.brokers import (
    TESTNET_BASE_URL,
    BinanceTestnetBroker,
    BrokerError,
    MainnetRefusedError,
    PaperBroker,
    assert_allowed_path,
    assert_spot_testnet,
    sign_query,
)
from signed_flow.orders import Order, Portfolio, Side


def test_mainnet_base_url_rejected() -> None:
    for url in (
        "https://api.binance.com",
        "https://api.binance.com/",
        "https://api.binance.com/api/v3",
        "https://api.binance.us",
        "http://testnet.binance.vision",
        "https://testnet.binance.vision.evil.example",
        "https://www.binance.com",
        "",
    ):
        with pytest.raises(MainnetRefusedError):
            assert_spot_testnet(url)
        with pytest.raises(MainnetRefusedError):
            BinanceTestnetBroker(base_url=url, api_key="k", api_secret="s")


def test_testnet_host_accepted_without_keys() -> None:
    b = BinanceTestnetBroker(base_url=TESTNET_BASE_URL)
    assert b.base_url == TESTNET_BASE_URL
    with pytest.raises(BrokerError, match="BINANCE_TESTNET"):
        b.require_keys()


def test_hmac_matches_independent_vector() -> None:
    params = {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "timestamp": 123456}
    secret = "testsecret"
    expected = hmac.new(
        secret.encode(), urlencode(params).encode(), hashlib.sha256
    ).hexdigest()
    assert sign_query(params, secret) == expected

    b = BinanceTestnetBroker(
        base_url=TESTNET_BASE_URL, api_key="key", api_secret=secret
    )
    req = b.build_signed_request(
        "POST",
        "/api/v3/order",
        {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET"},
        timestamp_ms=123456,
    )
    assert req.signature
    assert req.url.startswith(TESTNET_BASE_URL)
    assert "signature" not in req.query
    assert req.headers["X-MBX-APIKEY"] == "key"


def test_forbidden_paths() -> None:
    with pytest.raises(BrokerError):
        assert_allowed_path("/sapi/v1/capital/withdraw/apply")
    with pytest.raises(BrokerError):
        assert_allowed_path("/api/v3/withdraw")
    with pytest.raises(BrokerError):
        assert_allowed_path("/wapi/v3/withdraw.html")
    b = BinanceTestnetBroker(api_key="k", api_secret="s")
    with pytest.raises(BrokerError):
        b.build_signed_request("POST", "/sapi/v1/capital/withdraw/apply", {})


def test_paper_broker_no_fill_before_not_before() -> None:
    port = Portfolio(symbol="BTCUSDT", cash=10_000.0)
    br = PaperBroker(portfolio=port, taker_fee_bps=2.0)
    o = Order(
        client_id="sf-1",
        symbol="BTCUSDT",
        side=Side.BUY,
        qty=1.0,
        submitted_at=1_000,
        not_before_ms=1_000,
    )
    br.submit(o)
    fills = br.on_print(time=999, price=100.0, qty=5.0, agg_id=1)
    assert fills == []
    assert o.status.value == "ack"
    fills = br.on_print(time=1_000, price=101.0, qty=5.0, agg_id=2)
    assert len(fills) == 1
    assert fills[0].price == pytest.approx(101.0)
    assert port.qty == pytest.approx(1.0)
    assert port.fees == pytest.approx(1.0 * 101.0 * 0.0002)
