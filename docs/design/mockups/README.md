# Mission Control mockups

Artboard sources for the approved "DependaPilot Mission Control" design canvas:

- `Main.dc.html` — fleet dashboard (1440px): top bar, stat strip, bulk toolbar,
  repo cards with the PR grid, an expanded score-breakdown row, and one of each
  state (safe / caution / unsafe, stale, repo error, empty repo).
- `Audit.dc.html` — audit view (1440px): compliant card, findings with config
  diff and fix-PR actions, settings-toggle remediation hint, unreachable-repo
  error state.
- `BulkPreview.dc.html` — the bulk-merge preview/confirm panel (780px).
- `canvas.json` — the canvas layout manifest (artboard positions and sizes).

Each artboard is plain static HTML and renders standalone in a browser (the
`support.js` reference is the canvas runtime and is safely absent). All PR data
is sample content shaped like the real scoring rubric.

The distilled spec an implementing agent should follow is
[`../../design-language.md`](../../design-language.md).
