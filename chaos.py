#!/usr/bin/env python3

import argparse
import sys
from collections import defaultdict

import pandas as pd
import requests
import nfl_data_py as nfl


# =============================================================================
# Configuration
# =============================================================================

SLEEPER_BASE = "https://api.sleeper.app/v1"

DEFAULT_LEAGUE_ID = "1312284808574935040"

TACKLE_POINTS = 15
DROP_POINTS = 5

# Invalid roster spot: a started offensive skill player who barely played
# (< 15% of offensive snaps) AND never touched the ball is a wasted/invalid
# lineup slot, penalized -15.
INVALID_SPOT_POINTS = 15
INVALID_SNAP_THRESHOLD = 0.15

# Red-zone turnover: +5 to every started player involved in a turnover inside
# the opponent's 20 — the committer (QB on an INT, or the fumbler) and the
# defenders who caused it (interceptor, fumble forcer, fumble recoverer).
REDZONE_TURNOVER_POINTS = 5
REDZONE_YARDLINE = 20

# Only these positions are eligible for the +15 offensive tackle bonus.
OFFENSIVE_POSITIONS = {
    "QB",
    "RB",
    "FB",
    "WR",
    "TE",
}

SOLO_TACKLE_COLUMNS = [
    "solo_tackle_1_player_id",
    "solo_tackle_2_player_id",
]

ASSIST_TACKLE_COLUMNS = [
    "assist_tackle_1_player_id",
    "assist_tackle_2_player_id",
    "assist_tackle_3_player_id",
    "assist_tackle_4_player_id",
]


# =============================================================================
# HTTP
# =============================================================================

def get_json(url):
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


# =============================================================================
# Sleeper
# =============================================================================

def get_sleeper_players():
    """
    Returns Sleeper's NFL player map.

    Key:
        Sleeper player_id

    Value:
        Player metadata including gsis_id, position, name, etc.
    """

    return get_json(f"{SLEEPER_BASE}/players/nfl")


def get_league(league_id):
    return get_json(
        f"{SLEEPER_BASE}/league/{league_id}"
    )


def resolve_league_for_season(league_id, season):
    """
    Sleeper creates a new league object every season, so a given league_id
    only holds one season's rosters and lineups. Scoring, say, 2025 games
    against the 2026 league object silently pulls the wrong (current-season)
    starters.

    Walk the previous_league_id chain until we find the league whose season
    matches the one being scored. Falls back to the original id if no match
    is found.

    Returns:
        (resolved_league_id, league_object_or_none)
    """

    season = str(season)
    visited = set()
    current = league_id

    while current and current not in visited:
        visited.add(current)

        league = get_league(current)

        if str(league.get("season")) == season:
            return current, league

        current = league.get("previous_league_id")

    return league_id, None


def get_matchups(league_id, week):
    return get_json(
        f"{SLEEPER_BASE}/league/{league_id}/matchups/{week}"
    )


def get_rosters(league_id):
    return get_json(
        f"{SLEEPER_BASE}/league/{league_id}/rosters"
    )


def get_users(league_id):
    return get_json(
        f"{SLEEPER_BASE}/league/{league_id}/users"
    )


def get_team_names(league_id):
    """
    Build:
        roster_id -> team/display name
    """

    users = {
        user["user_id"]: user
        for user in get_users(league_id)
    }

    result = {}

    for roster in get_rosters(league_id):
        roster_id = roster["roster_id"]
        owner_id = roster.get("owner_id")

        user = users.get(owner_id, {})

        metadata = user.get("metadata") or {}

        name = (
            metadata.get("team_name")
            or user.get("display_name")
            or user.get("username")
            or f"Roster {roster_id}"
        )

        result[roster_id] = name

    return result


def get_starters(league_id, week, players, crosswalk=None):
    """
    Get every fantasy starter for the specified week.

    The resulting dictionary is keyed by GSIS ID so it can be joined
    directly against nflverse.

    crosswalk (optional):
        sleeper_id -> gsis_id map used to recover a GSIS ID when Sleeper's
        own player record doesn't include one.

    Returns:

        {
            "00-0031234": {
                "sleeper_id": "...",
                "gsis_id": "00-0031234",
                "name": "Player Name",
                "position": "QB",
                "roster_id": 5
            }
        }
    """

    starters = {}

    matchups = get_matchups(
        league_id,
        week,
    )

    for matchup in matchups:
        roster_id = matchup["roster_id"]

        for sleeper_id in matchup.get("starters", []):
            player = players.get(sleeper_id)

            # Team defenses and other non-player IDs won't necessarily
            # resolve to a normal player object.
            if not player:
                continue

            gsis_id = player.get("gsis_id")

            # Sleeper frequently leaves gsis_id null; fall back to the
            # nflverse crosswalk keyed by the Sleeper player_id.
            if not gsis_id and crosswalk:
                gsis_id = crosswalk.get(sleeper_id)

            # Sleeper (and some feeds) pad the id with whitespace, e.g.
            # " 00-0035261"; normalize so it matches nflverse's clean
            # gsis ids used in PBP, snaps, and the crosswalks.
            if gsis_id:
                gsis_id = gsis_id.strip()

            if not gsis_id:
                continue

            name = (
                player.get("full_name")
                or " ".join(
                    value
                    for value in [
                        player.get("first_name"),
                        player.get("last_name"),
                    ]
                    if value
                )
            )

            starters[gsis_id] = {
                "sleeper_id": sleeper_id,
                "gsis_id": gsis_id,
                "name": name,
                "position": player.get("position"),
                "roster_id": roster_id,
            }

    return starters


