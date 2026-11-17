"""Decode pending operator matchOrders calls (captured from Polygon) into expected fills."""

import json
from pathlib import Path

from app.integrations.polygon import decode_fill, decode_pending_fills

CASES = json.loads((Path(__file__).parent / "data" / "polymarket_match_orders.json").read_text())


def maker_asset_legs(fills):
    """(maker, token, side, amount the order gives up); the leg a pending decode knows exactly."""
    return sorted(
        (f.maker, f.token_id, f.side.value, f.usdc if f.side.value == "BUY" else f.shares)
        for f in fills
    )


def test_pending_decode_predicts_every_confirmed_fill_of_the_transaction():
    for case in CASES:
        pending = decode_pending_fills(case["tx"])
        confirmed = [decode_fill(log) for log in case["logs"]]
        assert maker_asset_legs(pending) == maker_asset_legs(confirmed), case["tx"]["hash"]
        assert all(f.block_number is None and f.log_index is None for f in pending)


def test_pending_decode_reproduces_every_confirmed_fill_exactly_including_the_taker():
    for case in CASES:
        pending = sorted(
            (f.maker, f.token_id, f.side, f.shares, f.usdc)
            for f in decode_pending_fills(case["tx"])
        )
        confirmed = sorted(
            (f.maker, f.token_id, f.side, f.shares, f.usdc) for f in map(decode_fill, case["logs"])
        )
        assert pending == confirmed, case["tx"]["hash"]


def test_other_transactions_decode_to_nothing():
    tx = CASES[0]["tx"]
    assert decode_pending_fills({**tx, "to": "0x" + "00" * 20}) == []
    assert decode_pending_fills({**tx, "input": "0xdeadbeef" + tx["input"][10:]}) == []
    assert decode_pending_fills({**tx, "input": tx["input"][:200]}) == []
