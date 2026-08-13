# Quant Agent Mini Program V2

> Status: active
> Last verified: 2026-08-10
> Scope: lightweight mobile status surface.

This mini-program is the lightweight mobile status surface for the trading system.
The full console runs in the personal local Tauri desktop client. The server domain below is
an API/WSS endpoint only; it is not a browser UI.

`https://www.zhuzhu666.icu`

## Scope

The mini-program intentionally keeps only:

1. `pages/login` - authentication
2. `pages/overview` - compact read-only status page

The overview page shows the essential live state only:

- WebSocket / polling status
- trading loop state
- account equity and balance
- open-position count and unrealized PnL
- realized session PnL
- circuit breaker, drawdown, and consecutive-loss status
- latest known XAU price when available

Trading operations, learning governance, factor details, charts, and operations health are handled
by the personal local Tauri desktop client. The mini-program source stays local/GitHub and is not
deployed to the backend server.

## Open in WeChat DevTools

Open this directory directly:

`C:\Users\zhu\quant_trading\miniprogram_v2`

Project config:

- `project.config.json`
- `project.private.config.json`

## Backend dependencies

This mini-program only depends on the lightweight live/auth surface:

- `/api/auth/*`
- `/api/live/account`
- `/api/live/positions`
- `/api/live/strategy-status`
- `/api/live/session-stats`
- `/api/live/loop-status`
- `/api/live/realized-pnl-series`
- `/api/risk/summary`
- `/ws/state`

## Status

- Tauri desktop renderer is the complete console.
- Mini-program is a compact mobile status card.
- No tabBar, charts, trading controls, learning pages, factor pages, or ops pages are registered.