# =============================================================================
# nflverse PBP
# =============================================================================

def build_sleeper_gsis_crosswalk():
    """
    Sleeper's own player map leaves gsis_id null for a large share of active
    players, so joining starters to nflverse on Sleeper's gsis_id alone drops
    most of the roster.

    nflverse publishes an ID crosswalk (import_ids) that maps sleeper_id to
    gsis_id. Build:

        sleeper_id (str) -> gsis_id

    so get_starters can recover a gsis_id when Sleeper doesn't supply one.
    """

    ids = nfl.import_ids()

    crosswalk = {}

    for _, row in ids[["sleeper_id", "gsis_id"]].iterrows():
        sleeper_id = row["sleeper_id"]
        gsis_id = row["gsis_id"]

        if pd.isna(sleeper_id) or pd.isna(gsis_id):
            continue

        gsis_id = str(gsis_id).strip()

        # sleeper_id arrives as a float-like value (e.g. 4034.0); normalize
        # to the plain string Sleeper uses as its player_id.
        sleeper_id = str(sleeper_id)
        if sleeper_id.endswith(".0"):
            sleeper_id = sleeper_id[:-2]

        crosswalk[sleeper_id] = gsis_id

    return crosswalk


def build_gsis_pfr_crosswalk():
    """
    Snap counts are keyed by Pro-Football-Reference id (pfr_player_id), while
    everything else here is keyed by gsis_id. Build:

        gsis_id -> pfr_id

    from nflverse's ID crosswalk so snap share can be joined to starters.
    """

    ids = nfl.import_ids()

    crosswalk = {}

    for _, row in ids[["gsis_id", "pfr_id"]].iterrows():
        gsis_id = row["gsis_id"]
        pfr_id = row["pfr_id"]

        if pd.isna(gsis_id) or pd.isna(pfr_id):
            continue

        crosswalk[str(gsis_id).strip()] = str(pfr_id).strip()

    return crosswalk


def load_pbp(season, week):
    """
    Download nflverse play-by-play for the season and restrict it
    to the requested week.
    """

    print(
        f"Loading nflverse play-by-play for {season}..."
    )

    columns = [
        "game_id",
        "play_id",
        "week",
        "qtr",
        "time",
        "home_team",
        "away_team",
        "desc",
        "receiver_player_id",
        "rusher_player_id",
        "complete_pass",
        "special",

        "fumble_not_forced",
        "fumbled_1_player_id",
        "yardline_100",
        "yards_gained",
        "touchback",
        "fumble_out_of_bounds",
        "touchdown",

        "interception",
        "fumble_lost",
        "yrdln",
        "passer_player_id",
        "interception_player_id",
        "forced_fumble_player_1_player_id",
        "fumble_recovery_1_player_id",

        "solo_tackle_1_player_id",
        "solo_tackle_2_player_id",

        "assist_tackle_1_player_id",
        "assist_tackle_2_player_id",
        "assist_tackle_3_player_id",
        "assist_tackle_4_player_id",
    ]

    pbp = nfl.import_pbp_data(
        [season],
        columns=columns,
        downcast=True,
        cache=False,
    )

    pbp = pbp[
        pbp["week"] == week
    ].copy()

    print(
        f"Loaded {len(pbp):,} plays for Week {week}."
    )

    return pbp


def play_context(play):
    """
    Extract the information we'll want to display later as an audit trail.
    """

    return {
        "game_id": play.get("game_id"),
        "play_id": play.get("play_id"),
        "qtr": play.get("qtr"),
        "time": play.get("time"),
        "away_team": play.get("away_team"),
        "home_team": play.get("home_team"),
        "desc": play.get("desc"),
    }


# =============================================================================
# Offensive tackles
# =============================================================================

def find_offensive_tackles(pbp, starters):
    """
    Find tackles made by offensive players who were STARTED in the
    fantasy league that week.

    Defensive players are explicitly excluded from this bonus.

    Returns:

        gsis_id -> {
            solo: int,
            assists: int,
            plays: [...]
        }
    """

    result = defaultdict(
        lambda: {
            "solo": 0,
            "assists": 0,
            "plays": [],
        }
    )

    offensive_ids = {
        gsis_id
        for gsis_id, player in starters.items()
        if player["position"] in OFFENSIVE_POSITIONS
    }

    for _, play in pbp.iterrows():
        # The bonus is for offensive players making tackles on offensive/
        # defensive plays, not special-teams coverage tackles (punt/kickoff
        # gunners, FG/XP), so skip special-teams plays entirely.
        if play.get("special") == 1:
            continue

        # An offensive player only legitimately records a tackle after his
        # team turns the ball over and he tackles the returner. On any other
        # play, an "offensive-position" starter credited with a tackle was
        # actually playing defense (two-way players like Travis Hunter at CB),
        # so require a change of possession (interception or lost fumble).
        turnover = (
            play.get("interception") == 1
            or play.get("fumble_lost") == 1
        )

        if not turnover:
            continue

        context = play_context(play)

        #
        # Solo tackles
        #

        for column in SOLO_TACKLE_COLUMNS:
            player_id = play.get(column)

            if pd.isna(player_id):
                continue

            if player_id in offensive_ids:
                result[player_id]["solo"] += 1

                result[player_id]["plays"].append(
                    {
                        **context,
                        "type": "solo",
                    }
                )

        #
        # Assisted tackles
        #

        for column in ASSIST_TACKLE_COLUMNS:
            player_id = play.get(column)

            if pd.isna(player_id):
                continue

            if player_id in offensive_ids:
                result[player_id]["assists"] += 1

                result[player_id]["plays"].append(
                    {
                        **context,
                        "type": "assist",
                    }
                )

    return result


