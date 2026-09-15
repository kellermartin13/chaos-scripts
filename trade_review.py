#!/usr/bin/env python3

"""
Dynasty trade review.

⚠️  WORK IN PROGRESS — this tool is unfinished and not yet verified against
known-good results. Output may be incomplete or incorrect (e.g. draft-pick
resolution and since-trade windows are still being validated). Do not rely on
its verdicts for league decisions yet.

Walks a Sleeper dynasty league's full history (every season's league object,
linked by previous_league_id), pulls every completed trade, and reports how the
assets each side received have performed *since the trade* — including traded
draft picks resolved to the players they actually became.

Production is scored with the league's own scoring settings (a dot product of
each player's weekly Sleeper stat line and the league scoring_settings), so the
"who won the trade" verdict reflects real league points, not a generic PPR line
— it even honors quirks like TE-premium receiving.

Everything comes from the public Sleeper API; no authentication and no nflverse
dependency are required for this feature.
"""

import argparse
import sys
from collections import defaultdict

import requests

from sleeper_cache import cached_players


# Printed at startup so anyone running the tool sees it is unverified.
WIP_DISCLAIMER = (
    "WORK IN PROGRESS: trade_review.py is unfinished and unverified. "
    "Its trade verdicts may be incomplete or incorrect — do not rely on "
    "them for league decisions yet."
)


# =============================================================================
# Configuration
# =============================================================================

SLEEPER_BASE = "https://api.sleeper.app/v1"

# 2026 season league; the chain walks back through every prior season.
DEFAULT_LEAGUE_ID = "1312092927635259392"

# NFL regular-season weeks to sum production over. Sleeper keys weekly stats by
# NFL week; weeks a player didn't play simply contribute nothing.
REGULAR_SEASON_WEEKS = range(1, 19)

# Guard against a malformed/looping previous_league_id chain.
MAX_CHAIN_DEPTH = 40

# A trade is flagged "lopsided" when the winning side out-produced the runner-up
# by at least LOPSIDED_RATIO x (and "heist" at HEIST_RATIO x). The absolute
# margin gate keeps trivially small trades from being flagged on ratio alone.
LOPSIDED_MIN_MARGIN = 50.0
LOPSIDED_RATIO = 2.0
HEIST_RATIO = 3.0


# =============================================================================
# HTTP
# =============================================================================

def get_json(url):
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


# =============================================================================
# Sleeper client
# =============================================================================

def get_league(league_id):
    return get_json(f"{SLEEPER_BASE}/league/{league_id}")


def get_users(league_id):
    return get_json(f"{SLEEPER_BASE}/league/{league_id}/users")


def get_rosters(league_id):
    return get_json(f"{SLEEPER_BASE}/league/{league_id}/rosters")


def get_transactions(league_id, week):
    return get_json(
        f"{SLEEPER_BASE}/league/{league_id}/transactions/{week}"
    )


def get_draft(draft_id):
    """
    Draft metadata. The key field for us is slot_to_roster_id, which maps each
    draft slot to the roster that *originally* owns it (by draft order). This
    lets us recover a pick's original owner even after the pick was traded.
    """

    return get_json(f"{SLEEPER_BASE}/draft/{draft_id}")


def get_draft_picks(draft_id):
    return get_json(f"{SLEEPER_BASE}/draft/{draft_id}/picks")


def get_players(use_cache=True, client=None):
    """
    Sleeper's NFL player map: sleeper player_id -> player metadata.

    Served from the shared local Redis cache (24h TTL) via sleeper_cache, with
    a transparent fall back to a live fetch when Redis is unavailable.
    """

    return cached_players(
        lambda: get_json(f"{SLEEPER_BASE}/players/nfl"),
        use_cache=use_cache,
        client=client,
    )


