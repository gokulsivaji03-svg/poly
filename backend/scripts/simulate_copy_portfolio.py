"""Replay a wallet's whole history as one proportionally-sized copy portfolio (no hedging).

Every source BUY spends ``our_equity * source_cost / source_equity`` (the app's PROPORTIONAL
sizing), limited by our free cash. SELLs and MERGEs are mirrored as the same fraction of
our holding that the source sold/merged of its own. Resolved markets settle at the on-chain
final price when the market closed; unresolved ones are marked at the source's last trade
price at the end.

Source equity is reconstructed without lookahead from the source's own activity, assuming
no external deposits or withdrawals:
    source_equity(t) = C0 + cash_flows(<= t) + sum(holdings * last traded price)
where C0 is the smallest starting cash that never goes negative. That makes the estimate a
lower bound, so allocation percentages are upper bounds (aggressive sizing).

Fills: source price + ``--slippage`` on buys (minus on sells), plus the market's taker fee.
Limits: no order book (depth, queue position, impact) and CONVERSION events are ignored.

Run from ``backend/``:
    uv run python scripts/simulate_copy_portfolio.py --wallet 0x... --bankroll 1000
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.core.money import ONE, ZERO, to_decimal
from app.domain.market import FeeSchedule
from app.integrations.polymarket.markets import parse_gamma_market

sys.path.insert(0, str(Path(__file__).parent))
from backtest_copy_hedge import DATA, GAMMA, Api

MAX_PRICE = Decimal("0.999")
MIN_PRICE = Decimal("0.001")
INFLOW_TYPES = {"REDEEM", "MERGE", "REWARD", "YIELD", "MAKER_REBATE", "TAKER_REBATE"}


@dataclass
class MarketMeta:
    condition_id: str
    question: str
    fees: FeeSchedule
    tokens: list[str]
    final: dict[str, Decimal]
    """Token -> payout; empty while unresolved."""
    settle_at: int | None


@dataclass
class Book:
    """Share holdings per token plus cost basis (for per-market attribution)."""

    shares: dict[str, Decimal] = field(default_factory=lambda: defaultdict(lambda: ZERO))
    cost: dict[str, Decimal] = field(default_factory=lambda: defaultdict(lambda: ZERO))


def _ts(v: Any) -> int | None:
    if not v:
        return None
    try:
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def load_meta(api: Api, condition_id: str) -> MarketMeta | None:
    gamma: list[dict[str, Any]] = []
    for extra in ({}, {"closed": "true"}):
        gamma = api.get(f"{GAMMA}/markets", {"condition_ids": condition_id, **extra}) or []
        if gamma:
            break
    if not gamma:
        return None
    info = parse_gamma_market(gamma[0], datetime.now(UTC))
    clob = {t.token_id for t in info.tokens}
    rows = api.get(f"{DATA}/v2/tokens", {"condition": condition_id}).get("data") or []
    toks = [t for t in rows if t["token_id"] in clob]
    resolved = bool(toks) and all(t.get("resolved") and t.get("final_price") is not None
                                  for t in toks)
    settle_at = None
    if resolved:
        g = gamma[0]
        settle_at = _ts(g.get("closedTime")) or _ts(g.get("umaEndDate")) or _ts(g.get("endDate"))
    return MarketMeta(
        condition_id=condition_id,
        question=info.question,
        fees=info.fee_schedule,
        tokens=[t.token_id for t in info.tokens],
        final={t["token_id"]: to_decimal(t["final_price"]) for t in toks} if resolved else {},
        settle_at=settle_at,
    )


def source_c0(rows: list[dict[str, Any]]) -> Decimal:
    cash = low = ZERO
    for r in rows:
        cash += _flow(r)
        low = min(low, cash)
    return -low


def _flow(r: dict[str, Any]) -> Decimal:
    u = to_decimal(r.get("usdc_size") or 0)
    t = r["type"]
    if t == "TRADE":
        return -u if r["side"] == "BUY" else u
    if t in INFLOW_TYPES:
        return u
    if t == "SPLIT":
        return -u
    return ZERO


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--wallet", required=True)
    ap.add_argument("--bankroll", type=Decimal, default=Decimal("1000"))
    ap.add_argument("--slippage", type=Decimal, default=Decimal("0.01"))
    ap.add_argument("--min-order-usd", type=Decimal, default=Decimal("1"))
    ap.add_argument("--source-c0-mult", type=Decimal, default=ONE,
                    help="scale the reconstructed source starting capital (sensitivity)")
    ap.add_argument("--small-orders", choices=("skip", "batch", "round-up"), default="skip",
                    help="copies under --min-order-usd: drop them, carry them per token "
                    "until they add up (the app's behaviour), or place the minimum")
    ap.add_argument("--multiplier", type=Decimal, default=ONE,
                    help="scale each proportional copy (the app's sizing multiplier)")
    ap.add_argument("--max-pct-per-trade", type=Decimal, default=ONE,
                    help="cap each buy at this fraction of our equity (1 = off)")
    ap.add_argument("--max-market-pct", type=Decimal, default=ONE,
                    help="cap cost basis per market at this fraction of our equity (1 = off)")
    ap.add_argument("--max-entry-price", type=Decimal, default=ONE)
    ap.add_argument("--min-source-usd", type=Decimal, default=ZERO,
                    help="copy only source BUYs costing at least this much")
    ap.add_argument("--exclude", action="append", default=[],
                    help="never copy buys in markets whose condition id equals, or whose "
                    "title contains (case-insensitive), this value; repeatable")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--cache", type=Path, default=Path.home() / ".cache" / "polycopy-backtest")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    api = Api(args.cache)
    rows = sorted(api.activity(args.wallet.lower()), key=lambda r: int(r["timestamp"]))
    rows = [r for r in rows if not r.get("is_combo")]
    conds = sorted({r["condition_id"] for r in rows if r.get("condition_id")})
    print(f"  {len(rows)} activity rows, {len(conds)} markets; loading metadata", file=sys.stderr)
    with ThreadPoolExecutor(args.workers) as pool:
        metas = dict(zip(conds, pool.map(lambda c: load_meta(api, c), conds), strict=True))

    c0 = source_c0(rows) * args.source_c0_mult
    events: list[tuple[int, int, str, Any]] = [
        (int(r["timestamp"]), 0, "src", r) for r in rows
    ]
    for cid, m in metas.items():
        if m and m.settle_at:
            events.append((m.settle_at, 1, "settle", cid))
    events.sort(key=lambda e: (e[0], e[1]))
    needles = [x.lower() for x in args.exclude]
    excluded = {
        cid for cid, m in metas.items()
        if m and any(n == cid.lower() or n in m.question.lower() for n in needles)
    }

    src = Book()
    src_cash = c0
    mark: dict[str, Decimal] = {}
    us = Book()
    cash = args.bankroll
    settled: set[str] = set()
    pnl_by_market: dict[str, Decimal] = defaultdict(lambda: ZERO)
    token_cond: dict[str, str] = {}
    skips: Counter[str] = Counter()
    copied = Counter()
    carry: dict[str, Decimal] = defaultdict(lambda: ZERO)
    """Batched budget per token, from copies too small to place on their own."""
    costs: dict[str, Decimal] = defaultdict(lambda: ZERO)
    curve: list[tuple[int, Decimal]] = []
    peak = args.bankroll
    max_dd = ZERO
    last_day = None

    def equity_of(book: Book, extra_cash: Decimal) -> Decimal:
        return extra_cash + sum(
            (sh * mark.get(t, ZERO) for t, sh in book.shares.items() if sh > ZERO), ZERO
        )

    def market_cost(cid: str) -> Decimal:
        return sum((c for t, c in us.cost.items() if token_cond.get(t) == cid), ZERO)

    for ts, _, kind, payload in events:
        if kind == "settle":
            cid = payload
            m = metas[cid]
            for tok, pay in m.final.items():
                mark[tok] = pay
                sh = us.shares.pop(tok, ZERO)
                if sh > ZERO:
                    proceeds = sh * pay
                    cash += proceeds
                    pnl_by_market[cid] += proceeds - us.cost.pop(tok, ZERO)
            settled.add(cid)
        else:
            r = payload
            cid, tok, t = r.get("condition_id"), r.get("token_id"), r["type"]
            m = metas.get(cid) if cid else None
            size = to_decimal(r.get("size") or 0)
            price = to_decimal(r.get("price") or 0)
            src_equity = equity_of(src, src_cash)
            src_cash += _flow(r)
            if t == "TRADE" and tok:
                token_cond[tok] = cid
                if ZERO < price < ONE:
                    mark[tok] = price
                if r["side"] == "BUY":
                    src.shares[tok] += size
                    if m is None or cid in settled:
                        skips["UNMAPPED_OR_SETTLED"] += 1
                    elif cid in excluded:
                        skips["EXCLUDED"] += 1
                    elif price > args.max_entry_price:
                        skips["ENTRY_PRICE_TOO_HIGH"] += 1
                    elif to_decimal(r["usdc_size"]) < args.min_source_usd:
                        skips["SOURCE_TRADE_TOO_SMALL"] += 1
                    elif src_equity <= ZERO:
                        skips["SOURCE_EQUITY_UNKNOWN"] += 1
                    else:
                        our_equity = equity_of(us, cash)
                        budget = (
                            our_equity * to_decimal(r["usdc_size"]) / src_equity * args.multiplier
                        )
                        budget = min(budget, our_equity * args.max_pct_per_trade)
                        if args.max_market_pct < ONE:
                            budget = min(
                                budget, our_equity * args.max_market_pct - market_cost(cid)
                            )
                        if args.small_orders == "batch":
                            budget += carry.pop(tok, ZERO)
                        small = budget < args.min_order_usd
                        if small and args.small_orders == "batch":
                            carry[tok] = budget
                            skips["BATCHED"] += 1
                        else:
                            if small and args.small_orders == "round-up":
                                budget = args.min_order_usd
                            limited = budget > cash
                            budget = min(budget, cash)
                            if budget < args.min_order_usd:
                                skips["NO_CASH" if limited else "BELOW_MIN_ORDER"] += 1
                            else:
                                if small:
                                    copied["BUY_ROUNDED_UP"] += 1
                                entry = min(price + args.slippage, MAX_PRICE)
                                per = entry + m.fees.fee_usdc(ONE, entry)
                                sh = budget / per
                                us.shares[tok] += sh
                                us.cost[tok] += budget
                                cash -= budget
                                costs["fees"] += sh * (per - entry)
                                costs["slippage"] += sh * (entry - price)
                                costs["turnover"] += budget
                                copied["BUY" + ("_CASH_LIMITED" if limited else "")] += 1
                elif r["side"] == "SELL":
                    carry.pop(tok, None)
                    held = src.shares[tok]
                    frac = min(ONE, size / held) if held > ZERO else ONE
                    src.shares[tok] = max(ZERO, held - size)
                    ours = us.shares.get(tok, ZERO)
                    if ours > ZERO and m is not None and cid not in settled:
                        sell = ours * frac
                        px = max(price - args.slippage, MIN_PRICE)
                        fee = m.fees.fee_usdc(sell, px)
                        proceeds = sell * px - fee
                        costs["fees"] += fee
                        costs["slippage"] += sell * (price - px)
                        costs["turnover"] += sell * px
                        basis = us.cost[tok] * sell / ours
                        us.shares[tok] = ours - sell
                        us.cost[tok] -= basis
                        cash += proceeds
                        pnl_by_market[cid] += proceeds - basis
                        copied["SELL"] += 1
            elif t == "SPLIT" and m is not None:
                for tk in m.tokens:
                    src.shares[tk] += size
            elif t == "MERGE" and m is not None:
                pair = m.tokens
                held = min((src.shares[tk] for tk in pair), default=ZERO)
                frac = min(ONE, size / held) if held > ZERO else ZERO
                for tk in pair:
                    src.shares[tk] = max(ZERO, src.shares[tk] - size)
                if len(pair) == 2 and frac > ZERO and cid not in settled:
                    pairs = min(us.shares.get(tk, ZERO) for tk in pair) * frac
                    if pairs > ZERO:
                        basis = ZERO
                        for tk in pair:
                            ours = us.shares[tk]
                            b = us.cost[tk] * pairs / ours
                            us.shares[tk] = ours - pairs
                            us.cost[tk] -= b
                            basis += b
                        cash += pairs
                        pnl_by_market[cid] += pairs - basis
                        copied["MERGE"] += 1
            elif t == "REDEEM" and m is not None:
                for tk in m.tokens:
                    src.shares[tk] = ZERO

        eq = equity_of(us, cash)
        peak = max(peak, eq)
        if peak > ZERO:
            max_dd = max(max_dd, (peak - eq) / peak)
        day = ts // 86400
        if day != last_day:
            curve.append((ts, eq))
            last_day = day

    final = equity_of(us, cash)
    open_value = final - cash
    for tok, sh in us.shares.items():
        if sh > ZERO:
            pnl_by_market[token_cond[tok]] += sh * mark.get(tok, ZERO) - us.cost[tok]

    def day(t: int) -> str:
        return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d")

    print(f"\nwallet {args.wallet}  bankroll ${args.bankroll}  slippage ${args.slippage}  "
          f"source C0 ${c0:,.0f}")
    print(f"period {day(events[0][0])} .. {day(events[-1][0])}")
    print(f"final equity ${final:,.2f}  ({(final / args.bankroll - 1):+.2%})  "
          f"cash ${cash:,.2f}  open positions ${open_value:,.2f}")
    print(f"max drawdown {max_dd:.1%}  peak ${peak:,.2f}")
    print("copied: " + ", ".join(f"{k} {v}" for k, v in copied.most_common()))
    print(f"costs: fees ${costs['fees']:,.2f}  slippage ${costs['slippage']:,.2f}  "
          f"turnover ${costs['turnover']:,.2f}")
    print("skipped buys: " + ", ".join(f"{k} {v}" for k, v in skips.most_common()))
    print("\nmonth-end equity:")
    month: dict[str, Decimal] = {}
    for t, eq in curve:
        month[day(t)[:7]] = eq
    for k, v in month.items():
        print(f"  {k}  ${v:>12,.2f}")
    ranked = sorted(pnl_by_market.items(), key=lambda kv: kv[1])
    q = {cid: (metas[cid].question if metas.get(cid) else cid)[:60] for cid, _ in ranked}
    print("\nworst markets:")
    for cid, v in ranked[:8]:
        print(f"  {v:>+10,.2f}  {q[cid]}")
    print("best markets:")
    for cid, v in ranked[-8:][::-1]:
        print(f"  {v:>+10,.2f}  {q[cid]}")
    if args.json:
        args.json.write_text(json.dumps({
            "final_equity": str(final), "max_drawdown": str(max_dd), "source_c0": str(c0),
            "curve": [[t, str(e)] for t, e in curve],
            "pnl_by_market": {cid: str(v) for cid, v in pnl_by_market.items()},
            "copied": dict(copied), "skips": dict(skips),
            "costs": {k: str(v) for k, v in costs.items()},
        }, indent=1))


if __name__ == "__main__":
    main()
