# CLAUDE.md — BTC Grid Engine Project

> **This is the authoritative project context file. Every Claude session MUST read this first.**
> **Last updated: 2026-04-15**

---

## CRITICAL RULES — READ BEFORE MAKING ANY CHANGES

1. **NEVER send `take_profit_steps` in 3Commas `create_bot` DCA payload.** The API rejects payloads with both steps and scalar TP. Always collapse to scalar `take_profit` at the max step profit %. This cost days to debug.

2. **NEVER recentre the grid every cycle.** Grid centre only updates on confirmed drift events or explicit redeploys. Recentring every cycle was a critical bug that caused constant order cancellation.

3. **NEVER call `start_bot()` on an already-running grid bot.** 3Commas `POST /enable` cancels and re-places ALL grid orders even if the bot is already running. This kills in-progress grid cycles and produces tiny fills. The engine uses `_bot_last_action` to track state and skip redundant calls.

4. **Bot IDs are strings in `.env` but may be used as ints in code.** Always check type when comparing.

5. **The deploy branch is `claude/grid-engine-chat-review-hEEGu`** — NOT `main`. Always push to this branch and deploy via the `/deploy` endpoint.

6. **SSH from Claude Code is blocked** (sandboxed environment, port 22 unreachable). All deploys go through git push + `POST /deploy`.

7. **`inventory_settings.json` on the droplet holds the LIVE inventory thresholds** — the defaults in `inventory.py` are fallbacks. Always check the live settings via `GET /inventory/settings`.

8. **Xero OAuth scopes** — NEVER tell Ash to enable scopes in the Xero Developer Portal; that UI was removed post-March 2026.

---

## What This Project Is

An adaptive BTC/USDC grid trading bot running on a DigitalOcean VPS. It manages three 3Commas grid bots across different range tiers (inner/mid/outer), and exposes a live web dashboard. The engine runs every ~2 minutes (100s minimum gap), reads market data from Coinbase, and autonomously starts/stops/redeploys bots based on regime, inventory, breakout detection, and drift logic.

---

## Infrastructure

| Item | Value |
|------|-------|
| Server | DigitalOcean Droplet `grid-engine` (London, Ubuntu 24.04) |
| Public IP | `165.232.101.253` |
| Tailscale IP | `100.94.227.121` |
| Dashboard URL | `http://100.94.227.121:5050` (Tailscale required) |
| Dashboard Token | Read from `$GRID_DASHBOARD_TOKEN` — never commit the value |
| Process manager | tmux session `grid` |
| Deploy branch | `claude/grid-engine-chat-review-hEEGu` |
| Deploy command | `curl -s -X POST "http://100.94.227.121:5050/deploy?token=$GRID_DEPLOY_TOKEN"` |

### Security
- UFW active. Ports 5050/9001/8080 restricted to Tailscale subnet (100.64.0.0/10)
- Port 443 (nginx HTTPS) and 22 (SSH) open to world
- Tailscale installed on droplet and macbook-air-3
- Port 5050 still plain HTTP (token in URL) — nginx HTTPS wrapping pending

---

## Bot Mapping

| Bot ID | Name | Tier | range_mult | base_levels |
|--------|------|------|------------|-------------|
| 2743885 | Narrow | inner | 0.75 | 10 |
| 2743889 | Mid | mid | 1.5 | 6 |
| 2743888 | Wider | outer | 2.0 | 4 |

All bots trade `USDC_BTC` on Coinbase Spot via 3Commas.

### Tier Capital Budgets (from `tier_budgets.json`)
- inner: 38%, mid: 31%, outer: 26%, reserve: 5%
- Editable via dashboard Grid tab sliders
- Budget calc: `qty_per_grid = budget_usd / (grids × mid_price)`

---

## Deploy Workflow

**Two-step process — Claude does both:**

1. Commit and push to `claude/grid-engine-chat-review-hEEGu`
2. POST to `/deploy`:

