# CLAUDE.md — BTC Grid Engine Project

> **This is the authoritative project memory file. Every Claude session should read this first.**

---

## What This Project Is

An adaptive BTC/USDC grid trading bot running on a DigitalOcean VPS. It manages three 3Commas grid bots across different range tiers (inner/mid/outer), and exposes a live web dashboard. The engine runs every 5 minutes, reads market data from Coinbase, and autonomously starts/stops/redeploys bots based on regime, inventory, breakout detection, and drift logic.

---

## Infrastructure

| Item | Value |
|------|-------|
| Server | DigitalOcean Droplet — `grid-engine` |
| IP | `165.232.101.253` (London, Ubuntu 24.04) |
| SSH | `ssh root@165.232.101.253` |
| Project dir | `/root/grid-engine/` |
| Dashboard URL | `http://100.94.227.121:5050` (Tailscale) — requires Tailscale connected |
| Dashboard Secret | `dbf92fff8e0baf1c856ea590d74cd640a556a037ddd12369` |
| Process manager | tmux session named `grid` |
| Start command | `cd /root/grid-engine && source venv/bin/activate && python dashboard_server.py` |
| DO Account | ashskett@gmail.com |
| DO API Token | stored in `~/.config/doctl/config.yaml` (authenticated) |

**Note:** Direct SSH from the Claude Code sandboxed environment is blocked (outbound port 22 unreachable). Use git-push + manual deploy for code changes. `doctl` is installed and authenticated.

---

## Git Repositories

| Repo | Purpose |
|------|---------|
| `github.com/ashskett/btc-dashboard` | HTML dashboard files (this repo) |
| `/root/grid-engine/` on droplet | Full Python engine (not in git — deployed via rsync from Mac) |

**Deploy from Mac:** `~/grid-engine/deploy.sh` (rsync, excludes venv/__pycache__)

---

## .env File (at `/root/grid-engine/.env`)

```
THREECOMMAS_API_KEY=<64-char hex key>
THREECOMMAS_API_SECRET=/root/grid-engine/3commas_private.pem
THREECOMMAS_ACCOUNT_ID=33343788
GRID_BOT_IDS=2743193,2743191,2743190
```

**Critical:** `THREECOMMAS_API_SECRET` is a file path to the RSA private key PEM file, not the key content itself.

---

## 3Commas API Authentication

- **Key type:** Self-generated RSA (NOT system-generated) — bypasses IP whitelisting
- **Signing:** RSASSA-PKCS1-v1_5 with SHA-256, Base64-encoded signature
- **Base URL:** `https://api.3commas.io/public/api` (NOT `https://api.3commas.io`)
- **Sign target:** `/public/api` + path + body — the `/public/api` prefix **MUST** be in the signed string
- **Headers:** `Apikey` and `Signature`
- **PEM issue:** Must have Unix line endings. Fix: `sed -i 's/\r//' 3commas_private.pem`

---

## Bot Mapping

| Bot ID | Name | Tier | Behaviour |
|--------|------|------|-----------|
| 2743885 | Narrow | range_mult 0.75×, 10 base levels | Catches small oscillations |
| 2743889 | Mid | range_mult 1.5×, 6 base levels | Main workhorse |
| 2743888 | Wider | range_mult 2.0×, 6 base levels | Safety net (tightened from 3.0× Mar 25 — recentre+flash guard make extreme levels unreachable) |

All bots trade `USDC_BTC` on Coinbase Spot via 3Commas.

---

## File Structure (on droplet)

```
/root/grid-engine/
├── engine.py              # Main loop — runs every 5min via schedule
├── engine_state.py        # EngineState dataclass
├── engine_log.py          # Structured JSONL logging
├── dashboard_server.py    # Flask server on port 5050
├── dashboard.html         # Trading terminal UI (served at /)
├── breakout.py            # Directional breakout detection
├── regime.py              # RANGE/TREND_UP/TREND_DOWN/COMPRESSION detection
├── grid_logic.py          # Three-tier grid width, drift detection, fee guard
├── inventory.py           # Live BTC/USDC balance fetch from 3Commas
├── threecommas.py         # 3Commas API: stop/start/redeploy bots
├── market_data.py         # Coinbase OHLCV via ccxt
├── indicators.py          # ATR + Bollinger Bands via ta library
├── session.py             # ASIA/EUROPE/US session detection
├── status.py              # Writes engine_status.json each cycle
├── config.py              # ACCOUNT_ID, QUOTE_CURRENCIES, MAX_SKEW
├── .env                   # Credentials (never commit)
├── 3commas_private.pem    # RSA private key (never commit)
├── grid_state.json        # Persisted grid centre price
├── regime_state.json      # Persists below_tl_count for TREND_DOWN hysteresis
├── breakout_state.json    # Persists breakout state across cycles
├── engine_log.jsonl       # Append-only structured log
└── logging_enabled.flag   # Presence = logging on
```

