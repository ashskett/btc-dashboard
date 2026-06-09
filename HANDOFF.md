# Agent Handoff — Grid Engine

> Updated by the last agent to work on this project. Read this before starting.

## Current State
- **Project:** grid-engine (canonical AI OS key — NOT `gridbot`)
- **Branch:** claude/grid-engine-chat-review-hEEGu
- **Last known commit:** amplitude gate (Phase 0) — swing-vs-fee-floor in status
- **Active task:** Calibrate amplitude lean-in/lean-out thresholds from live data

## Session note — grid amplitude research + observability gate

Ash observed the grid harvests better at the range lows than the highs.
`fills_research.py` (read-only, 85 days) CONFIRMED it: realized 30-min swing
~0.57% at 58-64k vs ~0.24% at 74-82k (≈2×), grid productivity 6.3 vs 3.4 profit
round-trips/RANGE-day. KEY: mean swing is BELOW the ~0.6% fee floor in every band
— the grid lives off the minority of spikes that clear the floor (common at lows,
rare at highs). So the fix is NOT tighter spacing (fee floor blocks it) but
**scaling activity with measured swing-vs-fee-floor**, keyed off measured
amplitude (self-correcting), not price level (this sample was one 82k→59k drop).

Built `amplitude.py` (observability only): ~30-min in-memory price ring →
`status.grid_amplitude` = {swing_pct, fee_floor_pct (0.6, imported from
grid_logic), ratio, band rich/ok/thin}. ratio≥1 = swings clear the floor.
Provisional bands are placeholders TO CALIBRATE. No trading decision uses it yet.
Deployed; reads "warming" for ~15 min after a restart while the ring refills.
NEXT: watch `grid_amplitude.ratio` across a few amplitude regimes, set the
lean-in/lean-out cuts, then wire into tier activity/capital. The Monday weekly
review (`weekly_backtest_review.py`) now prints + logs the amplitude/fee-floor
distribution (median ratio, p25/p75, rich/ok/thin bands, RANGE-thin %) AND the
order-book summary (collector health + regime-segmented RANGE wall hold-rate,
reusing orderbook_report helpers) each week — both informational, file no task —
so calibration data for both research threads accrues automatically. (Latest
dry-run: RANGE hold support 57% / resistance 64% — resistance clears the 60%
Phase-2 bar, support doesn't; anchoring the upper boundary looks the more viable
half.) NOTE: this turn also deployed the regime-segmented `orderbook_report.py`
to the droplet (the earlier scp had failed during an SSH outage).
- **Status:** All 3 bots live, ~$64k BTC. Engine restarted 2026-06-08 after the
  support-failure + capital deploy — 0 tracebacks, the two stale stuck targets
  expired cleanly (no erroneous sell), `support_targets` + `liquidity` in status.

## Session Summary (2026-06-08) — SF breakdown failsafe (#2/#3) + capital reset fix

Two separate issues.

### Support-failure: no-retest breakdown failsafe + stranded expire (a654502)
The 4-phase machine only fired via RETESTING, so a clean waterfall break that
never bounced sat in BROKEN forever and never launched a SmartTrade. Two live
DOWN targets ("Key Support" 65614, "bear flag fail" 69600) were found stuck this
way (missed the retest band by $16 and $101).
- **#2 failsafe** (`_advance_support_failure`, price_targets.py): close ≥
  `breakdown_failsafe_atr` (1.5) ATR below trigger with no retest → fire, flagged
  `sf_fire_reduced` so engine.py halves size (`failsafe_size_mult` 0.5). **Only
  fires on a RECENT break** (`failsafe_max_age_h` 3h) — a stale break never dumps
  a sell into a move that already happened.
- **#3 stranded expire**: ran far (`stranded_expire_atr` 3) OR in BROKEN too long
  (`stranded_expire_h` 24h) → `active=False` + recorded reason. New
  `get_support_failure_status()` → `status.support_targets` (sf_phase visible).
