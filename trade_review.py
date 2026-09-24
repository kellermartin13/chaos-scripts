#!/usr/bin/env python3

"""
Dynasty trade review.

Walks a Sleeper dynasty league's full history (every season's league object,
linked by previous_league_id), pulls every completed trade, and grades how the
assets each side received have performed — including traded draft picks
resolved to the players they actually became.

The headline metric is Points Above Replacement (PAR): a WAR-style measure of
how much better than a freely-available replacement (at the same position) each
asset produced, counted only while the receiving team actually held it, and
floored per week so a benchable dud costs nothing. Raw league points are kept
as a secondary sanity column. Pass --raw-points for the legacy points-only
report.

Production is scored with the league's own scoring settings (a dot product of
each player's weekly Sleeper stat line and the league scoring_settings), so
verdicts reflect real league points, not a generic PPR line — it even honors
quirks like TE-premium receiving.

Everything comes from the public Sleeper API; no authentication and no nflverse
dependency are required.

Note: PAR is the primary metric but verdicts have not been validated end-to-end
against known-good league results — spot-check against your league before
leaning on them for decisions.
"""

import argparse
import contextlib
import html
import io
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone

import requests

from sleeper_cache import cached_players


# Printed at startup so anyone running the tool knows verdicts are unvalidated.
WIP_DISCLAIMER = (
    "Note: PAR is the primary metric; verdicts are not yet validated "
    "end-to-end against known-good league results — spot-check against your "
    "league before relying on them."
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
    end_season=None,
    end_week=None,
):
    """
    Total league points a player has produced since a trade.

    Counting window (inclusive of the trade's filed week):
      - trade season: weeks >= start_week
      - every later season present in the chain: all weeks

    Offseason trades are filed under week 1, so an inclusive window correctly
    captures the entire season for them; an in-season trade counts from the
    week it was processed forward.

    end_season/end_week, when given, cap the window (exclusive) at the point
    the receiving team gave the asset up — a re-trade, waiver, or drop. Without
    them the window runs to the end of the chain (the asset is still held).

    Returns {total, games, per_week: [{season, week, points}]}.
    """

    start_season = str(start_season)
    end_key = _wk_key(end_season, end_week) if end_season is not None else None

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

            if end_key is not None and _wk_key(season, week) >= end_key:
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
# Points above replacement (WAR-style) — additive, non-destructive
# =============================================================================
#
# Raw "points since trade" says how much an asset produced. PAR says how much
# better than a freely-available replacement at the same position, which reads
# far more like a WAR (wins-above-replacement) number. It reuses the same
# league-accurate per-week scoring as compute_production_since; the only new
# ingredient is a per-week, per-position replacement baseline.
#
# Converting PAR -> literal "wins" needs a points-per-win divisor derived from
# the league's matchup history; that step imports softer assumptions and is
# intentionally left out of this prototype. PAR is the defensible core.

# Positions we compute a replacement baseline for. FLEX-type slots are not
# positions; their demand is spread across the eligible positions below.
BASELINE_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")

# Real positions each flex-type roster slot can be filled by. Slots not listed
# (BN, IR, TAXI, IDP slots) create no starter demand and are ignored.
FLEX_ELIGIBILITY = {
    "FLEX": ("RB", "WR", "TE"),
    "WRRB_FLEX": ("RB", "WR"),
    "REC_FLEX": ("WR", "TE"),
    "SUPER_FLEX": ("QB", "RB", "WR", "TE"),
}

# Replacement level is read a few ranks past the last leaguewide starter and
# averaged over a small band, since a single rank is noisy week to week.
DEFAULT_REPLACEMENT_BAND = 3


def _wk_key(season, week):
    """Chronological sort key for a (season, week) across the dynasty."""

    return (int(season), int(week))


# Cutoff for classifying a week-1 trade as offseason vs. a real in-season
# Week 1 trade. NFL Week 1 kicks off in early September, so a week-1 trade
# whose status_updated is before Sept 1 of the season is an offseason trade
# (Sleeper files all offseason trades under the week-1 leg).
_SEASON_START_MONTH = 9
_SEASON_START_DAY = 1


def is_offseason_trade(season, week, status_updated):
    """
    True when a trade filed under week 1 actually happened in the offseason,
    judged by its status_updated timestamp (epoch ms). Non-week-1 trades and
    trades without a timestamp are treated as in-season.
    """

    if int(week) != 1 or not status_updated:
        return False

    filed = datetime.fromtimestamp(int(status_updated) / 1000, tz=timezone.utc)
    cutoff = datetime(
        int(season), _SEASON_START_MONTH, _SEASON_START_DAY, tzinfo=timezone.utc
    )
    return filed < cutoff


def format_trade_when(review):
    """
    Display label for when a trade was filed: "2023 offseason" for offseason
    trades (filed under week 1 before the season started), else "2023 wk5".
    """

    season, week = review["season"], review["week"]
    if is_offseason_trade(season, week, review.get("status_updated")):
        return f"{season} offseason"
    return f"{season} wk{week}"


# =============================================================================
# Asset ownership timeline (dynasty: assets keep changing hands)
# =============================================================================

def build_ownership_timeline(
    chain, fetch=get_transactions, weeks=REGULAR_SEASON_WEEKS
):
    """
    Per-player chronological roster history across the whole dynasty.

    Every completed transaction (trade, waiver, free agent) encodes `adds`
    (player_id -> roster that gained him) and `drops` (player_id -> roster that
    lost him). We fold them all into, per player, a time-ordered list of events:

        {player_id: [{"key": (season_int, week), "season", "week",
                      "roster_id", "event": "add"|"drop"}, ...]}

    Events are ordered by (season, week) then Sleeper's status_updated, so a
    same-week acquire-then-flip resolves in the right order. This is the basis
    for bounding a trade's credit to the span the receiver actually held an
    asset. fetch(league_id, week) is injectable for testing.
    """

    events = defaultdict(list)

    for league in chain:
        league_id = league["league_id"]
        season = str(league.get("season"))

        for week in weeks:
            for txn in fetch(league_id, week) or []:
                if txn.get("status") != "complete":
                    continue

                seq = txn.get("status_updated") or 0

                for player_id, roster_id in (txn.get("adds") or {}).items():
                    events[player_id].append(
                        {
                            "key": (_wk_key(season, week), seq),
                            "season": season,
                            "week": week,
                            "roster_id": roster_id,
                            "event": "add",
                        }
                    )

                for player_id, roster_id in (txn.get("drops") or {}).items():
                    events[player_id].append(
                        {
                            "key": (_wk_key(season, week), seq),
                            "season": season,
                            "week": week,
                            "roster_id": roster_id,
                            "event": "drop",
                        }
                    )

    for player_events in events.values():
        player_events.sort(key=lambda e: e["key"])

    return dict(events)


