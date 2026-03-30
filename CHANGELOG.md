# Grid Engine Changelog

All notable changes to the BTC grid engine are recorded here.
Every deploy must add an entry. Format: `## [date] — [summary]`

---

## [2026-03-30] — API key recovery, inventory fix, regime transition redeploy, test suite

### Incident
Bad deploy corrupted `.env` on the droplet — 3Commas API key and RSA PEM path were
overwritten. Engine lost control of all bots. Bots continued trading autonomously
for several hours while engine showed 401 errors on all API calls.

### Changes
- **engine.py**: Added `_prev_regime` tracking and regime-transition redeploy block.
  When coming out of `TREND_DOWN` or `COMPRESSION`, engine now calls `redeploy_all_bots()`
  at current price instead of `start_bot()` at stale ranges.
- **engine.py**: `trending_up` in `TREND_UP` regime now keeps all 3 bots ON
  (previously turned inner off). `TREND_UP` is a confirmed uptrend — all bots run
  for pullback fills.
- **engine.py**: Wrapped startup code in `if __name__ == "__main__":` guard so
  test imports don't fire the scheduler and run() loop.
- **engine.py**: Gap guard: 240s → 100s. Cycle: 5min → 2min.
- **grid_logic.py**: Added `redeploy_allowed()` flood-fill guard — minimum 20min
  between grid recentres. Prevents fee bleed when price oscillates at drift threshold.
- **grid_logic.py**: Replaced session-based level adjustment (ASIA+2/US-2) with
  volatility-based: `vol_ratio > 1.3 → +2 levels`, `vol_ratio < 0.75 → -2 levels`.
- **grid_logic.py**: Outer tier tightened — `range_mult 3.0→2.0`, `base_levels 6→4`.
  Recentre logic + flash guard make extreme outer levels unreachable in practice.
- **threecommas_dca.py**: Added `create_smart_trade()` and `close_smart_trade()` for
  DOWN support-failure targets (sell-side SmartTrade with TP steps and SL).
- **inventory.py**: Fixed btc_ratio calculation — now uses `usd_value` from
  `pie_chart_data` for BTC (same as USDC), avoiding asymmetry when BTC is locked
  in open bot orders.
- **dashboard_server.py**: `set_inventory_mode()` now merges with existing override
  file instead of overwriting — preserves manual btc_ratio alongside mode setting.
- **dashboard_server.py**: Added `strict_slashes=False` to `/pnl-page` route.
  Added `pnl.html` and `notify.py` to `_DEPLOY_FILES`.
- **pnl.html**: Fixed auth — all fetch() calls use `window.fetch` monkey-patch
  (injected token). No `apiFetch` helper exists in this file.
- **notify.py**: Added to repo (was only on droplet, caused CI failures).
- **tests**: Replaced `TestSessionLevelAdjustments` with volatility-based equivalents.
  Added `TestRegimeTransitionRedeploy` (4 tests). Fixed `test_high_vol_adds_two_levels`
  ATR value (2600→2700 to clear 1.3× threshold). All 189 tests passing.
- **dashboard_server.py**: Added pre-deploy backup system, `/deploy/backups` and
  `/deploy/rollback` endpoints. Auto-logs each deploy to Notion memory.

### Recovery steps taken
1. User SSH'd to droplet, ran `nano /root/grid-engine/.env` and
   `nano /root/grid-engine/3commas_private.pem` to restore correct credentials.
2. Triggered deploy to restart dashboard server and reload environment.
3. Engine resumed with correct API key. Inventory API now returning live data.

---

## [2026-03-29] — SmartTrade backend, flash guard, volatility grids, P&L page

### Changes
- **engine.py**: SmartTrade API integration for DOWN support-failure targets.
  Calls `create_smart_trade()` when a target fires in DOWN direction.
- **engine.py**: Flash move guard — stops all bots on rapid move > 1.5×ATR in one
  cycle or 5m candle. Cooldown extends if price still volatile after expiry.
- **grid_logic.py**: Flood-fill guard (`MIN_REDEPLOY_INTERVAL_SECS = 1200`).
- **pnl.html**: New P&L page at `/pnl-page`. Portfolio value curve, vs-HODL line,
  allocation donut, daily/hourly bar charts, daily breakdown table.
- **dashboard_server.py**: `/portfolio/log`, `/bots/fills`, `/bots/daily-profit`
  endpoints for P&L page data.

---

## [2026-03-25] — Tier budgets, support failure detection, session drift

### Changes
- Tier capital budgets: inner 30%, mid 20%, outer 15%, 35% reserve.
  Persisted to `tier_budgets.json`. Dashboard sliders in Grid tab.
- Support failure detection: 4-phase state machine (WATCHING→BROKEN→RETESTING→fires).
- SmartTrade dual entry (scout + retest) and multi-step TP per target.
- TREND_DOWN hysteresis (Schmitt-trigger entry/exit).
- DOWN breakout recovery — clears when price > fire_price + 1.5×ATR.
- Inner-only drift — recentres independently, suppressed near full drift threshold.
- ASIA/WKD_ASIA drift thresholds loosened to (0.70, 0.80) from (0.65, 0.75).
- Session drift: EUROPE (0.75, 0.85), US (0.90, 0.95).
- Fill persistence (`fills_log.jsonl`), fill markers on chart.
- Events log — all bot actions via `_act()` with reason strings, limit 200.
- Portfolio snapshots — `portfolio_log.jsonl` every cycle.
- Daily Grid Profit chart (stacked bar by bot) on main dashboard P&L tab.
- Suspect bar flagging — amber on bars >3× median AND >$50.
- DCA bot visibility in sidebar — full config, edit via modal.
- Mousewheel zoom disabled on all charts.

---

## [2026-03-15] — Self-healing deploy system

### Changes
- `/deploy` endpoint added to `dashboard_server.py` — downloads files from GitHub
  feature branch and restarts server. Eliminates need for SSH on every deploy.
- `/deploy` also kills and replaces webhook server with feature-branch version,
  breaking the main-branch overwrite loop.
- Startup self-heal: 20s after Flask starts, re-downloads HTML from feature branch.
- Webhook hardcodes `DEPLOY_BRANCH`, self-restarts via `os.execv` after deploy.

---

## [Earlier 2026] — Foundation

- Three-tier grid system (inner/mid/outer) with independent ATR-based ranges.
- Regime detection: RANGE / TREND_UP / TREND_DOWN / COMPRESSION.
- Breakout detection: momentum (4 consecutive closes) + volatility spike (ATR/BB).
- Inventory system: live BTC/USDC ratio from 3Commas, taper skew, hard stops.
- Dashboard: candlestick chart, trendline drawing, controls panel, DRY_RUN toggle.
- RSA authentication for 3Commas API (self-generated key — bypasses IP whitelist).
