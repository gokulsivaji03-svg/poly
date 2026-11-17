"""Backtest: copy a wallet's trades at its exact fill prices, then hedge with the complement.

For each of the wallet's top resolved binary markets (by BUY notional):

1. Every source BUY becomes a copied lot at the source's own price (``--scale`` x shares),
   paying the market's taker fee as a copier would.
2. From ``--delay`` seconds after the buy, the opposite outcome's minute price history is
   scanned. The first minute where
       1 - entry - entry_fee - hedge_ask - hedge_fee >= --min-gap
   with ``hedge_ask = minute_price + --spread`` buys an equal number of opposite shares,
   locking $1 per pair at settlement.
3. Every source SELL sells ``--scale`` x its shares at the source's price (taker fee
   deducted), FIFO from our lots of that token. Without hedging (hold) any lot can be sold;
   with hedging only lots still unhedged at that moment (``PRESERVE_HEDGE``).
4. Remaining unhedged shares settle at the on-chain final price.

``--entry market`` replaces exact-price copying with what a follower would get: the last
minute price at ``--entry-delay`` after the source fill, plus ``--spread`` for buys (minus
for sells), skipping buys whose ask drifted more than ``--max-price-diff`` above the
source. ``--max-entry-price``, ``--max-buys-per-market`` and ``--max-market-usd`` mirror the
app's risk caps. ``--rank random`` samples markets with a fixed ``--seed``.

Limits, by construction: ``/prices-history`` is a price series, not an order book. Hedge
fills assume unlimited depth at ``price + spread``; queue position and impact are
ignored. Source MERGE/SPLIT/CONVERSION are not mirrored. Minutes where the opposite price
is below ``--min-hedge-price`` are skipped because they are effectively decided markets
with no real liquidity.

Run from ``backend/``:  uv run python scripts/backtest_copy_hedge.py --wallet 0x...
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx
from app.core.money import ONE, ZERO, to_decimal
from app.domain.market import FeeSchedule
from app.integrations.polymarket.markets import parse_gamma_market

DATA = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
HISTORY_WINDOW_S = 7 * 86400  # /prices-history rejects longer startTs..endTs spans


class Api:
    def __init__(self, cache_dir: Path) -> None:
        self._http = httpx.Client(timeout=30)
        self._cache = cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)

    def get(self, url: str, params: dict[str, Any], *, cache: bool = True) -> Any:
        key = sha256(json.dumps([url, params], sort_keys=True).encode()).hexdigest()
        path = self._cache / f"{key}.json"
        if cache and path.exists():
            return json.loads(path.read_text())
        resp = self._http.get(url, params=params)
        resp.raise_for_status()
        body = resp.json()
        if cache:
            path.write_text(json.dumps(body))
        return body

    def activity(self, wallet: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        first = {"user": wallet, "start": 1, "sort_direction": "ASC", "limit": 500}
        while True:
            q = {"user": wallet, "cursor": cursor} if cursor else first
            body = self.get(f"{DATA}/v2/activity", q, cache=False)
            rows += body.get("data") or []
            pag = body.get("pagination") or {}
            cursor = pag.get("next_cursor")
            if not pag.get("has_more") or not cursor:
                return rows

    def history(self, token_id: str, start: int, end: int) -> list[tuple[int, Decimal]]:
        out: dict[int, Decimal] = {}
        t = start
        while t < end:
            stop = min(t + HISTORY_WINDOW_S, end)
            body = self.get(
                f"{CLOB}/prices-history",
                {"market": token_id, "startTs": t, "endTs": stop, "fidelity": 1},
            )
            for pt in body.get("history") or []:
                out[int(pt["t"])] = to_decimal(pt["p"])
            t = stop
        return sorted(out.items())


@dataclass
class Market:
    condition_id: str
    question: str
    fees: FeeSchedule
    fees_known: bool
    final: dict[str, Decimal]
    opposite: dict[str, str]
    closed_at: int


@dataclass
class LotResult:
    ts: int
    token_id: str
    shares: Decimal
    entry: Decimal
    entry_fee: Decimal
    final: Decimal
    hedge_ts: int | None = None
    hedge_price: Decimal | None = None
    hedge_fee_rate: Decimal = ZERO
    """Hedge taker fee per share at ``hedge_price``."""
    hold_sold: Decimal = ZERO
    hold_proceeds: Decimal = ZERO
    strat_sold: Decimal = ZERO
    strat_proceeds: Decimal = ZERO

    @property
    def cost(self) -> Decimal:
        return self.shares * self.entry + self.entry_fee

    @property
    def baseline_pnl(self) -> Decimal:
        return self.hold_proceeds + (self.shares - self.hold_sold) * self.final - self.cost

    @property
    def strategy_pnl(self) -> Decimal:
        left = self.shares - self.strat_sold
        if self.hedge_price is None:
            return self.strat_proceeds + left * self.final - self.cost
        hedge = left * (self.hedge_price + self.hedge_fee_rate)
        return self.strat_proceeds + left * ONE - hedge - self.cost

    def unhedged_at(self, ts: int) -> bool:
        return self.hedge_ts is None or self.hedge_ts > ts


def load_market(api: Api, condition_id: str) -> Market | None:
    gamma = api.get(f"{GAMMA}/markets", {"condition_ids": condition_id, "closed": "true"})
    if not gamma:
        return None
    info = parse_gamma_market(gamma[0], datetime.now(UTC))
    clob_ids = {t.token_id for t in info.tokens}
    # /v2/tokens also lists structural rows (module "binary") that are not CLOB tokens.
    rows = api.get(f"{DATA}/v2/tokens", {"condition": condition_id}).get("data") or []
    tokens = [t for t in rows if t["token_id"] in clob_ids]
    if len(tokens) != 2 or not all(t.get("resolved") for t in tokens):
        return None
    closed = gamma[0].get("closedTime") or gamma[0].get("endDate")
    if not closed:
        return None
    closed_at = int(datetime.fromisoformat(str(closed).replace("Z", "+00:00")).timestamp())
    return Market(
        condition_id=condition_id,
        question=info.question,
        fees=info.fee_schedule,
        fees_known=info.fees_known,
        final={t["token_id"]: to_decimal(t["final_price"]) for t in tokens},
        opposite={t["token_id"]: t["opposite_token_id"] for t in tokens},
        closed_at=closed_at,
    )


def _price_at(series: list[tuple[int, Decimal]], ts: int, max_age_s: int) -> Decimal | None:
    """Last minute price at or before ``ts`` (no lookahead); ``None`` if older than allowed."""
    lo, hi = 0, len(series)
    while lo < hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= ts:
            lo = mid + 1
        else:
            hi = mid
    if lo == 0 or ts - series[lo - 1][0] > max_age_s:
        return None
    return series[lo - 1][1]


def run_market(
    api: Api,
    m: Market,
    buys: list[dict[str, Any]],
    sells: list[dict[str, Any]],
    args: argparse.Namespace,
) -> tuple[list[LotResult], Counter[str]]:
    # Start early enough that the last price before the first buy (<= max age) is visible;
    # _price_at still never looks past the decision time.
    start = min(int(b["timestamp"]) for b in buys) - args.max_price_age
    tokens = {b["token_id"] for b in buys} | {m.opposite[b["token_id"]] for b in buys}
    series = {tok: api.history(tok, start, m.closed_at + 3600) for tok in tokens}
    skips: Counter[str] = Counter()
    lots: list[LotResult] = []
    spent = ZERO
    for b in buys:
        tok, src_ts = b["token_id"], int(b["timestamp"])
        ts = src_ts + args.entry_delay
        src_price = to_decimal(b["price"])
        if src_price > args.max_entry_price:
            skips["ENTRY_PRICE_TOO_HIGH"] += 1
            continue
        if args.max_buys_per_market and len(lots) >= args.max_buys_per_market:
            skips["MAX_BUYS_PER_MARKET"] += 1
            continue
        if args.entry == "market":
            mid = _price_at(series[tok], ts, args.max_price_age)
            if mid is None:
                skips["NO_PRICE"] += 1
                continue
            entry = min(mid + args.spread, Decimal("0.999"))
            if entry > src_price * (ONE + args.max_price_diff) or entry > args.max_entry_price:
                skips["PRICE_MOVED"] += 1
                continue
        else:
            entry = src_price
        cost_ps = entry + m.fees.fee_usdc(ONE, entry)
        shares = to_decimal(b["size"]) * args.scale
        if shares * cost_ps < args.min_order_usd:
            skips["BELOW_MIN_ORDER"] += 1
            continue
        if args.max_market_usd:
            shares = min(shares, (args.max_market_usd - spent) / cost_ps)
            if shares * cost_ps < args.min_order_usd:
                skips["MAX_MARKET_EXPOSURE"] += 1
                continue
        lot = LotResult(
            ts=ts,
            token_id=tok,
            shares=shares,
            entry=entry,
            entry_fee=m.fees.fee_usdc(shares, entry),
            final=m.final[tok],
        )
        spent += lot.cost
        for t, p in series[m.opposite[tok]]:
            if t < ts + args.delay or t > m.closed_at:
                continue
            if p < args.min_hedge_price:
                continue
            ask = min(p + args.spread, Decimal("0.999"))
            net = ONE - cost_ps - ask - m.fees.fee_usdc(ONE, ask)
            if net >= args.min_gap:
                lot.hedge_ts, lot.hedge_price = t, ask
                lot.hedge_fee_rate = m.fees.fee_usdc(ONE, ask)
                break
        lots.append(lot)
    # Source holding just before each SELL (its buys minus sells so far), for proportional sells.
    src_pos: dict[str, Decimal] = defaultdict(lambda: ZERO)
    before: list[Decimal] = []
    events = sorted(
        [(int(r["timestamp"]), 0, r) for r in buys] + [(int(r["timestamp"]), 1, r) for r in sells],
        key=lambda e: (e[0], e[1]),
    )
    for _, is_sell, r in events:
        if is_sell:
            before.append(src_pos[r["token_id"]])
            src_pos[r["token_id"]] -= to_decimal(r["size"])
        else:
            src_pos[r["token_id"]] += to_decimal(r["size"])
    for hedged in (False, True):
        _apply_sells(m, lots, sells, before, series, args, hedged=hedged)
    return lots, skips


def _apply_sells(
    m: Market,
    lots: list[LotResult],
    sells: list[dict[str, Any]],
    src_before: list[Decimal],
    series: dict[str, list[tuple[int, Decimal]]],
    args: argparse.Namespace,
    *,
    hedged: bool,
) -> None:
    """Mirror source SELLs FIFO; with ``hedged`` only lots still unhedged at the sell.

    Each sell takes the fraction of our eligible holding that the source sold of its
    holding at that moment (``src_before``), so capped lots shrink sells proportionally.
    """
    for s, src_held in zip(sells, src_before, strict=True):
        tok = s["token_id"]
        ts = int(s["timestamp"]) + args.entry_delay
        if args.entry == "market":
            mid = _price_at(series.get(tok, []), ts, args.max_price_age)
            if mid is None:
                continue
            price = max(mid - args.spread, Decimal("0.001"))
        else:
            price = to_decimal(s["price"])
        held = [
            lt for lt in lots
            if lt.token_id == tok and lt.ts <= ts and (not hedged or lt.unhedged_at(ts))
        ]
        ours = sum((lt.shares - (lt.strat_sold if hedged else lt.hold_sold) for lt in held), ZERO)
        size = to_decimal(s["size"])
        want = ours * min(ONE, size / src_held) if src_held > ZERO else ours
        for lot in held:
            if want <= ZERO:
                break
            sold = lot.strat_sold if hedged else lot.hold_sold
            take = min(want, lot.shares - sold)
            if take <= ZERO:
                continue
            proceeds = take * price - m.fees.fee_usdc(take, price)
            if hedged:
                lot.strat_sold += take
                lot.strat_proceeds += proceeds
            else:
                lot.hold_sold += take
                lot.hold_proceeds += proceeds
            want -= take


def usd(v: Decimal) -> str:
    return f"{v:+,.2f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--wallet", required=True)
    ap.add_argument("--markets", type=int, default=15)
    ap.add_argument(
        "--rank", choices=["notional", "count", "random"], default="notional",
        help="pick markets by source BUY notional, number of BUYs, or a seeded random sample",
    )
    ap.add_argument("--seed", type=int, default=7, help="seed for --rank random")
    ap.add_argument(
        "--conditions", help="comma-separated condition ids to test (overrides --rank/--markets)"
    )
    ap.add_argument(
        "--scale", type=Decimal, default=Decimal("0.01"), help="fraction of source shares"
    )
    ap.add_argument("--min-gap", type=Decimal, default=Decimal("0.02"), help="net $/pair to hedge")
    ap.add_argument(
        "--spread", type=Decimal, default=Decimal("0.01"), help="ask premium over minute price"
    )
    ap.add_argument("--delay", type=int, default=60, help="seconds after the buy before hedging")
    ap.add_argument("--min-hedge-price", type=Decimal, default=Decimal("0.02"))
    ap.add_argument("--cache", type=Path, default=Path.home() / ".cache" / "polycopy-backtest")
    ap.add_argument("--json", type=Path, help="write per-lot results here")
    fair = ap.add_argument_group("copy realism (defaults reproduce exact-price copying)")
    fair.add_argument(
        "--entry", choices=["source", "market"], default="source",
        help="source: fill at the source's price; market: last minute price +/- spread",
    )
    fair.add_argument("--entry-delay", type=int, default=0, help="seconds from source fill to ours")
    fair.add_argument(
        "--max-price-age", type=int, default=600, help="max staleness of the minute price (s)"
    )
    fair.add_argument(
        "--max-price-diff", type=Decimal, default=Decimal("0.05"),
        help="skip buys whose ask exceeds source price by this fraction (market entry)",
    )
    fair.add_argument("--max-entry-price", type=Decimal, default=ONE)
    fair.add_argument("--max-buys-per-market", type=int, default=0, help="0 = unlimited")
    fair.add_argument(
        "--max-market-usd", type=Decimal, default=ZERO, help="cost cap per market; 0 = none"
    )
    fair.add_argument("--min-order-usd", type=Decimal, default=Decimal("1"))
    args = ap.parse_args()

    api = Api(args.cache)
    by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sells_by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in api.activity(args.wallet.lower()):
        if r["type"] != "TRADE" or not r.get("condition_id") or not r.get("token_id"):
            continue
        if r.get("is_combo"):
            continue
        if r["side"] == "BUY":
            by_market[r["condition_id"]].append(r)
        elif r["side"] == "SELL":
            sells_by_market[r["condition_id"]].append(r)
    if args.conditions:
        ranked = [c.strip() for c in args.conditions.split(",") if c.strip()]
        args.markets = len(ranked)
    elif args.rank == "count":
        ranked = sorted(by_market, key=lambda c: -len(by_market[c]))
    elif args.rank == "random":
        # Rank by a seeded hash per market: stable as the wallet adds new markets, unlike a
        # shuffle of the whole (growing) list.
        ranked = sorted(by_market, key=lambda c: sha256(f"{args.seed}:{c}".encode()).hexdigest())
    else:
        ranked = sorted(by_market, key=lambda c: -sum(float(r["usdc_size"]) for r in by_market[c]))

    rows: list[tuple[Market, list[LotResult], Counter[str]]] = []
    for cid in ranked:
        if len(rows) >= args.markets:
            break
        m = load_market(api, cid) if by_market.get(cid) else None
        if m is None:
            if args.conditions:
                print(f"  skipped {cid}: no BUYs or not a resolved binary", file=sys.stderr)
            continue
        buys = sorted(by_market[cid], key=lambda r: int(r["timestamp"]))
        sells = sorted(sells_by_market[cid], key=lambda r: int(r["timestamp"]))
        lots, skips = run_market(api, m, buys, sells, args)
        rows.append((m, lots, skips))
        print(
            f"  loaded {m.question[:70]} ({len(buys)} buys, {len(sells)} sells, "
            f"{sum(skips.values())} skipped)",
            file=sys.stderr,
        )

    print(
        f"\nwallet {args.wallet}  scale {args.scale}  min_gap ${args.min_gap}  "
        f"spread ${args.spread}  delay {args.delay}s  entry {args.entry} "
        f"(+{args.entry_delay}s)\n"
    )
    hdr = (
        f"{'market':<52} {'buys':>4} {'hedged':>7} {'cost':>11} {'hold P&L':>11} "
        f"{'hedge P&L':>11} {'locked':>9}"
    )
    print(hdr)
    print("-" * len(hdr))
    tot = defaultdict(lambda: ZERO)
    all_skips: Counter[str] = Counter()
    n_lots = n_hedged = 0
    for m, lots, skips in rows:
        all_skips += skips
        cost = sum((lt.cost for lt in lots), ZERO)
        base = sum((lt.baseline_pnl for lt in lots), ZERO)
        strat = sum((lt.strategy_pnl for lt in lots), ZERO)
        hedged = [lt for lt in lots if lt.hedge_price is not None]
        locked = sum((lt.strategy_pnl for lt in hedged), ZERO)
        fee_flag = "" if m.fees_known else "*"
        print(
            f"{(m.question[:50] + fee_flag):<52} {len(lots):>4} {len(hedged):>3}/{len(lots):<3} "
            f"{cost:>11,.2f} {usd(base):>11} {usd(strat):>11} {usd(locked):>9}"
        )
        tot["cost"] += cost
        tot["base"] += base
        tot["strat"] += strat
        tot["locked"] += locked
        n_lots += len(lots)
        n_hedged += len(hedged)
    print("-" * len(hdr))
    print(
        f"{'TOTAL':<52} {n_lots:>4} {n_hedged:>3}/{n_lots:<3} {tot['cost']:>11,.2f} "
        f"{usd(tot['base']):>11} {usd(tot['strat']):>11} {usd(tot['locked']):>9}"
    )
    if tot["cost"] > ZERO:
        print(
            f"\nreturn on cost: hold {tot['base'] / tot['cost']:+.2%}  "
            f"copy+hedge {tot['strat'] / tot['cost']:+.2%}"
        )
    if all_skips:
        print("skipped buys: " + ", ".join(f"{k} {v}" for k, v in all_skips.most_common()))
    if any(not m.fees_known for m, _, _ in rows):
        print("* fee schedule unknown; conservative 7% rate applied")

    if args.json:
        args.json.write_text(
            json.dumps(
                [
                    {
                        "condition_id": m.condition_id,
                        "question": m.question,
                        "skips": dict(skips),
                        "lots": [
                            {
                                "ts": lt.ts,
                                "token_id": lt.token_id,
                                "shares": str(lt.shares),
                                "entry": str(lt.entry),
                                "final": str(lt.final),
                                "hold_sold": str(lt.hold_sold),
                                "strat_sold": str(lt.strat_sold),
                                "hedge_ts": lt.hedge_ts,
                                "hedge_price": (
                                    None if lt.hedge_price is None else str(lt.hedge_price)
                                ),
                                "hold_pnl": str(lt.baseline_pnl),
                                "strategy_pnl": str(lt.strategy_pnl),
                            }
                            for lt in lots
                        ],
                    }
                    for m, lots, skips in rows
                ],
                indent=1,
            )
        )


if __name__ == "__main__":
    main()