def get_weekly_stats(season, week):
    """
    Per-player stat lines for one NFL week, keyed by Sleeper player_id.

    Each value is a dict of stat categories (pass_yd, rec, rec_td, ...) whose
    keys align exactly with a league's scoring_settings keys, plus Sleeper's
    own precomputed pts_ppr / pts_half_ppr / pts_std and a gp (games played)
    flag.

    Returns {} for a week Sleeper has no data for (e.g. a future week).
    """

    data = get_json(
        f"{SLEEPER_BASE}/stats/nfl/regular/{season}/{week}"
    )

    return data or {}


# =============================================================================
# Dynasty chain
# =============================================================================

def build_league_chain(league_id, fetch=get_league):
    """
    Sleeper mints a new league object every season and links them with
    previous_league_id (newest -> oldest). Walk that chain and return the list
    of league objects, newest first.

    fetch is injectable for testing.
    """

    chain = []
    visited = set()
    current = league_id

    while current and current not in visited and len(chain) < MAX_CHAIN_DEPTH:
        visited.add(current)

        league = fetch(current)

        if not league:
            break

        chain.append(league)

        current = league.get("previous_league_id")

    return chain


def index_chain(chain):
    """
    Build convenient season-keyed lookups from a league chain:

        {
            "leagues":   {season: league_object},
            "scoring":   {season: scoring_settings},
            "draft_ids": {season: draft_id},
            "seasons":   [ascending list of season strings present],
        }
    """

    leagues = {}
    scoring = {}
    draft_ids = {}

    for league in chain:
        season = str(league.get("season"))

        leagues[season] = league
        scoring[season] = league.get("scoring_settings") or {}
        draft_ids[season] = league.get("draft_id")

    seasons = sorted(leagues.keys())

    return {
        "leagues": leagues,
        "scoring": scoring,
        "draft_ids": draft_ids,
        "seasons": seasons,
    }


def format_team_label(user, roster_id):
    """
    Build a display label that pairs the (season-specific) team name with the
    owner's stable username/handle, since dynasty team names change over time
    but the owner does not, e.g. "RB FACTORY (macflyguy)".

    Falls back gracefully: handle-only when there's no team name, team-only
    when there's no handle, and "Roster N" when the owner is unknown.

    (Sleeper's user object often leaves `username` null in the league users
    feed; `display_name` is then the stable handle, so we prefer whichever is
    present.)
    """

    user = user or {}

    handle = user.get("username") or user.get("display_name")
    team_name = (user.get("metadata") or {}).get("team_name")

    if handle and team_name:
        return f"{team_name} ({handle})"

    if team_name:
        return team_name

    if handle:
        return handle

    return f"Roster {roster_id}"


def team_names_for_league(league_id):
    """
    Build roster_id -> "Team Name (username)" label for a single season's
    league object.
    """

    users = {
        user["user_id"]: user
        for user in get_users(league_id)
    }

    result = {}

    for roster in get_rosters(league_id):
        roster_id = roster["roster_id"]
        user = users.get(roster.get("owner_id"), {})

        result[roster_id] = format_team_label(user, roster_id)

    return result


def build_team_names_by_season(chain, resolver=team_names_for_league):
    """
    roster_id -> name can differ per season, so resolve it per league.

    Returns {season: {roster_id: name}}. resolver is injectable for testing.
    """

    result = {}

    for league in chain:
        season = str(league.get("season"))
        result[season] = resolver(league["league_id"])

    return result


def build_manager_directory(
    chain, users_fetch=get_users, rosters_fetch=get_rosters
):
    """
    Managers are the stable identity across a dynasty — roster_ids and team
    names change season to season, but the owning user does not. Build the
    lookups needed to aggregate trade outcomes by manager:

        {
            "owner_by_season_roster": {(season, roster_id): owner_id},
            "names": {owner_id: "Team Name (username)"},
        }

    The display label is taken from the most recent season the manager appears
    in (the chain is newest-first), so it reflects their current team + handle.

    users_fetch(league_id) and rosters_fetch(league_id) are injectable for
    testing.
    """

    owner_by_season_roster = {}
    names = {}

    for league in chain:  # newest -> oldest
        season = str(league.get("season"))
        league_id = league["league_id"]

        users = {
            user["user_id"]: user
            for user in users_fetch(league_id)
        }

        for roster in rosters_fetch(league_id):
            roster_id = roster["roster_id"]
            owner_id = roster.get("owner_id")

            owner_by_season_roster[(season, roster_id)] = owner_id

            if owner_id and owner_id not in names:
                names[owner_id] = format_team_label(
                    users.get(owner_id, {}), roster_id
                )

    return {
        "owner_by_season_roster": owner_by_season_roster,
        "names": names,
    }