```bash
curl -s -X POST "http://100.94.227.121:5050/deploy?token=$GRID_DEPLOY_TOKEN"
```

The `/deploy` endpoint downloads all engine files from the feature branch on GitHub and restarts itself. Verify with `curl -s http://100.94.227.121:5050/ping`.

---

## Engine Logic (engine.py)

### Cycle (~2 minutes, 100s minimum gap)
1. Fetch BTC/USDC 1H OHLCV from Coinbase → ATR + BB indicators
2. Read trendline from `trendlines.json`
3. Load grid centre from `grid_state.json`
4. Detect regime (RANGE / TREND_UP / TREND_DOWN / COMPRESSION)
5. Calculate trend_strength (gap_ratio, trending_up/down)
6. Fetch inventory (btc_ratio, skew) from 3Commas
7. Calculate three-tier grid parameters (grid_logic.py)
8. Weekend mode check (Fri 21:00 → Mon 07:00 UTC)
9. Inventory protection (SELL_ONLY / BUY_ONLY with hysteresis)
10. Breakout detection → asymmetric response
11. Drift check → redeploy if price moved >85% of deploy_grid_width from centre
12. Tiered bot decisions (start/stop via `_act()`)
13. Write status + log entry

### Bot Action Deduplication (`_act()`)
The engine tracks `_bot_last_action` per bot to avoid redundant `start_bot()`/`stop_bot()` API calls. Key rules:
- Only updates cache on successful API response (200/201/204)
- On failure: cache NOT updated, so next cycle retries
- Every 10 cycles (~20 min): cache cleared to catch external state changes
- After every `redeploy_all_bots()` call: `_mark_all_bots_started()` syncs cache

### Tiered Bot Decision Table
```
Regime/State     | inner | mid   | outer
-----------------+-------+-------+------
RANGE            | ON    | ON    | ON
TREND_UP         | ON    | ON    | ON
trending_up      | OFF   | ON    | ON
TREND_DOWN       | OFF   | OFF   | ON
COMPRESSION      | OFF   | OFF   | OFF
BREAKOUT_UP      | OFF   | OFF   | ON
BREAKOUT_DOWN    | OFF   | OFF   | OFF
```

---

## Three-Tier Grid System (grid_logic.py)

```python
TIERS = [
    {"name": "inner", "range_mult": 0.75, "base_levels": 10, "wkd_fee_buffer": 1.2, "compression_mult": 1.5},
    {"name": "mid",   "range_mult": 1.5,  "base_levels": 6,  "compression_mult": 1.2},
    {"name": "outer", "range_mult": 2.0,  "base_levels": 4,  "compression_mult": 1.0},
]
```

### Drift-Zone Cap
Each tier's drift limit scales proportionally by its `range_mult`:
```python
_base_drift = (_deploy_gw * 0.85)
_drift_limit = _base_drift * tier_range_mult / inner_range_mult
```
**History:** Was a flat cap derived from inner tier width, which collapsed mid and outer onto identical ranges. Fixed Apr 15 2026.

### Fee Guard
```python
TAKER_FEE = 0.0020
ROUND_TRIP_FEE = 0.0040   # buy + sell
FEE_BUFFER = 1.5           # step must be 1.5× break-even (1.2× on weekends for inner)
min_step = price × ROUND_TRIP_FEE × FEE_BUFFER   # ~$427 at $71k
```
If calculated step < min_step, level count is reduced until profitable.

---

## Inventory System (inventory.py)

### Live Settings (from `inventory_settings.json` on droplet)
```json
{
  "target_btc": 0.45,
  "lower_band": 0.38,
  "upper_band": 0.55,
  "taper_zone": 0.05,
  "min_btc": 0.35,
  "max_btc": 0.70
}
```

**These are the LIVE values** — defaults in `inventory.py` are different and only used if the settings file is missing. Always verify via `GET /inventory/settings`.