---

## Engine Logic (engine.py)

### Key Constants
```python
DRY_RUN = False           # True = simulation only, False = real bot actions
MAX_ACTIONS_PER_HOUR = 3  # Rate limit on bot start/stop calls
MAX_BTC = 0.80            # Hard stop — all bots off above this
MIN_BTC = 0.20            # Hard stop — all bots off below this
```

### Cycle (every 5 minutes, guarded by 240s minimum gap)
1. Fetch BTC/USDC 1H OHLCV from Coinbase → add ATR + BB indicators
2. Read active trendline from `trendlines.json` (drawn in dashboard)
3. Get grid centre from `grid_state.json`
4. Detect regime (`RANGE / TREND_UP / TREND_DOWN / COMPRESSION`)
5. Calculate trend_strength (gap_ratio, trending_up/down flags)
6. Fetch inventory (btc_ratio, skew) from 3Commas or manual override
7. Calculate three-tier grid parameters
8. Check breakout detection → asymmetric response
9. Check drift → redeploy if price moved >75% of grid width from centre
10. Check inventory protection (SELL_ONLY / BUY_ONLY)
11. Apply tiered bot decisions
12. Write status + log entry (always, even on early return)

### Tiered Bot Decision Table
```
Regime/State     │ inner │  mid  │ outer
─────────────────┼───────┼───────┼──────
RANGE            │  ON   │  ON   │  ON
TREND_UP         │  ON   │  ON   │  ON
trending_up      │  OFF  │  ON   │  ON
TREND_DOWN       │  OFF  │  OFF  │  ON
trending_down    │  OFF  │  OFF  │  ON
COMPRESSION      │  OFF  │  OFF  │  OFF
BREAKOUT_UP      │  OFF  │  OFF  │  ON   (outer rides the trend)
BREAKOUT_DOWN    │  OFF  │  OFF  │  OFF
```

---

## Three-Tier Grid System (grid_logic.py)

```python
TIERS = [
    {"name": "inner", "range_mult": 0.75, "base_levels": 10},
    {"name": "mid",   "range_mult": 1.5,  "base_levels": 6},
    {"name": "outer", "range_mult": 2.0,  "base_levels": 6},   # tightened from 3.0 Mar 25
]
TAKER_FEE = 0.0020
ROUND_TRIP_FEE = 0.0040
FEE_BUFFER = 1.5   # step must be 1.5× break-even minimum
```

Grid centre is persisted in `grid_state.json`. It only updates on a confirmed drift event (price >75% of grid_width from centre). **Never recentre every cycle — that was a critical bug.**

---

## Breakout Detection (breakout.py)

The original ATR > 1.8× detector was replaced after the $3,392 breakout (Mar 12-13) had ATR peak at only 1.26× — completely invisible to it.

### Detection Layers (priority order)
1. **Momentum** (primary): 4 consecutive closes in same direction + total move > ATR×1.0
2. **Volatility spike** (secondary): ATR > 1.7× rolling avg OR BB width > 2× avg

### Returns
- `"UP"` — outer runs, inner+mid off, await exhaustion then redeploy
- `"DOWN"` — all bots off
- `None` — no breakout

### Exhaustion Detection
`breakout_exhausting(df)` — 5-candle avg move < ATR×0.05. Triggers redeploy at new price level, clears breakout state.

**Common issue:** Stale breakout state from previous session blocks bot starts. Fix: `rm -f breakout_state.json`

---

## Inventory System (inventory.py)

```python
TARGET_BTC  = 0.65   # ideal BTC allocation
LOWER_BAND  = 0.55   # below here: grid tilts to buy
UPPER_BAND  = 0.72   # above here: grid tilts to sell
TAPER_ZONE  = 0.03   # soft ramp width at band edges
```

Hard stops: `MIN_BTC = 0.20`, `MAX_BTC = 0.80`
Fetches live balances via `POST /ver1/accounts/{ACCOUNT_ID}/pie_chart_data`.
**GBP bots must NOT be in `GRID_BOT_IDS`** — only USDC/USDT/USD bots are engine-managed.
Manual override via `inventory_override.json` or dashboard sliders.