def hold_window_end(
    timeline, player_id, roster_id, start_season, start_week, start_seq=0
):
    """
    When did `roster_id` give up `player_id` after acquiring him at
    (start_season, start_week)?

    Returns the (end_season, end_week) of the first later event that removes
    the player from that roster — a drop by that roster, or an add to a
    *different* roster (a re-trade or waiver claim elsewhere). Returns None when
    the player never left, i.e. the receiving team still holds him and the
    trade's window runs to the present.

    start_seq is the acquiring transaction's status_updated. It's required to
    order events *within the same filed week* — Sleeper stamps every offseason
    trade as week 1, so a player traded twice in the offseason has both hops in
    (season, 1). Without the sequence, an intermediate owner's window can't be
    bounded (it looks "still held") and their production is double-counted; the
    final owner can likewise be wrongly bounded by an earlier same-week add.
    """

    start_full = (_wk_key(start_season, start_week), start_seq)

    for event in timeline.get(player_id, []):
        # Skip everything up to and including the acquiring transaction, in
        # full (season, week, status_updated) order.
        if event["key"] <= start_full:
            continue

        left_roster = (
            event["event"] == "drop" and event["roster_id"] == roster_id
        )
        moved_elsewhere = (
            event["event"] == "add" and event["roster_id"] != roster_id
        )

        if left_roster or moved_elsewhere:
            return event["season"], event["week"]

    return None


def build_trade_ownership(trades, pick_index=None, players=None):
    """
    Per-player acquisition timeline derived from the TRADES themselves,
    ordered chronologically by status_updated.

    Two sources of acquisition:
      - direct player `adds` (player_id -> receiving roster), and
      - traded draft `picks` resolved to the player they became (owner_id is
        the receiving roster). A player can enter a league via a traded pick
        and never appear in any `adds`, and a pick can be traded through
        several teams before it's used — so pick movements are genuine
        ownership transfers of the resolved player and must bound holds too.

    Built from the same trades the report shows, so it can't diverge from
    what's displayed; needs no `drops`; robust to transaction-endpoint gaps.

    Returns {player_id: [{"key": ((season_int, week), seq), "season", "week",
    "roster_id"}, ...]} sorted ascending.
    """

    acquisitions = defaultdict(list)

    def record(player_id, roster_id, trade, seq):
        acquisitions[player_id].append(
            {
                "key": (_wk_key(trade["season"], trade["week"]), seq),
                "season": trade["season"],
                "week": trade["week"],
                "roster_id": roster_id,
            }
        )

    for trade in trades:
        seq = trade.get("status_updated") or 0

        for player_id, roster_id in (trade.get("adds") or {}).items():
            record(player_id, roster_id, trade, seq)

        if pick_index is not None:
            for pick in (trade.get("draft_picks") or []):
                resolved = resolve_pick(pick, pick_index, players)
                if resolved.get("resolved") and resolved.get("player_id"):
                    record(resolved["player_id"], pick.get("owner_id"), trade, seq)

    for events in acquisitions.values():
        events.sort(key=lambda e: e["key"])

    return dict(acquisitions)


def trade_hold_end(trade_ownership, player_id, start_season, start_week, start_seq):
    """
    End (season, week) of a received asset's hold, from the *next trade that
    moved the player* after the acquiring trade — a player can only be traded
    by his current owner, so the next trade-acquisition marks when this owner
    gave him up. Returns None when no later trade moved him (still held via
    trades). Independent of drops and of the ownership-timeline fetch.
    """

    start_full = (_wk_key(start_season, start_week), start_seq)

    for event in trade_ownership.get(player_id, []):
        if event["key"] <= start_full:
            continue
        return event["season"], event["week"]

    return None


def _earliest_end(*ends):
    """The most-constraining (earliest) of several (season, week) ends, or None
    when all are None. Bounds a hold at whichever exit came first."""

    present = [e for e in ends if e is not None]
    if not present:
        return None
    return min(present, key=lambda e: _wk_key(e[0], e[1]))


def _debug_player_ownership(spec, ctx, trades):
    """
    Print an ownership diagnosis for one player to stderr: every trade that
    moved him, the ownership-timeline events, and the computed hold-window end
    per acquiring trade (trade-derived, timeline, combined). For pinning
    double-counts from a live (non-throttled) run.
    """

    players = ctx.get("players") or {}
    spec_s = str(spec).strip()

    if spec_s.isdigit() and spec_s in players:
        player_id = spec_s
    else:
        player_id = next(
            (pid for pid, p in players.items()
             if (p.get("full_name") or "").strip().lower() == spec_s.lower()),
            None,
        )

    def log(message):
        print(message, file=sys.stderr)

    log("=" * 72)
    if player_id is None:
        log(f"DEBUG: no player matched {spec!r}")
        log("=" * 72)
        return

    name = (players.get(player_id) or {}).get("full_name") or player_id
    log(f"DEBUG ownership for {name} (id {player_id})")

    acqs = (ctx.get("trade_ownership") or {}).get(player_id, [])
    log("\nTrades that moved him (adds), chronological:")
    if not acqs:
        log("  (none found in the collected trades)")
    for event in acqs:
        log(f"  {event['season']} wk{event['week']} seq={event['key'][1]} "
            f"-> roster {event['roster_id']}")

    timeline = (ctx.get("timeline") or {}).get(player_id, [])
    log("\nOwnership-timeline events (adds/drops from all transactions):")
    if not timeline:
        log("  (none — transactions endpoint returned no events for him)")
    for event in timeline:
        log(f"  {event['season']} wk{event['week']} seq={event['key'][1]} "
            f"{event['event']} roster {event['roster_id']}")

    log("\nComputed hold-window end per acquiring trade:")
    for event in acqs:
        seq = event["key"][1]
        roster = event["roster_id"]
        t_end = trade_hold_end(
            ctx.get("trade_ownership") or {}, player_id,
            event["season"], event["week"], seq,
        )
        w_end = hold_window_end(
            ctx.get("timeline") or {}, player_id, roster,
            event["season"], event["week"], seq,
        )
        combined = _earliest_end(t_end, w_end)
        held = "  (STILL HELD)" if combined is None else ""
        log(f"  roster {roster} @ {event['season']} wk{event['week']}: "
            f"trade_end={t_end} timeline_end={w_end} -> end={combined}{held}")
    log("=" * 72)


def build_replacement_ranks(league, flex_eligibility=FLEX_ELIGIBILITY):
    """
    Leaguewide starter demand per position = the replacement rank.

    Counts dedicated starter slots per position from roster_positions, spreads
    each flex slot's demand evenly across its eligible positions, then scales
    by the number of teams. The result maps a position to how many startable
    players the league consumes each week; the player ranked just past that
    count is replacement level.

    Returns {position: starters_leaguewide (float)} for every BASELINE_POSITION.
    """

    teams = league.get("total_rosters") or 0
    slots = league.get("roster_positions") or []

    demand = {pos: 0.0 for pos in BASELINE_POSITIONS}

    for slot in slots:
        if slot in demand:
            demand[slot] += 1.0
        elif slot in flex_eligibility:
            eligible = [pos for pos in flex_eligibility[slot] if pos in demand]
            if eligible:
                share = 1.0 / len(eligible)
                for pos in eligible:
                    demand[pos] += share

    return {pos: count * teams for pos, count in demand.items()}


