# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Trade review — players acquired via traded picks are ownership-tracked.**
  A player who enters the league through a traded draft pick (resolved to the
  player he became), where that pick was dealt through several teams before the
  draft, was credited to *every* trade the pick passed through — never
  appearing in any `adds`, so the ownership logic couldn't bound it (e.g. James
  Cook counted in full on two different 2023 trades). `build_trade_ownership`
  now resolves traded picks to their players and records the pick's new owner
  as the acquiring roster, so only the team that actually used the pick keeps
  the production.
- **Trade review — ownership bounds derived from the trades themselves.**
  Hold windows are now bounded using the same trade list the report displays
  (a received player is held until the next trade that moved him), instead of
  relying solely on a separately-fetched ownership timeline. This is robust
  when a league records no `drops` (so a re-trade's `add` is the only exit
  signal) and immune to transaction-endpoint inconsistency — a player can be
  "still held" by at most one team, eliminating cross-trade double-counts. The
  ownership timeline is still consulted for waiver/drop exits; the earliest
  exit wins.
- **Trade review — same-week re-trades no longer double-count.** A player
  traded more than once within the same filed week (e.g. multiple offseason
  trades, which Sleeper all stamps as week 1) was credited to *every* trade,
  appearing "still held" on each. `hold_window_end` now orders same-week events
  by `status_updated`, so an intermediate owner's production window closes when
  they flipped the asset and only the true holder keeps the credit.

### Added

- **Trade review — `--debug-player ID_OR_NAME`.** Prints an ownership
  diagnosis for one player to stderr (every trade that moved him, the timeline
  events, and the computed hold-window end per trade) for pinning double-counts
  from a live run.

### Changed

- **Trade review — pass-through trades aren't mislabeled heists.** When the
  losing side flipped a received asset onward (the asset carries a lineage
  link), the trade is tagged `CHAINED` instead of `LOPSIDED`/`HEIST` — a side
  that acquired a stud and immediately re-traded him wasn't fleeced, and its
  single-trade PAR understates the return. PAR numbers are unchanged; the ↳
  links show where the value went.
- **Trade review — offseason trades are labeled "offseason".** Trades filed
  under week 1 before the season kicks off now display as `<season> offseason`
  instead of `<season> wk1`, based on the trade's timestamp. Scoring is
  unchanged — offseason trades still count the whole season.

## [1.0.0] - 2026-09-15

First stable release. `chaos.py` scores a week of League of Chaos bonuses from
official nflverse data, a GitHub Actions workflow runs it automatically once
each week's data is ready, and `trade_review.py` ships as a work-in-progress
dynasty tool.

### Added

#### Weekly Chaos scoring (`chaos.py`)

- Automated scoring of League of Chaos commissioner bonuses by joining Sleeper
  started lineups against nflverse play-by-play, snap counts, and FTN charting:
  - **Offensive Tackle (+15)** — a started offensive player records a tackle
    (solo, or assisted unless `--solo-tackles-only`).
  - **Dropped Pass (+5)** — a started player charted with a drop by FTN.
  - **Red Zone Turnover (+5)** — every started player involved in a turnover
    inside the opponent's 20 (committer and the defenders who caused it).
  - **Invalid Roster Spot (−15)** — a started offensive skill player with
    < 15% of offensive snaps and zero touches. In-game injuries are
    auto-exempt; benched players are exempted manually with `--exempt-invalid`.
  - **Non-QB TD Pass (+20)** — a trick-play touchdown thrown by a non-QB.
  - **Taunting / Unsportsmanlike (+15)** and **pre-snap penalties (+5)** for
    started players, keyed off nflverse `penalty_type`.
  - **Penalty Negates TD (+10)** — added on top when a started player's penalty
    wipes out his own team's touchdown.
- Review candidates (printed with `--flag-candidates`, never auto-scored):
  ejections (+20), premature goal-line celebration fumbles (+35), and
  one-point safeties (+1000).
- Refuses to score a partially-charted week: every scheduled game must be
  present in FTN before scoring, and an unpublished FTN season exits with a
  clear message instead of a raw 404.

#### Automation

- Auto-detection of the target season and week from the schedule
  (`derive_target_season` / `derive_target_week`): `--season` and `--week` are
  now optional and resolve to the most-recently-completed week. The rule is
  self-correcting across the Thursday boundary.
- `--check-only` — cheap FTN-coverage probe that loads no Sleeper, pbp, or snap
  data and exits non-zero until the week is fully charted.
- `--print-target` — prints the resolved `<season> <week>` for schedulers.
- The FTN coverage gate now runs before any Sleeper or play-by-play loading, so
  a not-ready week exits without wasted work or Sleeper calls.
- **GitHub Actions workflow "League of Chaos weekly scoring"** — polls Tue/Wed
  (06/12/18 UTC), scores the completed week once FTN is fully charted, and opens
  a GitHub issue (assigned to the repo owner) with the report. A per-week dedup
  guard makes later polls that week no-op until the target week advances.

#### Dynasty trade review (`trade_review.py`) — WORK IN PROGRESS

- Walks a dynasty league's full history, pulls every completed trade, and
  reports since-trade production using the league's own scoring settings,
  resolving traded picks to the players they became. **Unverified** — flagged
  WIP in the module docstring and via a startup banner; not for league
  decisions yet.

#### Shared infrastructure

- `sleeper_cache.py` — best-effort local Redis cache for Sleeper's ~16 MB NFL
  player map, shared by `chaos.py` and `trade_review.py`. Degrades gracefully
  to a live fetch when Redis is unavailable.
- Pinned dependencies and one-command setup (`install.sh`); full pytest suite
  with all external I/O mocked.

[1.0.0]: https://github.com/kellermartin13/chaos-scripts/releases/tag/v1.0.0