# =============================================================================
# FTN drops
# =============================================================================

def load_ftn(season):
    """
    Load FTN charting through nflverse.

    FTN supplies the is_drop flag.

    This data can lag the actual game by roughly a couple of days,
    so this script is intended as a post-week scoring audit rather
    than a real-time scoring system.
    """

    print(
        f"Loading FTN charting for {season}..."
    )

    ftn = nfl.import_ftn_data(
        [season],
        columns=[
            "nflverse_game_id",
            "nflverse_play_id",
            "is_drop",
        ],
        downcast=True,
    )

    print(
        f"Loaded {len(ftn):,} FTN charted plays."
    )

    return ftn


def get_week_schedule(season, week):
    """
    Return the regular-season schedule rows for a single week, providing the
    authoritative list of games that must be charted before the week can be
    scored.
    """

    schedule = nfl.import_schedules([season])

    return schedule[
        (schedule["week"] == week)
        & (schedule["game_type"] == "REG")
    ]


def assert_week_fully_charted(season, week, ftn):
    """
    Drops are only trustworthy once FTN has charted every game in the week.
    FTN charts each play within ~48h of a game, so a mid-week run (or one made
    the day after a slate) can silently miss games and under-award drop bonuses.

    Compare the week's scheduled games against the game IDs present in the FTN
    data. If a single game is missing, print the offenders and exit non-zero so
    the audit is never run against a partial week.
    """

    games = get_week_schedule(season, week)

    scheduled_ids = set(games["game_id"])

    if not scheduled_ids:
        print(
            f"WARNING: no regular-season games scheduled for "
            f"{season} Week {week}; skipping FTN coverage check."
        )
        return

    charted_ids = set(
        ftn["nflverse_game_id"]
        .dropna()
        .astype(str)
    )

    missing = [
        row
        for _, row in games.iterrows()
        if row["game_id"] not in charted_ids
    ]

    if missing:
        print()
        print("=" * 80)
        print(
            f"FTN DROP DATA INCOMPLETE FOR {season} WEEK {week}"
        )
        print("=" * 80)
        print(
            f"{len(missing)} of {len(scheduled_ids)} games "
            f"are not yet charted:"
        )

        for row in missing:
            print(
                f"  - {row['away_team']} @ {row['home_team']} "
                f"({row['game_id']})"
            )

        print()
        print(
            "FTN charts each play within ~48h of a game. "
            "Re-run once every game above is charted."
        )

        sys.exit(1)

    print(
        f"FTN drop data complete: "
        f"all {len(scheduled_ids)} games charted."
    )


def find_drops(pbp, ftn, starters):
    """
    FTN identifies whether the pass was dropped.

    nflverse PBP identifies the intended receiver.

    We join them using nflverse's game/play IDs and then only retain
    receivers who were actually STARTED in the fantasy league.
    """

    drops = ftn[
        ftn["is_drop"]
        .fillna(False)
        .astype(bool)
    ].copy()

    pbp_receivers = pbp[
        [
            "game_id",
            "play_id",
            "receiver_player_id",
            "qtr",
            "time",
            "home_team",
            "away_team",
            "desc",
        ]
    ].copy()

    merged = drops.merge(
        pbp_receivers,
        left_on=[
            "nflverse_game_id",
            "nflverse_play_id",
        ],
        right_on=[
            "game_id",
            "play_id",
        ],
        how="inner",
    )

    results = defaultdict(
        lambda: {
            "count": 0,
            "plays": [],
        }
    )

    starter_ids = set(
        starters.keys()
    )

    for _, play in merged.iterrows():
        receiver_id = play.get(
            "receiver_player_id"
        )

        if pd.isna(receiver_id):
            continue

        if receiver_id not in starter_ids:
            continue

        results[receiver_id]["count"] += 1

        results[receiver_id]["plays"].append(
            {
                "game_id": play.get("game_id"),
                "play_id": play.get("play_id"),
                "qtr": play.get("qtr"),
                "time": play.get("time"),
                "away_team": play.get("away_team"),
                "home_team": play.get("home_team"),
                "desc": play.get("desc"),
            }
        )

    return results


# =============================================================================
# Red-zone turnovers
# =============================================================================

