# chaos-scripts

Weekly scoring automation for the **League of Chaos** Sleeper fantasy league.
`chaos.py` pulls each week's started lineups from Sleeper and joins them against
official NFL data ([nflverse](https://github.com/nflverse)) to compute the
league's custom "Chaos" scoring adjustments — the bonuses and penalties that
live on top of standard Sleeper scoring.

The full rulebook is in **[LEAGUE_RULES.md](LEAGUE_RULES.md)**; post-ready
Sleeper pinned messages are in **[SLEEPER_PINNED.md](SLEEPER_PINNED.md)**.

## What it scores

Automatically, from official data:

| Rule                | Points | Trigger                                                                                                                     |
| ------------------- | ------ | --------------------------------------------------------------------------------------------------------------------------- |
| Offensive Tackle    | +15    | A started offensive player (QB/RB/FB/WR/TE) records a tackle.                                                               |
| Dropped Pass        | +5     | A started player is charted with a drop (FTN).                                                                              |
| Red Zone Turnover   | +5     | Every started player involved in a turnover inside the opponent's 20 (committer + interceptor / fumble forcer / recoverer). |
| Invalid Roster Spot | −15    | A started offensive skill player with < 15% of snaps and zero touches.                                                      |

**Invalid-spot exemptions:** players ruled Out/Doubtful on the injury report (or
hurt in-game) are auto-exempt; benched players can be exempted manually with
`--exempt-invalid` (benchings are a separate manual award).

**Review candidates** (printed with `--flag-candidates`, never auto-scored):
ejections (+20) and premature goal-line celebration fumbles (+35). These need a
commissioner's confirmation.

Everything else in the rulebook (arrests, suspensions, cuts, gambling, benchings,
chaotic-event votes) is manual — see `LEAGUE_RULES.md`.

## Dynasty trade review

`trade_review.py` is a separate tool for dynasty leagues. It walks a league's
full history (every season's Sleeper league object, linked by
`previous_league_id`), pulls every completed trade, and grades how the assets
each side received have performed — including traded draft picks resolved to
the players they actually became. The headline metric is **Points Above
Replacement (PAR)**, a WAR-style measure; raw league points are kept as a
secondary sanity column.

```bash
.venv/bin/python trade_review.py --league-id <current-season-league-id>
```

Pass the **current** season's league ID; the dynasty history is walked backward
from it. Add `--season 2025` to review only one season's trades.

**Why PAR instead of raw points?** Raw "points since trade" tells you how
*much* an asset produced; PAR tells you how much *better than a freely
available replacement* at the same position — which is what actually decides a
trade. 15 points from a TE (scarce) is worth far more than 15 from a QB
(plentiful).

How it works:

- **League-accurate scoring.** Production is the dot product of each player's
  weekly Sleeper stat line and the league's own `scoring_settings`, so verdicts
  reflect real league points — it even honors quirks like TE-premium receiving,
  not a generic PPR line.
- **Replacement level.** For each position, the league starts a fixed number of
  players each week (starter slots × teams, with FLEX/SUPER_FLEX demand spread
  across eligible positions). The best player *below* that cutoff — the guy you
  could stream off waivers for free — is replacement level. PAR is a player's
  league points minus that weekly, position-specific baseline.
- **Floored by default.** Each week's PAR is `max(0, points − replacement)`.
  You are never forced to start a below-replacement player (you bench or drop
  him), so a bad week contributes 0, not a penalty — this measures value *when
  the asset was startable*. Pass `--unfloored` for literal WAR, where
  sub-replacement weeks subtract (punishes depth over a long dynasty window).
- **Ownership-aware windows.** Production is counted **only while the receiving
  team actually held the asset**. When a player is later re-traded, waived, or
  dropped, credit stops — the trade isn't credited for production the team no
  longer owned (`build_ownership_timeline` / `hold_window_end`).
- **Traded picks → players.** A traded pick is resolved to the player it became
  by mapping the pick's *original owner* to its draft slot
  (`slot_to_roster_id`). Future picks with no draft yet are shown as pending.
- **Lineage linkage, not arithmetic.** Because dynasty assets keep moving, a
  highlight cross-references the *next* trade an asset flowed into and names
  what came back (e.g. `↳ became: Ja'Marr Chase, 2023 1st → Bijan (see T27)`).
  Value is **never summed or split across trades** — every figure stays a clean
  single-trade number, and you follow the chain yourself. Assets that left via
  a plain drop show `↳ later dropped — lineage ends`.
- **Readable highlights.** The report leads with the most lopsided trades (by
  PAR margin, flagged `LOPSIDED` at 2x / `HEIST` at 3x), each showing both
  sides' PAR and PAR/game, a per-season trajectory (`by season: 20:+540 21:+610 …`), seasons elapsed, whether each asset is still held, and a
  one-line takeaway. A full chronological listing of every trade follows,
  each showing both sides' rosters, PAR, and raw points.
- **Manager overview.** A closing scoreboard aggregates every trade by its
  (stable) owner across all seasons: best/worst trader by net PAR, most
  aggressive/passive by trade count, and a per-manager leaderboard **ranked by
  net PAR** with trades, W-L-T, total received, and net PAR. Managers who never
  traded are listed too.

Pass `--raw-points` for the legacy points-only report (per-trade verdicts by
raw production, with the same lopsided summary and a points-based manager
overview). Unlike `chaos.py`, this tool needs only the public Sleeper API — no
nflverse.

| Flag             | Purpose                                                                            |
| ---------------- | ---------------------------------------------------------------------------------- |
| `--league-id ID` | Current-season league ID; history is walked backward from it.                      |
| `--season YYYY`  | Only review this season's trades.                                                  |
| `--top N`        | Number of highlight trades to show (default 12).                                   |
| `--band N`       | Ranks past the last starter that define replacement level (default 3).             |
| `--unfloored`    | Use literal WAR (below-replacement weeks penalize) instead of the floored default. |
| `--raw-points`   | Print the legacy points-only report instead of the PAR report.                     |
| `--html`         | Emit the report as a self-contained HTML page (for GitHub Pages) instead of text.  |

> **Status:** PAR is the primary metric, but verdicts have not been validated
> end-to-end against known-good league results — spot-check against your league
> before relying on them. It assumes Sleeper `roster_id` is stable across the
> dynasty chain (the usual case); a league that reassigns roster_ids across
> seasons would need ownership keyed on the stable manager id.

### Running as a GitHub Action

`.github/workflows/trade-review.yml` runs the review on demand — **manual
trigger only, no schedule**. From the repo's **Actions** tab, pick *Dynasty
Trade Review* → *Run workflow*, enter a **league ID** (and optionally season,
highlight count, `unfloored`, or `raw_points`), and run.

Output lands in three places:

- **GitHub Pages** — the report is published as an HTML page at
  `https://<owner>.github.io/<repo>/leagues/<league-id>.html`. Each league gets
  its **own stable URL** (publishing uses `keep_files: true`, so leagues don't
  overwrite each other), so you can pin that link in each league's chat. The
  Pages URL is also printed at the top of the Job Summary.
- **Job Summary** on the run page — the full text report, inline.
- **Artifact** (`trade-review-report`) — the report `.txt` plus the generated
  HTML.

**One-time setup:** in **Settings → Pages**, set *Source* to *Deploy from a
branch* → **`gh-pages`** / **`/ (root)`**. (The first workflow run creates the
`gh-pages` branch.) Publishing uses the built-in `GITHUB_TOKEN` — no PAT
needed, since `gh-pages` is in the same repo.

The job needs only the public Sleeper API and `requests` (no nflverse/pandas),
so it finishes quickly. League-ID and other inputs are passed to the script via
environment variables — never interpolated into the shell — so an arbitrary
input can't inject commands. A `concurrency` group serializes runs so multiple
leagues never publish (or hit Sleeper) at the same time.

**Rate limits.** Sleeper asks callers to stay under ~1000 requests/minute. A
single review makes a few hundred **sequential** calls over its runtime, well
under that. To keep it there: the report shares one pass of the transaction log
(trade collection + ownership timeline) and one weekly-stats cache, and the
~16 MB player map is cached between runs (see below) — so the two largest call
buckets aren't fetched twice. Don't parallelize the fetchers.

## Setup

Requires Python 3.12.

```bash
./install.sh
```

This creates a `.venv` and installs pinned dependencies. `nfl_data_py` over-pins
`pandas`/`numpy` to versions without Python 3.12 wheels, so the installer
installs compatible pinned versions first and adds `nfl_data_py` with
`--no-deps` (see `requirements.txt`).

### Player-map cache (Redis or file)

Both scripts fetch Sleeper's NFL player map (`/players/nfl`) — a large (~16 MB)
payload that changes at most once a day. `sleeper_cache.py` caches it two ways,
and `chaos.py` and `trade_review.py` **share the same cached copy**:

- **Redis** (local dev): cached under key `sleeper:players:nfl` with a 24h TTL.
- **File** (CI / no Redis): set `SLEEPER_PLAYERS_FILE` to a JSON path and the
  map is read from / written to that file (24h freshness check). The trade-
  review Action points this at `.sleeper-cache/players-nfl.json` and persists
  it with `actions/cache` keyed by UTC date, so repeated runs across leagues
  don't re-fetch the map.

Lookup order is Redis → file → live fetch; every layer is best-effort, so with
neither cache available the scripts transparently fall back to a live fetch.

To enable Redis locally, run one (default `redis://localhost:6379/0`, override
with the `REDIS_URL` env var):

```bash
brew install redis && brew services start redis
```

## Usage

```bash
.venv/bin/python chaos.py --season 2025 --week 2
```

The script auto-selects the correct season's Sleeper league (it walks the
league history), so `--season` is all you normally change.

| Flag                          | Purpose                                                                                                                   |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `--week N`                    | NFL week to score (required).                                                                                             |
| `--season YYYY`               | NFL season (default 2026).                                                                                                |
| `--exempt-invalid "Name" ...` | Exempt benched players from the −15 penalty (accepts full names or gsis ids). Injured players are exempted automatically. |
| `--flag-candidates`           | Also print review candidates (ejections, goal-line fumbles).                                                              |
| `--solo-tackles-only`         | Only award +15 for solo offensive tackles.                                                                                |
| `--league-id ID`              | Score a different Sleeper league.                                                                                         |

### When results are ready

The script relies on play-by-play, snap counts, the injury report, and FTN drop
charting. FTN charts each play within ~48 hours of a game, and the script
**refuses to score a week until every game has been charted** (no partial
weeks). In practice that means **Chaos adjustments post the Wednesday after each
week's games**.

## Testing

```bash
.venv/bin/python -m pytest -q
```

This runs the full suite: `test_chaos.py`, `test_trade_review.py`, and
`test_sleeper_cache.py` (all external I/O is mocked; no network required).

## Data sources

All automated stats come from nflverse via
[`nfl_data_py`](https://github.com/nflverse/nfl_data_py):

- Play-by-play, snap counts, injury reports, and ID crosswalks:
  [nflverse-data](https://github.com/nflverse/nflverse-data)
- Drop charting is provided by **FTN Data via nflverse**, released under
  [CC-BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).

## Files

| File                          | Purpose                                              |
| ----------------------------- | ---------------------------------------------------- |
| `chaos.py`                    | The weekly scoring script.                           |
| `test_chaos.py`               | pytest suite.                                        |
| `trade_review.py`             | Dynasty trade-review tool.                           |
| `test_trade_review.py`        | pytest suite for the trade-review tool.              |
| `sleeper_cache.py`            | Shared local Redis cache for the Sleeper player map. |
| `test_sleeper_cache.py`       | pytest suite for the shared cache.                   |
| `requirements.txt`            | Pinned dependencies.                                 |
| `install.sh`                  | One-command environment setup.                       |
| `LEAGUE_RULES.md` / `.docx`   | Full league rulebook.                                |
| `SLEEPER_PINNED.md` / `.docx` | Post-ready Sleeper pinned messages.                  |
