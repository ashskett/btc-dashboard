# Agent Handoff — Grid Engine

> Updated by the last agent to work on this project. Read this before starting.

## Current State
- **Project:** grid-engine
- **Branch:** claude/grid-engine-chat-review-hEEGu
- **Last known commit:** cdd4a1d
- **Active task:** Security hardening: tokens rotated and sensitive endpoints locked down
- **Task owner:** codex
- **Status:** needs-review

## Completed This Session
- Removed exposed GitHub PAT from local `origin` remote and switched repo to SSH.
- Confirmed GitHub SSH authentication works for `ashskett`.
- Sanitised committed docs so dashboard/deploy tokens are referenced by env var only.
- Removed hardcoded deploy-token fallback from `engine/dashboard_server.py`.
- Rotated live droplet `DEPLOY_TOKEN` and `DASHBOARD_SECRET` in `/root/grid-engine/.env`.
- Restarted `grid-engine.service`; `/ping` is healthy.
- Confirmed old deploy token now returns 403 and old dashboard token now returns 401.
- Removed `/account/balance/raw` and `/notifications` from the public dashboard allowlist so they require dashboard auth.

## Files Changed
- `CLAUDE.md` — replaced concrete live tokens with `$GRID_DEPLOY_TOKEN` and `$GRID_DASHBOARD_TOKEN`.
- `engine/dashboard_server.py` — deploy endpoint now requires `DEPLOY_TOKEN` to be configured, no leaked fallback; sensitive account/notification endpoints no longer public.
- `HANDOFF.md` — updated current session handoff.

## Decisions Made
- Do not print newly generated live tokens into chat.
- Keep deploy/dashboard token values out of repo docs and code.
- Use SSH for GitHub instead of embedding PATs in Git remotes.
- Keep `/deploy` public at the global allowlist level because it has its own deploy-token check; require dashboard auth for account/debug/status context routes.

## Tests / Checks
- `ssh -T git@github.com` authenticated as `ashskett`.
- `git fetch origin` works over SSH.
- Secret scan found no remaining old deploy/dashboard token values after patch.
- AST syntax parse passed for `engine/dashboard_server.py` and `engine.py`.
- Live droplet `grid-engine.service` restarted and is active.
- `curl http://127.0.0.1:5050/ping` returned `{"ok": true}` on the droplet.
- AST syntax parse passed for the endpoint hardening change.

## Blockers
- `ASH_BRAIN_API_KEY` is not set locally, so Codex could not log this session to Ash Brain API.
- New live `DEPLOY_TOKEN` / `DASHBOARD_SECRET` are only on the droplet. Local shells need secure env vars if agents will deploy/check protected endpoints from the Mac.

## Recommended Next Action
- Commit, push, and deploy the security hardening changes after review.
- Securely copy the new live token values into Ash's local password manager or shell profile if needed.
- After deploy, verify `/account/balance/raw` and `/notifications` return 401 without token and 200 with `$GRID_DASHBOARD_TOKEN`.

---
*Last updated: codex, 2026-04-27T20:49:14Z*