def find_redzone_turnovers(pbp, starters):
    """
    +5 to every started player involved in a turnover inside the opponent's
    20-yard line (yardline_100 <= 20):

      - the player who committed it (QB on an interception, or the fumbler),
      - the defender who intercepted it,
      - the defender who forced the fumble,
      - the defender who recovered the fumble.

    A single play can award several players (offense + defense). A player who
    fills two roles on one play (e.g. forced and recovered) still scores +5
    once for that play, with both roles noted.

    Returns:
        gsis_id -> {count, plays: [{**context, role}]}
    """

    result = defaultdict(
        lambda: {
            "count": 0,
            "plays": [],
        }
    )

    redzone = pbp[
        (pbp["yardline_100"] <= REDZONE_YARDLINE)
        & (
            (pbp["interception"] == 1)
            | (pbp["fumble_lost"] == 1)
        )
    ]

    for _, play in redzone.iterrows():
        context = play_context(play)

        # Collect every started player's role on this play, deduped so a
        # player scores once per turnover even with multiple roles.
        roles = {}

        def add_role(player_id, role):
            if pd.isna(player_id) or player_id not in starters:
                return
            roles.setdefault(player_id, []).append(role)

        if play.get("interception") == 1:
            add_role(
                play.get("passer_player_id"),
                "committed turnover (interception thrown)",
            )
            add_role(
                play.get("interception_player_id"),
                "interception",
            )

        if play.get("fumble_lost") == 1:
            add_role(
                play.get("fumbled_1_player_id"),
                "committed turnover (fumble lost)",
            )
            add_role(
                play.get("forced_fumble_player_1_player_id"),
                "forced fumble",
            )
            add_role(
                play.get("fumble_recovery_1_player_id"),
                "fumble recovery",
            )

        for player_id, player_roles in roles.items():
            result[player_id]["count"] += 1
            result[player_id]["plays"].append({
                **context,
                "role": " + ".join(player_roles),
                "yrdln": play.get("yrdln"),
                "yardline_100": play.get("yardline_100"),
            })

    return result


# =============================================================================
# Invalid roster spots
# =============================================================================

def load_snap_share(season, week):
    """
    Load offensive snap share (offense_pct, a 0.0-1.0 fraction) for the week.

    Returns:
        pfr_player_id -> offense_pct
    """

    print(
        f"Loading snap counts for {season}..."
    )

    snaps = nfl.import_snap_counts([season])

    snaps = snaps[
        snaps["week"] == week
    ]

    result = {}

    for _, row in snaps.iterrows():
        pfr_id = row.get("pfr_player_id")

        if pd.isna(pfr_id):
            continue

        pct = row.get("offense_pct")

        result[str(pfr_id)] = (
            0.0 if pd.isna(pct) else float(pct)
        )

    print(
        f"Loaded snap share for {len(result)} players."
    )

    return result


def compute_touches(pbp):
    """
    Count ball touches per player from play-by-play.

    A touch is a rushing attempt (player is the rusher) or a completed
    reception (player is the receiver on a completed pass).

    Returns:
        gsis_id -> touch count
    """

    touches = defaultdict(int)

    for _, play in pbp.iterrows():
        rusher_id = play.get("rusher_player_id")

        if not pd.isna(rusher_id):
            touches[rusher_id] += 1

        if play.get("complete_pass") == 1:
            receiver_id = play.get("receiver_player_id")

            if not pd.isna(receiver_id):
                touches[receiver_id] += 1

    return touches


def load_injured_out(season, week):
    """
    Players ruled "Out" or "Doubtful" on the week's injury report are unlikely
    to have played (or played a full complement of snaps) due to injury and
    must be exempt from the invalid-roster-spot penalty.

    Returns:
        gsis_id -> reason string
    """

    print(
        f"Loading injury report for {season}..."
    )

    injuries = nfl.import_injuries([season])

    injuries = injuries[
        (injuries["week"] == week)
        & (injuries["report_status"].isin(["Out", "Doubtful"]))
    ]

    result = {}

    for _, row in injuries.iterrows():
        gsis_id = row.get("gsis_id")

        if pd.isna(gsis_id):
            continue

        status = row.get("report_status")
        injury = row.get("report_primary_injury")

        reason = f"ruled {status}"
        if not pd.isna(injury):
            reason = f"{reason} ({injury})"

        result[str(gsis_id).strip()] = reason

    print(
        f"Loaded {len(result)} players ruled Out/Doubtful."
    )

    return result


def find_ingame_injuries(pbp, starters):
    """
    Best-effort detection of players hurt during their game: scan play
    descriptions for injury language and match a started player's last name.
    Used only to exempt them from the invalid-roster-spot penalty.

    Returns:
        gsis_id -> reason string
    """

    mask = (
        pbp["desc"]
        .fillna("")
        .str.contains(
            "was injured|is injured|injured on the play|carted",
            case=False,
            regex=True,
        )
    )

    injured = {}

    for _, play in pbp[mask].iterrows():
        description = str(play.get("desc")).lower()

        for gsis_id, player in starters.items():
            name = player.get("name") or ""
            parts = name.split()

            if not parts:
                continue

            last_name = parts[-1].lower()

            if len(last_name) < 3:
                continue

            if last_name in description:
                injured.setdefault(
                    gsis_id,
                    "left game (injury noted in play-by-play)",
                )

    return injured


