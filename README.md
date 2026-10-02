# lighter_marketMaking

Market-making bot for [Lighter](https://lighter.xyz) (zkLighter perps DEX), built to earn the
**Liquidity Partner Program** rewards: a weekly LIT pool (~25,000 LIT/week in Sept 2026) paid to
Premium accounts for resting orders near the top of the book.

## What the program pays for (verified 2026-09-29)

From [docs.lighter.xyz/liquidity-partner-program](https://docs.lighter.xyz/liquidity-partner-program)
and its [public spreadsheet](https://docs.google.com/spreadsheets/d/1_Jr4C94Q-F2DsVar_Tmc15SBGLc-lSVCjwgkQ6f_3po):

- Order-book snapshots ~every minute at random. Rewards per snapshot scale with your resting
  size × a multiplier that **decays exponentially with distance from the best bid/offer** ×
  a volatility multiplier. Half the pool goes to bids, half to asks.
- One global weekly pool split across ~30 markets by weight. New weights every **Monday 17:00
  UTC**; results + LIT payouts every Monday.
- Premium account required. (Points Season 2 — what this repo originally targeted — **ended
  2025-12-26**.)
- Thin markets pay as much as deep ones. ANTHROPIC/OPENAI paid 971 LIT/week each with only
  ~$400–700 of competing liquidity within 2bps of mid, vs. ~$860k on US100 for 1,214 LIT.
  `tools/rank_markets.py` shows this every week.

## How the bot trades

`run_mm.py`, every second:

1. **Account-wide checks** — STOP file, daily loss limit (per UTC day, persisted in
   `data/state.json` so restarts don't reset it), consecutive tx errors, market-data feed
   health, gross exposure, free margin, systemic breaker (3+ markets tripping at once).
2. **Per market** — quotes are pulled if any of these say no: market not active / force-reduce-
   only · session blackout (`src/sessions.py`) · book too wide or mid far from mark price ·
   volatility breaker (mid moved > max(40bps, 2× hourly vol) in 30s → out for 5 min).
3. **Quote** — join the best bid/ask (step one tick inside when the spread ≥ 10bps), but no
   closer to mid than max(MIN_EDGE_BPS, EDGE_VOL_MULT × hourly vol), shifted against inventory
   (skew). Default is pure join (0.5bp / 0): on 2026-10-01 a vol-scaled edge left orders ~6bps
   behind the top, where they earned ~0 rewards but still got picked off. Position cap per
   market: the side that would breach it stops quoting. `MARKET_PARAMS` overrides any of these
   per market; `HARD_MAX_ORDER_USD` is a last-line ceiling checked just before signing.
4. **Reconcile** with the bot's resting orders using modify (1 tx) instead of cancel+place.

All orders are post-only (never pay taker fees). The bot only ever touches orders it tagged
itself (`client_order_index ≥ 2^47`); orders you place in the UI are left alone — except by the
dead man's switch, which is account-wide.

### Trading hours and event risk (`src/sessions.py`)

Lighter lists stocks/FX/commodities 24/7, but prices them off their own book while the real
market is closed, then snaps to the oracle at the reopen. Three weeks of Lighter candles showed
where the jumps are, so the bot pulls quotes in those windows and goes **reduce-only for 10
minutes before each one** (no new inventory into the event):

| class | blackout windows (US Eastern) |
|---|---|
| all | weekdays 08:28–08:40 (CPI/NFP/claims), FOMC decision days 13:58–14:30 |
| US stocks/indices | 03:55–04:10, 09:25–09:45, 15:58–16:05, 19:55–20:10 (Sun–Thu) |
| CME metals/energy | 16:58–18:05 daily, Sun 17:55–18:10 |
| Brent | 17:58–20:05 daily, Sun 19:55–20:10 |
| FX | 16:58–17:05 daily, Sun 16:55–17:10 |
| crypto, pre-IPO | only the "all" windows |

RWA weekends are the *quiet* part (median hourly move ~1/3 of weekdays); the danger is the
reopen, which the windows cover. Markets not listed in `sessions.py` are refused. FOMC dates are
hardcoded through 2027 — add 2028 when the Fed publishes them.

### Volume quota — the real constraint for a small account

Premium accounts spend 1 "volume quota" per order create/modify **and per cancel-all — which
includes every dead man's switch refresh** (single cancels are free), get one free tx every 15s
(taken by *any* tx, cancels too), and earn 1 quota per $2 traded. The bot refreshes the switch
only in a free slot (forced after 210s), and counts every tx against the free slot. The bot reads the remaining quota from every
response (`HEARTBEAT ... quota=`), caps paid updates at `MAX_PAID_TX_PER_MIN`, and below
`QUOTA_RESERVE` drops to the free slot only. When it can't afford to move an order that the
market has moved against, it cancels it (free) instead. More markets = more updates = faster
quota burn; 3–5 markets is the sweet spot at this size.

### Safety nets

- Dead man's switch: Lighter scheduled cancel-all 330s out, refreshed every 60s (Lighter's
  minimum is 300s). Process dies / laptop sleeps → all orders gone within ~5–6 min.
- Stop (`touch data/STOP`, `pkill -f run_mm.py`, Ctrl-C), daily-loss halt, and tx-error halt
  all cancel the bot's orders, then **close every position on the account** with reduce-only
  market orders (worst price 1%, then 3% from the touch) so it ends holding only USDC
  (`FLATTEN_ON_STOP=true`). The switch is disarmed only once the cancel is confirmed.
  A hard kill (`kill -9`), crash of the machine, or sleep skips this — the dead man's switch then
  cancels orders but positions stay open.
- Feed silent > 10s, or account data stale > 30s → all quotes pulled until healthy.
- Any exception in one market pulls that market's quotes; others keep running.
- `lighter-sdk` pinned to 1.1.5 (signer verified byte-identical to the official repo).

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                               # fill in account index + API key
.venv/bin/python tools/rank_markets.py             # pick MARKETS (Mondays)
.venv/bin/pytest -q tests                          # offline tests
DRY_RUN=true .venv/bin/python run_mm.py            # live data, paper orders — Ctrl-C to stop
nohup .venv/bin/python -u run_mm.py >> data/mm_live.log 2>&1 &   # live (DRY_RUN=false in .env)
tail -f data/mm_live.log                           # watch; HEARTBEAT line every 5 min
touch data/STOP                                    # graceful stop (delete the file to restart)
```

Weekly: check your address in the "Rewards …" tab of the spreadsheet on Mondays and compare
LIT earned × LIT price against the week's trading PnL. That number decides whether to scale up.

## Layout

```
src/config.py     all tunables (.env)          src/feed.py      websocket market data
src/sessions.py   trading hours / blackouts     src/risk.py      loss limit, breakers, book checks
src/strategy.py   quote math (pure)             src/trader.py    SDK wrapper, quota budget, reads
run_mm.py         the bot                       tools/rank_markets.py  weekly market ranking
run_monitor.py    old read-only depth monitor   tests/           offline tests (fake exchange)
```

## Disclaimer

Real funds. Rewards are paid in LIT at Lighter's discretion and the pool size changes weekly.
Thin markets that pay the most are also the most volatile, and new listings can gap (Feb 2026:
an $8.2M loss on the ARC market triggered auto-deleveraging). Keep size small.
