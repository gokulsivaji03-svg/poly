"""Order books from the CLOB market channel and REST snapshots: the prices every copy uses."""

from dataclasses import dataclass, field
from decimal import Decimal

from app.core.clock import ManualClock
from app.integrations.polymarket.clob import ClobRestBooks, MarketStream

from factories import NOW

D = Decimal
TOKEN = "101"


def snapshot(bids, asks, token=TOKEN):
    return {
        "event_type": "book",
        "asset_id": token,
        "market": "cond",
        "bids": [{"price": p, "size": s} for p, s in bids],
        "asks": [{"price": p, "size": s} for p, s in asks],
        "hash": "h0",
    }


def change(side, price, size, token=TOKEN):
    return {
        "event_type": "price_change",
        "price_changes": [{"asset_id": token, "side": side, "price": price, "size": size}],
    }


def live_stream():
    clock = ManualClock(NOW)
    updates: list[str] = []
    stream = MarketStream("wss://test", clock, on_update=updates.append)
    stream.connected = True
    return stream, clock, updates


def test_deltas_before_a_snapshot_never_build_a_book():
    stream, clock, updates = live_stream()
    stream.apply(change("SELL", "0.41", "100"), clock.now())
    assert stream.book(TOKEN) is None and updates == []


def test_snapshot_then_deltas_keep_best_prices_and_drop_emptied_levels():
    stream, clock, updates = live_stream()
    stream.apply(
        snapshot([("0.39", "50"), ("0.38", "70")], [("0.41", "30"), ("0.42", "90")]), clock.now()
    )
    stream.apply(change("SELL", "0.40", "25"), clock.now())
    stream.apply(change("BUY", "0.39", "0"), clock.now())
    book = stream.book(TOKEN)
    assert book is not None
    assert (book.best_bid, book.best_ask) == (D("0.38"), D("0.40"))
    assert [lv.price for lv in book.asks] == [D("0.40"), D("0.41"), D("0.42")]
    assert updates == [TOKEN] * 3


def test_no_book_is_served_while_disconnected():
    stream, clock, _ = live_stream()
    stream.apply(snapshot([("0.39", "50")], [("0.41", "30")]), clock.now())
    stream.connected = False
    assert stream.book(TOKEN) is None


def test_tick_size_change_survives_the_next_snapshot():
    stream, clock, _ = live_stream()
    stream.apply(snapshot([("0.39", "50")], [("0.41", "30")]), clock.now())
    stream.apply(
        {"event_type": "tick_size_change", "asset_id": TOKEN, "new_tick_size": "0.001"}, clock.now()
    )
    stream.apply(snapshot([("0.391", "50")], [("0.409", "30")]), clock.now())
    book = stream.book(TOKEN)
    assert book is not None and book.tick_size == D("0.001")


def test_book_age_tracks_the_last_update():
    stream, clock, _ = live_stream()
    stream.apply(snapshot([("0.39", "50")], [("0.41", "30")]), clock.now())
    clock.advance(3)
    stream.apply(change("SELL", "0.41", "10"), clock.now())
    clock.advance(2)
    book = stream.book(TOKEN)
    assert book is not None and book.age(clock.now()).total_seconds() == 2


def test_watch_resubscribes_only_for_new_tokens():
    stream, _clock, _ = live_stream()
    stream.watch([TOKEN, "202"])
    assert stream._resubscribe.is_set()
    stream._resubscribe.clear()
    stream.watch([TOKEN])
    assert not stream._resubscribe.is_set()


@dataclass
class Level:
    price: str
    size: str


@dataclass
class SdkBook:
    asset_id: str
    market: str = "cond"
    bids: list = field(default_factory=lambda: [Level("0.39", "10"), Level("0.38", "0")])
    asks: list = field(default_factory=lambda: [Level("0.41", "5"), Level("1.00", "9")])
    tick_size: str | None = None
    min_order_size: str | None = "5"
    hash: str = "h"


class FakeSdk:
    def __init__(self):
        self.calls: list[list[str]] = []

    async def get_order_books(self, *, token_ids):
        self.calls.append(token_ids)
        return [SdkBook(t) for t in token_ids]


async def test_rest_books_chunk_requests_and_drop_unusable_levels():
    sdk = FakeSdk()
    tokens = [str(i) for i in range(120)]
    books = await ClobRestBooks(sdk, ManualClock(NOW)).fetch(tokens)
    assert [len(c) for c in sdk.calls] == [50, 50, 20]
    book = books["7"]
    assert [lv.price for lv in book.bids] == [D("0.39")], "zero-size level dropped"
    assert [lv.price for lv in book.asks] == [D("0.41")], "price outside (0, 1) dropped"
    assert (book.tick_size, book.min_order_size) == (D("0.01"), D("5"))