- All params per-target overridable in breakout_targets.json. 9 new tests, 223
  pass. Verified live: both stale targets EXPIRED, fired nothing.

### Capital "resets to 60%" — root-caused (d47da90)
**tier_budgets.json was never modified** (95% / 38-31-26, unchanged since Apr 10);
bots currently deploy 96%. The drop was the engine's autonomous
`redeploy_all_bots` hitting a hardcoded **$60k portfolio fallback** when the
portfolio fetch fails (the known intermittent 401): 95% × $60k against a real
~$95k balance ≈ 60% deployed. **Fix:** on fetch failure, skip capital re-sizing
entirely (preserve each bot's qty_per_grid) rather than size against a fake $60k.
- NOTE: a second trap remains — the manual `/account/allocate_total` &
  `/bots/<id>/capital` controls are overwritten by the engine's next budget-based
  redeploy. Use the tier-budget sliders (tier_budgets.json), not the manual USD
  allocators, or they'll revert.
- OBSERVABILITY GAP — FIXED (commit follows): engine stdout now persisted to
  `engine_stdout.log` (timestamped, RotatingFileHandler 5MB×5=25MB cap) via
  `_drain_output` in dashboard_server.py, so history survives past the 200-line
  memory buffer. Both spawn paths covered. New `GET /engine/log?lines=N` tails it
  (token-protected). Verified live: full cycles captured with timestamps.

## Session Summary (2026-06-02) — Order-book awareness, Phase 0 (commit 1453c62)

Started making the engine order-book aware. Phase 0 = **read-only Coinbase L2
liquidity collector, zero trading decisions** (same observability-first
discipline as tier_states).

- **`engine/orderbook.py`** — once per cycle reads the Coinbase BTC/USDC book via
  the existing ccxt instance (`market_data.exchange.fetch_order_book`, limit
  1000), aggregates into $50 buckets, flags walls (bucket ≥ 4× median), and
  **tracks each wall's persistence across cycles** (persistence is the real
  signal; size alone is spoofable; state in `orderbook_state.json`, survives
  restarts). Emits a compact `status.liquidity` summary and appends a full
  record (top-6 walls/side) to `orderbook_log.jsonl`.
- **Wiring** — called in `engine.run()` right after market data, wrapped in
  try/except so a book outage can NEVER interrupt the cycle. One status key
  added (`liquidity`).
- **`engine/orderbook_report.py`** — read-only. Reports collector health and
  seeds the Phase-1 question: for each recentre, did a *persistent* wall sit
  between old centre and the price we recentred toward (the future veto
  condition)? Cross-tabbed vs dud outcome, reusing `backtest.py`. Defers
  gracefully until ~1-2 weeks of book history exist.
- **Venue reality (important):** Coinbase spot is dense near mid, thin far out
  (~±1×ATR usable even 1000 levels deep). The big round-number walls on
  multi-venue heatmaps are a cross-venue/perp phenomenon — **out of scope here**,
  that's the parked aggregate feed (Phase 2). This collector measures the
  liquidity our orders actually hit.
- Verified live: real walls detected (e.g. ~42 BTC bid at 69,750), persistence
  increments across cycles and survived the restart, 215 tests pass, 0
  tracebacks post-deploy.

**Roadmap — UPDATED 2026-06-08 after 6.5d review (see below).** Phase 1
(recentre veto) REJECTED by data. Phase 2 (range anchoring) is the active
direction but gated on regime-segmented wall hold-rate. Phase 3 (breakout +
erosion) unchanged/later.

## Session note (2026-06-08) — order-book 6.5d review, roadmap re-spec

`orderbook_report.py` rewritten: was hard-coding an optimistic "green light"
line; now prints a **data-driven verdict** and tests the **Phase-2** question
(do durable walls act as boundaries?) instead of the dead Phase-1 one.

6.5d / 4230 cycles, collector healthy (durable wall present bid 70% / ask 96%,
max persistence 251 cycles ≈ 8.4h):
- **Phase 1 (recentre veto): REJECTED.** A wall sat between old centre and the
  recentre target in only 5% of recentres (1/19, and it was productive); all 12
  duds were on a clear path → veto catches zero, blocks good recentres.
