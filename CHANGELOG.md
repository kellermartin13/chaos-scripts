# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