# =============================================================================
# Trades
# =============================================================================

def collect_trades(chain, fetch=get_transactions, weeks=REGULAR_SEASON_WEEKS):
    """
    Pull every completed trade across every season in the chain.

    Sleeper files trades under a scoring "leg"/week (offseason trades land in
    week 1). Fetch each week's transactions, keep type == "trade", attach the
    season and week the trade was processed in, dedup by transaction_id, and
    return them sorted oldest -> newest by status_updated.

    fetch(league_id, week) is injectable for testing.
    """

    trades = {}

    for league in chain:
        league_id = league["league_id"]
        season = str(league.get("season"))

        for week in weeks:
            for txn in fetch(league_id, week) or []:
                if txn.get("type") != "trade":
                    continue

                if txn.get("status") != "complete":
                    continue

                transaction_id = txn.get("transaction_id")

                if transaction_id in trades:
                    continue

                trades[transaction_id] = {
                    **txn,
                    "season": season,
                    "week": week,
                }

    return sorted(
        trades.values(),
        key=lambda t: t.get("status_updated") or 0,
    )


# =============================================================================
# Draft pick resolution
# =============================================================================

def build_pick_index(chain, picks_fetch=get_draft_picks, draft_fetch=get_draft):
    """
    Map each drafted pick to the player it became.

    A traded draft_pick identifies itself by (season, round, roster_id) where
    roster_id is the pick's *original* owner. Sleeper's draft picks endpoint,
    however, stamps each selection with the roster that actually *made* it
    (the owner at draft time, after any pick trades) — not the original owner.
    The stable link back to the original owner is the draft slot: every pick
    carries a draft_slot, and the draft's slot_to_roster_id maps that slot to
    its original (draft-order) owner.

    So we translate each selection's draft_slot to the original owner and key
    the index by (season, round, original_owner_roster_id), which is exactly
    the tuple a traded draft_pick carries.

    Returns:
        {(season, round, roster_id): {player_id, name, position}}

    picks_fetch(draft_id) and draft_fetch(draft_id) are injectable for testing.
    """

    index = {}

    for league in chain:
        season = str(league.get("season"))
        draft_id = league.get("draft_id")

        if not draft_id:
            continue

        draft = draft_fetch(draft_id) or {}
        slot_to_roster = draft.get("slot_to_roster_id") or {}

        for pick in picks_fetch(draft_id) or []:
            slot = pick.get("draft_slot")

            # Recover the pick's original owner from its slot; fall back to the
            # drafting roster only if the slot mapping is unavailable.
            original_owner = slot_to_roster.get(str(slot))
            if original_owner is None:
                original_owner = pick.get("roster_id")

            key = (
                season,
                pick.get("round"),
                original_owner,
            )

            metadata = pick.get("metadata") or {}

            name = " ".join(
                part
                for part in [
                    metadata.get("first_name"),
                    metadata.get("last_name"),
                ]
                if part
            ) or None

            index[key] = {
                "player_id": pick.get("player_id"),
                "name": name,
                "position": metadata.get("position"),
            }

    return index