def resolve_exempt_players(tokens, starters):
    """
    Resolve manual --exempt-invalid tokens (gsis id or full name) to gsis ids.
    Used to exempt players the commissioner is handling separately (e.g. a
    benched player awarded +20 under the benching rule).

    Returns:
        gsis_id -> reason string
    """

    result = {}

    if not tokens:
        return result

    by_name = {
        (player.get("name") or "").lower(): gsis_id
        for gsis_id, player in starters.items()
    }

    for token in tokens:
        stripped = token.strip()

        if stripped in starters:
            result[stripped] = "manual exempt (e.g. benched)"
        elif stripped.lower() in by_name:
            result[by_name[stripped.lower()]] = (
                "manual exempt (e.g. benched)"
            )
        else:
            print(
                f"WARNING: --exempt-invalid '{token}' did not match "
                f"a started player; ignoring."
            )

    return result


def find_invalid_roster_spots(
    pbp,
    starters,
    snap_share,
    gsis_to_pfr,
    exempt=None,
):
    """
    An invalid roster spot is a started OFFENSIVE skill player who played
    fewer than 15% of offensive snaps AND recorded no touches.

    Players with no snap record at all (inactive / bye) are treated as 0%
    snaps, so starting a player who didn't suit up is invalid.

    Non-offensive positions (K, DEF, IDP) are exempt: touches and offensive
    snap share don't describe their contribution.

    exempt (optional):
        gsis_id -> reason. A would-be-invalid player in this map is NOT
        penalized (injured or benched); it is returned separately for
        transparency instead.

    Returns:
        (invalid, exempted) where
          invalid:  gsis_id -> {snap_pct, touches}          (penalized)
          exempted: gsis_id -> {snap_pct, touches, reason}  (not penalized)
    """

    exempt = exempt or {}

    touches = compute_touches(pbp)

    invalid = {}
    exempted = {}

    for gsis_id, player in starters.items():
        if player["position"] not in OFFENSIVE_POSITIONS:
            continue

        pfr_id = gsis_to_pfr.get(gsis_id)

        snap_pct = snap_share.get(pfr_id, 0.0)

        touch_count = touches.get(gsis_id, 0)

        if (
            snap_pct < INVALID_SNAP_THRESHOLD
            and touch_count == 0
        ):
            data = {
                "snap_pct": snap_pct,
                "touches": touch_count,
            }

            if gsis_id in exempt:
                exempted[gsis_id] = {
                    **data,
                    "reason": exempt[gsis_id],
                }
            else:
                invalid[gsis_id] = data

    return invalid, exempted


# =============================================================================
# Review candidates (flagged for the commissioner, never auto-scored)
# =============================================================================

def find_ejection_candidates(pbp, starters):
    """
    Rule #4 (+20): player ejected from the game.

    There is no structured ejection field in PBP, so scan play descriptions
    for "ejected"/"disqualified" and flag any started player whose last name
    appears in that description. This is a noisy, review-only signal.
    """

    mask = (
        pbp["desc"]
        .fillna("")
        .str.contains("ejected|disqualified", case=False, regex=True)
    )

    candidates = []

    for _, play in pbp[mask].iterrows():
        description = str(play.get("desc")).lower()

        for gsis_id, player in starters.items():
            name = player.get("name") or ""
            parts = name.split()

            if not parts:
                continue

            last_name = parts[-1].lower()

            # require a reasonably distinctive last name to limit false hits
            if len(last_name) < 3:
                continue

            if last_name in description:
                candidates.append({
                    "gsis_id": gsis_id,
                    "name": player["name"],
                    "position": player["position"],
                    "roster_id": player["roster_id"],
                    **play_context(play),
                })

    return candidates


def find_goal_line_fumble_candidates(pbp, starters, goal_line_yards=5):
    """
    Rule #5 (+35): dropping the ball before crossing the goal line in
    celebration. Explicitly excludes forced fumbles.

    Detecting this needs the fumble LOCATION, not the snap spot. yardline_100
    is where the play started, so a long touchdown run that ends in a
    goal-line fumble still shows a large yardline_100. Approximate the fumble
    spot as yardline_100 - yards_gained, and also flag the canonical outcome:
    an unforced fumble that goes out of the back of the end zone for a
    touchback.

    Still a review-only signal (QB aborted snaps also surface).
    """

    candidates = []

    for _, play in pbp.iterrows():
        if play.get("fumble_not_forced") != 1:
            continue

        fumbler_id = play.get("fumbled_1_player_id")

        if pd.isna(fumbler_id) or fumbler_id not in starters:
            continue

        yardline = play.get("yardline_100")
        yards_gained = play.get("yards_gained")

        if not pd.isna(yardline) and not pd.isna(yards_gained):
            fumble_spot = yardline - yards_gained
        else:
            fumble_spot = yardline

        near_goal = (
            not pd.isna(fumble_spot)
            and fumble_spot <= goal_line_yards
        )

        through_end_zone = (
            play.get("touchback") == 1
            and play.get("fumble_out_of_bounds") == 1
        )

        if not (near_goal or through_end_zone):
            continue

        player = starters[fumbler_id]

        candidates.append({
            "gsis_id": fumbler_id,
            "name": player["name"],
            "position": player["position"],
            "roster_id": player["roster_id"],
            "fumble_spot": fumble_spot,
            "through_end_zone": through_end_zone,
            **play_context(play),
        })

    return candidates


