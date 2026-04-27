# AGENTS.md — Grid Engine

Instructions for Codex and any non-Claude agent working on this project.

## Project
BTC grid trading engine. Python. Deployed on DigitalOcean droplet 165.232.101.253, port 5050.
Do not touch production directly. All changes via Git → deploy script.

## Before Starting Work

1. Read `HANDOFF.md` — understand what was last done and what's in progress.
2. Fetch live project context:
   ```
   GET https://api.uncrewedmaritime.com/agent/context?project=grid-engine&limit=20
   Authorization: Bearer <ASH_BRAIN_API_KEY>
   ```
3. Check active claims — do not edit files already claimed by another agent:
   ```
   GET https://api.uncrewedmaritime.com/agent/claims?project=grid-engine
   Authorization: Bearer <ASH_BRAIN_API_KEY>
   ```
4. Claim your task before editing:
   ```
   POST https://api.uncrewedmaritime.com/agent/claim
   { "project": "grid-engine", "agent": "codex", "task": "<short description>", "files": ["file1.py"] }
   ```

## During Work

- Log meaningful milestones:
  ```
  POST https://api.uncrewedmaritime.com/memory/log
  { "topic": "<short title>", "insight": "<what happened>", "source": "codex", "category": "Work", "project": "grid-engine" }
  ```
- Do not modify files listed in another agent's active claim.
- Do not touch the live server or 3Commas API keys.

## Before Finishing

1. Run tests if applicable.
2. Update `HANDOFF.md` — fill in completed work, files changed, decisions, next recommended action.
3. Log a final summary:
   ```
   POST https://api.uncrewedmaritime.com/memory/log
   { "topic": "session-stop", "insight": "<summary of what was done and what's next>", "source": "codex", "category": "Work", "project": "grid-engine" }
   ```
4. Release your task claim:
   ```
   POST https://api.uncrewedmaritime.com/agent/release
   { "project": "grid-engine", "agent": "codex" }
   ```

## What Not To Touch
- Live server SSH / droplet config
- 3Commas API keys or bot IDs
- The `.env` file
- Any file with an active claim from another agent
