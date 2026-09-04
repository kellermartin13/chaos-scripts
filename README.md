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

| Rule | Points | Trigger |
|------|--------|---------|
| Offensive Tackle | +15 | A started offensive player (QB/RB/FB/WR/TE) records a tackle. |
| Dropped Pass | +5 | A started player is charted with a drop (FTN). |
| Red Zone Turnover | +5 | Every started player involved in a turnover inside the opponent's 20 (committer + interceptor / fumble forcer / recoverer). |
| Invalid Roster Spot | −15 | A started offensive skill player with < 15% of snaps and zero touches. |

**Invalid-spot exemptions:** players ruled Out/Doubtful on the injury report (or
hurt in-game) are auto-exempt; benched players can be exempted manually with
`--exempt-invalid` (benchings are a separate manual award).

**Review candidates** (printed with `--flag-candidates`, never auto-scored):
ejections (+20) and premature goal-line celebration fumbles (+35). These need a
commissioner's confirmation.

Everything else in the rulebook (arrests, suspensions, cuts, gambling, benchings,
chaotic-event votes) is manual — see `LEAGUE_RULES.md`.

## Setup

Requires Python 3.12.

```bash
./install.sh
```

This creates a `.venv` and installs pinned dependencies. `nfl_data_py` over-pins
`pandas`/`numpy` to versions without Python 3.12 wheels, so the installer
installs compatible pinned versions first and adds `nfl_data_py` with
`--no-deps` (see `requirements.txt`).

## Usage

```bash
.venv/bin/python chaos.py --season 2025 --week 2
```

The script auto-selects the correct season's Sleeper league (it walks the
league history), so `--season` is all you normally change.

| Flag | Purpose |
|------|---------|
| `--week N` | NFL week to score (required). |
| `--season YYYY` | NFL season (default 2026). |
| `--exempt-invalid "Name" ...` | Exempt benched players from the −15 penalty (accepts full names or gsis ids). Injured players are exempted automatically. |
| `--flag-candidates` | Also print review candidates (ejections, goal-line fumbles). |
| `--solo-tackles-only` | Only award +15 for solo offensive tackles. |
| `--league-id ID` | Score a different Sleeper league. |

### When results are ready

The script relies on play-by-play, snap counts, the injury report, and FTN drop
charting. FTN charts each play within ~48 hours of a game, and the script
**refuses to score a week until every game has been charted** (no partial
weeks). In practice that means **Chaos adjustments post the Wednesday after each
week's games**.

## Testing

```bash
.venv/bin/python -m pytest test_chaos.py -q
```

## Data sources

All automated stats come from nflverse via
[`nfl_data_py`](https://github.com/nflverse/nfl_data_py):

- Play-by-play, snap counts, injury reports, and ID crosswalks:
  [nflverse-data](https://github.com/nflverse/nflverse-data)
- Drop charting is provided by **FTN Data via nflverse**, released under
  [CC-BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).

## Files

| File | Purpose |
|------|---------|
| `chaos.py` | The weekly scoring script. |
| `test_chaos.py` | pytest suite. |
| `requirements.txt` | Pinned dependencies. |
| `install.sh` | One-command environment setup. |
| `LEAGUE_RULES.md` / `.docx` | Full league rulebook. |
| `SLEEPER_PINNED.md` / `.docx` | Post-ready Sleeper pinned messages. |