def resolve_pick(pick, pick_index, players=None):
    """
    Resolve a traded draft pick to the asset it represents.

    If the pick's draft has happened, return the drafted player (kind="pick",
    resolved=True). Otherwise (a future season with no draft yet, or a pick
    with no matching selection) return an unresolved placeholder describing the
    pick (kind="pick", resolved=False).
    """

    season = str(pick.get("season"))
    rnd = pick.get("round")
    roster_id = pick.get("roster_id")

    label = f"{season} Round {rnd} pick"

    key = (season, rnd, roster_id)
    hit = pick_index.get(key)

    if not hit or not hit.get("player_id"):
        return {
            "kind": "pick",
            "resolved": False,
            "label": f"{label} (pending — no draft selection yet)",
            "player_id": None,
            "name": None,
            "position": None,
            "season": season,
            "round": rnd,
        }

    name = hit.get("name")
    position = hit.get("position")

    # Prefer the canonical player map for name/position when available.
    if players:
        player = players.get(hit["player_id"])
        if player:
            name = (
                player.get("full_name")
                or name
                or hit["player_id"]
            )
            position = player.get("position") or position

    return {
        "kind": "pick",
        "resolved": True,
        "label": f"{label} \u2192 {name or hit['player_id']}",
        "player_id": hit["player_id"],
        "name": name or hit["player_id"],
        "position": position,
        "season": season,
        "round": rnd,
    }


def resolve_player(player_id, players):
    """
    Resolve a traded player_id to a display asset (kind="player").
    """

    player = players.get(player_id) or {}

    name = (
        player.get("full_name")
        or " ".join(
            part
            for part in [
                player.get("first_name"),
                player.get("last_name"),
            ]
            if part
        )
        or player_id
    )

    return {
        "kind": "player",
        "resolved": True,
        "label": name,
        "player_id": player_id,
        "name": name,
        "position": player.get("position"),
    }


# =============================================================================
# League-accurate scoring
# =============================================================================

def score_stat_line(stat_line, scoring_settings):
    """
    League points for one weekly stat line = dot product of the player's stats
    and the league scoring settings. Stat keys with no scoring rule (and rules
    with no matching stat) contribute nothing.
    """

    return sum(
        value * scoring_settings[key]
        for key, value in stat_line.items()
        if key in scoring_settings
        and isinstance(value, (int, float))
    )


class WeeklyStatsCache:
    """
    Lazy, memoized accessor for weekly stats so a player-heavy report fetches
    each (season, week) slate at most once.
    """

    def __init__(self, fetch=get_weekly_stats):
        self._fetch = fetch
        self._cache = {}

    def get(self, season, week):
        key = (str(season), week)

        if key not in self._cache:
            self._cache[key] = self._fetch(season, week) or {}

        return self._cache[key]


def compute_production_since(
    player_id,
    start_season,
    start_week,
    chain_index,
    stats_cache,
    weeks=REGULAR_SEASON_WEEKS,
):
    """
    Total league points a player has produced since a trade.

    Counting window (inclusive of the trade's filed week):
      - trade season: weeks >= start_week
      - every later season present in the chain: all weeks

    Offseason trades are filed under week 1, so an inclusive window correctly
    captures the entire season for them; an in-season trade counts from the
    week it was processed forward.

    Returns {total, games, per_week: [{season, week, points}]}.
    """

    start_season = str(start_season)

    seasons = [
        season
        for season in chain_index["seasons"]
        if season >= start_season
    ]

    total = 0.0
    games = 0
    per_week = []

    for season in seasons:
        scoring = chain_index["scoring"].get(season) or {}

        first_week = start_week if season == start_season else 1

        for week in weeks:
            if week < first_week:
                continue

            slate = stats_cache.get(season, week)
            stat_line = slate.get(player_id)

            if not stat_line:
                continue

            points = score_stat_line(stat_line, scoring)

            if stat_line.get("gp"):
                games += 1

            total += points

            per_week.append(
                {
                    "season": season,
                    "week": week,
                    "points": points,
                }
            )

    return {
        "total": round(total, 2),
        "games": games,
        "per_week": per_week,
    }


def score_asset(asset, trade, chain_index, stats_cache):
    """
    Attach production-since-trade to a resolved asset. Unresolved picks (future
    drafts) and FAAB carry no production.

    Returns the asset augmented with a "production" dict (or None).
    """

    if not asset.get("resolved") or not asset.get("player_id"):
        return {**asset, "production": None}

    production = compute_production_since(
        asset["player_id"],
        trade["season"],
        trade["week"],
        chain_index,
        stats_cache,
    )

    return {**asset, "production": production}


