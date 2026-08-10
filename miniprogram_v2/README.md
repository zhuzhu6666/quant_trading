# Quant Agent Mini Program V2

> Status: active
> Last verified: 2026-08-10
> Scope: lightweight mobile status surface.

This mini-program is now the lightweight mobile status surface for the trading system.
The full console has moved to the Web frontend at:

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

Trading operations, learning governance, factor details, charts, and operations health are handled by the Web frontend.

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

- Web frontend is the complete console.
- Mini-program is a compact mobile status card.
- No tabBar, charts, trading controls, learning pages, factor pages, or ops pages are registered.