### Inventory Mode Hysteresis (Apr 15 2026)
- Enter SELL_ONLY: btc_ratio > max_btc (0.70)
- Exit SELL_ONLY → NORMAL: btc_ratio < max_btc - 0.05 (0.65)
- Enter BUY_ONLY: btc_ratio < min_btc (0.35)
- Exit BUY_ONLY → NORMAL: btc_ratio > min_btc + 0.05 (0.40)

**History:** Without hysteresis, inventory mode oscillated rapidly (4 flips in hours), triggering full redeploys each time. Each redeploy cancels all orders, so fills could only happen within ~2 minute windows.

### SELL_ONLY / BUY_ONLY Intensive Mode
When entering SELL_ONLY/BUY_ONLY, `_make_intensive_sell_tiers()` / `_make_intensive_buy_tiers()` shifts all grid ranges above/below price (60% width compression). Drift is suppressed during intensive mode.

**Known issue:** The 60% compression on top of already-compressed grids can produce sub-fee-floor steps. The intensive sell functions do NOT run through the fee guard.

**Known issue:** 3Commas reports BTC locked in grid bots as part of your balance. When SELL_ONLY deploys all-BTC ranges, the reported BTC ratio inflates artificially (e.g. 74% → 85%), creating a feedback loop. Consider raising max_btc to 0.80 if this causes problems.

---

## Weekend Mode

- Active: Fri 21:00 → Mon 07:00 UTC
- Grid compressed to 65% width, level count reduced by 0.65×
- Near-level guard: won't activate if price is within 20% of tightest step of any grid level
- Sunday 23:00 UTC: Asia open recentre if drift > 40% of tight grid width
- Drift suppressed during weekend mode

---

## Breakout Detection (breakout.py)

### Detection Layers
1. **Momentum** (primary): 4 consecutive closes in same direction + total move > ATR×1.0
2. **Volatility spike** (secondary): ATR > 1.7× rolling avg OR BB width > 2× avg

### Response
- `BREAKOUT_UP`: outer runs, inner+mid off, await exhaustion then redeploy
- `BREAKOUT_DOWN`: all bots off (or BUY_ONLY stays on if active)
- DOWN recovery: clears when price > fire_price + 1.5×ATR

### Exhaustion Detection
5-candle avg move < ATR×0.05. Triggers redeploy at new price level, clears breakout state.

---

## Price Targets & DCA Bots

### DCA Bot Creation (threecommas_dca.py)
**CRITICAL:** Never send `take_profit_steps` in the create_bot payload. Always use scalar:
```python
body["take_profit_type"] = "total"
body["take_profit"] = str(round(max_step_profit_pct, 2))
```

Required fields that 3Commas doesn't document well:
- `strategy: "long"` — classifies as long bot
- `start_order_type: "market"` — base order fills at market (default is limit = no fill)
- `strategy_list: [{"strategy": "nonstop", "options": {}}]` — auto-starts deals

### SmartTrade Features
- Dual entry: scout + retest bots for UP targets
- Multi-step TP: array of {profit_pct, close_pct} steps
- Status polling each cycle; on terminal status clears target + restarts grid
- 2h auto-clear timeout as safety net

---

## Dashboard Features

### Key Endpoints
```
GET  /status              — engine status JSON
GET  /candles?tf=1h       — OHLCV from Coinbase
GET  /bots                — all bot configs from 3Commas
POST /bots/<id>/start|stop
GET  /inventory/settings  — live band/threshold settings
POST /inventory/settings  — update settings
GET  /bots/fills          — fill history (from 3Commas + fills_log.jsonl)
GET  /notifications       — parsed events from engine_log.jsonl
GET  /engine/output       — last 200 lines of engine stdout
GET  /engine/status       — engine process running/stopped
POST /engine/start|stop
GET  /budgets             — tier budget percentages
POST /budgets             — update tier budgets
```

