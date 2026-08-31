# DependaPilot design language — "Mission Control"

The visual system for DependaPilot's web UI, distilled from the approved design canvas
("DependaPilot Mission Control"). This document is the source of truth for any agent
implementing or extending the UI: adopt the tokens and component recipes below verbatim.
The reference mockups cover the fleet dashboard, the audit view, and the bulk-action
preview panel.

## Principles

1. **A cockpit, not a brochure.** Dark, dense, data-first. The operator scans dozens of
   PRs at a glance; every pixel of color must carry meaning.
2. **Status colors are reserved.** Green / amber / red belong exclusively to safety
   buckets, CI verdicts, audit severities, and diff add/remove. Never use them
   decoratively. The one interactive accent is teal.
3. **Mono means data.** Anything machine-shaped — versions, scores, PR numbers, repo
   slugs, check codes, timestamps, diffs, column labels — is set in JetBrains Mono.
   Prose, buttons, and labels are IBM Plex Sans.
4. **Status is never color alone.** Every status pairs its color with a text label
   (and usually an icon): `SAFE` / `passing` / `HIGH`, never a bare colored dot.
5. **The rubric's rules are visible in the chrome.** Merge buttons render disabled
   whenever CI isn't green (the hard cap); bulk actions always preview before executing;
   skip reasons say *why*.
6. **Icons are inline SVG.** Stroke-based, `currentColor`, 16-unit viewBox, stroke-width
   1.6–1.9, round caps/joins. Never emoji, never icon fonts.

## Typography

Load once per page (self-hosted or Google Fonts):

```html
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap">
```

```css
--font-sans: 'IBM Plex Sans', -apple-system, 'Segoe UI', system-ui, sans-serif;
--font-mono: 'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
```

Type scale (px / weight / face):

| Role | Spec |
|---|---|
| Stat-tile hero number | 26 / 600 / mono |
| Wordmark | 15 / 700 / sans, letter-spacing −0.01em |
| Panel / dialog title | 15 / 600 / sans |
| Section heading | 14 / 600 / sans |
| Repo slug | 13.5 / 600 / mono — owner in `--ink-muted`, name in `--ink` |
| Nav tab | 13 / 500 (active 600) / sans |
| Cell primary (dep name, finding text) | 13 / 500 / sans |
| Button | 12.5 / 500 / sans (row button 11.5) |
| Secondary text, CI verdict word | 12–12.5 / 400–500 / sans |
| Mono data (versions, ages, scores) | 11.5–12.5 / 400–600 / mono |
| Diff lines | 11.5 / 400 / mono, line-height 1.75 |
| Column header / eyebrow (`.th`) | 10 / 600 / mono, UPPERCASE, letter-spacing 0.09em |
| Chip text | 10.5 / 600 / mono, letter-spacing 0.03em |

Buckets and severities are uppercase in chips (`SAFE`, `HIGH`); semver, types, and audit
states are lowercase (`patch`, `direct:dev`, `audit ok`).

## Color tokens