---

## Regime Detection (regime.py)

| Regime | Condition |
|--------|-----------|
| TREND_DOWN | price < trendline − ATR×0.15 for 2 consecutive cycles |
| COMPRESSION | last 3 BB_width candles below 10th percentile AND ATR < mean |
| TREND_UP | price > trendline×1.03 AND BB expanding AND ATR expanding |
| RANGE | default |

Trend strength thresholds:
- `trending_up`: gap_ratio > 3.0 (price > trendline + 3×ATR)
- `trending_down`: gap_ratio < -1.5 (price < trendline − 1.5×ATR)

---

## Dashboard (dashboard.html + dashboard_server.py)

Flask server on port 5050, `host="0.0.0.0"`. Serves `dashboard.html` at `/`.

### Key Endpoints
```
GET  /status                    — engine status JSON (polled every 60s)
GET  /candles?tf=1h&limit=150   — OHLCV from Coinbase
GET  /bots                      — all bot configs from 3Commas
POST /bots/<id>/start|stop      — start/stop individual bot
GET|POST /inventory/mode        — get/set inventory mode
POST /inventory/override        — set manual btc_ratio/skew
DELETE /inventory/override      — clear manual override
GET  /engine/status             — engine process status
POST /engine/start|stop         — start/stop engine subprocess
GET  /log/entries?limit=N       — last N log entries
POST /log/clear                 — clear log
GET  /log/download              — download full log as JSON
POST /trendlines/save           — save drawn trendlines
GET  /trendlines/load           — load trendlines
```

### Dashboard Features
- Live candlestick chart (Lightweight Charts) with grid tier overlays
- Trendline drawing tool
- Controls tab: engine start/stop, bot start/stop, inventory override, trendline management
- P&L tab: simulator with timeframe selector, capital inputs per tier, APY calculation
- DRY RUN MODE toggle (bottom right)

### This Git Repo (btc-dashboard)
The files `btc_macro_dashboard.html` and `btc_macro_dashboard_mobile.html` are a **macro analysis dashboard** (React + Babel, CDN-based) that fetches from CoinGecko, Binance, Alternative.me, Yahoo Finance, FRED etc. They are **separate** from the Flask `dashboard.html` on the droplet. The Flask dashboard is the live trading terminal.

The macro dashboard will be served as static files by the Flask server and linked from the engine dashboard. Key signals to embed in the engine dashboard top bar (see Pending Work):
- Fear & Greed Index score + label
- Macro bias badge (BULLISH/BEARISH/NEUTRAL)
- Weekly RSI (14) value
- Binance funding rate (annualised)
- Liquidity risk score + regime (GREEN/AMBER/RED)

---

## Known Issues & Fixes Applied

| Issue | Fix |
|-------|-----|
| Centre drift bug — grid recentred every cycle | `grid_state.json` only updates on proper drift events |
| Double-cycle bug — engine running twice per tick | Cycle guard raised from 60s to 240s |
| trendline_gap logging 0 when no trendline set | `_trendline_active` flag, logs `None` when no real trendline |
| 3Commas API 204 on all endpoints | Base URL must be `https://api.3commas.io/public/api` |
| RSA PEM MalformedFraming on server | `sed -i 's/\r//' 3commas_private.pem` |
| Breakout state persisting across restarts | `rm -f breakout_state.json` before restarting |
| IP whitelist blocking API calls | Use Self-generated RSA key type (not System-generated) |
| ~~Controls tab inventory bar labels swapped~~ | Fixed |
| 3Commas "Realized P&L" inflated on pumps | Relabelled "Bot P&L †" with footnote — includes unrealised BTC appreciation. Daily profit bars flag suspect values amber. |

---

## Pending Work (as of Mar 25 2026)

### Security (immediate)
1. **Wrap port 5050 in nginx HTTPS** — dashboard token currently travels in plaintext and appears in server logs as query string. Same nginx/Let's Encrypt setup as the AI OS API.

### Engine
2. **Support level timeout** — fired targets should auto-clear after 2h if price doesn't recover. Agreed but not built.
3. **Loosen `trending_down` threshold** — `gap_ratio < -1.5` → `gap_ratio < -2.0` in `regime.py`. Fires too aggressively on normal pullbacks.
4. **Faster compression exit** — single large 5m candle body trigger (>0.4×ATR), volume spike trigger (>2.5× rolling mean), lower existing thresholds.

