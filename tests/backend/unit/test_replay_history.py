"""Minute-bar books and point-in-time market state for history replays."""

from datetime import timedelta

from app.services.replay_history import DEPTH, MarketHistory, equity_path

from factories import COND, NO, NOW, YES, D, market


def history(**updates):
    info = market().model_copy(update=updates)
    bars = [(NOW, D("0.40")), (NOW + timedelta(minutes=1), D("0.45"))]
    return MarketHistory(markets={COND: info}, bars={YES: bars})


def test_a_book_is_the_last_bar_at_or_before_now_never_a_later_one():
    h = history()
    assert h.book_at(YES, COND, NOW - timedelta(seconds=1)) is None
    book = h.book_at(YES, COND, NOW + timedelta(seconds=59))
    assert book is not None and book.best_ask == D("0.40"), "the 0.45 bar is still in the future"
    assert book.asks[0].size == DEPTH and book.best_bid == D("0.39")


def test_a_bar_book_fits_the_ledger_columns_for_a_real_token_id():
    token = "1" * 77
    h = MarketHistory(markets=history().markets, bars={token: [(NOW, D("0.40"))]})
    book = h.book_at(token, COND, NOW)
    assert book is not None and len(book.hash or "") <= 80, "fills.book_hash is VARCHAR(80)"


def test_a_bar_is_current_for_90_seconds_then_the_book_goes_stale():
    h = history()
    fresh = h.book_at(YES, COND, NOW + timedelta(minutes=2))
    assert fresh is not None and fresh.received_at == NOW + timedelta(minutes=2)
    old = h.book_at(YES, COND, NOW + timedelta(minutes=5))
    assert old is not None and old.received_at == NOW + timedelta(minutes=1)


def test_a_market_trades_until_it_closes_and_settles_only_after():
    h = history(
        closed=True,
        accepting_orders=False,
        resolved=True,
        payouts={YES: D(1), NO: D(0)},
        closed_at=NOW + timedelta(hours=1),
    )
    before = h.market_at(COND, NOW)
    assert before is not None
    assert (before.closed, before.accepting_orders, before.resolved, before.payouts) == (
        False,
        True,
        False,
        {},
    ), "today's closed flags must not leak into the past"
    after = h.market_at(COND, NOW + timedelta(hours=1))
    assert after is not None and after.resolved and after.payouts == {YES: D(1), NO: D(0)}
    assert h.market_at("0xother", NOW) is None


def test_equity_walks_back_from_one_observation_along_the_pnl():
    pnl = [(NOW + timedelta(hours=h), D(v)) for h, v in ((0, "0"), (1, "3000"), (2, "5000"))]
    path = equity_path(D("10000"), NOW + timedelta(hours=2), pnl, NOW, NOW + timedelta(hours=2))
    assert path == [(NOW, D("5000")), (NOW + timedelta(hours=1), D("8000"))]
    broke = equity_path(D("4000"), NOW + timedelta(hours=2), pnl, NOW, NOW + timedelta(hours=2))
    assert broke == [(NOW + timedelta(hours=1), D("2000"))], "a non-positive equity is dropped"
    assert equity_path(D("1"), NOW - timedelta(hours=1), pnl, NOW, NOW) == [], "no P&L at anchor"
