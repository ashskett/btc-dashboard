# Agent Handoff — Grid Engine

> Updated by the last agent to work on this project. Read this before starting.

## Current State
- **Project:** grid-engine
- **Branch:** claude/grid-engine-chat-review-hEEGu
- **Last known commit:** becf77c
- **Active task:** None — deployed and running
- **Status:** All 3 bots live, weekend mode active (Fri 21:00 → Mon 07:00 UTC)

## What's Running Right Now (May 23 2026)

- BTC price: ~$75,745
- Trendline: $76,413
- ATR: $364.50, gap_ratio: ~-1.83×ATR
- trending_down: False — all bots running
- Regime: RANGE / compression ON (weekend mode, drift suppressed)
- Grid centre: $75,791

## Session Summary (May 22–23 2026)

### Problem diagnosed and fixed: trending_down oscillation (becf77c)

After the May 22 recentre at $76,510, BTC drifted down to ~$75,640.
This put gap_ratio right on the -2.0×ATR threshold (trendline $76,413,
ATR ~$380 → threshold = $75,653). The `trending_down` flag in
`trend_strength()` was a simple `bool(gap_ratio < -2.0)` with no state
memory — it flipped True/False every 2-min cycle, starting and stopping
inner+mid bots continuously.

**Root cause:** `trending_down` had no hysteresis; `trending_up` already had
a proper Schmitt trigger (entry 5.5, exit 4.5). The `trending_down` flag
lacked the equivalent.

**Fix (engine/regime.py):**
- Added `TRENDING_DOWN_ENTRY = -2.0` and `TRENDING_DOWN_EXIT = -1.0` constants
- Schmitt trigger: once below -2.0, stays True until gap_ratio recovers above -1.0
  (price must reach trendline − 1×ATR = ~$76,049 before clearing)
- State persisted as `"trending_down_flag"` in `regime_state.json`

This is a pure state-logic fix — no threshold values changed, entry is identical
to before. The 1×ATR dead zone (entry at -2.0, exit at -1.0) eliminates chop.

### Earlier changes in this session (80c8d29 — May 22)
Three improvements to drift/recentre timing:
1. Stabilisation requirement: 3 consecutive cycles before recentre fires
2. Wider threshold during trending_down: 125% vs 85% of deploy_grid_width
3. Longer flood guard during trending_down: 45 min vs 20 min

## Files Changed
- `engine/regime.py` (becf77c) — trending_down Schmitt trigger
- `engine/engine.py` (80c8d29) — drift block improvements
- `engine/grid_logic.py` (80c8d29) — drift_detected() / redeploy_allowed() params

## Decisions Made
- Schmitt trigger hysteresis band: 1×ATR (entry -2.0, exit -1.0). Mirrors trending_up's 1×ATR band (entry 5.5, exit 4.5). Prevents oscillation without changing the entry sensitivity.
- `trending_down_flag` key chosen (not `trending_down_active`) to avoid collision with `trend_down_active` (TREND_DOWN regime flag) in the same regime_state.json file.

## Pending / Known Issues
- P&L page fix deferred — nav link issues and token threading still need fixing
- Port 5050 plain HTTP — token in URL
- `bots_on` field in portfolio_log.jsonl is always [] — cosmetic bug, doesn't affect trading

## What Ash Needs To Do
Nothing urgent. All bots running, oscillation fix deployed.
Weekend mode active until Mon 07:00 UTC.

## Recommended Next Action (for next Claude session)
- Check fills are flowing now oscillation is fixed
- Revisit P&L page fix (auth token threading from main dashboard nav link)
- Consider whether `bots_on` logging bug should be fixed (cosmetic)

---
*Last updated: claude-code, 2026-05-23*