### Dashboard / reporting
5. **P&L chart lines invisible** — noted in earlier session, not confirmed fixed.

### Next engine update (gather data first — do NOT deploy yet)
8. **Loosen `trending_down` threshold** — change `gap_ratio < -1.5` to `gap_ratio < -2.0` in `regime.py:93`. The -1.5×ATR threshold fires too aggressively on normal pullbacks when the trendline is slightly optimistic. The TREND_DOWN hysteresis (2-cycle, ATR×0.15) already guards real downtrends; -2.0 avoids shutting inner off unnecessarily. Monitor current session logs before deploying.

9. **Faster compression exit — three improvements to `compression_exit_fast()` in `regime.py:98`:**
   Engine missed a post-compression breakout (Mar 15 ~01:00) because the move resolved in 2–3 candles before the 30-min accumulation check satisfied. Three fixes:

   **(a) Single large-candle body trigger** ← highest priority
   If a single 5m candle body > 0.4× 1H ATR, exit compression immediately. Fires within one engine cycle.
   ```python
   candle_body = abs(df_5m["close"].iloc[-1] - df_5m["open"].iloc[-1])
   if atr_1h and candle_body > atr_1h * 0.4:
       return True
   ```

   **(b) Volume spike trigger**
   If 5m volume on last candle > 2.5× rolling 20-period mean, exit compression.
   ```python
   vol_mean = df_5m["volume"].rolling(20).mean().iloc[-1]
   if df_5m["volume"].iloc[-1] > vol_mean * 2.5:
       return True
   ```

   **(c) Lower existing rolling thresholds**
   `atr_5m > atr_5m_mean * 1.5` → `1.3×` and `move_30m > atr_1h * 0.5` → `0.35×`
   Deploy (a)+(b) first; add (c) if still too slow after monitoring.

### Macro dashboard integration (next deploy cycle)
9. **Deploy macro dashboards to droplet** — copy `btc_macro_dashboard.html` and `btc_macro_dashboard_mobile.html` to `/root/grid-engine/`. Add Flask routes in `dashboard_server.py`:
   ```python
   @app.route('/macro')
   def macro_desktop(): return send_from_directory('.', 'btc_macro_dashboard.html')
   @app.route('/macro/mobile')
   def macro_mobile(): return send_from_directory('.', 'btc_macro_dashboard_mobile.html')
   ```
10. **Link macro dashboard from engine** — add a nav link/button in `dashboard.html` header pointing to `/macro` (desktop) and `/macro/mobile`.
11. **Macro indicator strip on engine dashboard** — add a compact top bar to `dashboard.html` that fetches the same APIs as the macro dashboard and displays 5 read-only pills:
    - **Fear & Greed** — score + colour-coded label (from Alternative.me)
    - **Macro Bias** — composite BULLISH/BEARISH/NEUTRAL badge
    - **Weekly RSI** — numeric value + overbought/oversold colour
    - **Funding Rate** — Binance perpetual, annualised %
    - **Liquidity** — GREEN/AMBER/RED regime badge (DXY+VIX composite)

    These are purely informational overlays — they do not affect engine logic. Fetch client-side via the same CORS-proxy pattern used in the macro dashboard.

### Mobile dashboard (next deploy cycle)
12. **Phase 1 — Responsive engine dashboard + PWA install**
    - Add responsive CSS breakpoints to `dashboard.html` so it works on small screens (use `btc_macro_dashboard_mobile.html` as layout reference)
    - Add `manifest.json` + mobile meta tags so it installs on iPhone/Android home screen, launches full-screen
    - Lightweight Charts has native touch/pinch-zoom support — test and enable
    - Redesign controls panel (bot start/stop, inventory sliders) for tap targets

13. **Phase 2 — Push notifications from engine**
    - Add `web-push` library to droplet (`pip install pywebpush`)
    - New Flask endpoint `POST /push/subscribe` — stores browser push subscription
    - Engine emits push notification on key events: BREAKOUT detected, hard stop triggered, engine crash, hourly cycle summary
    - Client-side: register service worker in `dashboard.html`, prompt for notification permission on load

### Backtesting (standalone script — future cycle)
14. **`backtest.py` — 30-day strategy replay**
    - Fetch 30 days of 1H OHLCV from Coinbase via ccxt
    - Replay `detect_regime`, `trend_strength`, `detect_breakout`, `grid_logic`, bot ON/OFF decisions per candle
    - Substitute hand-drawn trendline with 50-period rolling linear regression
    - Approximate grid P&L per candle: `ATR × grid_step_count × fee_savings` when bot is ON
    - Output: % time each bot ON, estimated total P&L, regime breakdown, breakout false positive rate, comparison vs always-on baseline
    - Key limitation: actual 3Commas fills are intra-candle; treat output as directional signal not precise P&L

