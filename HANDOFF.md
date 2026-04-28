# Agent Handoff — Grid Engine

> Updated by the last agent to work on this project. Read this before starting.

## Current State
- **Project:** grid-engine
- **Branch:** claude/grid-engine-chat-review-hEEGu
- **Last known commit:** 3ad5c64
- **Active task:** Diagnose TREND_DOWN stop-start churn
- **Task owner:** codex
- **Status:** local fix prepared, not deployed

## Completed This Session
- Removed exposed GitHub PAT from local `origin` remote and switched repo to SSH.
- Confirmed GitHub SSH authentication works for `ashskett`.
- Sanitised committed docs so dashboard/deploy tokens are referenced by env var only.
- Removed hardcoded deploy-token fallback from `engine/dashboard_server.py`.
- Rotated live droplet `DEPLOY_TOKEN` and `DASHBOARD_SECRET` in `/root/grid-engine/.env`.
- Restarted `grid-engine.service`; `/ping` is healthy.
- Confirmed old deploy token now returns 403 and old dashboard token now returns 401.
- Removed `/account/balance/raw` and `/notifications` from the public dashboard allowlist so they require dashboard auth.
- Committed and pushed `bd57b6b` to `claude/grid-engine-chat-review-hEEGu`.
- Deployed via `/deploy`; deploy backup created as `2026-04-27-205116`.
- Verified live endpoint auth after deploy.
- Fixed `grid_logic.get_grid_state()` so missing `grid_state.json` returns an in-memory default instead of writing a runtime state file during grid calculations/tests.
- Added `flash_move_state.json` and `redeploy_state.json` to `.gitignore`.
- Added fee-guard enforcement to intensive BUY_ONLY/SELL_ONLY tier transforms so compressed grids reduce levels until `step >= min_step`.
- Added direct tests for intensive buy/sell transforms on narrow tiers that previously would have been sub-fee-floor.
- Committed and pushed `e3f71fc`.
- Deployed pending runtime-state and intensive fee guard fixes; deploy backup created as `2026-04-27-205736`.
- Verified `grid-engine.service` is active and `/ping` returns ok after deploy.
- Corrected `CLAUDE.md` COMPRESSION decision table: outer bot remains ON.
- Added `decision_summary` and `bot_actions` to engine status/log output so each cycle explains desired bot states and why.
- Added tests proving COMPRESSION status explains "inner+mid off; outer on".
- Committed and pushed `3ad5c64`.
- Deployed observability update; deploy backup created as `2026-04-27-210407`.
- Verified live `/status` now includes `decision_summary` and list-valued `bot_actions` after the next engine cycle.
- Investigated live stop-start churn on 2026-04-28: notifications showed repeated `TREND_DOWN → RANGE → TREND_DOWN` flips around $76.7k-$76.9k while price stayed >1x ATR below the active trendline.
- Root cause: TREND_DOWN auto-clear after stabilisation returned RANGE even though price remained below the TREND_DOWN entry threshold; two cycles later the same stale trendline re-triggered TREND_DOWN and stopped inner+mid again.
- Added a `td_reentry_block` after auto-clear so re-entry is blocked until price recovers near the trendline or makes a fresh lower low.
- Added regression tests for the stop-start loop and the fresh-low re-arm case.

## Files Changed
- `CLAUDE.md` — replaced concrete live tokens with `$GRID_DEPLOY_TOKEN` and `$GRID_DASHBOARD_TOKEN`.
- `engine/dashboard_server.py` — deploy endpoint now requires `DEPLOY_TOKEN` to be configured, no leaked fallback; sensitive account/notification endpoints no longer public.
- `HANDOFF.md` — updated current session handoff.
- `engine/grid_logic.py` — removed read-side creation of `grid_state.json`.
- `.gitignore` — ignores additional runtime state files.
- `engine/engine.py` — added `_apply_intensive_fee_guard()` and applied it to `_make_intensive_buy_tiers()` / `_make_intensive_sell_tiers()`.
- `engine/tests/test_engine_decisions.py` — added intensive tier fee guard tests.
- `CLAUDE.md` — corrected COMPRESSION table and rationale.
- `engine/engine.py` — exports decision summary and per-bot desired actions.
- `engine/tests/test_engine_decisions.py` — added COMPRESSION observability assertions.
- `engine/regime.py` — blocks immediate TREND_DOWN re-entry after stabilisation auto-clear on a stale trendline.
- `engine/tests/test_regime.py` — added auto-clear re-entry regression coverage.

