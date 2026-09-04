# 🔥 League of Chaos — Official Rules

## The Idea

Standard Sleeper scoring runs the matchup. On top of that, the **Chaos layer**
rewards chaotic and unusual on-field events and penalizes dead roster spots.
Some of this is now automated by a weekly script; the rest depends on you
calling it out.

## How Scoring Works

1. **Sleeper** scores your lineup normally, live, during games.
2. **Chaos adjustments** are applied *after* the week, on top of your Sleeper score.
3. Adjustments come from two places:
   - **The Chaos script** — runs automatically off official NFL play-by-play data.
   - **Manual rules** — things a computer can't judge. You report these; the commissioner applies them.

### 📅 When Chaos scores post — Wednesdays

The script relies on play-by-play, snap counts, drop charting, and the injury
report. That data isn't final until roughly 48 hours after the last game of the
week, and the script **refuses to score a week until every game has been fully
charted** (no partial weeks). So:

> **Chaos adjustments are posted the Wednesday after each week's games.**

If the numbers change your matchup result, **records are updated**. Late stat
corrections from the NFL can also move things after the fact.

---

## Tier 1 — Automated (the script does this for you)

No action needed. Calculated from official data every Wednesday.

| Rule | Points | How it's scored |
|------|--------|-----------------|
| **Offensive Tackle** | **+15** | A started offensive player (QB/RB/FB/WR/TE) records a tackle (solo or assist). Defensive tackles never count. |
| **Dropped Pass** | **+5** | A started player is charted with a drop (FTN charting). |
| **Red Zone Turnover** | **+5** | Every started player involved in a turnover inside the opponent's 20-yard line — the player who committed it (QB on an interception, or the fumbler) **and** any started defender who caused it (interceptor, fumble forcer, fumble recoverer). |
| **Non-QB TD Pass** | **+20** | A started non-QB (RB/WR/TE/FB) throws a touchdown pass (trick play). |
| **Taunting / Unsportsmanlike** | **+15** | A started player is flagged for taunting or unsportsmanlike conduct. |
| **Pre-Snap Penalty** | **+5** | A started player commits a pre-snap penalty (false start, delay of game, illegal formation/shift/motion). |
| **Invalid Roster Spot** | **−15** | A started offensive skill player who played **< 15% of offensive snaps AND had zero touches** (a wasted lineup slot). See exemptions below. |

### Invalid Roster Spot — exemptions

The penalty targets *unavailable* players, not unlucky ones:

- **Injured players are auto-exempt.** Anyone ruled **Out or Doubtful** on the
  week's injury report (or hurt in-game) is excluded automatically — no penalty.
- **Benched players are exempt too**, but the commissioner enters those by hand
  (see the Benching rule). Since a benching is a **+20 reward**, it is never also
  a −15 penalty.
- Exempted players are still listed each week (with the reason) for transparency.

> **⚠️ No Intentional Invalid Lineups.** The −15 exists to penalize unavailable
> players, not as a strategy. Any manager who *intentionally* leaves an invalid
> spot to game the system **forfeits that week's matchup.**

---

## Tier 2 — Commissioner-Reviewed (the script flags, you confirm)

The script surfaces these as **candidates** each week because the data hints at
them, but a human has to confirm. If you think you qualify, ping the commissioner.

| Rule | Points | Trigger |
|------|--------|---------|
| **Ejection** | **+20** | Your started player is ejected/disqualified from the game. |
| **Premature Celebration Fumble** | **+35** | Your player drops the ball *before* crossing the goal line in celebration. **Forced fumbles do not count** — only the pure idiocy of not finishing the play. |

---

## Tier 3 — Manual (you must report these)

These rules require news or human judgment the script can't provide. **If you
see that one of your
started players qualifies, ping the commissioner in the chat.** Points are
applied after that week's games are finalized; if it changes a matchup outcome,
records are updated.

### Player conduct & availability

| Rule | Points | Notes |
|------|--------|-------|
| **Arrested** | **+35** | Applied to the player's **previous game**. |
| **Suspended by the league** | **+25** | Applied to the player's **previous game**. |
| **Cut after their game** | **+30** | Your starter is released following that weekend's games. |
| **The Antonio Brown Rule** | **+25** | Player leaves the game **voluntarily** due to a meltdown. |
| **Celebration Self-Injury** | **+15** | Your player injures himself while celebrating. |

### 🎰 The Pete Rose Award (gambling)

