# Latency: network, CPU and hardware

Goal. Tail sharp wallets' directional Polymarket bets with the least possible lag, because every millisecond of delay means a worse price than the source paid.

Framing. HFT shops optimize the last microsecond because their edge lives there. polycopy's lag budget is different. Detection takes hundreds of milliseconds to seconds (chain and mempool). The order trip from a badly placed host takes about 150 to 200 ms. The service is about 11 ms (measured, local; see the PR 3 and PR 4 tables in git history). Decision logic takes about 20 µs (measured). Optimize in that order. The CPU is not the problem.

Vantage note. Probes labeled "measured here" ran with curl from a US West Coast workstation on 2026-11-17, not a production host. Service-time numbers come from `pytest -m bench -s` and cProfile on the same machine (PostgreSQL 17 on localhost).

## 1. Where Polymarket runs, and rate limits

**Hosting.** Polymarket's own geoblock docs list "Primary Servers: eu-west-2" and "Closest Non-Georestricted Region: eu-west-1" (https://docs.polymarket.com/api-reference/geoblock.md). eu-west-2 is AWS London. eu-west-1 is AWS Ireland. The UK is geoblocked, so London itself is not an eligible host for live trading. Dublin is the documented fallback.

**Edge layer (measured here).** `clob.polymarket.com` answers through Cloudflare (`server: cloudflare`, `cf-ray ...-SJC`). Order HTTP traffic reaches a nearby Cloudflare PoP and then backhauls to the origin. `ws-subscriptions-clob.polymarket.com` runs behind AWS CloudFront (`x-amz-cf-pop: SFO53`) with an `istio-envoy` origin. `ws-live-data.polymarket.com` (RTDS) runs behind Cloudflare. All three negotiate HTTP/2.