### Grid positioning improvements (for consideration)
18. **Grid tilt on redeploy** — when `trending_up` is active, shift the inner grid asymmetrically toward the trend at the next drift-triggered redeploy. The `tilt` parameter exists in `grid_logic.py` but is always `0.0`. A tilt of 0.10–0.15 would place more levels above current price in an uptrend, reducing near-miss fills at the upper boundary. Low risk — only activates on proper drift events, not reactively. Implement by passing `tilt = 0.12 if trending_up else (-0.12 if trending_down else 0.0)` from `engine.py` into `grid_logic.calculate_tiers()`.

19. **Tighten drift threshold for inner bot** — currently 75% of grid width (~$964 on inner). During slow grinds, inner can spend hours with price stuck near its upper edge getting zero fills. Reducing to 60% (~$771) would recentre more aggressively and increase fill frequency. Downside: more stop/restart cycles. Gather data on how often inner hits 60–75% drift before deciding.

20. **True P&L tracking (balance-based)** — see note below. 3Commas bot P&L is unreliable across stop/start cycles. The only accurate measure is portfolio value snapshots: `(BTC_qty × price) + USDC_qty` at fixed intervals. Implement as a lightweight logger in `engine.py` that appends `{ts, btc_qty, usdc_qty, price, portfolio_usd}` to `portfolio_log.jsonl` each cycle. Dashboard P&L tab should show this curve, not 3Commas bot P&L.

### Liquidity monitoring (future cycle — after backtesting data gathered)
15. **Micro-liquidity: bid-ask spread guard**
    - `ccxt.fetch_order_book('BTC/USDC', limit=5)` each cycle
    - If spread > 1.5× grid step size: pause inner bot, log `LOW_LIQUIDITY` event
    - Cheap check — same exchange, same connection already open

16. **Meso-liquidity: volume as third COMPRESSION condition**
    - Add to `detect_regime` in `regime.py`: if 24h rolling volume < 15th percentile of 30-day volume, factor into COMPRESSION confirmation
    - Prevents engine waking bots in dead markets where grid fills won't materialise even if BB/ATR look normal

17. **Macro-liquidity: exchange reserve trend (dashboard signal only)**
    - Already in macro dashboard via blockchain.info
    - Do NOT wire into automated engine logic — too slow-moving and noisy on daily fetches
    - Surface as a read-only badge in the macro indicator strip (item 11 above)

---

## Python Dependencies

```
flask flask-cors ccxt requests python-dotenv ta cryptography schedule rich pandas
```

---

## Current Status (Mar 25 2026)

- `DRY_RUN = False` — **LIVE**
- Engine running on droplet in tmux session `grid`
- Three bots: Narrow (2743885), Mid (2743889), Wider (2743888) — all BTC/USDC on Coinbase
- ASIA drift thresholds loosened to (0.70, 0.80) — was (0.65, 0.75)
- Outer bot range_mult = 2.0 (tightened from 3.0)
- UFW firewall active — ports 5050/9001/8080 restricted to Ash's home IP
- Session-aware drift: ASIA (0.70, 0.80), EUROPE (0.75, 0.85), US (0.90, 0.95)

## Server Security (Mar 25 2026)

- UFW rules: 5050, 9001, 8080 → allow from 86.137.18.160 only; 443/22 open to world
- `~/update-firewall.sh` on Mac — updates UFW if home IP changes
- `/ping` debug modes require dashboard token (previously unauthenticated)
- Webhook HMAC signature mandatory on `/deploy` (previously optional)
- `/deploy-ai-os` requires `X-Deploy-Token` header (previously unauthenticated)
- **Pending:** port 5050 still plain HTTP — nginx HTTPS wrapping not yet done
- **Side effect:** port 9001 firewalled means GitHub auto-webhooks can't reach it. Deploy manually via POST /deploy on port 5050.

---

## Quick Reference Commands

