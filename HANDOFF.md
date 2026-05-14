# Agent Handoff — Grid Engine

> Updated by the last agent to work on this project. Read this before starting.

## Current State
- **Project:** grid-engine
- **Branch:** claude/grid-engine-chat-review-hEEGu
- **Last known commit:** 913ee9c
- **Active task:** None — code ready, awaiting manual deploy by Ash
- **Task owner:** Ash
- **Status:** code fixes committed and pushed; NOT yet deployed to droplet

## What Was Found (May 14 2026 audit)

### Critical issues on live server
1. **Capital deployment was 0.7% of portfolio** — $665 deployed vs $89,914 portfolio.
   Root cause: last `redeploy_all_bots()` call estimated portfolio at ~$2,100 (bad
   portfolio_snapshot cache value). Fix: after deploy, do a manual redeploy via
   dashboard to recalculate qty_per_grid at correct portfolio value ($89k+).
   Budget percentages (38%/31%/26%) are fine — set via dashboard sliders, stay in
   `tier_budgets.json` on droplet.

2. **Inner bot being stopped in TREND_UP** — server was running older code where the
   `elif trending_up and regime not in (...)` condition fired on TREND_UP.
   Local code has always been correct (`not in ("RANGE","TREND_UP")`). Comment added
   by previous session said inner=OFF in TREND_UP — that was wrong; fixed in 913ee9c.

3. **Server was running pre-3ad5c64 code** — `decision_summary` was null in status,
   confirming server had older engine.py. Full redeploy from 913ee9c will fix this.

### Inventory settings changed
- Old live settings: target_btc=0.30, lower=0.23, upper=0.40, max=0.80, min=0.20
- New committed settings (913ee9c): target_btc=0.40, lower=0.30, upper=0.50, max=0.80, min=0.20
- `inventory_settings.json` is now in `_DEPLOY_FILES` — it will be deployed to
  the droplet on the next deploy, overwriting the stale live values.
- max_btc=0.80 intentionally wide — 3Commas counts bot-locked BTC in balance,
  inflating ratio during SELL_ONLY. 0.80 gives room before hard stop fires.

## Completed This Session (May 14 2026)
- Full codebase audit against live server state.
- Found and documented: capital near-zero, inner bot stopping wrong, stale code.
- Corrected decision table comment in engine.py (TREND_UP+trending_up → all ON).
- Updated inventory.py defaults (target 0.40, upper 0.50, taper 0.05).
- Created engine/inventory_settings.json as committed config, added to _DEPLOY_FILES.
- Committed 913ee9c and pushed to deploy branch.

## Files Changed
- `engine/engine.py` — fixed wrong decision table comment (trending_up+TREND_UP → inner ON)
- `engine/inventory.py` — updated _DEFAULT_SETTINGS and stagger layout comment
- `engine/inventory_settings.json` — new committed config file (deployed on each deploy)
- `engine/dashboard_server.py` — added inventory_settings.json to _DEPLOY_FILES
- `HANDOFF.md` — this update

## Decisions Made
- Inner bot stays ON in TREND_UP+trending_up — chop fills during uptrend outweigh risk;
  outer bot already acts as safety net; this matches the code, the test, and Ash's intent.
- Capital allocation 38%/31%/26% kept as-is (Ash confirmed).
- Inventory target set to 0.40 BTC (Ash instructed).
- inventory_settings.json is now config-as-code; future dashboard setting changes will
  be overwritten on next deploy unless the committed file is also updated first.

## Tests / Checks
- `python3 -m py_compile engine.py, inventory.py, dashboard_server.py` → all OK
- `python3 -c "json.load(open('inventory_settings.json'))"` → OK
- No trading logic changed — only a comment fix and inventory config values.

## Blockers
- Server is NOT yet deployed. Old (broken) code still running live.
- Capital deployment is still ~$665 until manual redeploy fires after deploy.

## What Ash Needs To Do
1. Trigger deploy:
   ```bash
   curl -s -X POST "http://100.94.227.121:5050/deploy?token=$GRID_DEPLOY_TOKEN"
   ```
   Deploy token is in `.codex-secrets/grid-engine.json` locally, or check droplet .env.
   The old fixed token `dbf92fff8e0baf1c856ea590d74cd640a556a037ddd12369` currently
   works (dashboard fallback in code — see commit 2aa9366).

2. After deploy, restart the engine (the deploy does this automatically via tmux).

3. **Then trigger a manual redeploy of all bots** via the dashboard Grid tab to
   recalculate qty_per_grid with the correct portfolio value (~$89k).
   Without this, the bots will start with the old tiny qty_per_grid values.

4. Verify in the dashboard:
   - `decision_summary` appears in status (confirms new engine.py is live)
   - Inner bot running in TREND_UP
   - Inventory settings show target 0.40

## Recommended Next Action (for next Claude session)
- Confirm deploy completed and inner bot is running in TREND_UP
- Confirm capital is correctly allocated after manual redeploy
- Consider whether `tier_budgets.json` should also become config-as-code
  (currently 38/31/26 is only on the droplet — another bad portfolio estimate
  during redeploy would miscalculate qty even with correct percentages)

---
*Last updated: claude-code, 2026-05-14*