def build_replacement_ranks_by_season(
    chain_index, flex_eligibility=FLEX_ELIGIBILITY
):
    """
    Replacement ranks for every season in the chain. roster_positions and team
    counts can change season to season, so each season gets its own ranks.

    Returns {season: {position: starters_leaguewide}}.
    """

    leagues = chain_index.get("leagues") or {}

    return {
        season: build_replacement_ranks(league, flex_eligibility)
        for season, league in leagues.items()
    }


class ReplacementBaselineCache:
    """
    Lazy, memoized per-(season, week) replacement-level points by position.

    For a given week: score every player in the slate with that season's
    scoring, group by position, sort descending, and read the replacement
    band — the `band` players ranked at/just past the leaguewide starter count
    for that position. The baseline is their mean points.

    Baseline is 0.0 when the position has no starter demand (rank < 1) or the
    slate has no players that deep — i.e. replacement is effectively a zero and
    the player's points pass through as PAR.
    """

    def __init__(
        self,
        chain_index,
        players,
        stats_cache,
        replacement_ranks_by_season,
        band=DEFAULT_REPLACEMENT_BAND,
    ):
        self._chain_index = chain_index
        self._players = players or {}
        self._stats_cache = stats_cache
        self._ranks = replacement_ranks_by_season or {}
        self._band = max(1, int(band))
        self._cache = {}

    def get(self, season, week):
        season = str(season)
        key = (season, week)

        if key not in self._cache:
            self._cache[key] = self._compute(season, week)

        return self._cache[key]

    def _compute(self, season, week):
        scoring = (self._chain_index.get("scoring") or {}).get(season) or {}
        slate = self._stats_cache.get(season, week)
        ranks = self._ranks.get(season) or {}

        by_pos = defaultdict(list)

        for player_id, stat_line in slate.items():
            position = (self._players.get(player_id) or {}).get("position")
            if position in ranks:
                by_pos[position].append(
                    score_stat_line(stat_line, scoring)
                )

        baselines = {}

        for position, points in by_pos.items():
            start = int(round(ranks[position]))

            if start < 1:
                baselines[position] = 0.0
                continue

            points.sort(reverse=True)
            band = points[start : start + self._band]

            baselines[position] = (
                round(sum(band) / len(band), 4) if band else 0.0
            )

        return baselines


def compute_par_since(
    player_id,
    position,
    start_season,
    start_week,
    chain_index,
    stats_cache,
    baseline_cache,
    weeks=REGULAR_SEASON_WEEKS,
    floor_weekly=False,
    end_season=None,
    end_week=None,
):
    """
    Points-above-replacement a player produced since a trade — the WAR-style
    analog of compute_production_since.

    Same counting window (filed week inclusive; later seasons in full). For
    each week the player actually posted a stat line, PAR = the player's league
    points minus the replacement baseline for their position that week. Weeks
    the player didn't play contribute nothing (no credit, no penalty), which is
    correct: a player on bye isn't beating a replacement.

    floor_weekly clamps each week's PAR at 0. Off, PAR is literal WAR (a
    below-replacement week is a penalty) — but summed over a long dynasty
    window that punishes depth pieces you'd simply have benched. On, PAR
    measures value-above-replacement *when the asset was startable*: a
    sub-replacement week contributes 0, not a penalty, since an asset's floor
    is to be dropped for the actual replacement. Floored is the better trade-
    grading metric; unfloored is the truer WAR analog.

    end_season/end_week cap the window (exclusive) at the point the receiving
    team gave the asset up (re-trade, waiver, or drop) — see hold_window_end.
    Without them the asset is treated as still held through the present.

    Returns {par_total, points_total, games, par_per_game, per_week:[{season,
    week, points, baseline, par}]}.
    """

    start_season = str(start_season)
    end_key = _wk_key(end_season, end_week) if end_season is not None else None

    seasons = [
        season
        for season in chain_index["seasons"]
        if season >= start_season
    ]

    par_total = 0.0
    points_total = 0.0
    games = 0
    per_week = []

    for season in seasons:
        scoring = chain_index["scoring"].get(season) or {}
        first_week = start_week if season == start_season else 1

        for week in weeks:
            if week < first_week:
                continue

            if end_key is not None and _wk_key(season, week) >= end_key:
                continue

            stat_line = stats_cache.get(season, week).get(player_id)

            if not stat_line:
                continue

            points = score_stat_line(stat_line, scoring)
            baseline = baseline_cache.get(season, week).get(position, 0.0)
            par = points - baseline

            if floor_weekly and par < 0:
                par = 0.0

            if stat_line.get("gp"):
                games += 1

            points_total += points
            par_total += par

            per_week.append(
                {
                    "season": season,
                    "week": week,
                    "points": round(points, 2),
                    "baseline": round(baseline, 2),
                    "par": round(par, 2),
                }
            )

    return {
        "par_total": round(par_total, 2),
        "points_total": round(points_total, 2),
        "games": games,
        "par_per_game": round(par_total / games, 2) if games else 0.0,
        "per_week": per_week,
    }


def score_asset_par(
    asset, trade, chain_index, stats_cache, baseline_cache, floor_weekly=False
):
    """
    PAR analog of score_asset: attach points-above-replacement to a resolved
    asset. Unresolved picks (future drafts) and FAAB carry no production.

    Returns the asset augmented with a "par" dict (or None).
    """

    if not asset.get("resolved") or not asset.get("player_id"):
        return {**asset, "par": None}

    par = compute_par_since(
        asset["player_id"],
        asset.get("position"),
        trade["season"],
        trade["week"],
        chain_index,
        stats_cache,
        baseline_cache,
        floor_weekly=floor_weekly,
    )

    return {**asset, "par": par}


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


