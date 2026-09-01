# DependaPilot

Automated dependency update orchestration for a fleet of GitHub repositories that use
Dependabot.

DependaPilot rests on three pillars:

- **Audit** — checks every managed repo's `.github/dependabot.yml` against what its file
  tree actually needs (missing/orphaned/misconfigured ecosystems, weakened cooldowns) and
  against the two repo-level security toggles Dependabot depends on, then can open a
  reviewable fix PR for you.
- **Single pane of glass** — a live dashboard of every open Dependabot PR across the fleet,
  each scored against a deterministic merge-safety rubric (see below) so you can tell at a
  glance which updates are safe to land.
- **Actions in three ways** — approve, merge, or nudge-rebase one PR straight from the
  dashboard; or run a bulk "approve all" / "merge all eligible" pass, fleet-wide or scoped
  to one repo, with a preview step before anything actually happens.

DependaPilot never stores a GitHub token itself. It shells out to the `gh` CLI for every
call, so your existing `gh` login (keyring or `~/.config/gh/hosts.yml`) is the only
credential in play.

## Prerequisites

- **[`gh`](https://cli.github.com/)**, logged in: `gh auth login`. DependaPilot calls
  `gh auth token` under the hood and fails fast with an actionable message if `gh` is
  missing or has no active login.
- *(Optional)* **`security_events` scope**, for the "closes an open Dependabot alert"
  scoring signal: `gh auth refresh -h github.com -s security_events`. Without it,
  DependaPilot degrades gracefully — that one signal is simply omitted from every PR's
  score breakdown (no penalty, no guess) rather than raising or faking an answer. A
  classic OAuth token reports its scopes up front; a fine-grained PAT never does, so
  DependaPilot spot-checks per repo the first time it needs to know.
- **[`uv`](https://docs.astral.sh/uv/)** — dependency management and running the app.
- **[`just`](https://just.systems/)** — the task runner for setup, quality checks, tests,
  and serving.

Requires Python 3.13+ (managed for you by `uv`).

## Quickstart

```sh
git clone https://github.com/SimplicityGuy/DependaPilot.git
cd DependaPilot
just install          # uv sync --all-extras --dev
# edit repos.yml to list the repos you want to manage
just serve            # uv run dependapilot serve --reload
```

Then open `http://127.0.0.1:8000` — `/fleet` is the dashboard, `/audit` is the audit page.
`GET /healthz` returns `{"status": "ok"}` for a liveness check.

`just serve` runs with auto-reload: the server watches both the source tree and
`repos.yml` itself, so a config edit shows up on save — no manual restart. Each reload
re-reads the config and re-authenticates via `gh` inside the app's startup lifespan
(`create_live_app` in `src/dependapilot/app.py`); a bad `repos.yml` fails that startup
with a clear error, and the watcher simply retries on your next save. Use `just
serve-prod [host] [port]` to serve without reload on an explicit bind address, or `uv run
dependapilot serve --config <path>` to point at a different fleet file.

Run `just --list` to see every available recipe, grouped by purpose (`setup`, `quality`,
`test`, `server`, `ci`):

```sh
just --list
```

Highlights: `just install` (sync the dev environment), `just check` (the full validation
suite — format check, lint, type check, tests — the same thing CI runs), and `just
test-only <pattern>` (run tests matching a keyword).

## `repos.yml` reference

[`repos.yml`](repos.yml) is the committed source of truth for which GitHub repositories
DependaPilot manages and the policy knobs the audit and dashboard read. It's validated by
`FleetConfig` in `src/dependapilot/config.py` — an unknown key, a malformed `owner/repo`
slug, or an unrecognized `merge_method` all fail to load with a clear, field-naming error
rather than a stack trace or a silently-ignored typo.

```yaml
defaults:
  merge_method: merge          # merge | squash | rebase
  cooldown_floor_days: 3

repos:
  - repo: SimplicityGuy/cronduit
    # no overrides: merge_method inherits defaults.merge_method,
    # audit defaults to true, actions defaults to false

  - repo: SimplicityGuy/discogsography
    merge_method: squash        # overrides the fleet default for this repo only

  - repo: SimplicityGuy/DependaPilot
    actions: true                # opt in to dashboard writes for this repo
```

### `defaults` (fleet-wide)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `merge_method` | `merge` \| `squash` \| `rebase` | `merge` | The GitHub merge strategy used when DependaPilot merges a PR, unless a repo overrides it. |
| `cooldown_floor_days` | integer ≥ 0 | `3` | The minimum acceptable value for a `dependabot.yml` `cooldown` field (`default-days` or any `semver-*-days` key). An explicit value below this floor is flagged as `WEAKENED_COOLDOWN`. Omitting `cooldown` entirely is *not* a finding — Dependabot then applies its own native GitHub cooldown (currently 3 days), and this floor exists to catch someone explicitly weakening that, not to demand every repo configure the block. |

### `repos[]` (per-repo)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `repo` | `"owner/name"` | *(required)* | The repo slug. Must match `owner/repo` shape; validated by regex. |
| `merge_method` | `merge` \| `squash` \| `rebase` \| omitted | `defaults.merge_method` | Per-repo override of the fleet's merge strategy. |
| `audit` | boolean | `true` | Whether the config audit runs for this repo. **On by default** — auditing is read-only and is the tool's core purpose, so a repo has to opt *out*, not in. |
| `actions` | boolean | `false` | Whether the dashboard may write to this repo at all — approve, merge, rebase-comment, or bulk-act. **Off by default**: these are the only write paths DependaPilot has, so a repo must opt in explicitly before anything can be actioned against it, no matter how safe the dashboard thinks a PR is. |

A repo absent from `repos:` entirely is invisible to DependaPilot — not discovered, not
audited, not actionable.

## The safety rubric

`score_pr` (`src/dependapilot/scoring.py`) is a pure, deterministic function: the same
facts always produce the same score, bucket, and breakdown — no I/O, no hidden state, and
every signal that moved the score is recorded (in the order it was applied) as a
`SignalBreakdown` entry, so a human can always see *why* a PR landed where it did.

Signals, in the order they're applied:

| Signal | What it measures | Delta (from a base score of 60) |
|---|---|---|
| `ci_verdict` | The folded CI verdict (green / pending / no CI / failing) — see [CI verdict](#ci-verdict) below. | green +10, pending −10, no CI −15, failing −60 |
| `metadata_unknown` *(replaces `semver` + `dependency_type` below when set)* | The Dependabot commit trailer couldn't be trusted or parsed at all (missing, malformed, or a commit not authored by `dependabot[bot]`). | −30 |
| `semver` | The riskiest semver bump among the PR's updates. | patch +15, minor 0, major −25, unrecognized −30 |
| `dependency_type` | The riskiest dependency type among the PR's updates. | direct:development +10, direct:production 0, indirect −15, unrecognized −20 |
| `mergeable` | GitHub's mergeability read for the PR. | clean +10, conflicting −20, mergeable-but-not-clean 0, unknown 0 |
| `closes_open_alert` *(only when the fact is actually known — see [`security_events`](#prerequisites))* | Whether the PR closes an open Dependabot security alert. | closes it +10, doesn't 0 |
| `stale` | The PR has been open more than 30 days (`STALE_AFTER_DAYS`) — Dependabot's auto-rebase has stopped keeping it current. | −10 |

The raw score is clamped to 0–100, then bucketed by threshold:

- **safe**: score ≥ 80
- **caution**: score ≥ 50
- **unsafe**: score < 50

### Hard caps

Two properties hold regardless of how `ScoreWeights` gets retuned later — they're enforced
as caps on the *bucket*, not baked into a score delta that tuning could undo:

- **CI is a hard cap, not just a weighted signal.** A PR can never be `safe` unless CI is
  green. A `failing` verdict forces `unsafe` outright, no matter how favorable every other
  signal is. Any other non-green verdict (`pending`, `no_ci`) caps the bucket at `caution`.
- **Untrusted or missing update metadata caps at `caution`.** If the Dependabot commit
  trailer is missing, malformed, or a commit wasn't authored by `dependabot[bot]`, the PR's
  dependency facts can't be trusted — it must never read as `safe` just because every other
  signal happens to look good.

### CI verdict

Folded from GitHub's two independent CI signals — the Checks API and the legacy combined-
status API — because some CI still reports exclusively through one or the other
(`src/dependapilot/ci.py`):

- **green**: at least one check/status succeeded, nothing failed or is still running.
- **pending**: nothing has failed yet, but at least one check/status is still queued,
  in progress, or pending.
- **failing**: at least one check/status failed, timed out, was cancelled, or needs action.
- **no_ci**: no check run and no legacy status reported *any* signal (including the case
  where every reported check was neutral/skipped) — deliberately distinct from `green`, so
  "nothing ran" is never treated as "everything passed."

## Audit checks catalog

The config audit (`src/dependapilot/audit/`) compares each audited repo's
`.github/dependabot.yml` against what its own file tree says it needs, plus two repo-level
security settings. Every check fails *open*: evidence it couldn't gather (a 403, a
truncated file tree) is reported as unknown rather than asserted as a violation.

| Check | Severity | Meaning | Remediation |
|---|---|---|---|
| `MISSING_CONFIG` | high | The repo has no `.github/dependabot.yml` at all. | Open a fix PR to create one from scratch — every detected ecosystem gets a templated `updates[]` entry. |
| `INVALID_YAML` | high | The config file isn't valid YAML. | Fix the YAML by hand; a fix PR can't safely rewrite a file it can't parse. |
| `SCHEMA_ERROR` | medium | The config violates Dependabot's own JSON schema (`src/dependapilot/audit/schemas/dependabot-2.0.json`) at a specific path/keyword. | Fix the offending field per the reported JSON Schema path and keyword. |
| `MISSING_ECOSYSTEM` | medium | Manifests for an ecosystem exist in a directory, but no `updates[]` entry covers them. | Open a fix PR — it adds the missing entry alongside the existing ones. |
| `ORPHAN_ENTRY` | low | An `updates[]` entry names a detectable ecosystem/directory where no matching manifest was found. | Remove the stale entry, or double check the directory is right. Never reported for ecosystems DependaPilot can't detect at all (e.g. `nuget`, `helm`) — no ground truth to contradict them with. |
| `WRONG_ECOSYSTEM` | high | A `pip` entry points at a directory `uv` actually manages — Dependabot would resolve the wrong dependency graph. | Open a fix PR — it swaps the entry to `uv`. |
| `DUPLICATE_ENTRY` | medium | The same ecosystem+directory pair is configured by more than one `updates[]` entry. Dependabot rejects the *entire file* over this, silently disabling updates fleet-wide for that repo. | Remove the duplicate entry by hand — the fix-PR generator carries every existing entry over as-is and doesn't dedupe them, so this one needs a manual edit. |
| `WEAKENED_COOLDOWN` | medium | An entry's `cooldown` (`default-days` or a `semver-*-days` field) is set explicitly below `cooldown_floor_days`. | Raise the cooldown value to at least the floor, or remove the override to fall back to Dependabot's native cooldown. |
| `ALERTS_DISABLED` | high | Dependabot alerts (vulnerability scanning) are off for the repo — no vulnerability is being reported at all. | Enable Dependabot alerts in the repo's **Settings → Code security** page. Not fixable via a config PR — see the note below. |
| `ALERTS_UNKNOWN` | info | The token lacks admin access to read this setting. | Grant the `gh`-authenticated user admin on the repo, or accept the degraded signal. |
| `SECURITY_UPDATES_DISABLED` | medium | Dependabot's automated security updates aren't running — vulnerable dependencies won't be patched automatically. | Enable it in **Settings → Code security**. Not fixable via a config PR. |
| `SECURITY_UPDATES_UNKNOWN` | info | The token lacks admin access to read this setting. | Same as `ALERTS_UNKNOWN`. |

`ALERTS_*` and `SECURITY_UPDATES_*` are repo-level GitHub *settings*, not config-file
content — a fix PR can't touch them (DependaPilot's token only needs read access, not
admin write, to the rest of the audit). The audit page shows a manual-remediation hint for
these instead of a diff.

### The fix-PR flow

For every finding a config-file rewrite *can* fix, the audit page can generate a corrected
`.github/dependabot.yml` and open (or update) a PR carrying it:

- The fix always lands on a dedicated `dependapilot/dependabot-config` branch, created from
  the repo's current default-branch head — **never** a write to the default branch itself.
- It's **idempotent**: if DependaPilot already has an open PR from that branch, re-running
  the fix updates that branch's file and returns the existing PR instead of opening a
  second one.
- The PR body lists every config-level finding it resolves, by check id and message.

## Actions

Actions are gated per repo by `repos.yml`'s `actions` key (off by default — see
[the reference above](#repos-per-repo)) and, for merge, by a CI check re-run at the
moment of the click, not whatever the dashboard rendered a page load ago.

### Single-PR actions

- **Approve** — `POST .../pulls/{number}/reviews` with `event=APPROVE`.
- **Merge** — `PUT .../pulls/{number}/merge`, using the repo's resolved `merge_method`
  (its own override, else the fleet default) and the head sha the dashboard last rendered
  as a race guard: if the PR has moved on since, GitHub itself rejects the merge rather
  than silently merging a commit nobody reviewed. Blocked (never sent to GitHub at all)
  unless a freshly-checked CI verdict for that sha is green.
- **Rebase** — posts the `@dependabot rebase` comment Dependabot recognizes, nudging it to
  update the branch.

Every attempt — success, a GitHub-side rejection, or a policy/CI skip — is logged as one
structured record, so dashboard actions carry the same audit trail any other write would.

### Bulk actions

"Approve all" and "merge all eligible", fleet-wide or scoped to one repo, in two phases
driven by the *same* eligibility rule so they can never disagree:

1. **Preview** — splits every in-scope PR into eligible vs. skipped-with-a-reason, so you
   see exactly what would happen before confirming anything.
2. **Execute** — re-derives eligibility from scratch (never trusts the preview response)
   and then acts on each eligible PR sequentially. One PR failing or getting rejected by
   GitHub never aborts the batch — every PR's outcome is recorded independently.

A PR is **eligible** when:

- It loaded without error (a row that failed to hydrate is never eligible — there's no
  safety score or CI verdict to trust), **and**
- Its CI verdict is green, **and**
- Its safety bucket ranks at or above the chosen `min_bucket` threshold (defaults to
  `safe`; the dashboard's **widen-to-caution** option lowers this to `caution` when you
  want to sweep in PRs the rubric flagged as needing a closer look, not just the fully
  safe ones).

Eligibility can only ever *shrink* between preview and execute — CI regressing in the
interim is the expected case, not an error, so a PR that was eligible a moment ago comes
back skipped-with-reason rather than merged anyway.

### Staleness note

A PR open more than 30 days is flagged `stale` in the safety breakdown (a −10 score
penalty, not a hard cap): Dependabot's own auto-rebase behavior stops keeping such PRs
current, so an old "safe"-looking PR may actually be behind its base branch in ways the
dashboard can't otherwise see. Rebase it (single-PR or by re-running discovery) before
trusting its score.

## Known limitations

- **Version numbers are parsed from the PR title, not from any structured field.**
  Dependabot's commit trailer carries dependency name, dependency type, and semver bump
  size, but not the actual old/new version strings — those are extracted with a regex
  against the PR title's usual "Bump foo from X to Y" shape. An edited or unusually-shaped
  title degrades to "unknown" for both versions rather than guessing.
- The `closes_open_alert` scoring signal, and the audit's `ALERTS_*` / `SECURITY_UPDATES_*`
  checks, all depend on GitHub permissions your `gh` token may not have (the
  `security_events` OAuth scope, or admin on the repo, respectively) — see
  [Prerequisites](#prerequisites). Every one of these degrades to an explicit "unknown"
  rather than a guess when the permission isn't there.

## Development

```sh
just check       # fmt-check + lint + typecheck + test -- what CI runs
just test-only scoring   # run tests matching a keyword expression
just ci           # fresh `uv sync` + the full check suite, exactly as CI does it
```

See `just --list` for the complete, grouped set of recipes.

The web UI follows the ["Mission Control" design language](docs/design-language.md) —
tokens, component recipes, and reference mockups for the fleet dashboard, audit view, and
bulk-action panel live under [`docs/`](docs/).

This repo develops via AGF (Agentic Git Flow) on [`bh`](AGENTS.md): work is tracked as
beads and driven through `bh work`, not raw `git`.

---

<p align="center">Made with ❤️ in the Pacific Northwest</p>