```css
:root {
  /* Surfaces (deep blue-charcoal, never pure black) */
  --bg:            #0B1015;  /* page ground */
  --bg-bar:        #0D131A;  /* top bar */
  --surface:       #11181F;  /* cards, panels */
  --surface-inset: #0E141B;  /* code/diff/ledger insets, list rows inside panels */
  --surface-open:  #141C25;  /* expanded/selected row highlight */
  --surface-ctl:   #161F28;  /* buttons, controls */
  --surface-code:  #1B2530;  /* inline code chips */

  /* Borders */
  --border:        #1F2B37;  /* cards, dividers */
  --border-hair:   #182230;  /* row hairlines */
  --border-strong: #2B3A4A;  /* controls, modal edge */
  --border-hover:  #3C4F63;  /* control hover */

  /* Ink */
  --ink:        #E7EDF3;
  --ink-ctl:    #C7D2DE;  /* button labels, ledger totals */
  --ink-2:      #98A6B7;  /* secondary */
  --ink-muted:  #64758A;  /* labels, hints, deemphasized */
  --ink-faint:  #3C4F63;  /* arrows, footer, near-invisible */

  /* Accent — interactive only (links, active tab, primary CTA, focus) */
  --accent:          #2DD4BF;
  --accent-hover:    #43E0CC;
  --accent-ink:      #5EEAD4;               /* accent-colored text on dark */
  --on-accent:       #062A24;               /* text on solid accent */
  --accent-bg:       rgba(45,212,191,0.10);
  --accent-border:   rgba(45,212,191,0.30);

  /* Status: safe / green (safety bucket SAFE, CI passing, diff +, compliant) */
  --safe:        #6FDFA0;
  --safe-solid:  #56D592;               /* meters, standalone icons */
  --safe-bg:     rgba(86,213,146,0.12);
  --safe-border: rgba(86,213,146,0.28);

  /* Status: caution / amber (CAUTION, CI pending, MEDIUM, stale, findings) */
  --caution:        #F2C14E;
  --caution-bg:     rgba(242,193,78,0.11);
  --caution-border: rgba(242,193,78,0.28);

  /* Status: unsafe / red (UNSAFE, CI failing, HIGH, major, errors, diff −) */
  --unsafe:        #FF8A80;
  --unsafe-bg:     rgba(255,122,112,0.11);
  --unsafe-border: rgba(255,122,112,0.30);
  --error-bg:      rgba(255,122,112,0.08);  /* error banners */
  --error-border:  rgba(255,122,112,0.25);

  /* Status: info / blue (minor bumps, LOW severity) */
  --info:        #8FBEFF;
  --info-bg:     rgba(127,181,255,0.11);
  --info-border: rgba(127,181,255,0.26);

  /* Dimension: purple (dependency type chips, diff hunk headers) */
  --dim:        #C9AFF8;
  --dim-alt:    #C4A7F7;                 /* diff @@ hunk lines */
  --dim-bg:     rgba(196,167,247,0.10);
  --dim-border: rgba(196,167,247,0.24);

  /* Neutral chips (audit off/unknown, merge-method notes) */
  --neutral:        #93A1B3;
  --neutral-bg:     rgba(147,161,179,0.10);
  --neutral-border: rgba(147,161,179,0.22);
}
```

Semantic assignments (do not improvise):

| Meaning | Token set |
|---|---|
| Safety `safe` · CI `green` · severity ok · diff add | safe |
| Safety `caution` · CI `pending` · severity `medium` · stale · "N findings" | caution |
| Safety `unsafe` · CI `failing` · severity `high` · semver `major`/`unknown` · errors · diff del | unsafe |
| Semver `minor` · severity `low` | info |
| Semver `patch` | safe |
| Dependency type (`direct:dev`, `direct:prod`, `indirect`) | dim (purple) |
| CI `no_ci` · audit off/unknown · merge-method note | neutral |
| Links, active nav, primary CTA, "closes alert" PR number highlight | accent |

## Layout & spacing

- Page: full-bleed `--bg`; content padded 28px horizontally, 22px top.
- Vertical rhythm between page-level blocks: 18px.
- Top bar: 58px tall, `--bg-bar`, 1px `--border` bottom. Contents: logo + wordmark,
  nav tabs, spacer, mono status timestamp (`synced 12 s ago`), ghost Refresh button.
- Card: `--surface`, 1px `--border`, radius 10px. Card header: 13px vertical / 20px
  horizontal padding, 1px `--border` bottom, flex with 12px gap.
- Table rows: 11px vertical / 20px horizontal padding, separated by 1px `--border-hair`.
- Modal/floating panel: radius 12px, `--border-strong`, shadow `0 16px 48px rgba(0,0,0,0.5)`.
- Radii: 12 modal · 10 card · 8 inset · 6 button (5 row button) · 4 code chip ·
  999 chip/toggle · 2 meter.
- All sibling groups use flex/grid with `gap`, never margins between siblings.

### Fleet PR table grid

```css
.prgrid {
  display: grid;
  grid-template-columns: 58px minmax(0, 1fr) 88px 128px 100px 170px 92px 232px;
  column-gap: 12px;
  align-items: center;
}
/* PR# · Dependency · Bump · Type · CI · Safety · Age · Actions */
```

Column headers use `.th` in a row of their own with a `--border` bottom.

### Stat strip

Five equal tiles: `grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px`.
Each tile is a card padded 14px/18px: `.th` label (status-colored when the tile is a
status count), 26px mono number (same color), 11.5px `--ink-muted` sub-line. Sub-lines
explain, never decorate ("blocked by CI", not an icon salad).

## Component recipes