def print_manager_overview(overview, manager_names, unit="pts"):
    """
    Print a manager scoreboard: headline best/worst/most-active traders, then a
    table of every manager's trade record and net production, best net first.

    unit labels the net/received figures ("pts" for the legacy raw-points
    report, "PAR" for the default report).
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
        f"({overview[best]['net']:+.1f} net {unit} over "
        f"{overview[best]['trades']} trades)"
    )
    print(
        f"  Worst trader:    {name_of(worst)} "
        f"({overview[worst]['net']:+.1f} net {unit} over "
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
    print(f"  RANKING by net {unit}:")
    print(
        f"  {'#':<4}{'Manager':<32}{'Trades':>7}{'W-L-T':>9}"
        f"{'Received':>11}{'Net':>9}"
    )
    print("  " + "-" * 72)

    ordered = sorted(
        overview.items(),
        key=lambda kv: kv[1]["net"],
        reverse=True,
    )

    for rank, (owner_id, entry) in enumerate(ordered, start=1):
        record = f"{entry['wins']}-{entry['losses']}-{entry['ties']}"

        label = name_of(owner_id)
        if len(label) > 31:
            label = label[:28] + "..."

        print(
            f"  {rank:<4}{label:<32}{entry['trades']:>7}{record:>9}"
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
# PAR report (default): ownership-bounded, lineage-linked, readable highlights
# =============================================================================

def _par_by_season(per_week, key="par"):
    """Sum a per-week metric into an ordered {season: rounded_total}."""

    out = defaultdict(float)
    for wk in per_week:
        out[wk["season"]] += wk[key]
    return {season: round(total, 1) for season, total in sorted(out.items())}


def _merge_seasons(assets):
    out = defaultdict(float)
    for asset in assets:
        for season, value in asset["by_season"].items():
            out[season] += value
    return {season: round(total, 1) for season, total in sorted(out.items())}


def review_asset_par(asset, trade, roster_id, ctx):
    """
    Score one received asset for the roster that received it, bounded to the
    span that roster held it (ownership-aware). Returns a render-ready dict
    carrying PAR (headline), raw points (secondary), the hold window, a
    per-season PAR trajectory, and fields for lineage linking.
    """

    base = {
        "name": asset.get("name") or asset.get("label") or "?",
        "position": asset.get("position"),
        "player_id": asset.get("player_id"),
        "resolved": bool(asset.get("resolved") and asset.get("player_id")),
        "end": None,
        "became": None,
    }

    if not base["resolved"]:
        base.update(
            {"par": 0.0, "par_pg": 0.0, "points": 0.0, "games": 0,
             "hold": "unresolved (pick/FAAB)", "seasons": 0, "by_season": {}}
        )
        return base

    start_seq = trade.get("status_updated") or 0

    # Bound the hold window at the earliest exit we can establish. The
    # trades-derived bound (next trade that moved the player) is robust — it
    # comes from the same trades the report shows and needs no drops. The
    # timeline bound additionally catches waiver/drop exits when those are
    # recorded. Whichever comes first wins.
    trade_end = trade_hold_end(
        ctx.get("trade_ownership") or {}, asset["player_id"],
        trade["season"], trade["week"], start_seq,
    )
    timeline_end = hold_window_end(
        ctx["timeline"], asset["player_id"], roster_id,
        trade["season"], trade["week"], start_seq,
    )
    end = _earliest_end(trade_end, timeline_end)
    end_season, end_week = end if end else (None, None)
    base["end"] = end

    par = compute_par_since(
        asset["player_id"], asset.get("position"),
        trade["season"], trade["week"],
        ctx["chain_index"], ctx["stats_cache"], ctx["baseline_cache"],
        floor_weekly=ctx["floor"], end_season=end_season, end_week=end_week,
    )
    pts = compute_production_since(
        asset["player_id"], trade["season"], trade["week"],
        ctx["chain_index"], ctx["stats_cache"],
        end_season=end_season, end_week=end_week,
    )

    seasons = sorted({wk["season"] for wk in par["per_week"]})

    if end is None:
        hold = f"still held · {len(seasons)} seas"
    else:
        hold = f"held {len(seasons)} seas → left {end_season} wk{end_week}"

    base.update(
        {
            "par": par["par_total"],
            "par_pg": par["par_per_game"],
            "points": pts["total"],
            "games": par["games"],
            "hold": hold,
            "seasons": len(seasons),
            "by_season": _par_by_season(par["per_week"]),
        }
    )
    return base


def build_par_review(trade, ctx):
    """
    Assemble one trade scored by PAR (headline) with raw points kept as a
    secondary column. The verdict and lopsided classification run on PAR
    totals. The review dict exposes `totals` (PAR) and `winner_roster` so the
    existing manager-overview aggregation works unchanged.
    """

    sides = received_assets(trade, ctx["players"], ctx["pick_index"])
    labels = ctx["team_names"].get(trade["season"], {})

    reviewed = {}
    par_totals = {}
    points_totals = {}

    for roster_id, side in sides.items():
        assets = [
            review_asset_par(asset, trade, roster_id, ctx)
            for asset in side["assets"]
        ]
        par_sum = round(sum(a["par"] for a in assets), 2)
        points_sum = round(sum(a["points"] for a in assets), 2)
        games = sum(a["games"] for a in assets)

        reviewed[roster_id] = {
            "label": labels.get(roster_id, f"Roster {roster_id}"),
            "assets": assets,
            "faab_in": side["faab_in"],
            "par": par_sum,
            "points": points_sum,
            "par_pg": round(par_sum / games, 2) if games else 0.0,
            "by_season": _merge_seasons(assets),
        }
        par_totals[roster_id] = par_sum
        points_totals[roster_id] = points_sum

    winner, margin = verdict(par_totals)
    lopsided = assess_lopsidedness(par_totals, winner, margin)

    latest = int(ctx["chain_index"]["seasons"][-1])
    return {
        "transaction_id": trade.get("transaction_id"),
        "season": trade["season"],
        "week": trade["week"],
        "status_updated": trade.get("status_updated"),
        "sides": reviewed,
        "totals": par_totals,          # PAR — drives verdict + manager overview
        "points_totals": points_totals,  # secondary sanity column
        "winner_roster": winner,
        "margin": margin,
        "lopsided": lopsided,
        "seasons_elapsed": latest - int(trade["season"]) + 1,
    }


def attach_lineage(reviews, trades):
    """
    Link (never sum) trades that share an asset.

    When a received asset's hold window ended because the team re-traded it, we
    point to the trade where they shipped it and name what they got back. No
    value is rolled up or split — the reader follows the chain themselves, so
    every number stays a clean single-trade figure.

    (Assumes Sleeper roster_id is stable across the dynasty chain, which is how
    the hold-window bounding already works. A league that reassigns roster_ids
    across seasons would need this keyed on the stable manager id.)
    """

    for i, review in enumerate(reviews):
        review["trade_no"] = i + 1

    reviews_by_no = {r["trade_no"]: r for r in reviews}

    # (season, week, sending_roster, player_id) -> trade_no it was shipped in.
    shipped_in = {}
    for review, trade in zip(reviews, trades):
        for player_id, roster_id in (trade.get("drops") or {}).items():
            shipped_in[
                (trade["season"], trade["week"], roster_id, player_id)
            ] = review["trade_no"]

    for review in reviews:
        for roster_id, side in review["sides"].items():
            for asset in side["assets"]:
                if not asset["end"] or not asset["player_id"]:
                    continue  # still held (or unresolved) — nothing to link

                end_season, end_week = asset["end"]
                next_no = shipped_in.get(
                    (end_season, end_week, roster_id, asset["player_id"])
                )

                if next_no is None:
                    # Left via waiver/drop, not a trade — lineage ends here.
                    asset["became"] = {"dropped": True}
                    continue

                return_side = reviews_by_no[next_no]["sides"].get(roster_id, {})
                asset["became"] = {
                    "trade_no": next_no,
                    "assets": [a["name"] for a in return_side.get("assets", [])],
                }


def annotate_review_managers(reviews, directory):
    """
    Tag each review with the stable manager identities of its participants so
    the HTML report can filter by manager (a manager who renamed their team
    across seasons still resolves to one identity). Sets review["managers"] to
    a sorted list of manager names, falling back to the per-season team label
    when an owner can't be resolved.
    """

    owner_by = directory.get("owner_by_season_roster", {})
    names = directory.get("names", {})

    for review in reviews:
        managers = set()
        for roster_id, side in review["sides"].items():
            owner_id = owner_by.get((review["season"], roster_id))
            managers.add(names.get(owner_id) or side["label"])
        review["managers"] = sorted(managers)


def _par_traj(by_season):
    """Compact per-season trajectory, e.g. '19:+540  20:+310'."""

    if not by_season:
        return "—"
    return "  ".join(
        f"{season[2:]}:{value:+.0f}" for season, value in by_season.items()
    )


def _par_lineage_line(asset):
    """Cross-reference to the re-trade an asset flowed into (no value summed)."""

    became = asset.get("became")
    if not became:
        return None
    if became.get("dropped"):
        return "            \u21b3 later dropped — lineage ends (no trade)"
    got = ", ".join(became["assets"]) or "picks/FAAB"
    return f"            \u21b3 became: {got}  (see T{became['trade_no']})"


def _par_takeaway(review, winner):
    if review.get("chained") and review.get("chain_note"):
        note = review["chain_note"]
        return (
            f"\u2192 Chained trade: {note['team']} flipped {note['asset']} "
            f"onward (see T{note['trade_no']}) — single-trade PAR understates "
            "their return."
        )

    if winner is None:
        return "\u2192 Even by PAR."

    win_side = review["sides"][winner]
    others = [s["par"] for rid, s in review["sides"].items() if rid != winner]
    runner_up = max(others) if others else 0.0

    top = max(win_side["assets"], key=lambda a: a["par"], default=None)
    if not top or top["par"] <= 0:
        return "\u2192 Winner by attrition; neither side got much startable value."

    note = f"\u2192 {top['name']} was the engine ({top['par']:.0f} PAR)"
    if runner_up >= 0 and top["par"] >= runner_up:
        note += " — alone out-produced the entire return."
    else:
        note += "."
    return note


def flag_chained_trades(reviews):
    """
    Reclassify trades where a *losing* side flipped a received asset onward.

    When a side that lost the per-trade PAR comparison later re-traded one of
    the assets it received (the asset carries a lineage link), its single-trade
    PAR understates what it actually got — the value left via the next trade.
    Labeling such a trade LOPSIDED/HEIST would misread a deliberate pass-through
    as a fleecing (e.g. acquiring a stud and immediately flipping him). Mark it
    "chained" and drop the lopsided flag; PAR numbers are unchanged and the ↳
    links show where the value went. Run after attach_lineage.
    """

    for review in reviews:
        review["chained"] = False
        winner = review["winner_roster"]
        if winner is None:
            continue

        for roster_id, side in review["sides"].items():
            if roster_id == winner:
                continue
            for asset in side["assets"]:
                became = asset.get("became")
                if became and became.get("trade_no"):
                    review["chained"] = True
                    review["chain_note"] = {
                        "team": side["label"],
                        "asset": asset["name"],
                        "trade_no": became["trade_no"],
                    }
                    review["lopsided"] = None  # not a heist — a pass-through
                    break
            if review["chained"]:
                break


def render_par_highlight(rank, review):
    lines = []
    tag = "CHAINED" if review.get("chained") else (
        review["lopsided"] or "notable"
    ).upper()
    when = format_trade_when(review)
    lines.append(
        f"\n#{rank}  [T{review['trade_no']}]  {tag} · {when} · "
        f"{review['seasons_elapsed']} seasons elapsed"
    )

    winner = review["winner_roster"]
    ordered = sorted(
        review["sides"].items(), key=lambda kv: kv[1]["par"], reverse=True
    )

    for roster_id, side in ordered:
        mark = "WON " if roster_id == winner else "lost"
        if winner is None:
            mark = "tie "
        head = (
            f"  {mark}  {side['label']:<34} "
            f"{side['par']:>8.1f} PAR  ({side['par_pg']:.1f}/G · "
            f"{side['points']:.0f} pts)"
        )
        if roster_id == winner and review["margin"]:
            head += f"  \u25b8 +{review['margin']:.1f}"
        lines.append(head)
        lines.append(f"        by season: {_par_traj(side['by_season'])}")
        for asset in side["assets"]:
            pos = f" ({asset['position']})" if asset["position"] else ""
            lines.append(
                f"          {asset['name']}{pos:<6}  "
                f"{asset['par']:>7.1f} PAR · {asset['par_pg']:.1f}/G · "
                f"{asset['hold']}"
            )
            lineage = _par_lineage_line(asset)
            if lineage:
                lines.append(lineage)

    lines.append("  " + _par_takeaway(review, winner))
    return "\n".join(lines)


def render_par_index_entry(review):
    """
    Medium-detail listing for one trade: a header line (trade no, when, class,
    winner + margin) then one line per side with PAR, raw points, and the
    assets received. Enough context to judge any trade without the full
    highlight treatment.
    """

    when = format_trade_when(review)
    winner = review["winner_roster"]

    bits = [f"T{review['trade_no']:<4}{when}"]
    if review.get("chained"):
        bits.append("CHAINED")
    elif review["lopsided"]:
        bits.append(review["lopsided"].upper())
    if winner is None:
        bits.append("even")
    else:
        bits.append(
            f"{review['sides'][winner]['label']} +{review['margin']:.1f} PAR"
        )

    lines = ["  " + "  ·  ".join(bits)]

    ordered = sorted(
        review["sides"].items(), key=lambda kv: kv[1]["par"], reverse=True
    )
    for roster_id, side in ordered:
        mark = "\u25b8" if roster_id == winner else " "
        names = ", ".join(a["name"] for a in side["assets"]) or "(picks/FAAB)"
        if len(names) > 50:
            names = names[:47] + "..."
        lines.append(
            f"     {mark} {side['label']:<30} {side['par']:>7.1f} PAR "
            f"({side['points']:>6.0f} pts)  {names}"
        )

    return "\n".join(lines)


def print_par_report(reviews, league_name=None, top=12, floored=True):
    """
    Print the default PAR report: a header, the most lopsided trades rendered
    with full context (per-asset PAR, hold windows, per-season trajectory,
    lineage links), then a compact chronological index of every trade.
    """

    mode = "floored (value-when-startable)" if floored else "literal WAR"
    print("=" * 78)
    print(
        "DYNASTY TRADE REVIEW — Points Above Replacement (PAR)"
        + (f" — {league_name}" if league_name else "")
    )

    if not reviews:
        print("\nNo completed trades found.")
        return

    seasons = sorted({r["season"] for r in reviews})
    print(
        f"{len(reviews)} trades · {seasons[0]}–{seasons[-1]} · PAR mode: {mode}"
    )
    print(
        "PAR is the headline (raw points shown as a secondary column). "
        "Production counted only while the receiving team held the asset."
    )
    print("=" * 78)

    ranked = sorted(reviews, key=lambda r: r["margin"], reverse=True)
    print(f"\nTOP {min(top, len(ranked))} HIGHLIGHTS (by PAR margin)")
    print("-" * 78)
    for rank, review in enumerate(ranked[:top], start=1):
        print(render_par_highlight(rank, review))

    print("\n\nALL TRADES (chronological — rosters, PAR, and raw points)")
    print("-" * 78)
    for review in reviews:
        print(render_par_index_entry(review))
        print()


def _report_title(league_name, season=None):
    base = f"{league_name or 'Dynasty'} — Trade Review (PAR)"
    return f"{base} · {season}" if season else base


def _html_shell(title, body_html, generated=None):
    """
    Full HTML document: <head> with embedded CSS + the given body HTML. Shared
    by the rich PAR report and the plain-text (<pre>) fallback.
    """

    if generated is None:
        generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    safe_title = html.escape(title)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:#0d1117; color:#e6edf3; line-height:1.5;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }}
  main {{ max-width: 960px; margin: 0 auto; padding: 1.5rem 1.25rem 4rem; }}
  header.page {{ border-bottom:1px solid #30363d; padding-bottom:1rem; margin-bottom:1.5rem; }}
  h1 {{ font-size:1.6rem; margin:0 0 .35rem; }}
  h2 {{ font-size:1.15rem; margin:2rem 0 .75rem; padding-bottom:.35rem; border-bottom:1px solid #21262d; }}
  h3 {{ font-size:1rem; margin:0; }}
  .meta {{ color:#8b949e; font-size:.9rem; }}
  code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
  a {{ color:#58a6ff; }}
  .muted {{ color:#8b949e; }}
  .num {{ font-variant-numeric: tabular-nums; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}

  .filterbar {{ position:sticky; top:0; z-index:5; display:flex; flex-wrap:wrap;
    align-items:center; gap:.5rem; padding:.6rem 0; margin-bottom:1rem;
    background:#0d1117; border-bottom:1px solid #21262d; }}
  .filterbar label {{ color:#adbac7; font-size:.9rem; }}
  .filterbar select {{ background:#161b22; color:#e6edf3; border:1px solid #30363d;
    border-radius:6px; padding:.3rem .5rem; font-size:.9rem; }}
  button.linkish {{ background:none; border:none; color:#58a6ff; padding:0;
    font:inherit; cursor:pointer; text-align:left; }}
  button.linkish:hover {{ text-decoration:underline; }}

  .trade {{ border:1px solid #30363d; border-radius:10px; padding:1rem 1.1rem; margin:0 0 1rem;
    background:#0f141a; }}
  .trade > .thead {{ display:flex; flex-wrap:wrap; align-items:center; gap:.5rem; margin-bottom:.75rem; }}
  .rank {{ font-weight:700; color:#8b949e; }}
  .tno {{ color:#8b949e; font-size:.85rem; }}
  .when {{ color:#adbac7; }}
  .elapsed {{ color:#8b949e; font-size:.82rem; margin-left:auto; }}

  .badge {{ display:inline-block; padding:.08rem .5rem; border-radius:999px; font-size:.72rem;
    font-weight:700; letter-spacing:.03em; }}
  .badge.heist {{ background:#da3633; color:#fff; }}
  .badge.lopsided {{ background:#9e6a03; color:#fff; }}
  .badge.chained {{ background:#6e7681; color:#fff; }}
  .tag.win {{ background:#238636; color:#fff; padding:.05rem .45rem; border-radius:6px; font-size:.72rem; font-weight:700; }}
  .pos {{ display:inline-block; background:#21262d; color:#adbac7; border:1px solid #30363d;
    border-radius:5px; padding:0 .35rem; font-size:.7rem; margin-left:.35rem; }}
  .pill {{ display:inline-block; background:#161b22; color:#adbac7; border:1px solid #21262d;
    border-radius:5px; padding:0 .4rem; margin:.1rem .25rem .1rem 0; font-size:.75rem; }}

  .side {{ border:1px solid #21262d; border-radius:8px; padding:.6rem .75rem; margin:.5rem 0; }}
  .side.win {{ border-left:3px solid #238636; }}
  .side-head {{ display:flex; flex-wrap:wrap; align-items:baseline; gap:.5rem; }}
  .team {{ font-weight:600; }}
  .par {{ font-weight:700; }}
  .par.pos {{ color:#3fb950; background:none; border:none; }}
  .traj {{ margin:.4rem 0 .1rem; }}
  ul.assets {{ list-style:none; margin:.5rem 0 0; padding:0; }}
  ul.assets li {{ padding:.15rem 0; border-top:1px dashed #21262d; }}
  ul.assets li:first-child {{ border-top:none; }}
  .lineage {{ display:block; color:#8b949e; font-size:.82rem; margin:.1rem 0 .1rem 1rem; }}
  .takeaway {{ margin:.6rem 0 0; font-style:italic; color:#adbac7; }}

  table {{ border-collapse:collapse; width:100%; margin-top:.5rem; font-size:.9rem; }}
  caption {{ text-align:left; color:#8b949e; font-size:.82rem; margin-bottom:.4rem; }}
  th, td {{ text-align:left; padding:.4rem .6rem; border-bottom:1px solid #21262d; }}
  th[scope="col"] {{ color:#adbac7; border-bottom:1px solid #30363d; }}
  td.num, th.num {{ text-align:right; }}
  tbody tr:hover {{ background:#11161d; }}

  @media (max-width:600px) {{
    .elapsed {{ margin-left:0; width:100%; }}
    h1 {{ font-size:1.35rem; }}
  }}
</style>
</head>
<body>
<main>
{body_html}
<footer class="meta" style="margin-top:2rem;border-top:1px solid #30363d;padding-top:1rem;">
Generated {html.escape(generated)} · Points Above Replacement (PAR)
</footer>
</main>
</body>
</html>
"""