```bash
# SSH to server
ssh root@165.232.101.253

# Attach to running engine
tmux attach -t grid

# Restart engine
tmux send-keys -t grid C-c Enter
sleep 2
tmux send-keys -t grid 'cd /root/grid-engine && source venv/bin/activate && python dashboard_server.py' Enter

# Fix PEM line endings
sed -i 's/\r//' /root/grid-engine/3commas_private.pem

# Clear stale state files
rm -f /root/grid-engine/breakout_state.json
rm -f /root/grid-engine/grid_state.json
rm -f /root/grid-engine/regime_state.json

# Check engine is running
curl http://localhost:5050/status | python3 -m json.tool

# Test 3Commas API connection
cd /root/grid-engine && source venv/bin/activate && python test_connection.py

# View live engine log
tail -f /root/grid-engine/engine_log.jsonl | python3 -c "import sys,json; [print(json.dumps(json.loads(l), indent=2)) for l in sys.stdin]"

# Check/flip DRY_RUN
grep "DRY_RUN = " /root/grid-engine/engine.py
sed -i 's/DRY_RUN = True/DRY_RUN = False/' /root/grid-engine/engine.py
sed -i 's/DRY_RUN = False/DRY_RUN = True/' /root/grid-engine/engine.py
```

---

## How Claude Deploys Changes

### Normal deploy (no user action needed)

**Two-step process — Claude does both:**

1. Commit and push to `claude/grid-engine-chat-review-hEEGu`
2. POST to `/deploy` on the running Flask server:

```bash
curl -s -X POST "http://100.94.227.121:5050/deploy?token=grid-deploy-2026"
```

The `/deploy` endpoint downloads all engine files from the feature branch on GitHub
and restarts itself. Fully automated — no SSH needed.

**Why two steps?** The webhook at port 9001 updates files via rsync but its tmux
restart is unreliable (Flask runs outside tmux). The `/deploy` endpoint on port 5050
is the authoritative restart mechanism and must always be called after pushing.

**Verify it worked:**
```bash
curl -s http://165.232.101.253:5050/ping          # → {"ok":true}
curl -s "http://165.232.101.253:5050/" | grep "tabGrid"   # → Grid tab present = new version
```

---

### Self-healing system (as of Mar 15 2026)

The deploy system is now self-healing at two levels:

**1. `/deploy` endpoint fixes the webhook on every call**
Every `POST /deploy` also downloads `scripts/webhook_server.py` from the feature
branch to `/root/webhook_server.py`, kills the old webhook process, and starts the
new one. This breaks the main-branch overwrite loop permanently.

**2. Startup self-heal in dashboard_server.py**
20 seconds after Flask starts (even if started from bad webhook-deployed code),
it re-downloads `dashboard.html` and macro dashboards from the feature branch.
No restart needed — Flask reads HTML files from disk on every request.

---

### If /deploy is broken (bootstrap recovery)

Only needed if the server is completely locked out (no `/deploy` endpoint, e.g.
`main` branch webhook deployed the very old pre-/deploy server). Signs: `/ping`
returns 404, `/deploy` returns 404.

```bash
ssh root@165.232.101.253
lsof -ti:5050 | xargs kill -9
cd /root/grid-engine

branch="claude/grid-engine-chat-review-hEEGu"
base="https://raw.githubusercontent.com/ashskett/btc-dashboard/${branch}/engine"
curl -fsSL "${base}/dashboard_server.py" -o dashboard_server.py
curl -fsSL "${base}/dashboard.html"      -o dashboard.html

# If curl returns cached old file (NameError: threading not defined):
sed -i 's/import json, os, base64, time, subprocess, signal, sys, secrets$/import json, os, base64, time, subprocess, signal, sys, secrets, threading/' dashboard_server.py

# Kill anything still on port 5050 then start
lsof -ti:5050 | xargs kill -9 2>/dev/null; python dashboard_server.py
```

Then immediately call `/deploy` from Claude to pull all files and fix the webhook:
```bash
curl -s -X POST "http://100.94.227.121:5050/deploy?token=grid-deploy-2026"
```

After this, no further manual steps are needed.

---

### Root cause of the old breakage (fixed)

The webhook at port 9001 (`/root/webhook_server.py`) was an old version that read
`git rev-parse --abbrev-ref HEAD` on `/root/btc-dashboard` (checked out on `main`)
and deployed from `main`. This caused a self-reinforcing loop:
- Old webhook fires → deploys `main` → overwrites `dashboard_server.py` with old
  version (no `/deploy`, no `/ping`) → reinstalls old webhook from `main`

**Fixed by:** `/deploy` now always kills and replaces the webhook process with the
feature-branch version. The new webhook hardcodes `DEPLOY_BRANCH` and self-restarts
via `os.execv` after each deploy so the updated code takes effect immediately.
