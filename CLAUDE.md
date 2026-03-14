# CLAUDE.md — BTC Grid Engine Dashboard Project

## Project Overview
This repository contains the web dashboard for a BTC grid trading engine hosted on DigitalOcean.

## Infrastructure
- **Droplet:** `grid-engine` at `165.232.101.253` (DigitalOcean, lon1, Ubuntu 24.04)
- **DO Account:** ashskett@gmail.com
- **DO API Token:** stored in doctl config (authenticated)
- **Web root:** `/root/grid-engine/dashboard/` (to be confirmed)
- **Engine root:** `/root/grid-engine/`

## Repository Structure
```
btc-dashboard/                    # This repo (github via local proxy)
├── CLAUDE.md                     # This file - project memory
├── btc_dashboard.html            # Main desktop dashboard (React + Babel, CDN)
├── btc_macro_dashboard_mobile.html  # Mobile version
├── btc_macro_dashboard (2).html  # Alternate/backup version
└── scripts/
    └── droplet-setup.sh          # One-time setup script for droplet
```

## Droplet Layout (from screenshot)
```
/root/grid-engine/
├── engine.py                 # Main trading engine
├── engine.py.bak
├── grid_logic.py             # Grid trading logic
├── market_data.py            # Market data fetching
├── indicators.py             # Technical indicators
├── regime.py                 # Market regime detection
├── breakout.py               # Breakout detection
├── liquidity.py              # Liquidity analysis
├── inventory.py              # Inventory management
├── session.py                # Trading session logic
├── status.py                 # Status reporting
├── config.py                 # Configuration
├── dashboard/                # Dashboard directory
├── dashboard.html            # Dashboard HTML
├── dashboard.py              # Dashboard Python
├── dashboard_server.py       # Dashboard server
├── 'dashboard_server copy.py'
├── bot.log                   # Bot logs
├── engine.log                # Engine logs
├── engine_log.jsonl          # Structured engine logs
├── engine_state.py           # Engine state management
├── engine_status.json        # Current engine status
├── grid_state.json           # Grid state
├── breakout_state.json       # Breakout detector state
├── inventory_override.json   # Manual inventory overrides
├── regime_state.json         # Regime detector state
├── trendlines.json           # Trendline data
├── 3commas_private.pem       # 3commas API key (private)
├── 3commas_public.pem        # 3commas API key (public)
├── requirements.txt          # Python dependencies
├── venv/                     # Python virtual environment
├── run.sh                    # Run script
├── start.sh                  # Start script
└── deploy.sh                 # Deploy script
```

## Git Workflow (How Claude Deploys)
1. Claude edits files in this repo and commits + pushes
2. Droplet webhook auto-pulls and restarts services

## SSH Access
- Public key added to droplet: `ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOz0Hy8wY5c/3WHMdxFB3s7g8TkOiNdgPHkp2cllNiR7 claude-code@btc-dashboard`
- Private key stored at: `/root/.ssh/id_ed25519` (in this Claude Code environment)
- NOTE: Direct SSH from this environment may be sandboxed — use git-pull deploy as primary method

## APIs Used by Dashboard
- CoinGecko API — BTC price & market data
- Binance API — BTCUSDT futures (funding rates, OI, candles)
- Alternative.me API — Fear & Greed Index
- CoinGlass API — ETF flows
- Glassnode API — Exchange reserves
- allorigins.win — CORS proxy fallback

## 3Commas Integration
- Keys stored on droplet: `3commas_private.pem`, `3commas_public.pem`
- Used by the Python trading engine (not the HTML dashboard)

## Development Commands
```bash
# Authenticate DO CLI
doctl auth init --access-token <token>

# Check droplet status
doctl compute droplet list

# View droplet logs (if configured)
doctl compute droplet-action list <droplet-id>
```

## TODO / Pending Context
- [ ] Get contents of config.py, engine.py, grid_logic.py, run.sh, start.sh, deploy.sh
- [ ] Understand current dashboard server setup (port, service management)
- [ ] Review user's .md context files from previous Claude sessions
- [ ] Set up auto-deploy webhook on droplet