def wrap_report_html(report_text, title, generated=None):
    """
    Plain-text fallback: HTML-escape a monospace report and drop it into a
    <pre> (used for --raw-points --html, which has a different data shape).
    """

    return _html_shell(title, f"<pre>{html.escape(report_text)}</pre>", generated)


# --- rich HTML renderers (data-driven; used for the default PAR report) -----

def _h(value):
    return html.escape(str(value))


def _html_class_badge(lopsided):
    if lopsided == "heist":
        return '<span class="badge heist">HEIST</span>'
    if lopsided == "lopsided":
        return '<span class="badge lopsided">LOPSIDED</span>'
    return ""


def _html_pos(position):
    return f'<span class="pos">{_h(position)}</span>' if position else ""


def _html_traj(by_season):
    if not by_season:
        return ""
    pills = "".join(
        f'<span class="pill num">{season[2:]}:{value:+.0f}</span>'
        for season, value in by_season.items()
    )
    return f'<div class="traj">{pills}</div>'


def _html_lineage(asset):
    became = asset.get("became")
    if not became:
        return ""
    if became.get("dropped"):
        return '<span class="lineage">↳ later dropped — lineage ends</span>'
    names = ", ".join(_h(n) for n in became["assets"]) or "picks/FAAB"
    no = became["trade_no"]
    return (
        f'<span class="lineage">↳ became: {names} '
        f'<a href="#t{no}">(see T{no})</a></span>'
    )