If a player is suspended for gambling or sports-betting violations, points are
applied retroactively to that player's **most recent game**:

| Suspension length | Points |
|-------------------|--------|
| 1–6 games | **+35** |
| 7+ games | **+50** |
| Indefinite / season-long | **+100** |

### 🃏 Chaotic Events

| Rule | Points | Notes |
|------|--------|-------|
| **Extraordinary/chaotic football event** not covered elsewhere | **+20** | Requires a **majority league vote.** |

### 🪑 The Benching Rule (+20)

**+20 if a starter loses their starting role due to poor performance.** This is a
manual award (report it), and it's what exempts a player from the Invalid Roster
Spot penalty.

A player **qualifies** if **any one** of these occurs:

1. The head coach, coordinator, or team officially states the player was benched due to performance.
2. The player is replaced during a competitive game and does not return despite being healthy and available.
3. The player loses the starting job before the next game and the team publicly attributes the change to performance.
4. The player is designated inactive for the next game due to performance.

**Does NOT qualify:** injury · illness · concussion protocol · load management ·
resting starters · Week 18 shenanigans · pulling starters in a blowout (winning
or losing) · suspension · planned QB packages · rotational substitutions.

---

## TL;DR

- **Automatic every Wednesday:** offensive tackles (+15), drops (+5), red-zone turnovers (+5 to everyone involved), non-QB TD passes (+20), taunting/unsportsmanlike (+15), pre-snap penalties (+5), invalid spots (−15, injuries auto-exempt).
- **Script flags, you confirm:** ejections (+20), goal-line celebration fumbles (+35).
- **You report:** arrests, suspensions, cuts, gambling (Pete Rose), voluntary meltdowns (Antonio Brown), benchings (+20), and chaotic-event votes.
- **Ping the commissioner** for anything manual. Records update if outcomes change. Chaos! 🔥

---

## Appendix — How the Automation Works (Commissioner Only)

The weekly Chaos adjustments come from `chaos.py`, which pulls fantasy starters
from Sleeper and joins them against nflverse NFL data (play-by-play, FTN drop
charting, snap counts, and the injury report).

### One-time setup

```bash
./install.sh
```

Creates a `.venv` and installs pinned dependencies. (`nfl_data_py` over-pins
pandas/numpy, so the installer handles that; see `requirements.txt`.)

### Weekly run (each Wednesday)

```bash
.venv/bin/python chaos.py --season 2025 --week 2
```

- Auto-selects the correct season's Sleeper league (walks the league history).
- **Refuses to run if any game in the week isn't fully charted yet** — this is
  why results wait until Wednesday. Re-run once all games are charted.
- Prints per-player evidence (the actual play descriptions) plus a
  commissioner-ready adjustment total per team.

### Options

| Flag | Purpose |
|------|---------|
| `--exempt-invalid "Player Name" ...` | Exempt benched players (awarded +20 separately) from the −15 invalid penalty. Accepts full names or gsis ids. Players ruled Out/Doubtful are exempted automatically. |
| `--flag-candidates` | Also print review candidates for ejections (+20) and premature goal-line fumbles (+35) — never scored, just surfaced for a manual call. |
| `--solo-tackles-only` | Only award +15 for solo offensive tackles (assisted tackles excluded). |
| `--league-id <id>` | Score a different Sleeper league. |

### What is and isn't automated

- **Scored automatically:** offensive tackles, drops, red-zone turnovers, non-QB
  TD passes, taunting/unsportsmanlike and pre-snap penalties, and invalid roster
  spots (with injury auto-exemption).
- **Flagged for review:** ejections, premature goal-line fumbles.
- **Entirely manual:** arrests, league suspensions, cuts, the Pete Rose Award,
  the Antonio Brown Rule, benchings, and chaotic-event votes — these require
  news or judgment the data can't provide.

### Data sources & attribution

All automated stats come from **nflverse**:

- **nflverse-data:** <https://github.com/nflverse/nflverse-data>
- Play-by-play, snap counts, injury reports, and ID crosswalks via the
  `nfl_data_py` package: <https://github.com/nflverse/nfl_data_py>
- **Drop charting** is provided by **FTN Data via nflverse**, released under the
  Creative Commons **CC-BY-SA 4.0** license
  (<https://creativecommons.org/licenses/by-sa/4.0/>). FTN charts each play
  within ~48 hours of a game, which is why Chaos scoring posts on Wednesdays.
