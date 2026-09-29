# lighter_marketMaking

Market-making bot + points-farming tracker for [Lighter](https://lighter.xyz) (zkLighter perps DEX).
Goal: run resting two-sided quotes on Lighter to earn Season 2 points (retail volume +
market-maker liquidity points), while measuring real cost-per-point so we know whether
it's actually worth running past the test phase.

**Status: Phase 0 (market-data monitoring only). No live orders are placed yet.**

## Why this shape

Full research writeup is in the conversation history; the short version:

- Lighter's public REST API (`https://mainnet.zklighter.elliot.ai/api/v1/...`) exposes order
  books, depth, and fees **with no auth**. Placing orders needs an API key derived from the
  account's L1 key, generated via the SDK.
- Market-maker points (20% of the weekly 250k pool) are scored from **hourly random order-book
  sampling**: depth at $10k/$30k/$100k/$300k and spread bands at 1/2/5/10 bps from mid, bid and
  ask scored separately. Weekly maker volume also scores directly:
  `score = volume + max(0, (volume - 2.5B) * 0.25)`.
- Only **Premium accounts** earn MM points, and Premium accounts pay fees (0.004% maker /
  0.028% taker base, discounted by staking LIT or buying fee credits).
- Live top-of-book on majors/RWA pairs (BTC, gold, TSLA, NVDA, WTI, EURUSD) is already sub-1-3
  bps, held by a handful of clearly systematic accounts. We will not win the spread there — the
  realistic edge is resting **depth in the 5-10bps / $100k+ bands**, which the scoring explicitly
  rewards and which is less contested.
- Season 2's points → $ conversion rate is **not published**. Treat every dollar spent on fees
  as a real cost against an unconfirmed payoff. This is a paid experiment, not a known-profitable
  strategy.

## Repo layout

```
src/
  config.py          # loads .env, holds all tunables in one place
  market_data.py      # public, no-auth: order book snapshots, spread/depth calc
  points_tracker.py   # CSV log of volume/fees vs. points -> $/point over time
  strategy.py          # quote generation (bands, position/loss limits, circuit breaker)
  trader.py            # SignerClient wrapper — STUBBED, see "Order placement" below
run_monitor.py         # safe to run right now, no API keys needed
run_mm.py               # the actual bot loop — DRY_RUN=true by default
data/                   # gitignored — csv logs land here
.env.example            # template; copy to .env and fill in real values
```

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in real values, .env is gitignored
```

## Phase plan

**Phase 0 — Monitor only (now, no keys needed)**
Run `python run_monitor.py`. It polls public order books for the configured markets every
N seconds, logs spread (bps), depth at each threshold, and top-of-book account indices to
`data/market_snapshots.csv`. Purpose: confirm which markets/bands are actually winnable
before risking a cent, and build a baseline of "normal" spread so the bot can tell when it's
about to quote into a moving market.

**Phase 1 — Testnet, dry-run**
Once keys exist: point `LIGHTER_BASE_URL` at `https://testnet.zklighter.elliot.ai`, keep
`DRY_RUN=true`. `run_mm.py` computes what it *would* quote (price, size, bands) from
`strategy.py` and logs it, but the order-placement calls in `trader.py` are not implemented
yet — see below. This phase is about validating the quoting logic and risk limits against
real testnet book data before any code can touch funds.

**Phase 2 — Mainnet, tiny size, still capped**
Flip to mainnet base URL, small `MAX_POSITION_USD` / `MAX_ORDER_USD`, `DRY_RUN=false`.
Run for a few days, pull `data/points_log.csv` after each Friday distribution, compute
$/point, decide whether to scale or stop. Do not skip Phase 0/1.

## Order placement — needs verification before Phase 1

`src/trader.py` has the wiring (config, position tracking, safety checks) but the actual
`SignerClient.create_order(...)` / `cancel_order(...)` calls are left as `NotImplementedError`
with the exact shape to fill in. Reason: I don't have a verified, current method signature for
Lighter's Python SDK order calls, and guessing at that with real funds is how you send a
malformed order or fat-finger a size. Before Phase 1:

1. `pip install lighter-sdk` (or `pip install git+https://github.com/elliottech/zklighter-perps-python.git`)
2. Read `examples/system_setup.py` and the order-placement examples in that SDK repo directly.
3. Fill in the two methods in `trader.py`, test on testnet with `DRY_RUN=true` first (it'll log
   the exact call it would make without sending it), then `DRY_RUN=false` on testnet only.

## Safety limits (config.py / .env)

- `MAX_POSITION_USD` — hard cap on net exposure per market
- `MAX_ORDER_USD` — cap on a single resting order
- `DAILY_LOSS_LIMIT_USD` — bot halts and cancels all orders if breached
- `DRY_RUN` — when true, no order-placing call is ever made, only logged
- `MARKETS` — comma-separated symbols to quote (start with 1-2, e.g. `XAU,EURUSD`)

## Points economics worksheet

Track these every week in `data/points_log.csv` (`run_monitor.py`/manual entry, whichever
the dashboard allows):

| metric | source |
|---|---|
| maker volume ($) | your account / Lighter UI |
| fees paid ($) | your account / Lighter UI |
| MM points earned | Lighter points dashboard |
| retail/volume points earned | Lighter points dashboard |
| implied $/point | fees_paid / total_points (lower bound, ignores adverse fills) |

Stop-loss rule: if measured $/point stays above what you're willing to pay for an unconfirmed
token conversion, stop — don't extrapolate from Season 1's ~$40-140/point, Season 2's rate is
unknown.

## Disclaimer

This is experimental. Real funds, real fees, no guaranteed points value, no guaranteed token
conversion. Start on testnet, then mainnet with the smallest size that still tells you
something.