# =============================================================================
# Trade review assembly
# =============================================================================

def received_assets(trade, players, pick_index):
    """
    Group the assets each roster *received* in a trade.

      - players: adds maps player_id -> receiving roster_id
      - picks:   each draft_pick's owner_id is the receiving roster_id
      - FAAB:    each waiver_budget entry moves amount from sender to receiver

    Returns {roster_id: {"assets": [...], "faab_in": int}}.
    """

    sides = defaultdict(lambda: {"assets": [], "faab_in": 0})

    # Ensure every participating roster shows up even if it only sent assets.
    for roster_id in trade.get("roster_ids") or []:
        _ = sides[roster_id]

    for player_id, roster_id in (trade.get("adds") or {}).items():
        sides[roster_id]["assets"].append(
            resolve_player(player_id, players)
        )

    for pick in trade.get("draft_picks") or []:
        roster_id = pick.get("owner_id")
        sides[roster_id]["assets"].append(
            resolve_pick(pick, pick_index, players)
        )

    for entry in trade.get("waiver_budget") or []:
        receiver = entry.get("receiver")
        amount = entry.get("amount") or 0
        sides[receiver]["faab_in"] += amount

    return dict(sides)


def side_total(scored_assets):
    """
    Sum production across a side's resolved assets.
    """

    return round(
        sum(
            asset["production"]["total"]
            for asset in scored_assets
            if asset.get("production")
        ),
        2,
    )


def verdict(sides_totals):
    """
    Given {roster_id: total_points}, return (winner_roster_id, margin).

    A tie (or a trade with no scored production yet) returns (None, 0.0).
    """

    if not sides_totals:
        return None, 0.0

    ordered = sorted(
        sides_totals.items(),
        key=lambda item: item[1],
        reverse=True,
    )

    best_roster, best_points = ordered[0]

    if len(ordered) == 1:
        return (best_roster, 0.0) if best_points else (None, 0.0)

    _, second_points = ordered[1]
    margin = round(best_points - second_points, 2)

    if margin == 0:
        return None, 0.0

    return best_roster, margin


def assess_lopsidedness(sides_totals, winner_roster, margin):
    """
    Classify how lopsided a trade turned out, based on the winner's production
    relative to the runner-up.

    Returns "heist" (>= 3x), "lopsided" (>= 2x), or None. A minimum absolute
    margin gate keeps trivially small trades (e.g. 1.1 vs 0.0) from being
    flagged just because the ratio is large.
    """

    if winner_roster is None or margin < LOPSIDED_MIN_MARGIN:
        return None

    ordered = sorted(sides_totals.values(), reverse=True)

    if not ordered:
        return None

    winner_pts = ordered[0]
    runner_up = ordered[1] if len(ordered) > 1 else 0.0

    if winner_pts <= 0:
        return None

    ratio = winner_pts / runner_up if runner_up > 0 else float("inf")

    if ratio >= HEIST_RATIO:
        return "heist"

    if ratio >= LOPSIDED_RATIO:
        return "lopsided"

    return None


def build_trade_review(trade, players, pick_index, chain_index, stats_cache):
    """
    Assemble a single reviewed trade: each side's scored assets, side totals,
    and the winner verdict.
    """

    sides = received_assets(trade, players, pick_index)

    reviewed_sides = {}
    totals = {}

    for roster_id, side in sides.items():
        scored = [
            score_asset(asset, trade, chain_index, stats_cache)
            for asset in side["assets"]
        ]

        reviewed_sides[roster_id] = {
            "assets": scored,
            "faab_in": side["faab_in"],
        }

        totals[roster_id] = side_total(scored)

    winner_roster, margin = verdict(totals)
    lopsided = assess_lopsidedness(totals, winner_roster, margin)

    return {
        "transaction_id": trade.get("transaction_id"),
        "season": trade["season"],
        "week": trade["week"],
        "status_updated": trade.get("status_updated"),
        "sides": reviewed_sides,
        "totals": totals,
        "winner_roster": winner_roster,
        "margin": margin,
        "lopsided": lopsided,
    }