# =============================================================================
# Report formatting
# =============================================================================

def safe_team(value):
    if value is None or pd.isna(value):
        return "?"

    return str(value)


def safe_time(value):
    if value is None or pd.isna(value):
        return "?:??"

    return str(value)


def safe_description(value):
    if value is None or pd.isna(value):
        return "(no play description available)"

    return str(value)


def safe_play_id(value):
    if value is None or pd.isna(value):
        return "?"

    try:
        return str(int(float(value)))
    except (TypeError, ValueError):
        return str(value)


def format_clock(qtr, time):
    if qtr is None or pd.isna(qtr):
        return safe_time(time)

    try:
        quarter = int(float(qtr))
        return f"Q{quarter} {safe_time(time)}"

    except (TypeError, ValueError):
        return f"Q{qtr} {safe_time(time)}"


def format_play(play):
    away = safe_team(
        play.get("away_team")
    )

    home = safe_team(
        play.get("home_team")
    )

    matchup = f"{away} @ {home}"

    clock = format_clock(
        play.get("qtr"),
        play.get("time"),
    )

    description = safe_description(
        play.get("desc")
    )

    return (
        matchup,
        clock,
        description,
    )


def redzone_los_text(play):
    """
    Human-readable line of scrimmage for a red-zone turnover play, e.g.
    "KC 18". Falls back to distance-to-goal, then a generic label.
    """

    yrdln = play.get("yrdln")

    if isinstance(yrdln, str) and yrdln.strip():
        return yrdln.strip()

    yardline = play.get("yardline_100")

    if yardline is not None and not pd.isna(yardline):
        return f"opponent's {int(yardline)}-yard line"

    return "the red zone"


def print_report(
    week,
    starters,
    team_names,
    tackles,
    drops,
    invalid=None,
    exempt=None,
    redzone=None,
    count_assists=True,
):
    """
    Print both:

    1. Detailed evidence for each bonus.
    2. Commissioner-ready adjustment totals.
    """

    invalid = invalid or {}
    exempt = exempt or {}
    redzone = redzone or {}

    adjustments = defaultdict(int)

    def _print_exemptions():
        if not exempt:
            return

        print()
        print("-" * 80)
        print(
            "EXEMPT FROM INVALID ROSTER PENALTY "
            "(injury/benched — not scored)"
        )

        for gsis_id in sorted(
            exempt,
            key=lambda g: starters[g]["name"],
        ):
            player = starters[gsis_id]
            data = exempt[gsis_id]

            team_name = team_names.get(
                player["roster_id"],
                f"Roster {player['roster_id']}",
            )

            print(
                f"  {player['name']} ({player['position']}) "
                f"— {team_name}: {data['reason']} "
                f"({data['snap_pct']:.0%} snaps, "
                f"{data['touches']} touches)"
            )

    relevant_players = (
        set(tackles.keys())
        | set(drops.keys())
        | set(invalid.keys())
        | set(redzone.keys())
    )

    print()
    print("=" * 80)
    print(
        f"CHAOS LEAGUE WEEK {week} AUDIT"
    )
    print("=" * 80)

    if not relevant_players:
        print()
        print(
            "No offensive tackle, drop, or "
            "invalid-roster-spot adjustments found."
        )
        _print_exemptions()
        print()
        return

    sorted_players = sorted(
        relevant_players,
        key=lambda gsis_id:
            starters[gsis_id]["name"],
    )

    for gsis_id in sorted_players:
        player = starters[gsis_id]

        roster_id = player["roster_id"]

        team_name = team_names.get(
            roster_id,
            f"Roster {roster_id}",
        )

        tackle_data = tackles.get(
            gsis_id,
            {
                "solo": 0,
                "assists": 0,
                "plays": [],
            },
        )

        drop_data = drops.get(
            gsis_id,
            {
                "count": 0,
                "plays": [],
            },
        )

        redzone_data = redzone.get(
            gsis_id,
            {
                "count": 0,
                "plays": [],
            },
        )

        solo = tackle_data["solo"]
        assists = tackle_data["assists"]
        drop_count = drop_data["count"]
        redzone_count = redzone_data["count"]

        if count_assists:
            tackle_count = (
                solo + assists
            )
        else:
            tackle_count = solo

        tackle_bonus = (
            tackle_count
            * TACKLE_POINTS
        )

        drop_bonus = (
            drop_count
            * DROP_POINTS
        )

        redzone_bonus = (
            redzone_count
            * REDZONE_TURNOVER_POINTS
        )

        invalid_data = invalid.get(gsis_id)

        invalid_penalty = (
            INVALID_SPOT_POINTS
            if invalid_data
            else 0
        )

        total = (
            tackle_bonus
            + drop_bonus
            + redzone_bonus
            - invalid_penalty
        )

        if (
            tackle_bonus == 0
            and drop_bonus == 0
            and redzone_bonus == 0
            and not invalid_data
        ):
            continue

        adjustments[roster_id] += total

        print()
        print(
            f"{player['name']} "
            f"({player['position']}) "
            f"— {team_name}"
        )

        print("-" * 80)

        #
        # Tackles
        #

        for play in tackle_data["plays"]:
            tackle_type = play["type"]

            if (
                tackle_type == "assist"
                and not count_assists
            ):
                continue

            (
                matchup,
                clock,
                description,
            ) = format_play(play)

            if tackle_type == "solo":
                label = "OFFENSIVE TACKLE"
            else:
                label = (
                    "OFFENSIVE ASSISTED TACKLE"
                )

            print(
                f"  +{TACKLE_POINTS} {label}"
            )

            print(
                f"    {matchup} — {clock}"
            )

            print(
                f"    Play "
                f"{safe_play_id(play.get('play_id'))}"
            )

            print(
                f'    "{description}"'
            )

            print()

        #
        # Drops
        #

        for play in drop_data["plays"]:
            (
                matchup,
                clock,
                description,
            ) = format_play(play)

            print(
                f"  +{DROP_POINTS} DROPPED PASS"
            )

            print(
                f"    {matchup} — {clock}"
            )

            print(
                f"    Play "
                f"{safe_play_id(play.get('play_id'))}"
            )

            print(
                f'    "{description}"'
            )

            print()

        #
        # Red-zone turnovers
        #

        for play in redzone_data["plays"]:
            (
                matchup,
                clock,
                description,
            ) = format_play(play)

            print(
                f"  +{REDZONE_TURNOVER_POINTS} RED ZONE TURNOVER "
                f"({play['role']})"
            )

            print(
                f"    {matchup} — {clock}"
            )

            print(
                f"    Line of scrimmage: "
                f"{redzone_los_text(play)} (red zone)"
            )

            print(
                f"    Play "
                f"{safe_play_id(play.get('play_id'))}"
            )

            print(
                f'    "{description}"'
            )

            print()

        #
        # Invalid roster spot
        #

        if invalid_data:
            print(
                f"  -{INVALID_SPOT_POINTS} INVALID ROSTER SPOT"
            )

            print(
                f"    {invalid_data['snap_pct']:.0%} offensive snaps, "
                f"{invalid_data['touches']} touches "
                f"(< {INVALID_SNAP_THRESHOLD:.0%} snaps and no touches)"
            )

            print()

        #
        # Player summary
        #

        print(
            f"  TACKLES: "
            f"{solo} solo, "
            f"{assists} assisted"
        )

        print(
            f"  DROPS: {drop_count}"
        )

        print(
            f"  RED ZONE TURNOVERS: {redzone_count}"
        )

        if invalid_data:
            print(
                "  INVALID ROSTER SPOT: yes"
            )

        print(
            f"  TOTAL ADJUSTMENT: {total:+d}"
        )

    #
    # Commissioner totals
    #

    _print_exemptions()

    print()
    print("=" * 80)
    print(
        "COMMISSIONER ADJUSTMENTS"
    )
    print("=" * 80)

    total_league_adjustments = 0

    sorted_adjustments = sorted(
        adjustments.items(),
        key=lambda item:
            team_names.get(
                item[0],
                str(item[0]),
            ),
    )

    for roster_id, points in sorted_adjustments:
        team_name = team_names.get(
            roster_id,
            f"Roster {roster_id}",
        )

        total_league_adjustments += points

        print(
            f"{team_name:<40} "
            f"{points:+d}"
        )

    print("-" * 80)

    print(
        f"{'TOTAL CHAOS ADJUSTMENTS':<40} "
        f"{total_league_adjustments:+d}"
    )

    print()