### Chips

```css
.chip {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 2px 9px; border-radius: 999px;
  font-family: var(--font-mono); font-size: 10.5px; font-weight: 600;
  letter-spacing: 0.03em; line-height: 1.7; white-space: nowrap;
}
.chip-safe    { color: var(--safe);    background: var(--safe-bg);    border: 1px solid var(--safe-border); }
.chip-caution { color: var(--caution); background: var(--caution-bg); border: 1px solid var(--caution-border); }
.chip-unsafe  { color: var(--unsafe);  background: var(--unsafe-bg);  border: 1px solid var(--unsafe-border); }
.chip-info    { color: var(--info);    background: var(--info-bg);    border: 1px solid var(--info-border); }
.chip-type    { color: var(--dim);     background: var(--dim-bg);     border: 1px solid var(--dim-border); }
.chip-neutral { color: var(--neutral); background: var(--neutral-bg); border: 1px solid var(--neutral-border); }
```

Audit chips embed an 11px shield SVG (check inside when ok, exclamation when findings)
and link to the repo's audit anchor.

### Buttons

```css
.btn {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 5px 13px; border-radius: 6px;
  border: 1px solid var(--border-strong); background: var(--surface-ctl);
  color: var(--ink-ctl); font: 500 12.5px/1.5 var(--font-sans); white-space: nowrap;
}
.btn:hover { border-color: var(--border-hover); }
.btn-primary { background: var(--accent); border-color: var(--accent); color: var(--on-accent); font-weight: 600; }
.btn-primary:hover { background: var(--accent-hover); border-color: var(--accent-hover); }
.btn-row { padding: 3px 10px; font-size: 11.5px; border-radius: 5px; }  /* in-table */
.btn-disabled, .btn:disabled { opacity: 0.35; cursor: default; }
```

Exactly one solid-accent (`btn-primary`) action per context: the eligible Merge in a row,
"Merge all eligible" in the toolbar, "Confirm merge" in the preview, "Open fix PR" on an
audit card. Everything else is a ghost button. A disabled Merge always carries
`title="CI must be green to merge"`.

### Nav tabs

Active: `padding: 5px 14px; radius 6px; background: var(--accent-bg);
border: 1px solid var(--accent-border); color: var(--accent-ink); font-weight: 600`.
Inactive: same padding, transparent, `--ink-2`, weight 500.

### CI verdict cell

Icon + lowercase word, 12px/500, in the status color: check → `passing` (safe),
clock → `pending` (caution), X → `failing` (unsafe), neutral for `no_ci`.

### Safety cell

Bucket chip + mono score (12.5/600) + meter: 40×4px track in `--border`,
radius 2, fill width = score %, fill color = bucket color (`--safe-solid` /
`--caution` / `--unsafe`). Expandable rows add a chevron; the expanded row and its
breakdown panel sit on `--surface-open`.

### Score-breakdown ledger

Inset panel (`--surface-inset`, 1px `--border`, radius 8, padding 12px/16px, max-width
620px) under `.th` heading "Score breakdown". One line per signal:
mono signal name (118px col, `--ink-2`) · right-aligned mono delta (36px col;
positive `--safe`, negative `--unsafe`, zero `--ink-muted`) · reason in `--ink-muted`.
Total row separated by a `--border` top rule, ending in the bucket chip. Signal names
and reasons come verbatim from `scoring.py` (`base`, `ci`, `semver`, `dependency_type`,
`mergeable`, `closes_open_alert`, `stale`; "CI is green", "mergeable, clean", …).

### Version transitions

Always mono: old version in `--ink-muted`, `→` in `--ink-faint`, new version in
`--ink-ctl`. E.g. `0.115.6 → 0.116.0`.

### Diff block

`--surface-inset`, 1px `--border`, radius 8, `overflow-x: auto`, 10px vertical padding.
Lines: mono 11.5/1.75, `white-space: pre`, 14px horizontal padding. Context `--ink-2`;
adds `--safe` on `rgba(86,213,146,0.09)`; deletes `--unsafe` on `rgba(255,122,112,0.09)`;
`@@` hunks `--dim-alt`.

### Inline code (check codes, paths, package names in prose)

`--surface-code` background, 1px `--border-strong`, radius 4, padding 1px 6px,
mono 11px/600 in `--ink-ctl`. Audit check codes (`MISSING_ECOSYSTEM`, …) render this way.