### Fill Markers on Chart
Uses Lightweight Charts `setMarkers()` — known limitation: only one marker per candle per side, older fills outside visible range are dropped. This is a persistent issue; approach needs rebuilding with scatter series.

### Bot Override System
- `bot_overrides.json` — manual Lock/Unlock per bot
- Lock prevents engine from auto-starting a bot
- Dashboard has Lock/Unlock buttons on bot cards (desktop + mobile)

---

## Known Issues (Apr 15 2026)

| Issue | Status |
|-------|--------|
| Port 5050 plain HTTP | Pending — nginx HTTPS wrapping needed |
| Fill markers missing on chart | Known — setMarkers approach needs rebuild |
| Intensive sell 60% compression ignores fee guard | Known — can produce sub-profitable steps |
| 3Commas BTC ratio inflated during SELL_ONLY | Known — bot-locked BTC counted in ratio |
| `auto_clear_h` per-target not in dashboard UI | Must edit breakout_targets.json directly |
| Inventory API 401 intermittent | inventory_override.json as workaround |

---

## Engine Features (all completed)

- Tier capital budget system (dashboard sliders)
- Support failure detection (4-phase state machine)
- SmartTrade sell launch on support failure
- SmartTrade status polling + 2h auto-clear
- SmartTrade dual entry (scout + retest) for UP targets
- TREND_DOWN hysteresis (Schmitt-trigger)
- TREND_DOWN auto-clear (8 stable cycles + bounce)
- Trendline auto-activation after TREND_DOWN recovery
- SELL_ONLY intensive sell tiers
- BUY_ONLY intensive buy tiers
- Inventory mode hysteresis (enter/exit thresholds)
- Bot action deduplication (_act tracks state, skips redundant API calls)
- Bot override system (Lock/Unlock)
- Weekend tight grid (Fri 21:00 → Mon 07:00 UTC)
- DOWN breakout recovery
- Fill persistence (fills_log.jsonl)
- Events log (engine_log.jsonl)
- Portfolio snapshots (portfolio_log.jsonl)
- Flash move detector
- Faster compression exit
- Drift-zone cap scaled per tier
- DCA launch error surfacing in notifications

---

## 3Commas API Reference

- **Base URL:** `https://api.3commas.io/public/api`
- **Auth:** RSA PKCS1v15 SHA256, sign `/public/api` + path + body
- **Grid bot enable:** `POST /ver1/grid_bots/{id}/enable` — WARNING: cancels and re-places all orders
- **Grid bot disable:** `POST /ver1/grid_bots/{id}/disable`
- **Grid bot params:** `PATCH /ver1/grid_bots/{id}/manual` then enable
- **DCA bot create:** `POST /ver1/bots/create_bot` — see DCA rules above
- **Balance sync:** `POST /ver1/accounts/{id}/load_balances` then wait 8s
- **Balance read:** `POST /ver1/accounts/{id}/pie_chart_data`

---

## Quick Reference

```bash
# Deploy from Claude Code
cd "/Users/ashleyskett-seakit/Ashs Brain/btc-dashboard"
git add <files> && git commit -m "message" && git push origin claude/grid-engine-chat-review-hEEGu
curl -s -X POST "http://100.94.227.121:5050/deploy?token=$GRID_DEPLOY_TOKEN"

# Verify deploy
curl -s "http://100.94.227.121:5050/ping"

# Check engine output
curl -s "http://100.94.227.121:5050/engine/output?token=$GRID_DASHBOARD_TOKEN"

# Check bot state
curl -s "http://100.94.227.121:5050/bots?token=$GRID_DASHBOARD_TOKEN"

# Check inventory settings
curl -s "http://100.94.227.121:5050/inventory/settings?token=$GRID_DASHBOARD_TOKEN"

# Check notifications
curl -s "http://100.94.227.121:5050/notifications?token=$GRID_DASHBOARD_TOKEN"
```