def build_all_reviews(
    chain,
    players,
    season_filter=None,
    transactions_fetch=get_transactions,
    draft_picks_fetch=get_draft_picks,
    draft_meta_fetch=get_draft,
    stats_fetch=get_weekly_stats,
):
    """
    End-to-end assembly: collect trades, resolve picks, score production, and
    return a list of reviewed trades (optionally filtered to one season).
    """

    chain_index = index_chain(chain)
    pick_index = build_pick_index(
        chain, picks_fetch=draft_picks_fetch, draft_fetch=draft_meta_fetch
    )
    stats_cache = WeeklyStatsCache(fetch=stats_fetch)

    trades = collect_trades(chain, fetch=transactions_fetch)

    if season_filter:
        trades = [
            trade
            for trade in trades
            if trade["season"] == str(season_filter)
        ]

    return [
        build_trade_review(
            trade, players, pick_index, chain_index, stats_cache
        )
        for trade in trades
    ]


# =============================================================================
# Manager overview
# =============================================================================

def compute_manager_overview(reviews, owner_by_season_roster):
    """
    Aggregate trade outcomes by manager (stable owner_id) across every trade.

    For each side of a trade, "given" is what the other side(s) received, so a
    manager's net for a trade is (received - given); in a two-team trade that's
    simply their production minus their partner's. Summed across all trades,
    net is how much a manager has won or lost on the trade market so far
    (unresolved future picks count as 0 production).

    Returns {owner_id: {trades, wins, losses, ties, received, net}}.

    Every manager found in owner_by_season_roster is seeded with a zero record
    so managers who never traded still appear (as the most "passive").
    """

    stats = {}

    # Seed every known manager at zero so non-traders show up too.
    for owner_id in set(owner_by_season_roster.values()):
        if owner_id is None:
            continue
        stats[owner_id] = {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "ties": 0,
            "received": 0.0,
            "net": 0.0,
        }

    for review in reviews:
        season = review["season"]
        totals = review["totals"]
        winner = review["winner_roster"]
        total_all = sum(totals.values())

        for roster_id, received in totals.items():
            owner_id = owner_by_season_roster.get((season, roster_id))

            entry = stats.setdefault(
                owner_id,
                {
                    "trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "ties": 0,
                    "received": 0.0,
                    "net": 0.0,
                },
            )

            given = total_all - received

            entry["trades"] += 1
            entry["received"] += received
            entry["net"] += received - given

            if winner is None:
                entry["ties"] += 1
            elif winner == roster_id:
                entry["wins"] += 1
            else:
                entry["losses"] += 1

    for entry in stats.values():
        entry["received"] = round(entry["received"], 2)
        entry["net"] = round(entry["net"], 2)

    return stats


def manager_rankings(overview):
    """
    Pick the headline managers from an overview: best/worst by net points, and
    most active (aggressive) / least active (passive) by trade count. Empty
    overview yields all None.
    """

    if not overview:
        return {
            "best": None,
            "worst": None,
            "most_active": None,
            "most_passive": None,
        }

    items = list(overview.items())

    return {
        "best": max(items, key=lambda kv: kv[1]["net"])[0],
        "worst": min(items, key=lambda kv: kv[1]["net"])[0],
        "most_active": max(items, key=lambda kv: kv[1]["trades"])[0],
        "most_passive": min(items, key=lambda kv: kv[1]["trades"])[0],
    }


# =============================================================================
# Report
# =============================================================================