**Reported latency.**
- From this West Coast machine (measured here, n=8). TCP connect took about 11 to 14 ms to the edge. TTFB on `GET /time` was 183 to 190 ms typical, with outliers at 460 ms. So about 170 ms is edge-to-origin backhaul plus origin time.
- One third-party benchmark measured TTFB to `clob.polymarket.com`. It found about 6 ms from Amsterdam, 16 ms from Frankfurt, 19 ms from London, 110 ms from US-East and 195 ms from Singapore (https://dev.to/bluewhale-quant-lab/i-measured-polymarkets-api-latency-from-5-regions-heres-the-script-and-the-results-3mjm). The author's "Amsterdam" origin claim conflicts with the official eu-west-2 listing. It is likely a Cloudflare PoP effect (inference).
- A VPS vendor reports about 0.83 ms from Dublin to the CLOB (https://www.quantvps.com/blog/polymarket-servers-location). The vendor has a commercial interest in that number.
- A US-East bot author reports 120 to 180 ms order round trips (https://dev.to/lkto1m/execution-latency-in-my-polymarket-trading-bot-sub-second-trade-execution-system-design-277j).

**Rate limits.** The official reference lists these limits (https://docs.polymarket.com/api-reference/rate-limits.md):
- `POST /order` and `DELETE /order`. Burst 5,000 per 10 s. Sustained 120,000 per 10 min.
- `/book`. 1,500 per 10 s.
- General CLOB. 9,000 per 10 s.

Limits sit on sliding windows. Cloudflare throttles by delaying or queuing requests rather than rejecting them. Per-signer token buckets are documented, but enforcement has not started (https://docs.polymarket.com/api-reference/trading-rate-limits.md). A single copier sends a few orders per minute, so no limit binds. The one hazard is the silent queuing, which shows up as unexplained tail latency rather than a 429. Keep `/book` polling off the order path.

**Protocol floors nobody can beat.** CLOB V2 went live on 2026-04-28. It brings new exchange contracts, EIP-712 domain version "2" and pUSD collateral (https://docs.polymarket.com/v2-migration.md). Marketable orders on crypto and finance up/down markets are held for 250 ms before matching. Sports markets have a configurable delay window, during which the order cannot be canceled (https://docs.polymarket.com/concepts/order-lifecycle.md). The help center cites 3 s on sports and a 1 s test on NBA and MLB (https://intercom.help/polymarket/en/articles/13364444-limit-orders). These delays add to polycopy's lag on those markets whatever the infrastructure.

## 2. Polygon PoS timing

- **Block time.** It was about 2 s from genesis. PIP-86 cut it to 1.75 s in May 2026, with a planned second step to 1.5 s (https://yellow.com/news/polygon-block-time-1-75-seconds-pip-86). A June 2026 report cites about 1.5 s (https://www.bitrue.com/blog/matic-hype-returns-polygon-blockchain-speed-upgrade). Check Polygonscan for the live value.
- **Finality.** Heimdall v2 (July 2025) gives deterministic milestone finality in 2 to 5 s. Use the `finalized` tag to read it (https://docs.polygon.technology/pos/concepts/finality/finality.md, https://www.polygon.technology/blog/polygon-5-second-fast-finality-upgrade).
- **Reorgs.** Rio (October 2025) introduced VEBloP (PIP-64). One elected producer proposes blocks while backups stand by. Polygon claims this eliminates reorgs (https://www.theblock.co/post/373811/polygon-activates-rio-upgrade-to-revamp-block-production-speed-up-network, https://polygon.technology/blog/polygon-launches-major-payments-upgrade-with-rio-faster-lighter-and-easier-to-build). Acting on the `latest` head is now low risk. Keep the `removed: true` log handling anyway, because the claim comes from the vendor.
- **Log availability.**
  - (a) Public RPCs. On `eth_getLogs`, PublicNode's p50 reached 1,891 ms in one benchmark, while paid providers stayed in the tens of ms (https://www.dwellir.com/blog/best-polygon-rpc-for-polymarket). Public WebSocket endpoints have no SLA and are not fit for this.
  - (b) Paid providers. `eth_call` p50 ranged from 9 ms (Dwellir) to 68 ms (dRPC) from Stockholm (same Dwellir source, vendor-published). An Alchemy Tokyo test put Polygon p95 at 44 ms for Alchemy, 125 ms for dRPC and 216 ms for QuickNode (https://www.alchemy.com/blog/which-rpc-provider-tokyo.md, vendor-published). No neutral benchmark of `newHeads` or `logs` push delay exists. Alchemy's guide recommends racing `newHeads` across providers and counting first arrivals (https://www.alchemy.com/blog/how-to-benchmark-rpc-providers).
  - (c) A self-hosted Bor node. It sees a block as soon as it imports it, with no provider fan-out layer. No published number exists. The gain over a good provider is an estimate of 10 to 100 ms. Requirements are 8 to 16 cores, 32 to 64 GB RAM, 4 to 6 TB NVMe and 1 Gbit/s (https://docs.polygon.technology/pos/how-to/prerequisites.md).
- **Mempool.** Plain `newPendingTransactions` returns hashes only. Full bodies need `newPendingTransactionsWithBody` on Chainstack (https://docs.chainstack.com/reference/polygon-native-subscribe-newpendingtransactions) or Alchemy's full-pending methods (https://docs.alchemy.com/reference/alchemy-pendingtransactions). Polymarket's operator transactions are public. This session measured a median 0.58 s lead before the block, with 58% visible. Struct reports about 97% visible in its mempool feed and 99.9% to 100% of pending CLOB fills later confirming (https://www.struct.to/blog/polymarket-rtds-vs-struct-trades-ws). The gap between 58% and 97% is most likely peering breadth (inference). Under VEBloP, the useful peer is the one close to the active producer.

## 3. Seeing the target's trade earliest

The sequence of events is fixed. The source's order matches off-chain. The operator then submits `matchOrders` on-chain, and only the operator can call it (https://docs.polymarket.com/concepts/order-lifecycle.md, https://www.chaincatcher.com/en/article/2262869). The transaction enters the mempool, then lands in a block, then gets indexed.

| Rank | Feed | Identity? | Lead vs RTDS | Evidence |
|---|---|---|---|---|
| 1 | Mempool decode of operator `matchOrders` calldata (maker and taker addresses are in the signed orders) | Yes | median about 2.6 s, best case about 5 s | Struct vendor benchmark, about 11k pairs (https://www.struct.to/blog/polymarket-rtds-vs-struct-trades-ws). Measured here, 0.58 s before the block, 58% visible |
| 2 | On-chain `OrderFilled` log push over WS | Yes | median about 0.5 s, ahead 85% to 90% of the time | Struct (same URL). Measured here, WS beats 0.5 s polling by a median 537 ms |
| 3 | RTDS `activity` / `trades` (`wss://ws-live-data.polymarket.com`) | Yes (wallet on trade) | baseline | https://docs.polymarket.com/market-data/websocket/overview.md. One report of streams silently stalling after about 20 min (https://github.com/Polymarket/real-time-data-client/issues/26) |
| 4 | `eth_getLogs` polling | Yes | behind by about half the poll interval | measured here |
| n/a | CLOB market channel `last_trade_price` | No | fires at match, so earliest of all | https://docs.polymarket.com/market-data/websocket/market-channel.md |

The market channel is the true first signal, but it carries no wallet. Its use is as a pre-arm. When a trade prints on a market the target is known to hold or watch, start warming that market's book and prebuilding the order shell. Let the mempool or the log then confirm whose trade it was. The expected gain is not measured and is likely tens of ms (estimate). The user channel is authenticated and covers only your own orders (https://docs.polymarket.com/market-data/websocket/user-channel.md). It is useless for watching others.

The right shape is to run mempool and log feeds together and dedupe on `(tx_hash, log_index)`. Act on the first sighting. Treat the confirmed log as the reconciliation record. Revert risk is small (99.9% to 100% per Struct). One academic study still counted about 2 million reverted `matchOrders` transactions between August 2025 and May 2026 (https://arxiv.org/html/2606.16852v1). So booking must tolerate a pending fill that never confirms.

## 4. Networking

1. **Colocate in AWS eu-west-1 (Dublin), if the operator is eligible.** This is the nearest non-restricted region (official docs). It is a latency choice for an eligible operator, not a way around restrictions: the geoblock check from this workstation reports US/CA, and polycopy refuses live trading there by design. Expected order RTT falls from about 183 ms (measured here) to single-digit ms (vendor-reported 0.83 ms, estimate 2 to 10 ms). Put the RPC endpoint or Bor node in the same region. Dwellir's data shows RPC latency is a proximity problem.
2. **Stop the per-order geoblock call (done in PR 4).** `LiveGate.check(fresh=True)` calls `GeoblockChecker.check()`, which makes a full HTTPS round trip to `polymarket.com/api/geoblock` before each order (`app/integrations/polymarket/live.py`, about line 88). It took 120 to 430 ms measured here. Refresh it on a timer, for example every 60 s, and on IP change. Gate on the cached value. Eligibility stays enforced, because the venue geoblocks server-side too.
3. **Keep connections warm.** A reused connection cut TTFB from 191 to 158 ms here (measured, one sample), because it skips TCP and TLS setup. Hold one long-lived client. Send a cheap `GET /time` every 15 to 30 s so Cloudflare and the origin keep the connection. `ResilientHttp` already pools connections (`app/integrations/http.py`).
4. **HTTP/2.** The CLOB negotiates h2 (measured here). `http2=False` is set in `app/integrations/http.py`, and `h2` is already installed. h2 removes HTTP/1.1 head-of-line blocking when a book fetch and an order overlap on one connection. Gain only under concurrency (estimate). Or keep a dedicated connection for orders only.
5. **TLS resumption and DNS.** A warm connection makes resumption moot. Cache DNS in-process, or pin resolved IPs with a TTL refresh. Cold DNS took up to 29 ms here (measured).
6. **TCP_NODELAY.** asyncio sets it by default since Python 3.6, with a Linux fix in 2017 (https://www.python.org/pipermail/python-checkins/2017-December/153208.html). Nothing to do.
7. **Order templates.** EIP-712 signing with `eth_account` costs 2.4 ms per order (measured here, Apple Silicon, n=200). Price and size depend on the signal, so full pre-signing is impossible. Precompute the domain separator, the API-key HMAC inputs and the JSON skeleton. A libsecp256k1 binding (coincurve) would cut the 2.4 ms further (estimate).
8. **WS hygiene.** Use one dedicated socket per feed, so log, mempool and book traffic never share a reader. Send app-level pings. Add a staleness watchdog that reconnects when no message arrives in N seconds (RTDS issue above). Run two RPC providers in a hot-hot race. Dedupe the race by first arrival.

## 5. CPU and runtime

- **The numbers.** Service time is about 10.8 ms p50 and 28 to 33 ms p99 at 34 SQL statements per copy. cProfile puts about 45% of it waiting on PostgreSQL to execute, about 40% in SQLAlchemy ORM CPU, and under 5% in polycopy's own code. The logic takes about 20 µs. `synchronous_commit=off` gains only 2 to 3%, so WAL flush is not the cost (all measured, local). The fix is architecture, not a faster language.
- **Take the DB off the critical path.** Hold market metadata, source equity, portfolio and cash in memory as the authoritative copy. Load it at startup and update it on each event. Reserve cash with an in-process lock. Send the order. Push tape, journal and ledger rows to a write-behind queue that one writer drains in batches with `COPY` or `executemany`. Crash safety needs an append-only local journal (fsync'd file or SQLite WAL) before the order, or an accepted "replay from chain" recovery. That is an idempotency design question, not a latency one. Expected p50 falls from about 11 ms to under 1 ms (estimate: it removes both the PostgreSQL wait and the ORM CPU above). The p99 tail falls further.
- **uvloop.** It is already installed. The project claims 2 to 4 times asyncio throughput (https://github.com/MagicStack/uvloop). At this message rate the gain is tens of µs per event (estimate).
- **Parsing.** Use orjson (https://github.com/ijl/orjson) or msgspec with typed Structs (https://jcristharif.com/msgspec/benchmarks.html). Decode only the fields you need, and filter by topic before decoding. The mempool body feed is the one high-volume stream, so pre-filter on `to == exchange` and the `matchOrders` selector before any ABI decode.
- **GC.** Call `gc.freeze()` after startup loads, so long-lived objects leave the scan set (https://docs.python.org/3/library/gc.html#gc.freeze). Raise gen0 thresholds. This trims rare multi-ms pauses (estimate).
- **Isolation.** Run one process per hot feed, with its own core. On a VM, pinning is coarse and gains little. Do not share the box with Postgres when Postgres is off the critical path.
- **When Rust or C++ is worth it.** It is not worth it today. Logic plus parse takes well under 1 ms, against 300 ms to 2 s of detection lag. Revisit only if mempool decode of every pending Polygon transaction becomes CPU-bound. Profile first. Even then, a Rust parser module behind PyO3 beats a rewrite.

## 6. Hardware

What matters:
- Region and network path, by hundreds of ms.
- Peering of the mempool-watching node, by detection coverage from 58% toward 97%.
- NVMe and RAM on a self-hosted Bor node, so it keeps up with the chain tip. A lagging node is worse than a provider.
- A modern CPU with fast single-thread speed for Python. That is ordinary cloud hardware.

What does not matter, candidly:
- **Kernel bypass** (DPDK, Onload, ef_vi). It saves single-digit µs per packet. The order crosses Cloudflare and AWS over TLS. Irrelevant.
- **FPGAs and NIC timestamping.** They target ns-level tick-to-trade on exchange cross-connects. Polymarket has no cross-connect, no binary protocol and no colocation product. Irrelevant.
- **Microwave or private links, and bare-metal tuning** (huge pages, isolcpus, IRQ affinity). They are µs-scale wins buried under the 250 ms taker delays and 1.5 s blocks. Not worth the effort.

The HFT lesson that does transfer is discipline, not hardware. Measure every hop with timestamps, take the shared resource (the DB) off the hot path, race redundant feeds, and keep everything warm.

## Recommendations for polycopy

| # | Change | Expected latency gain (basis) | Effort | Risk |
|---|---|---|---|---|
| 1 | Add mempool detection with full-body pending subscription, decode `matchOrders`, race against logs | about 0.58 s median on 58% of fills now (measured). Up to about 2.6 s vs RTDS with better peering (sourced, Struct) | Medium | Low. Pending fills revert rarely (sourced). Booking must handle never-confirmed fills |
| 2 | Move the live host and RPC to AWS eu-west-1, **only for an operator who is themselves eligible to trade there** | order RTT about 183 ms down to about 2 to 10 ms (measured here plus estimate) | Low | Legal. Server location never changes who may trade. Never use hosting to get around a geoblock |
| 3 | Cache the geoblock result and refresh it on a timer, not per order. **Shipped:** orders reuse a result up to 90 s old | 120 to 430 ms per order (measured here) | Low | Low. A cached blocked or failed result still refuses |
| 4 | Make in-memory state authoritative and the DB write-behind | about 11 ms p50 and 30 ms p99 down to under 1 ms (estimate from measured 99% DB share) | High | Medium. Needs a crash-safe journal and an idempotent replay |
| 5 | Add a second RPC provider hot-hot, or run a self-hosted Bor node in-region with wide peering | 10 to 100 ms on logs, plus mempool coverage (estimate) | Medium (provider) or High (node) | Low (provider) or Medium (ops) |
| 6 | Run a warm order connection with `/time` keepalive, enable HTTP/2, cache DNS | about 30 ms on cold orders (measured here, single sample) | Low | Low |
| 7 | Pre-arm on the CLOB market-channel `last_trade_price` (warm book, prebuild order shell) | tens of ms (estimate) | Medium | Low |
| 8 | Precompute the signing template, or switch to coincurve signing | up to about 2 ms (measured cost 2.4 ms) | Low | Low |
| 9 | uvloop, msgspec/orjson, `gc.freeze()` | under 1 ms, mostly tail (estimate) | Low | Low |
| 10 | Rust, kernel bypass, FPGA | about 0 at this scale (estimate) | High | High. Skip |

Unavoidable floor. Polymarket's 250 ms crypto taker delay and per-market sports delays add to the lag (sourced, order-lifecycle docs). They also apply to the source's own fills, so they shift both sides. Relative ranking against other copiers is decided by detection, which is rows 1, 5 and 7.