## Decisions Made
- Do not print newly generated live tokens into chat.
- Keep deploy/dashboard token values out of repo docs and code.
- Use SSH for GitHub instead of embedding PATs in Git remotes.
- Keep `/deploy` public at the global allowlist level because it has its own deploy-token check; require dashboard auth for account/debug/status context routes.
- Runtime state should only be written by explicit state mutation paths such as `update_grid_center()`, not by read helpers used during calculation/tests.
- Intensive protective modes should preserve profitability by reducing level count rather than keeping dense but sub-fee-floor grids.
- COMPRESSION behaviour should stay as code/tests already define it: inner and mid off, outer on.
- Observability should explain decisions in machine-readable status/log fields without changing trading behaviour.
- TREND_DOWN auto-clear should not immediately re-enter on the same stale trendline; it should wait for either recovery near the trendline or a meaningful fresh lower low.

## Tests / Checks
- `ssh -T git@github.com` authenticated as `ashskett`.
- `git fetch origin` works over SSH.
- Secret scan found no remaining old deploy/dashboard token values after patch.
- AST syntax parse passed for `engine/dashboard_server.py` and `engine.py`.
- Live droplet `grid-engine.service` restarted and is active.
- `curl http://127.0.0.1:5050/ping` returned `{"ok": true}` on the droplet.
- AST syntax parse passed for the endpoint hardening change.
- Post-deploy: `/account/balance/raw` returns 401 without token and 200 with dashboard token.
- Post-deploy: `/notifications` returns 401 without token and 200 with dashboard token.
- Full local test suite: `python3 -m pytest tests/ -q` -> 189 passed, 21 warnings.
- Focused grid tests: `python3 -m pytest tests/test_grid.py tests/test_grid_extended.py -q` -> 43 passed.
- After intensive fee guard tests: `python3 -m pytest tests/ -q` -> 191 passed, 21 warnings.
- `python3 -m pytest tests/test_engine_decisions.py -q` -> 15 passed, 21 warnings.
- Post-deploy: `systemctl is-active grid-engine.service` -> active.
- Post-deploy: `/ping` -> `{"ok": true}`.
- Post-deploy: no recent traceback/error/exception/failed lines in `grid-engine.service` journal tail.
- After observability patch: `python3 -m pytest tests/test_engine_decisions.py -q` -> 16 passed, 22 warnings.
- After observability patch: `python3 -m pytest tests/ -q` -> 192 passed, 22 warnings.
- Post-deploy: `grid-engine.service` active and `/ping` ok.
- Post-deploy: `/status` reported `decision_summary="RANGE: all bots on for normal grid trading"` and `bot_actions` as a list.
- Post-deploy: no recent traceback/error/exception/failed lines in `grid-engine.service` journal tail.
- Live diagnosis: `/status` showed `regime="TREND_DOWN"`, `decision_summary="TREND_DOWN: confirmed downside regime; inner+mid off; outer on"`, `drift_triggered=false`, `inventory_mode="NORMAL"`, price around $76,796, trendline around $77,388, ATR around $382.
- Notifications converted to UTC showed repeated flips: 02:43, 04:19, 05:39, 07:40, 08:34 `RANGE → TREND_DOWN`, with intervening `TREND_DOWN → RANGE` clears.
- `python3 -m py_compile regime.py` passed.
- `python3 -m pytest tests/test_regime.py -q` -> 19 passed.
- `python3 -m pytest tests/ -q` -> 194 passed, 22 warnings.

## Blockers
- Agent context API call returned 403 with the local `ASH_BRAIN_API_KEY`; dashboard/deploy tokens are available in `.codex-secrets/grid-engine.json`.
- TREND_DOWN churn fix is local only; not committed, pushed, or deployed yet.

## Recommended Next Action
- Review and, if accepted, commit/push/deploy the TREND_DOWN re-entry block.
- After deploy, watch `/notifications` and `/status` for at least 20-30 minutes to confirm RANGE/TREND_DOWN churn stops unless BTC makes a fresh lower low or recovers near the active trendline.

---
*Last updated: codex, 2026-04-28T09:18:00Z*
