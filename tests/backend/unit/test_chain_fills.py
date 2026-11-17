"""Decode real Polymarket exchange fill logs (captured from Polygon) into trades."""

import json
from decimal import Decimal
from pathlib import Path

from app.domain.market import Side
from app.integrations.polygon import decode_fill

LOGS = json.loads((Path(__file__).parent / "data" / "polymarket_fill_logs.json").read_text())
PBF = "0xdaef2be2a19ad331737d06545f85615b094554e9"


def test_real_buy_and_sell_match_the_data_api_rows():
    # Data API rows for the same transactions:
    #   0x0b089aa7: BUY 2327.72 @ 0.49, usdc 1140.5828 (token 4561349...)
    #   0xd3e287f1: SELL 880.09 @ 0.164, usdc 144.33476
    fills = {f.tx_hash[:10]: f for f in map(decode_fill, LOGS) if f and f.maker == PBF}
    buy, sell = fills["0x0b089aa7"], fills["0xd3e287f1"]
    assert (buy.side, buy.shares, buy.usdc, buy.price) == (
        Side.BUY, Decimal("2327.72"), Decimal("1140.5828"), Decimal("0.49")
    )
    assert buy.token_id == (
        "45613490020259419484902258434669222271298644010879790820930582511334795922155"
    )
    assert (sell.side, sell.shares, sell.usdc, sell.price) == (
        Side.SELL, Decimal("880.09"), Decimal("144.33476"), Decimal("0.164")
    )


def test_every_order_in_a_match_decodes_including_the_takers_own_fill():
    fills = [decode_fill(e) for e in LOGS]
    assert all(f is not None for f in fills)
    # The taker's own order is logged with the taker as maker and the exchange as taker.
    taker_own = [f for f in fills if f and f.maker.startswith("0xe674dede")]
    assert len(taker_own) == 1 and taker_own[0].side is Side.BUY


def test_non_fill_or_malformed_logs_are_ignored():
    real = LOGS[0]
    assert decode_fill({**real, "topics": ["0x" + "00" * 32, *real["topics"][1:]]}) is None
    assert decode_fill({**real, "data": real["data"][:100]}) is None
    unknown_side = "0x" + f"{2:064x}" + real["data"][66:]
    assert decode_fill({**real, "data": unknown_side}) is None
