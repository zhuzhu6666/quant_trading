# Design

## Theme

- **Type**: light
- **Mood**: "现代科技工作台 — 洁净亮色背景,精密蓝色点缀,金色作温和的品牌触感"
- **Color strategy**: Restrained (product default). Tech blue leads interaction, gold is the brand accent.

## Palette (OKLCH)

### Base
| Role | Value | Use |
|---|---|---|
| `--bg` | `oklch(0.97 0.003 250)` | page background, subtle cool tint |
| `--surface` | `oklch(1 0 0)` | cards, panels, pure white |
| `--surface-2` | `oklch(0.94 0.005 250)` | sidebar, hover states |
| `--border` | `oklch(0.88 0.005 250)` | dividers, outlines |
| `--border-light` | `oklch(0.92 0.005 250)` | lighter borders |

### Text
| Role | Value | Use |
|---|---|---|
| `--ink` | `oklch(0.12 0.01 250)` | body text, high contrast |
| `--muted` | `oklch(0.50 0.01 250)` | secondary text, labels |
| `--placeholder` | `oklch(0.65 0.01 250)` | placeholder text |

### Brand
| Role | Value | Use |
|---|---|---|
| `--primary` (gold) | `oklch(0.62 0.12 74.6)` | brand accent, active nav |
| `--primary-hover` | `oklch(0.67 0.13 74.6)` | hover state |
| `--primary-muted` | `oklch(0.62 0.12 74.6 / 0.10)` | 10% fill for pills, badges |
| `--accent` (blue) | `oklch(0.50 0.15 250)` | primary actions, links, focus |
| `--accent-hover` | `oklch(0.55 0.15 250)` | hover state |
| `--accent-muted` | `oklch(0.50 0.15 250 / 0.10)` | 10% fill |

### Semantic (trading)
| Role | Value |
|---|---|
| `--up` | `oklch(0.52 0.16 150)` — clean green |
| `--up-muted` | 10% fill |
| `--down` | `oklch(0.48 0.18 25)` — clean red |
| `--down-muted` | 10% fill |
| `--warn` | `oklch(0.68 0.14 85)` — amber |
| `--warn-muted` | 10% fill |

### Text-on-color

| Fill | Text |
|---|---|
| Gold primary (L=0.62) | white |
| Blue accent (L=0.50) | white |
| Green up (L=0.52) | white |
| Red down (L=0.48) | white |
| Amber warn (L=0.68) | dark `oklch(0.12 0.01 250)` |
| Muted fills (10%) | respective full color |

## Typography

- **Family**: system-ui sans for UI, monospace for all numbers/data
- **Scale**: rem-based, 1rem=16px

| Token | Size | Weight | Use |
|---|---|---|---|
| `text-xs` | 0.75rem (12px) | 500 | label, footnote, table header |
| `text-sm` | 0.875rem (14px) | 500 | body, nav, table cell |
| `text-base` | 1rem (16px) | 500 | default |
| `text-lg` | 1.125rem (18px) | 600 | card title, section |
| `text-xl` | 1.5rem (24px) | 600 | panel heading |
| `text-2xl` | 2rem (32px) | 600 | page title |
| `text-3xl` | 2.5rem (40px) | 600 | hero metric |

## Layout

- **Shell**: sidebar + topbar + scrollable content
- **Cards**: pure white surface, subtle shadow `0 1px 3px oklch(0 0 0 / 0.06)`
- **Dashboard grid**: 3 cols → 2 cols → 1 col responsive

## Component rules

- Cards: 8px radius, no nested cards, no side-stripe borders
- Tabular numbers in `.num` with `font-mono`
- Focus ring: 2px `--accent` outline
- Buttons: primary (blue bg / white text), secondary (border / ink text), ghost (no border)
- Navigation: active item gets `--primary` text on `--primary-muted` bg, pill shape
