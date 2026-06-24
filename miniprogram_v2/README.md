# Quant Agent Mini Program V2

This is the new WeChat mini-program frontend for the self-evolving trading system.

## Scope

This version replaces the old `miniprogram/` UI as the primary mini-program surface.

Core views:

1. `pages/overview` - global operating picture
2. `pages/trading` - live trading and risk state
3. `pages/learning` - rule-learning, suggestions, reviews, applications
4. `pages/factors` - factor governance view
5. `pages/ops` - scheduler, evolution, health checks

## Open in WeChat DevTools

Open this directory directly:

`C:\Users\zhu\quant_trading\miniprogram_v2`

Project config:

- `project.config.json`
- `project.private.config.json`

## Backend dependencies

This mini-program expects the current FastAPI backend to expose:

- `/api/live/*`
- `/api/v4/*`
- `/api/factor-health/latest`
- `/api/control/*`
- `/api/system/db-health`
- `/api/learning/*`

## Status

This is the clean rebuild line.
The old `miniprogram/` directory should be treated as deprecated.

Current status as of 2026-06-25:

- iOS-style bright visual rebuild is the active UI line.
- Overview / Trading / Learning / Factors / Ops are all wired to the current backend.
- Learning UI now distinguishes review summaries, actionable suggestions, lifecycle events, and application-effect tracking.
- Remaining work is mostly backend-linked semantics polish, not another structural frontend rewrite.
