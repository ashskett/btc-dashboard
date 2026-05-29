# Agent Handoff — Grid Engine

> Updated by the last agent to work on this project. Read this before starting.

## Current State
- **Project:** grid-engine (canonical AI OS key — NOT `gridbot`)
- **Branch:** claude/grid-engine-chat-review-hEEGu
- **Last known commit:** 75cb334 (regime-aware recentre gate)
- **Active task:** None — deployed and running
- **Status:** All 3 bots live, RANGE regime, healthy coverage. Engine restarted
  2026-05-29 23:1x UTC after gate deploy — 5 clean cycles, 0 tracebacks.

## Session Summary (2026-05-30) — Regime-aware recentre gate (item 1, commit 75cb334)

Implemented the long-pending "recentre stabilisation" item, but the data
redefined the fix. Re-ran `backtest.py recenter --since 2026-05-22` (post the
80c8d29 stabilisation fix) to avoid the earlier skew from pre-fix data:

- Post-fix recentring is still twitchy — **7.8/day, 82% earn <2 fills** in the
  next hour. NOT a pre-fix artifact.
- Built a fill-aware-gate counterfactual into `backtest.py`. It showed the
  fill-aware gate **doesn't discriminate**: 82% "precision" = base dud rate;
  9/11 productive recentres followed a low-fill deployment. Wrong lever.
- Root cause is **trending**: trending_down recentres were **100% duds (21/21,
  0 fills forgone if all suppressed)**, trending_up **83% (15/18)**, RANGE 65%.

**Change (engine.py `_recentre_gate_params`, pure + unit-tested):**
| State | Before | After |
|---|---|---|
| RANGE | 0.85× / 3 cyc | unchanged |
| trending_up | 0.85× / 3 cyc | **1.10× / 6 cyc** |
| trending_down | 1.25× / 3 cyc | **2.0× safety-valve / 3 cyc** |

trending_down keeps the 45-min flood guard + full redeploy path; it just won't
fire unless drift exceeds 2× deploy width (genuine collapse). Deployed via SCP
+ `systemctl restart` (stale-cache avoidance). 4 new `TestRecentreGate` tests +
12 backtest tests pass.

> NOTE: 6 `TestBotDecisionTable` tests fail **on weekends only** (they don't
> freeze the clock, so weekend mode hijacks bot decisions). Pre-existing,
> confirmed against pristine code. Spawned a cleanup task to freeze the clock.