def print_candidates(ejections, fumbles, team_names):
    """
    Print review-only candidates for rules that can't be scored
    automatically. These are NEVER added to any total — the commissioner
    confirms and applies points manually.
    """

    print()
    print("=" * 80)
    print(
        "REVIEW CANDIDATES "
        "(NOT SCORED — commissioner confirmation required)"
    )
    print("=" * 80)

    if not ejections and not fumbles:
        print()
        print(
            "No ejection or goal-line-celebration "
            "fumble candidates found."
        )
        print()
        return

    def _team(candidate):
        return team_names.get(
            candidate["roster_id"],
            f"Roster {candidate['roster_id']}",
        )

    if ejections:
        print()
        print("POSSIBLE EJECTIONS (+20, Rule #4):")

        for candidate in ejections:
            matchup, clock, description = format_play(candidate)

            print()
            print(
                f"  {candidate['name']} "
                f"({candidate['position']}) — {_team(candidate)}"
            )
            print(
                f"    {matchup} — {clock}  "
                f"Play {safe_play_id(candidate.get('play_id'))}"
            )
            print(f'    "{description}"')

    if fumbles:
        print()
        print(
            "POSSIBLE PREMATURE-CELEBRATION FUMBLES "
            "(+35, Rule #5 — excludes forced fumbles):"
        )

        for candidate in fumbles:
            matchup, clock, description = format_play(candidate)

            spot = candidate.get("fumble_spot")

            if candidate.get("through_end_zone"):
                spot_text = "fumbled through end zone (touchback)"
            elif pd.isna(spot):
                spot_text = "near goal line, unforced"
            else:
                spot_text = f"~{int(spot)} yds from goal, unforced"

            print()
            print(
                f"  {candidate['name']} "
                f"({candidate['position']}) — {_team(candidate)}"
            )
            print(
                f"    {matchup} — {clock}  ({spot_text})"
            )
            print(
                f"    Play {safe_play_id(candidate.get('play_id'))}"
            )
            print(f'    "{description}"')

    print()


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "League of Chaos weekly "
            "commissioner bonus audit"
        )
    )

    parser.add_argument(
        "--week",
        type=int,
        required=True,
        help="NFL regular-season week",
    )

    parser.add_argument(
        "--season",
        type=int,
        default=2026,
        help="NFL season (default: 2026)",
    )

    parser.add_argument(
        "--league-id",
        default=DEFAULT_LEAGUE_ID,
        help=(
            "Sleeper league ID "
            f"(default: {DEFAULT_LEAGUE_ID})"
        ),
    )

    parser.add_argument(
        "--solo-tackles-only",
        action="store_true",
        help=(
            "Only award +15 for solo offensive tackles. "
            "By default assisted offensive tackles also "
            "receive +15."
        ),
    )

    parser.add_argument(
        "--flag-candidates",
        action="store_true",
        help=(
            "Additionally print (never score) review candidates for "
            "ejections (Rule #4) and premature goal-line fumbles "
            "(Rule #5) involving started players."
        ),
    )

    parser.add_argument(
        "--exempt-invalid",
        nargs="*",
        default=[],
        metavar="PLAYER",
        help=(
            "Player gsis id or full name to exempt from the -15 invalid "
            "roster penalty (e.g. a benched player awarded +20 separately). "
            "Players ruled Out on the injury report are exempted "
            "automatically."
        ),
    )

    args = parser.parse_args()

    #
    # Sleeper
    #

    print(
        "Loading Sleeper NFL players..."
    )

    players = get_sleeper_players()

    print(
        f"Loaded {len(players):,} "
        f"Sleeper player records."
    )

    #
    # Make sure we're scoring the league object that actually holds the
    # requested season's rosters/lineups. Sleeper mints a new league_id per
    # season, so the default (current-season) id would otherwise pull the
    # wrong starters for a historical --season.
    #

    league_id, league = resolve_league_for_season(
        args.league_id,
        args.season,
    )

    if league is not None:
        print(
            f"Using league {league_id} "
            f"(\"{league.get('name')}\", season {league.get('season')})."
        )
    else:
        print(
            f"WARNING: no league in the chain matched season "
            f"{args.season}; using {league_id} as-is. Starters may not "
            f"match the scored season."
        )

    #
    # nflverse ID crosswalk (recovers gsis_id when Sleeper omits it).
    #

    print(
        "Loading nflverse ID crosswalk..."
    )

    crosswalk = build_sleeper_gsis_crosswalk()

    print(
        f"Loaded {len(crosswalk):,} "
        f"sleeper->gsis mappings."
    )

    print(
        f"Loading Week {args.week} starters..."
    )

    starters = get_starters(
        league_id,
        args.week,
        players,
        crosswalk,
    )

    print(
        f"Found {len(starters)} starters "
        f"with GSIS IDs."
    )

    team_names = get_team_names(
        league_id
    )

    #
    # nflverse PBP
    #

    pbp = load_pbp(
        args.season,
        args.week,
    )

    tackles = find_offensive_tackles(
        pbp,
        starters,
    )

    #
    # FTN charting
    #

    ftn = load_ftn(
        args.season,
    )

    #
    # Refuse to score a partial week: every scheduled game must be charted
    # by FTN or the drop totals would be silently incomplete.
    #

    assert_week_fully_charted(
        args.season,
        args.week,
        ftn,
    )

    drops = find_drops(
        pbp,
        ftn,
        starters,
    )

    redzone = find_redzone_turnovers(
        pbp,
        starters,
    )

    #
    # Invalid roster spots (snap share + touches)
    #

    gsis_to_pfr = build_gsis_pfr_crosswalk()

    snap_share = load_snap_share(
        args.season,
        args.week,
    )

    #
    # Build invalid-spot exemptions: injured (auto) + benched/manual.
    #

    exempt = {}

    for gsis_id, reason in load_injured_out(
        args.season, args.week
    ).items():
        if gsis_id in starters:
            exempt[gsis_id] = f"injury: {reason}"

    for gsis_id, reason in find_ingame_injuries(
        pbp, starters
    ).items():
        exempt.setdefault(gsis_id, f"injury: {reason}")

    for gsis_id, reason in resolve_exempt_players(
        args.exempt_invalid, starters
    ).items():
        exempt.setdefault(gsis_id, reason)

    invalid, exempted = find_invalid_roster_spots(
        pbp,
        starters,
        snap_share,
        gsis_to_pfr,
        exempt=exempt,
    )

    #
    # Report
    #

    print_report(
        week=args.week,
        starters=starters,
        team_names=team_names,
        tackles=tackles,
        drops=drops,
        invalid=invalid,
        exempt=exempted,
        redzone=redzone,
        count_assists=(
            not args.solo_tackles_only
        ),
    )

    #
    # Optional review candidates (printed, never scored)
    #

    if args.flag_candidates:
        ejections = find_ejection_candidates(
            pbp,
            starters,
        )

        fumbles = find_goal_line_fumble_candidates(
            pbp,
            starters,
        )

        print_candidates(
            ejections,
            fumbles,
            team_names,
        )


if __name__ == "__main__":
    main()