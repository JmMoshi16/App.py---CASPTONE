# ECOReward Kiosk — Design System

The kiosk is a café appliance, not a dashboard. Every screen answers two questions for a person holding a
used cup: *what is happening?* and *what do I do next?* Everything else (diagnostics, history) is for staff
and lives behind staff keys.

Source of truth in code: `kiosk/ui/theme.py` (tokens), `kiosk/ui/draw.py` (primitives),
`kiosk/ui/components.py` (components), `kiosk/ui/screens.py` (screens). If this document and the code
disagree, the code wins and this document is out of date.

## 1. Principles

1. **One job per screen.** The right-hand panel always shows the current stage and one next step.
2. **Honest states.** Only stages the configured hardware performs are shown. Simulation is labelled on
   every frame. The kiosk says a code was *claimed*, never that points were *credited* (the app does that).
3. **Readable from a step away.** Body text ≥ 18 px, nothing below 13 px, contrast ≥ 4.5:1 for text.
4. **Colour is never the only signal.** Categories and states always pair colour with an icon and a word.
5. **Calm motion.** Motion explains a change of state; it never delays one. A reduced-motion switch removes it.

## 2. Layout

Designed on a **1280 × 720 grid** and scaled by `s = min(W / 1280, H / 720)` to the real display
(1280×720 laptop window, 1024×600 7" kiosk panel, 1920×1080 monitor). Extra width goes to the camera.

```
┌──────────────────────────── top bar · 64 ─────────────────────────────┐
│ ◖leaf◗ ECOReward            [● Online] [● Sorter] [● Camera]   10:42 AM │
├──────────────────────────────────────┬──────────────────────────────────┤
│                                      │  Stage panel (40 % of width,     │
│   Camera viewport                    │  min 420·s)                      │
│   radius 28, live, detection box     │  headline · body · primary card  │
│   + caption chip                     │  stepper · actions               │
│                                      │                                  │
└────────────── margins 24 · gutter 24 ┴──────────────────────────────────┘
```

Full-screen moments (success, failure, receipt, history, diagnostics) use a centred card over a scrim.

## 3. Colour tokens

| Token | Hex | Use | Contrast |
|---|---|---|---|
| `canvas` | `#F7F3EC` | app background (warm cream) | — |
| `surface` | `#FFFFFF` | cards | — |
| `sunken` | `#EFE9DF` | tracks, wells | — |
| `border` | `#E4DCD0` | 1 px card borders, dividers | — |
| `ink` | `#211A17` | primary text (espresso) | 15.5 on canvas |
| `ink_2` | `#6B645E` | secondary text | 5.26 on canvas |
| `ink_3` | `#77716B` | tertiary text — only ≥ 18 px or on white | 4.82 on white |
| `coffee` | `#795548` | brand accent, wordmark | 5.92 on canvas |
| `forest` | `#247A52` | primary actions, success, Biodegradable | 5.28 on white |
| `forest_dark` | `#1B5E3F` | pressed primary; text on `sage` (forest on sage is only 4.21) | 6.16 on sage |
| `sage` | `#DCE9DF` | success / Biodegradable tint | ink 13.7 on it |
| `blue` | `#2B6CB0` | Recyclable | 5.42 on white |
| `blue_tint` | `#DDE8F4` | Recyclable tint | — |
| `blue_ink` | `#1F5A94` | Recyclable text on its tint | 5.74 on blue_tint |
| `charcoal` | `#4A4440` | Residual | white 9.58 on it |
| `charcoal_tint` | `#E7E3DF` | Residual tint | — |
| `amber` | `#D99A26` | warning **fill/icon only** (2.2 on canvas as text) | ink 7.0 on it |
| `amber_ink` | `#8A5A0B` | warning text | 5.35 on canvas |
| `amber_tint` | `#F8ECD3` | warning surface | — |
| `error` | `#C74440` | error fill/icon | white 4.85 on it |
| `error_ink` | `#A8352F` | error text | — |
| `error_tint` | `#F6DEDC` | error surface | — |
| `scrim` | `#211A17` @ 55 % | behind modals | — |

**Waste categories follow the physical bin colours** used in Philippine segregation (green / blue / black),
so the screen matches the chute the item drops into:

| Category | Colour | Tint | Icon | Base points |
|---|---|---|---|---|
| Biodegradable | `forest` | `sage` | leaf | from `config/rules.json` |
| Recyclable | `blue` | `blue_tint` | recycle arrows | from `config/rules.json` |
| Residual | `charcoal` | `charcoal_tint` | bin | from `config/rules.json` |

Points are always labelled **base points**; streak bonuses are applied by the app.

## 4. Typography

Fonts: **DM Sans** (UI) and **Fraunces** (display), the same OFL fonts the ECOReward app ships
(`assets/fonts/`, licences included). Text is rendered with Pillow, so it looks the same on OpenCV 4 and 5;
if the fonts are missing the kiosk falls back to Segoe UI / Arial, then to OpenCV's own font.

| Role | Font | Size (720p) | Use |
|---|---|---|---|
| `display` | Fraunces 700 | 52 | success headline, points earned |
| `title` | Fraunces 600 | 34 | stage headline |
| `heading` | DM Sans 700 | 24 | card titles, category name |
| `body_lg` | DM Sans 500 | 20 | primary instruction |
| `body` | DM Sans 400 | 18 | supporting text |
| `label` | DM Sans 600 | 15 | small uppercase overlines, chips |
| `caption` | DM Sans 400 | 15 | timestamps, footnotes |
| `code` | DM Sans 700 | 44 | claim code `ECO-4821` |

Minimum rendered size 13 px after scaling. Line height 1.25 (display 1.1).

## 5. Space, radius, elevation

- **Spacing** (720p units): 4 · 8 · 12 · 16 · 24 · 32 · 48. Card padding 24; gutter 24; margin 24.
- **Radius:** chip 999 (pill) · control 14 · card 20 · viewport 28.
- **Border:** 1 px `border` on cards; 2 px category colour on the active detection box.
- **Elevation:** one card shadow (y 6, blur 18, ink @ 10 %) and one modal shadow (y 14, blur 36, ink @ 18 %).
  No glows, no gradients on surfaces.

## 6. Components

| Component | Anatomy / rules |
|---|---|
| **TopBar** | wordmark (leaf mark + "ECO" forest, "Reward" ink), status chips, local time. Height 64. |
| **StatusChip** | dot + word, pill; states `ok` (forest), `busy` (amber, pulsing dot), `error` (error), `off` (ink_3), `sim` (amber outline + "SIMULATION"). Never shows "connected" until a real connection exists. |
| **Card** | surface, radius 20, border, card shadow, padding 24. |
| **Button** | min height 56, radius 14, label `body_lg` 600. Primary = forest/white; secondary = white/ink with border; ghost = text only. Pressed = darker fill for 120 ms. Every button has a keyboard equivalent for staff. |
| **CategoryBadge** | tinted pill, icon + category word in the category colour; large variant 64 px icon tile. |
| **Stepper** | vertical list of the stages the sorter mode supports: done (check, forest), active (spinner, ink, bold), pending (hollow circle, ink_3). |
| **ProgressBar / Meter** | 8 px track `sunken`, fill category or forest, rounded ends; label + value in text. |
| **Spinner** | 270° arc, 3 px, 1.2 s per turn; static "…" in reduced motion. |
| **CountdownRing** | ring 6 px; forest → amber (< 5 min) → error (< 1 min); time as text inside. |
| **QRCard** | white card, QR with 4-module quiet zone, ≥ 300 px at 720p, never scaled with interpolation. |
| **Banner** | full-width strip under the top bar: info/warn/error tint + icon + one sentence. |
| **Modal** | scrim + centred card; close button 56 px; Esc closes. |
| **SimulationBadge** | amber outline chip in the top bar + caption on the camera: "Simulated sorter — no hardware". |

Icons are drawn in code (`kiosk/ui/icons.py`), 2 px strokes, rounded caps, on a 24-unit grid: leaf, recycle,
bin, check, cross, alert, wifi, plug, camera, clock, qr, cup, receipt, list.

## 7. Motion

| Token | Duration | Use |
|---|---|---|
| `fast` | 160 ms | button press, chip change |
| `base` | 240 ms | stage panel cross-fade |
| `slow` | 420 ms | QR reveal (fade + 0.96→1 scale), check-mark draw |
| `count` | 600 ms | points count-up on success |

Easing: ease-out cubic. Loops (spinner 1.2 s, connecting pulse 1.6 s) run only while that state lasts.
All motion is computed from `time.monotonic()` inside the frame — nothing sleeps, nothing blocks.
`"reduce_motion": true` in `config/kiosk_settings.json` (or `--reduce-motion`) sets every duration to 0.

## 8. Interaction

- Customers never need a key. Touch targets ≥ 56 px; taps are handled through the OpenCV mouse callback.
- Staff keys: **SPACE** confirm deposit (attendant mode), **R** receipt, **H** history, **D** diagnostics,
  **Esc** close overlay, **Q** quit.
- Every action gets feedback within one frame (pressed state, toast or stage change).

## 9. Copy

Second person, plain verbs, one idea per sentence. No internal names on customer screens (Firebase,
credential, README, token). Times in local 12-hour format. Errors say what happened, whether the item was
kept, and what to do next ("Your item was sorted. We couldn't create a reward code because the kiosk is
offline. Please ask staff.").