### Error banner (repo unreachable, failed action)

Inside the card body: `--error-bg`, 1px `--error-border`, radius 8, padding 11px 14px,
warning-triangle SVG in `--unsafe`. Two lines: bold 12.5px `--unsafe` summary
("Could not load this repo's PRs"), then the raw mono detail in `--ink-2`
(`gh: HTTP 502 from api.github.com — retried 3×, giving up for this refresh`).
Show the real error string; never paraphrase it away.

### Empty state

One quiet line, not a hero: green check SVG + 12.5px `--ink-muted`
"No open Dependabot pull requests — all caught up."

### Bulk preview panel

Floating card (`--surface`, `--border-strong`, radius 12, shadow). Sections divided by
`--border`: title + plain-language summary ("4 PRs qualify at safe+ with green CI ·
3 skipped · nothing runs until you confirm"); "WILL MERGE · IN THIS ORDER" list of
inset rows (mono `repo#num` with accent number, dep + version, bucket chip with score);
"SKIPPED · N" list with an honest mono-adjacent reason per PR (a pending-CI skip cites
the hard cap, not the bucket); footer with `btn-primary` "Confirm merge · N PRs",
ghost Cancel, and a mono `--ink-muted` note ("runs sequentially via gh · per-PR result
reported").

### Toggle ("include caution")

26×15px pill track (`--border` fill, `--border-strong` edge) with an 11px knob;
off knob `--ink-muted`, on knob `--accent` with the track in `--accent-bg`.
Label 12px `--ink-2`.

### Logo

Teal paper-plane glyph, 20px:

```html
<svg width="20" height="20" viewBox="0 0 16 16" fill="none">
  <path d="M1.8 8.6l12.4-6-4.6 12-2.3-4.4L1.8 8.6z" fill="#2DD4BF"/>
  <path d="M7.3 10.2l6.9-7.6" stroke="#0B1015" stroke-width="0.9"/>
</svg>
```

### Footer

One mono 11px `--ink-faint` line stating the trust facts:
"every call goes through the gh CLI — your gh login is the only credential ·
merge needs green CI, always".

## Voice

- Sentence case everywhere except chips and `.th` labels.
- Copy states rules and reasons plainly: "blocked by CI", "below the safe bucket
  (check \"include caution\" to widen)", "nothing lands without you".
- Timestamps and counts are mono and terse: `synced 12 s ago`, `34d`, `4 open PRs`.
- No exclamation marks, no emoji, no marketing adjectives.

## Adoption notes for the existing templates

The current UI lives in `src/dependapilot/templates/` with per-page inline `<style>`
blocks. To adopt:

1. Move all CSS into one stylesheet at `src/dependapilot/static/style.css` (served
   alongside `static/vendor/htmx.min.js`), starting with the `:root` token block above.
   Both `index.html` and `audit.html` link it; delete the duplicated inline styles.
2. Class migration: `badge`→`chip`; `badge-bucket-safe`→`chip-safe` (etc.);
   `badge-semver-patch`→`chip-safe`, `-minor`→`chip-info`, `-major`/`-unknown`→`chip-unsafe`;
   `badge-dependency-type`→`chip-type`; `badge-ci-*` cells become icon+word CI cells;
   `badge-audit-*`→`chip-safe`/`chip-caution`/`chip-neutral`/`chip-unsafe`;
   `badge-severity-*`→`chip-unsafe`/`chip-caution`/`chip-info`/`chip-neutral`.
3. The fleet `<table>` becomes the `.prgrid` grid (or keep `<table>` and apply the same
   column widths/typography — grid is what the mockup uses). The `<details>` score
   breakdown keeps its disclosure behavior, restyled as the ledger.
4. Keep every existing htmx attribute and endpoint untouched; this is a reskin plus
   the additive elements (stat strip, age column, score meter, "closes alert" chip),
   which need small context additions where the data isn't already in the template
   context (PR age from `created_at`; fleet totals aggregated in the index view).
5. `.htmx-indicator` opacity transition stays; style the "Refreshing…" indicator as
   mono 11px `--ink-muted`.
6. The mockups are desktop-first (1440px). Below ~1100px, let the PR grid scroll
   horizontally inside the card (`overflow-x: auto`) rather than reflowing.