def _asset_line(asset):
    """
    One display line for an asset, e.g.:
        "Player X (WR) — 142.6 pts since (11 gms, 13.0/gm)"
    """

    position = asset.get("position") or "?"
    label = asset.get("label") or asset.get("name") or "?"

    production = asset.get("production")

    if not asset.get("resolved"):
        return f"{label}"

    if not production:
        return f"{label} ({position})"

    total = production["total"]
    games = production["games"]

    if games:
        ppg = round(total / games, 1)
        detail = f"{total:.1f} pts since ({games} gms, {ppg}/gm)"
    else:
        detail = f"{total:.1f} pts since (no games yet)"

    return f"{label} ({position}) — {detail}"


def runner_up_of(totals, winner):
    """
    Return (roster_id, points) of the highest-scoring side that isn't the
    winner, or (None, 0.0) when there's no other side.
    """

    others = [
        (roster_id, pts)
        for roster_id, pts in totals.items()
        if roster_id != winner
    ]

    if not others:
        return None, 0.0

    return max(others, key=lambda item: item[1])


def print_lopsided_summary(reviews, team_names_by_season):
    """
    Print a highlight section, at the end of the report, calling out the
    trades flagged lopsided/heist — most lopsided first.
    """

    lopsided = [review for review in reviews if review.get("lopsided")]

    if not lopsided:
        return

    def ratio_of(review):
        winner_pts = review["totals"][review["winner_roster"]]
        _, runner_pts = runner_up_of(review["totals"], review["winner_roster"])
        return winner_pts / runner_pts if runner_pts > 0 else float("inf")

    print()
    print("=" * 80)
    print("LOPSIDED TRADES")
    print("=" * 80)

    for review in sorted(lopsided, key=ratio_of, reverse=True):
        season = review["season"]
        week = review["week"]
        names = team_names_by_season.get(season, {})

        winner = review["winner_roster"]
        winner_pts = review["totals"][winner]

        runner_roster, runner_pts = runner_up_of(review["totals"], winner)

        ratio_txt = (
            f"{winner_pts / runner_pts:.1f}x more"
            if runner_pts > 0
            else "a shutout"
        )

        tag = "HEIST" if review["lopsided"] == "heist" else "LOPSIDED"

        winner_team = names.get(winner, f"Roster {winner}")
        runner_team = names.get(runner_roster, f"Roster {runner_roster}")

        print(
            f"  *** {tag} *** [{season} Week {week}] "
            f"{winner_team} won by +{review['margin']:.1f} pts over "
            f"{runner_team} ({winner_pts:.1f} to {runner_pts:.1f} "
            f"— {ratio_txt})"
        )

    print()


def print_manager_overview(overview, manager_names):
    """
    Print a manager scoreboard: headline best/worst/most-active traders, then a
    table of every manager's trade record and net production, best net first.
    """

    if not overview:
        return

    def name_of(owner_id):
        return manager_names.get(owner_id) or "Unknown manager"

    rankings = manager_rankings(overview)

    print()
    print("=" * 80)
    print("MANAGER OVERVIEW")
    print("=" * 80)

    best = rankings["best"]
    worst = rankings["worst"]
    active = rankings["most_active"]
    passive = rankings["most_passive"]

    print(
        f"  Best trader:     {name_of(best)} "
        f"({overview[best]['net']:+.1f} net pts over "
        f"{overview[best]['trades']} trades)"
    )
    print(
        f"  Worst trader:    {name_of(worst)} "
        f"({overview[worst]['net']:+.1f} net pts over "
        f"{overview[worst]['trades']} trades)"
    )
    print(
        f"  Most aggressive: {name_of(active)} "
        f"({overview[active]['trades']} trades)"
    )
    print(
        f"  Most passive:    {name_of(passive)} "
        f"({overview[passive]['trades']} trades)"
    )

    print()
    print(
        f"  {'Manager':<34}{'Trades':>7}{'W-L-T':>9}"
        f"{'Received':>11}{'Net':>9}"
    )
    print("  " + "-" * 68)

    ordered = sorted(
        overview.items(),
        key=lambda kv: kv[1]["net"],
        reverse=True,
    )

    for owner_id, entry in ordered:
        record = f"{entry['wins']}-{entry['losses']}-{entry['ties']}"

        label = name_of(owner_id)
        if len(label) > 34:
            label = label[:31] + "..."

        print(
            f"  {label:<34}{entry['trades']:>7}{record:>9}"
            f"{entry['received']:>11.1f}{entry['net']:>+9.1f}"
        )

    print()