def _html_asset(asset, detailed):
    pos = _html_pos(asset["position"])
    par_cls = "par pos" if asset["par"] > 0 else "par"
    line = (
        f'{_h(asset["name"])}{pos} '
        f'<span class="{par_cls} num">{asset["par"]:.1f} PAR</span>'
    )
    if detailed:
        line += (
            f' <span class="muted num">{asset["par_pg"]:.1f}/G</span>'
            f' <span class="muted">· {_h(asset["hold"])}</span>'
        )
        lineage = _html_lineage(asset)
        if lineage:
            line += lineage
    return f"<li>{line}</li>"


def _html_side(roster_id, side, winner, margin, detailed):
    win = roster_id == winner
    classes = "side win" if win else "side"
    tag = (
        f'<span class="tag win">WON +{margin:.1f}</span>'
        if win and margin
        else ""
    )
    head = (
        '<div class="side-head">'
        f'<span class="team">{_h(side["label"])}</span>'
        f'<span class="par num">{side["par"]:.1f} PAR</span>'
        f'<span class="muted num">{side["par_pg"]:.1f}/G · '
        f'{side["points"]:.0f} pts</span>{tag}</div>'
    )
    traj = _html_traj(side["by_season"]) if detailed else ""
    assets = "".join(_html_asset(a, detailed) for a in side["assets"])
    return f'<div class="{classes}">{head}{traj}<ul class="assets">{assets}</ul></div>'


