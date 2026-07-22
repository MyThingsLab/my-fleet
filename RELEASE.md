# MyThingsLab release contract — rules for a v1 repo

You are an agent developing a MyThingsLab repo that has graduated to **v1**:
`my-things-core`, `my-guard`, `my-director`, `my-fleet`, `my-dashboard`,
`my-reporter`. Everything else in the fleet is v0 — build freely, float on
`@main`, no version discipline required. This file only applies once a repo
is on the v1 list. The canonical copy ships in `mythings/release.md`; inside
a v1 repo this is a **vendored copy** kept in sync by a drift-check test —
never edit it in a repo. To change a rule, edit the canonical in
`my-things-core`, then re-vendor: `python -m mythings._release <workspace-root>`.

## Semver

`MAJOR.MINOR.PATCH` in `pyproject.toml`, one `git tag vX.Y.Z` + GitHub
Release per bump.

- **PATCH** — behavior fix, no public-surface change.
- **MINOR** — additive, backward-compatible (new CLI flag, new exported
  symbol, new optional field).
- **MAJOR** — removes or changes the meaning of something another repo could
  depend on: an exported symbol, a CLI flag/subcommand, a schema field a
  consumer parses (e.g. `SessionPlan`, an `Action`, a `Ledger` record shape).

## Deprecation

A MAJOR removal must have shipped **deprecated-but-working in the prior
MINOR** — keep the old symbol/flag functional, note in its docstring/help
text that it is deprecated and what replaces it, and land the CHANGELOG entry
for that MINOR release saying so. "Prior release" means one merged version
bump, not calendar time — this fleet ships continuously, so the notice
period is *a PR cycle*, not weeks.

## CHANGELOG

`CHANGELOG.md` at repo root, [Keep a Changelog](https://keepachangelog.com/)
format. Every version bump gets a `## [X.Y.Z] - YYYY-MM-DD` entry in the same
PR that bumps `pyproject.toml`'s `version`. `python -m mythings._release
--check` fails a PR whose version changed without a matching entry — it does
not judge whether a bump was *warranted*, only that a bump and its changelog
agree. That judgment call stays with whoever authors the PR.

## Pinning between v1 repos

A v1 repo depending on another v1 repo pins to the exact tag, not `@main`:

```
my-things-core @ git+https://github.com/MyThingsLab/my-things-core@v1.0.0
```

in both `pyproject.toml`'s `dependencies` and the CI install step. A v1 repo
may still float on `@main` for any v0 repo it depends on — the pin
requirement is only v1-to-v1. `python -m mythings._compat` keeps running
regardless, as defense-in-depth against accidental drift, not as the primary
coherence mechanism anymore.