- **Phase 2 (wall-as-boundary): NOT SUPPORTED overall, but regime-suspect.**
  Combined hold-rate 59% (need ≥60%). Asymmetric: ask/resistance 63% vs
  bid/support 51% — over a 70k→62k **downtrend**, where support fails and
  resistance holds by construction. So hold-rate is **likely regime-dependent**.
- **Next step before any Phase-2 build:** regime-segment the hold-rate (add
  regime to the orderbook snapshot, or join `orderbook_log.jsonl` to
  `engine_log.jsonl` by ts) and report hold-rate per RANGE/trending_up/down.
  Build anchoring only on the side+regime with hold-rate ≥60%; consider
  asymmetric anchoring.

> AI OS NOTE: the `grid-engine`/`gridbot` project disappeared from the API's
> `/projects` store today (`tasks/upsert` → 404 "Project 'gridbot' not found"),
> though `/agent/digest` and `/memory/log` for grid-engine still work. Roadmap
> above was logged to `/memory/log` instead of the task list. Worth a look —
> the project record may need re-creating in the AI OS.

## Session Summary (2026-05-31) — Weekly backtest automation + clock-freeze fix

- **Weekly automated backtest review (commit e2c6d64).** New
  `engine/weekly_backtest_review.py` runs on the droplet via **system cron,
  Mondays 08:13 London** (crontab line 27, under `TZ=Europe/London`; cron daemon
  active). It imports the read-only `backtest.py` building blocks, computes
  recentre-payoff + fee-floor health metrics over a 14-day window, and files an
  improvement suggestion onto the **grid-engine AI OS task list** via
  `POST /projects/grid-engine/tasks/upsert` — but **only when a metric breaches
  threshold**. Deduped by deterministic task id (quiet weeks stay silent;
  resolved suggestions never reopen). Trending-state checks filter to post-gate
  data (`GATE_DEPLOYED = "2026-05-30"`) so they measure the LIVE config. Also
  logs a `/memory/log` entry each run. **Analysis only** — never changes trading
  logic, deploys, or restarts. Supports `--dry-run`. Dry-run validated on the
  droplet; local + droplet copies md5-identical.
  - First real fire: **Monday 08:13 London**. Per the dry-run it would likely
    file "trending_up recentres still mostly duds" (10 post-gate recentres,
    ~100% <2 fills) — surfaced for human review, not auto-applied.
  - **To retune:** edit the threshold constants near the top of the script, and
    bump `GATE_DEPLOYED` whenever the recentre gate constants change.
- **Clock-freeze test fix (commit bdc373c).** Closed the spawned cleanup task:
  `TestBotDecisionTable` failed every weekend because the tests never froze the
  clock, so the weekend window (Fri 21:00→Mon 07:00 UTC) hijacked bot decisions.
  Added a single `_utcnow()` source in `engine.py` for all wall-clock logic and
  patched it in the test harness to a fixed weekday. Pure time-source refactor,
  no behaviour change. **Full suite: 25 passed** (previously 6 weekend fails).
  NOT yet deployed to the droplet — test-only change, deploy opportunistically
  with the next engine change.

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
- ~~Cleanup: freeze the clock in `TestBotDecisionTable`~~ — DONE (commit bdc373c).
- Review the weekly cron's first real output (Mon 08:13 London) — check
  `/root/grid-engine/weekly_backtest_review.log` and the grid-engine task list.
- ~~Deploy the `_utcnow()` engine.py change~~ — DONE (rode along with the Phase 0
  deploy, commit 1453c62 includes it on the droplet).
- **Order-book Phase 0 is collecting.** In ~1-2 weeks run
  `venv/bin/python orderbook_report.py` on the droplet; if the recentre×wall
  coincidence supports it, build **Phase 1** (order-book recentre veto).

---
*Last updated: claude-code, 2026-06-02*