def _html_trade_card(review, rank=None, detailed=False, anchor=False):
    when = format_trade_when(review)
    winner = review["winner_roster"]
    margin = review["margin"]

    head_bits = []
    if rank is not None:
        head_bits.append(f'<span class="rank">#{rank}</span>')
    head_bits.append(f'<span class="tno">T{review["trade_no"]}</span>')
    if review.get("chained"):
        head_bits.append('<span class="badge chained">CHAINED</span>')
    else:
        badge = _html_class_badge(review["lopsided"])
        if badge:
            head_bits.append(badge)
    head_bits.append(f'<span class="when">{_h(when)}</span>')
    head_bits.append(
        f'<span class="elapsed">{review["seasons_elapsed"]} seasons elapsed</span>'
    )
    thead = f'<div class="thead">{"".join(head_bits)}</div>'

    ordered = sorted(
        review["sides"].items(), key=lambda kv: kv[1]["par"], reverse=True
    )
    sides = "".join(
        _html_side(rid, side, winner, margin, detailed)
        for rid, side in ordered
    )

    takeaway = ""
    if detailed:
        text = _par_takeaway(review, winner).lstrip("→ ").strip()
        takeaway = f'<p class="takeaway">{_h(text)}</p>'

    attr = f' id="t{review["trade_no"]}"' if anchor else ""
    managers = html.escape(json.dumps(review.get("managers", [])), quote=True)
    return (
        f'<article class="trade"{attr} data-managers="{managers}">'
        f'{thead}{sides}{takeaway}</article>'
    )


def _html_manager_section(overview, manager_names):
    if not overview:
        return ""

    def name_of(owner_id):
        return manager_names.get(owner_id) or "Unknown manager"

    ranks = manager_rankings(overview)
    cards = (
        f'<p class="meta">Best: <strong>{_h(name_of(ranks["best"]))}</strong> '
        f'({overview[ranks["best"]]["net"]:+.1f} net PAR) · '
        f'Worst: <strong>{_h(name_of(ranks["worst"]))}</strong> '
        f'({overview[ranks["worst"]]["net"]:+.1f}) · '
        f'Most active: <strong>{_h(name_of(ranks["most_active"]))}</strong> '
        f'({overview[ranks["most_active"]]["trades"]} trades)</p>'
    )

    rows = ""
    ordered = sorted(overview.items(), key=lambda kv: kv[1]["net"], reverse=True)
    for i, (owner_id, e) in enumerate(ordered, start=1):
        record = f'{e["wins"]}-{e["losses"]}-{e["ties"]}'
        mgr = name_of(owner_id)
        name_cell = (
            f'<button type="button" class="linkish" '
            f'data-mgr-jump="{html.escape(mgr, quote=True)}">{_h(mgr)}</button>'
        )
        rows += (
            f"<tr><td class='num'>{i}</td><td>{name_cell}</td>"
            f"<td class='num'>{e['trades']}</td><td class='num'>{record}</td>"
            f"<td class='num'>{e['received']:.1f}</td>"
            f"<td class='num'>{e['net']:+.1f}</td></tr>"
        )

    table = (
        '<table><caption>Ranked by net PAR</caption><thead><tr>'
        '<th scope="col" class="num">#</th><th scope="col">Manager</th>'
        '<th scope="col" class="num">Trades</th><th scope="col" class="num">W-L-T</th>'
        '<th scope="col" class="num">Received</th><th scope="col" class="num">Net PAR</th>'
        f'</tr></thead><tbody>{rows}</tbody></table>'
    )
    return (
        '<section aria-labelledby="mgr-h"><h2 id="mgr-h">Manager Leaderboard</h2>'
        f'{cards}{table}</section>'
    )


def render_html_report(
    reviews, overview, manager_names, league_name=None, season=None,
    top=12, floored=True, generated=None,
):
    """
    Build the rich, styled HTML report (highlight cards, per-season
    trajectories, clickable lineage links, and a manager leaderboard table)
    from the review data — not from the text report.
    """

    title = _report_title(league_name, season)
    mode = "floored (value-when-startable)" if floored else "literal WAR"

    if not reviews:
        body = (
            f'<header class="page"><h1>{_h(title)}</h1></header>'
            "<p>No completed trades found.</p>"
        )
        return _html_shell(title, body, generated)

    seasons = sorted({r["season"] for r in reviews})
    header = (
        '<header class="page">'
        f'<h1>{_h(title)}</h1>'
        f'<p class="meta">{len(reviews)} trades · {seasons[0]}–{seasons[-1]} · '
        f'PAR mode: {mode} · production counted only while the receiving team '
        'held the asset</p></header>'
    )

    # Manager filter bar (client-side). Options are every manager who appears
    # in a trade, by stable identity.
    all_managers = sorted({m for r in reviews for m in r.get("managers", [])})
    options = '<option value="__all__">All managers</option>' + "".join(
        f'<option value="{html.escape(m, quote=True)}">{_h(m)}</option>'
        for m in all_managers
    )
    filterbar = (
        '<div class="filterbar">'
        '<label for="mgr-filter">Filter by manager:</label> '
        f'<select id="mgr-filter">{options}</select> '
        '<span id="filter-count" class="muted" aria-live="polite"></span>'
        '</div>'
    )

    ranked = sorted(reviews, key=lambda r: r["margin"], reverse=True)
    highlights = "".join(
        _html_trade_card(r, rank=i, detailed=True)
        for i, r in enumerate(ranked[:top], start=1)
    )
    highlights_section = (
        '<section class="filterable" aria-labelledby="hi-h">'
        f'<h2 id="hi-h">Top {min(top, len(ranked))} Highlights</h2>'
        f'{highlights}</section>'
    )

    all_cards = "".join(
        _html_trade_card(r, detailed=False, anchor=True) for r in reviews
    )
    all_section = (
        '<section class="filterable" aria-labelledby="all-h">'
        '<h2 id="all-h">All Trades</h2>'
        f'{all_cards}</section>'
    )

    manager_section = _html_manager_section(overview, manager_names)

    return _html_shell(
        title,
        header + filterbar + highlights_section + all_section
        + manager_section + _FILTER_SCRIPT,
        generated,
    )


