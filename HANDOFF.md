# Agent Handoff — Grid Engine

> Updated by the last agent to work on this project. Read this before starting.

## Current State
- **Project:** grid-engine
- **Branch:** claude/grid-engine-chat-review-hEEGu
- **Last known commit:** 80c8d29
- **Active task:** None — deployed and running
- **Status:** All 3 bots live, recentred at ~$76,510

## What's Running Right Now (May 22 2026)

- BTC price: ~$76,316
- Trendline auto-activated at: $76,393 (matched after TREND_DOWN auto-clear)
- trending_down: False — all bots running
- Regime: RANGE / mild compression
- Grid centre: $76,510
- Inner (2743885): enabled, 3 active orders, $75,284–$77,254
- Mid (2743889): enabled, 5 active orders, $75,126–$77,976
- Outer (2743888): enabled, 3 active orders, $74,555–$78,465
- ATR: $362, step sizes: inner $544, mid $466, outer $1,088

## Session Summary (May 22 2026)

### Investigation — no TPs this week
- BTC dropped from ~$82k to ~$76.7k since May 14 deploy
- trending_down flag (5.5×ATR gap threshold) had been ON all week, stopping inner+mid
- Only outer bot was running on a stale grid centred at $77,992
- Outer bot's sell orders were at $78,741 — BTC peak this week was $78,092 (missed by $649)
- Capital protected: portfolio held ~$87k through the 7% BTC drop

### Logic changes committed (80c8d29)
Three improvements to drift/recentre timing (engine.py + grid_logic.py):

1. **Stabilisation requirement**: Drift must be confirmed for 3 consecutive cycles (~6 min)
   before a recentre fires. Filters single-candle spikes that would previously trigger
   an immediate full redeploy.

2. **Wider threshold during trending_down (125% vs 85%)**: When trending_down is active
   and only the outer bot is running, the drift threshold scales from 85% to 125% of
   deploy_grid_width. Prevents the outer bot from chasing price on each leg of a
   sustained drop.

3. **Longer flood guard during trending_down (45 min vs 20 min)**: Minimum time between
   recentres extended from 1200s to 2700s when in trending_down mode.

### Deploy
- Deployed 80c8d29 to droplet
- Engine restart triggered a full recentre at current price ($76,510)
- Trendline auto-activation matched a lower trendline at $76,393
- trending_down cleared → all three bots restarted on fresh grid

## Files Changed (80c8d29)
- `engine/engine.py` — drift block: stabilisation counter, trending_down threshold mult, regime-aware flood guard
- `engine/grid_logic.py` — drift_detected() threshold_mult param, redeploy_allowed() min_interval_secs param
- `HANDOFF.md` — this update

## Decisions Made
- 85% → 125% threshold multiplier during trending_down: lets outer bot hold its range
  longer during sustained drops rather than chasing price down every $1,433
- 3-cycle stabilisation: prevents single-candle triggers; 6 min is short enough to
  not materially delay legitimate recentres
- 45 min flood guard during trending_down: space out outer-only recentres
- All thresholds revert to normal (85%, 20 min) when trending_down is False

## Pending / Known Issues
- P&L page fix deferred — nav link issues and token threading still need fixing
- Port 5050 plain HTTP — token in URL
- `bots_on` field in portfolio_log.jsonl is always [] — bug, log_data doesn't populate bot_X_on keys
- The trendline at $76,393 is auto-activated. If BTC drops significantly from here,
  trending_down may re-trigger and inner+mid will stop again — correct behaviour

## What Ash Needs To Do
Nothing urgent. All bots running, grid recentred, logic improvements deployed.
Monitor for fills over the next few hours.

## Recommended Next Action (for next Claude session)
- Confirm fills are flowing on the new grid
- Revisit P&L page fix (auth token threading from main dashboard nav link)
- Consider whether `bots_on` logging bug should be fixed (cosmetic — doesn't affect trading)

---
*Last updated: claude-code, 2026-05-22*