def print_report(reviews, team_names_by_season, league_name=None):
    """
    Print a chronological, evidence-first trade review with a winner verdict
    per trade.
    """

    print()
    print("=" * 80)
    print(
        f"DYNASTY TRADE REVIEW"
        + (f" — {league_name}" if league_name else "")
    )
    print("=" * 80)
    print(
        "League-accurate scoring. Production counted from the trade's filed "
        "week (inclusive) forward."
    )

    if not reviews:
        print()
        print("No completed trades found.")
        print()
        return

    for review in reviews:
        season = review["season"]
        week = review["week"]
        names = team_names_by_season.get(season, {})

        print()
        print("-" * 80)
        print(f"[{season} Week {week}] trade")

        for roster_id, side in review["sides"].items():
            team = names.get(roster_id, f"Roster {roster_id}")

            print(f"  {team} received:")

            if not side["assets"] and not side["faab_in"]:
                print("    (nothing)")

            for asset in side["assets"]:
                print(f"    - {_asset_line(asset)}")

            if side["faab_in"]:
                print(f"    - ${side['faab_in']} FAAB")

        winner = review["winner_roster"]

        if winner is None:
            in_progress = any(
                asset.get("production") is None
                or asset["production"]["games"] == 0
                for side in review["sides"].values()
                for asset in side["assets"]
            )

            note = (
                " (production still accruing)"
                if in_progress
                else ""
            )

            print(f"  VERDICT: even so far{note}")
        else:
            team = names.get(winner, f"Roster {winner}")
            totals = review["totals"]
            others = ", ".join(
                f"{pts:.1f}" for pts in totals.values()
            )

            print(
                f"  VERDICT: {team} winning by +{review['margin']:.1f} "
                f"pts ({others})"
            )

    print_lopsided_summary(reviews, team_names_by_season)

    print()


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Dynasty trade review: how the assets in each past trade "
            "have performed since, with a per-trade winner verdict."
        )
    )

    parser.add_argument(
        "--league-id",
        default=DEFAULT_LEAGUE_ID,
        help=(
            "Sleeper league ID for the current season; the dynasty history "
            f"is walked backward from it (default: {DEFAULT_LEAGUE_ID})."
        ),
    )

    parser.add_argument(
        "--season",
        default=None,
        help=(
            "Only review trades processed in this season (e.g. 2025). "
            "By default every season in the dynasty is reviewed."
        ),
    )

    args = parser.parse_args()

    print("=" * 80, file=sys.stderr)
    print(WIP_DISCLAIMER, file=sys.stderr)
    print("=" * 80, file=sys.stderr)

    print("Building dynasty league chain...")
    chain = build_league_chain(args.league_id)

    if not chain:
        print(f"No league found for id {args.league_id}.")
        return

    league_name = chain[0].get("name")
    seasons = ", ".join(
        str(league.get("season")) for league in chain
    )
    print(
        f"Found {len(chain)} season(s) for \"{league_name}\": {seasons}."
    )

    print("Loading Sleeper player map...")
    players = get_players()
    print(f"Loaded {len(players):,} player records.")

    print("Loading team names per season...")
    team_names_by_season = build_team_names_by_season(chain)

    print("Building manager directory...")
    directory = build_manager_directory(chain)

    print("Collecting trades and scoring production since each...")
    reviews = build_all_reviews(
        chain,
        players,
        season_filter=args.season,
    )

    print_report(
        reviews,
        team_names_by_season,
        league_name=league_name,
    )

    overview = compute_manager_overview(
        reviews, directory["owner_by_season_roster"]
    )
    print_manager_overview(overview, directory["names"])


if __name__ == "__main__":
    main()