# Client-side manager filter: show/hide trade cards, hide emptied sections,
# update a live count, and let leaderboard names jump-filter. No dependencies.
_FILTER_SCRIPT = """
<script>
(function () {
  var sel = document.getElementById('mgr-filter');
  if (!sel) return;
  var count = document.getElementById('filter-count');
  var cards = Array.prototype.slice.call(document.querySelectorAll('.trade'));
  var sections = Array.prototype.slice.call(
    document.querySelectorAll('section.filterable'));

  function apply() {
    var v = sel.value, shown = 0;
    cards.forEach(function (c) {
      var mgrs = [];
      try { mgrs = JSON.parse(c.dataset.managers || '[]'); } catch (e) {}
      var match = (v === '__all__') || mgrs.indexOf(v) > -1;
      c.style.display = match ? '' : 'none';
      if (match) shown++;
    });
    sections.forEach(function (s) {
      var visible = Array.prototype.some.call(
        s.querySelectorAll('.trade'),
        function (c) { return c.style.display !== 'none'; });
      s.style.display = visible ? '' : 'none';
    });
    count.textContent = (v === '__all__')
      ? '' : (shown + ' trade' + (shown === 1 ? '' : 's'));
  }

  sel.addEventListener('change', apply);
  document.querySelectorAll('[data-mgr-jump]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      sel.value = btn.getAttribute('data-mgr-jump');
      apply();
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });
  });
})();
</script>
"""


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Dynasty trade review: how the assets in each past trade have "
            "performed since, graded by Points Above Replacement (PAR)."
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
    parser.add_argument(
        "--top",
        type=int,
        default=12,
        help="Number of highlight trades to show (default 12).",
    )
    parser.add_argument(
        "--band",
        type=int,
        default=DEFAULT_REPLACEMENT_BAND,
        help=(
            "Ranks past the last leaguewide starter that define replacement "
            f"level (default {DEFAULT_REPLACEMENT_BAND})."
        ),
    )
    parser.add_argument(
        "--unfloored",
        action="store_true",
        help=(
            "Use literal WAR (below-replacement weeks penalize) instead of "
            "the floored default (value-when-startable)."
        ),
    )
    parser.add_argument(
        "--raw-points",
        action="store_true",
        help="Print the legacy points-only report instead of the PAR report.",
    )
    parser.add_argument(
        "--html",
        action="store_true",
        help=(
            "Emit the report as a self-contained HTML page (for GitHub Pages) "
            "instead of plain text. Progress goes to stderr so stdout is pure "
            "HTML."
        ),
    )
    parser.add_argument(
        "--debug-player",
        default=None,
        metavar="ID_OR_NAME",
        help=(
            "Print an ownership diagnosis for one player (Sleeper id or full "
            "name) to stderr: every trade that moved him, the ownership "
            "timeline events, and the computed hold-window end per trade. For "
            "debugging double-counts."
        ),
    )

    args = parser.parse_args()

    # Progress + disclaimer go to stderr so stdout carries only the report
    # (plain text or, with --html, a clean HTML document).
    def log(message):
        print(message, file=sys.stderr)

    log("=" * 80)
    log(WIP_DISCLAIMER)
    log("=" * 80)

    log("Building dynasty league chain...")
    chain = build_league_chain(args.league_id)

    if not chain:
        log(f"No league found for id {args.league_id}.")
        return

    league_name = chain[0].get("name")
    seasons = ", ".join(str(league.get("season")) for league in chain)
    log(f"Found {len(chain)} season(s) for \"{league_name}\": {seasons}.")

    log("Loading Sleeper player map...")
    players = get_players()
    log(f"Loaded {len(players):,} player records.")

    log("Loading team names per season...")
    team_names_by_season = build_team_names_by_season(chain)

    log("Building manager directory...")
    directory = build_manager_directory(chain)

    if args.raw_points:
        log("Collecting trades and scoring raw production since each...")
        reviews = build_all_reviews(chain, players, season_filter=args.season)
        overview = compute_manager_overview(
            reviews, directory["owner_by_season_roster"]
        )

        def write():
            print_report(reviews, team_names_by_season, league_name=league_name)
            print_manager_overview(overview, directory["names"])

        if args.html:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                write()
            print(wrap_report_html(
                buffer.getvalue(), _report_title(league_name, args.season)
            ))
        else:
            write()
        return

    log("Collecting trades, ownership timeline, and replacement baselines...")
    chain_index = index_chain(chain)

    # Share one memoized pass of the transaction log across trade
    # collection and the ownership timeline (both scan every week), so each
    # week's transactions is fetched once, not twice. Likewise share a
    # single WeeklyStatsCache between the report and the replacement
    # baselines. Together these halve the two largest Sleeper call buckets
    # and keep us well clear of the ~1000 req/min guidance.
    _txn_cache = {}

    def txn_fetch(league_id, week):
        key = (league_id, week)
        if key not in _txn_cache:
            _txn_cache[key] = get_transactions(league_id, week)
        return _txn_cache[key]

    stats_cache = WeeklyStatsCache()
    pick_index = build_pick_index(chain)

    # Ownership is derived from ALL trades (a re-trade in a later season must
    # still bound an earlier acquisition), even when --season filters which
    # trades are reviewed. Picks are resolved to the players they became so a
    # player acquired via a traded pick is tracked too.
    all_trades = collect_trades(chain, fetch=txn_fetch)
    trade_ownership = build_trade_ownership(all_trades, pick_index, players)

    trades = all_trades
    if args.season:
        trades = [t for t in all_trades if t["season"] == str(args.season)]

    ctx = {
        "chain_index": chain_index,
        "players": players,
        "pick_index": pick_index,
        "team_names": team_names_by_season,
        "stats_cache": stats_cache,
        "timeline": build_ownership_timeline(chain, fetch=txn_fetch),
        "trade_ownership": trade_ownership,
        "floor": not args.unfloored,
        "baseline_cache": ReplacementBaselineCache(
            chain_index, players, stats_cache,
            build_replacement_ranks_by_season(chain_index), band=args.band,
        ),
    }

    if args.debug_player:
        _debug_player_ownership(args.debug_player, ctx, all_trades)

    reviews = [build_par_review(trade, ctx) for trade in trades]
    attach_lineage(reviews, trades)
    flag_chained_trades(reviews)
    annotate_review_managers(reviews, directory)
    overview = compute_manager_overview(
        reviews, directory["owner_by_season_roster"]
    )

    if args.html:
        print(render_html_report(
            reviews, overview, directory["names"],
            league_name=league_name, season=args.season,
            top=args.top, floored=ctx["floor"],
        ))
    else:
        print_par_report(
            reviews, league_name=league_name, top=args.top,
            floored=ctx["floor"],
        )
        print_manager_overview(overview, directory["names"], unit="PAR")


if __name__ == "__main__":
    main()