## How It Runs (IMPORTANT — corrected 2026-05-29)
- The engine runs under **systemd: `grid-engine.service`**, NOT tmux.
- The service starts `dashboard_server.py`, which spawns `engine.py` as a child — both via venv Python.
- Restart: `ssh root@165.232.101.253 'systemctl restart grid-engine.service && sleep 4 && systemctl is-active grid-engine.service'`
- Engine files are flat at `/root/grid-engine/*.py` (repo's `engine/` maps to droplet root).
- SSH works via the Bash tool (Mac keys). Always use it before asking Ash to SSH.

## What's Running Right Now (May 29 2026)
- BTC price: ~$73,509
- Regime: RANGE (mild compression, not confirmed → all bots on)
- gap_ratio: +2.5×ATR (price above trendline ~$72,622)
- trending_up: False, trending_down: False
- Inventory: NORMAL, BTC 41.7% (target 45%)
- Bots: Narrow 2/3 orders, Mid 5/6, Wider 3/4 — all enabled

## Session Summary (May 29 2026)

### Deployed: per-tier re-enable-condition observability (620c045)
Answers the 2026-05-29 daily-review request to define the exact market
condition that re-enables Narrow (inner) and Mid lanes, so disabled tiers no
longer rely on operator memory.

**Change (observability only — does NOT influence bot actions):**
- New `engine._compute_tier_states()` and `status.tier_states`. For each tier
  (Narrow/inner, Mid/mid, Wider/outer) it reports: `enabled`, `reason`,
  `reenable_when`, `reenable_price`.
- Re-enable logic mirrors the TIERED BOT DECISIONS table:
  - Inner/Mid re-enable when gap_ratio recovers above **-1.0×ATR**
    (price > trendline − 1×ATR) after a downside move, OR on COMPRESSION exit.
  - Inner additionally re-enables when a `trending_up` run cools to
    gap_ratio < **4.5×ATR**.
  - Outer is a permanent safety net (no re-enable condition).
- Promoted the four trend-strength Schmitt thresholds
  (`TRENDING_UP_ENTRY/EXIT`, `TRENDING_DOWN_ENTRY/EXIT`) to module-level
  constants in `regime.py` so the engine can derive resume prices.
- 5 new tests in `tests/test_engine_decisions.py` (`TestTierStates`).
  Full suite: **199 passed**.

Deployed via direct SCP (engine.py + regime.py) + `systemctl restart` to avoid
the known stale GitHub-raw-cache issue on `/deploy`. Verified `tier_states`
present in live `engine_status.json`.

## Files Changed
- `engine/engine.py` (620c045) — `_compute_tier_states()`, `tier_states` in status export, import of thresholds
- `engine/regime.py` (620c045) — Schmitt thresholds promoted to module constants
- `engine/tests/test_engine_decisions.py` (620c045) — `TestTierStates` (5 tests)
- `CLAUDE.md`, `HANDOFF.md` — systemd correction + tier_states feature

## AI OS Updates Made This Session
- Marked **5 tasks done** on `grid-engine`: both "Define re-enable condition for
  Narrow/Mid", and the three "Investigate Narrow/Mid 0/N coverage" items
  (resolved as not-bugs — snapshots from defensive TREND_DOWN cycles).
- Logged memory entry (source `claude-code`, category `Agent Event`).
- Added a project note summarising the deploy and remaining planned items.

## Backtest tool + findings (added 2026-05-29, commit 49759a8)

`engine/backtest.py` is a **read-only, non-trading** log analyser (stdlib,
streams the 76MB `engine_log.jsonl`). On the droplet: `venv/bin/python backtest.py all`.
Modes: `trend-down [--sweep]`, `recenter [--window N]`, `spacing`, `all`.
Ran over 78 days / 37,954 cycles — findings:

- **Recentring is twitchy (actionable):** 188 recentres (2.4/day), **89%
  followed by <2 fills in the next hour** (median 0), median post-recentre
  drawdown −0.25% / worst −2.39%. → add a stabilisation/hysteresis gate before
  recentre fires. *This is the recommended next change.*
- **trending_down over-pauses:** deployed −2.0/−1.0 pauses inner+mid 20.3% of
  cycles but avoids almost no downside (worst single-cycle drop −0.73%; net
  drift while paused +3.9%) and forgoes only 11 fills. Stricter −2.5/−1.5 would
  pause 18.5% / forgo 2. Replay matches logged flag 98–99% (harness validated).
- **RANGE spacing is fee-limited, not config-limited:** step $475 sits just
  above the fee floor (~$440), step/ATR 1.21×, 99% fee_ok. Tightening would
  breach the fee guard → no spacing change warranted.

Tasks `Backtest trend_down` and `Review range spacing` marked **done**;
`Measure recenter → add hysteresis` kept **planned** (measurement done,
implementation pending).

## Pending / Known Issues
- Port 5050 plain HTTP — token in URL (Tailscale-only mitigates)
- `bots_on` field in portfolio_log.jsonl always [] — cosmetic
- Intensive sell 60% compression ignores fee guard (known)
- 3Commas BTC ratio inflated during SELL_ONLY (bot-locked BTC counted)

## Recommended Next Action
- **Verify the regime-aware gate in the wild** — next time the engine enters
  `trending_down`/`trending_up`, confirm via `journalctl`/engine output that the
  drift tag shows `[2.00x trending_down safety-valve]` / `[1.10x / 6cyc
  trending_up]` and that recentres drop off. Re-run `backtest.py recenter
  --since <deploy-date>` in ~1 week to measure the actual reduction in duds.
- Consider surfacing `tier_states` on the dashboard (currently in status JSON only).
- Cleanup: freeze the clock in `TestBotDecisionTable` (fails every weekend).

---
*Last updated: claude-code, 2026-05-29*
