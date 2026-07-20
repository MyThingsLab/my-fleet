# Changelog

All notable changes to `my-fleet` are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[semver](https://semver.org/), per the rules in `RELEASE.md`.

## [1.0.0] - 2026-07-20

First stable release. Baseline of the fleet ops tooling as it already
existed: the dispatch loop, `fleet_cycle`/`cycle_driver`, the cross-repo test
gate, ASK-channel merge routing, and usage/account monitoring. No behavior
changes in this release. Adopts the v1 release contract (`RELEASE.md`) and
pins its `my-things-core` and `my-guard` dependencies to `@v1.0.0` instead of
floating on `@main`. `my-orchestrator` and `my-telegram-bot` stay on `@main`
— still v0.
