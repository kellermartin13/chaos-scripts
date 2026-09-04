"""
Tests for chaos.py.

Focus: correctness of who gets scored.
  - Only fantasy STARTERS are evaluated (bench excluded).
  - Starters are NOT silently dropped when Sleeper omits gsis_id
    (the nflverse crosswalk must recover them).
  - Only OFFENSIVE-position starters earn tackle bonuses.
  - Only started receivers earn drop bonuses.
  - The week is scored against the correct season's league.
  - Scoring refuses to run on a partially-charted week.

Network/nflverse calls are monkeypatched; no external I/O.
"""

import numpy as np
import pandas as pd
import pytest

import chaos


# ---------------------------------------------------------------------------
# get_starters
# ---------------------------------------------------------------------------

class TestGetStarters:

    @pytest.fixture
    def players(self):
        return {
            # started, has its own gsis_id
            "100": {
                "gsis_id": "00-1",
                "full_name": "Alice Arm",
                "position": "QB",
            },
            # started, NO gsis_id -> must be recovered via crosswalk
            "200": {
                "position": "RB",
                "first_name": "Bob",
                "last_name": "Runner",
            },
            # started, NO gsis_id and NOT in crosswalk -> unresolvable
            "300": {
                "position": "WR",
                "full_name": "Cara Catch",
            },
            # started on another roster, has gsis_id
            "400": {
                "gsis_id": "00-4",
                "full_name": "Dave Deep",
                "position": "WR",
            },
            # on the roster but BENCHED (never appears in starters)
            "999": {
                "gsis_id": "00-9",
                "full_name": "Benny Bench",
                "position": "TE",
            },
        }

    @pytest.fixture
    def crosswalk(self):
        return {"200": "00-2"}

    @pytest.fixture
    def patched_matchups(self, monkeypatch):
        matchups = [
            {
                "roster_id": 1,
                # includes a team-defense id ("SF") not present in players
                "starters": ["100", "200", "300", "SF"],
            },
            {
                "roster_id": 2,
                "starters": ["400"],
            },
        ]
        monkeypatch.setattr(
            chaos, "get_matchups", lambda league_id, week: matchups
        )

    def test_started_players_with_gsis_included(
        self, players, crosswalk, patched_matchups
    ):
        result = chaos.get_starters("L", 2, players, crosswalk)
        assert result["00-1"]["roster_id"] == 1
        assert result["00-4"]["roster_id"] == 2

    def test_crosswalk_recovers_missing_gsis(
        self, players, crosswalk, patched_matchups
    ):
        # Regression: player 200 has no Sleeper gsis_id but IS in the
        # crosswalk. It must NOT be silently dropped.
        result = chaos.get_starters("L", 2, players, crosswalk)
        assert "00-2" in result
        assert result["00-2"]["name"] == "Bob Runner"
        assert result["00-2"]["position"] == "RB"
        assert result["00-2"]["sleeper_id"] == "200"

    def test_unresolvable_starter_excluded(
        self, players, crosswalk, patched_matchups
    ):
        # player 300: no gsis, not in crosswalk -> cannot join nflverse.
        result = chaos.get_starters("L", 2, players, crosswalk)
        gsis_ids = {p["gsis_id"] for p in result.values()}
        assert "Cara Catch" not in {p["name"] for p in result.values()}
        assert len(gsis_ids) == len(result)  # keys are gsis ids

    def test_team_defense_id_excluded(
        self, players, crosswalk, patched_matchups
    ):
        # "SF" is not in the players map -> skipped, no crash.
        result = chaos.get_starters("L", 2, players, crosswalk)
        assert "SF" not in result

    def test_benched_player_never_scored(
        self, players, crosswalk, patched_matchups
    ):
        result = chaos.get_starters("L", 2, players, crosswalk)
        assert "00-9" not in result

    def test_exact_resolved_set(
        self, players, crosswalk, patched_matchups
    ):
        result = chaos.get_starters("L", 2, players, crosswalk)
        assert set(result.keys()) == {"00-1", "00-2", "00-4"}

    def test_without_crosswalk_missing_gsis_dropped(
        self, players, patched_matchups
    ):
        # Documents the pre-fix behavior: no crosswalk -> player 200 lost.
        result = chaos.get_starters("L", 2, players, crosswalk=None)
        assert "00-2" not in result
        assert set(result.keys()) == {"00-1", "00-4"}

    def test_gsis_whitespace_is_stripped(self, monkeypatch):
        # Regression: Sleeper stores some gsis_ids padded (e.g. " 00-1 ").
        # If not stripped, the key never matches nflverse PBP/snap ids and
        # the player is silently unscoreable / falsely flagged invalid.
        players = {
            "1": {"gsis_id": " 00-1 ", "full_name": "Spacey", "position": "WR"},
        }
        monkeypatch.setattr(
            chaos, "get_matchups",
            lambda league_id, week: [{"roster_id": 1, "starters": ["1"]}],
        )
        result = chaos.get_starters("L", 1, players, {})
        assert list(result.keys()) == ["00-1"]


# ---------------------------------------------------------------------------
# build_sleeper_gsis_crosswalk
# ---------------------------------------------------------------------------

class TestBuildCrosswalk:

    def test_normalizes_and_skips_nan(self, monkeypatch):
        ids = pd.DataFrame(
            {
                "sleeper_id": [4034.0, 200.0, np.nan, 300.0],
                "gsis_id": ["00-4034", "00-2", "00-x", np.nan],
            }
        )
        monkeypatch.setattr(chaos.nfl, "import_ids", lambda: ids)

        crosswalk = chaos.build_sleeper_gsis_crosswalk()

        # float-like sleeper ids normalized to plain string keys
        assert crosswalk["4034"] == "00-4034"
        assert crosswalk["200"] == "00-2"
        # rows with NaN sleeper_id or NaN gsis_id are skipped
        assert "300" not in crosswalk
        assert len(crosswalk) == 2


# ---------------------------------------------------------------------------
# resolve_league_for_season
# ---------------------------------------------------------------------------

class TestResolveLeague:

    @pytest.fixture
    def leagues(self):
        return {
            "L2026": {
                "season": "2026",
                "name": "League of Chaos",
                "previous_league_id": "L2025",
            },
            "L2025": {
                "season": "2025",
                "name": "League of Chaos",
                "previous_league_id": "L2024",
            },
            "L2024": {
                "season": "2024",
                "name": "League of Chaos",
                "previous_league_id": None,
            },
        }

    @pytest.fixture
    def patched(self, monkeypatch, leagues):
        monkeypatch.setattr(chaos, "get_league", lambda lid: leagues[lid])

    def test_current_season_returns_same_league(self, patched):
        lid, league = chaos.resolve_league_for_season("L2026", 2026)
        assert lid == "L2026"
        assert league["season"] == "2026"

    def test_walks_chain_to_prior_season(self, patched):
        lid, league = chaos.resolve_league_for_season("L2026", 2025)
        assert lid == "L2025"
        assert league["season"] == "2025"

    def test_no_match_returns_original_and_none(self, patched):
        lid, league = chaos.resolve_league_for_season("L2026", 2019)
        assert lid == "L2026"
        assert league is None


# ---------------------------------------------------------------------------
# find_offensive_tackles
# ---------------------------------------------------------------------------

class TestFindOffensiveTackles:

    @pytest.fixture
    def starters(self):
        return {
            "00-2": {"position": "WR", "name": "Bob", "roster_id": 1},
            "00-5": {"position": "CB", "name": "Def Back", "roster_id": 2},
        }

    def _pbp(self, rows):
        cols = [
            "game_id", "play_id", "qtr", "time",
            "home_team", "away_team", "desc", "special",
            "interception", "fumble_lost",
            "solo_tackle_1_player_id", "solo_tackle_2_player_id",
            "assist_tackle_1_player_id", "assist_tackle_2_player_id",
            "assist_tackle_3_player_id", "assist_tackle_4_player_id",
        ]
        # default: a turnover play (interception) so tackles are eligible.
        base = {"special": 0, "interception": 1, "fumble_lost": 0}
        return pd.DataFrame([{**base, **r} for r in rows], columns=cols)

    def test_offensive_starter_solo_and_assist_counted(self, starters):
        pbp = self._pbp([
            {"game_id": "g", "play_id": 1, "solo_tackle_1_player_id": "00-2"},
            {"game_id": "g", "play_id": 2, "assist_tackle_1_player_id": "00-2"},
        ])
        result = chaos.find_offensive_tackles(pbp, starters)
        assert result["00-2"]["solo"] == 1
        assert result["00-2"]["assists"] == 1

    def test_defensive_starter_not_credited(self, starters):
        pbp = self._pbp([
            {"game_id": "g", "play_id": 1, "solo_tackle_1_player_id": "00-5"},
        ])
        result = chaos.find_offensive_tackles(pbp, starters)
        assert "00-5" not in result

    def test_non_starter_not_credited(self, starters):
        pbp = self._pbp([
            {"game_id": "g", "play_id": 1, "solo_tackle_1_player_id": "00-777"},
        ])
        result = chaos.find_offensive_tackles(pbp, starters)
        assert "00-777" not in result

    def test_special_teams_tackle_excluded(self, starters):
        # a WR gunner making a punt-coverage tackle must NOT get the bonus.
        pbp = self._pbp([
            {"game_id": "g", "play_id": 1, "special": 1, "interception": 1,
             "assist_tackle_1_player_id": "00-2"},
        ])
        result = chaos.find_offensive_tackles(pbp, starters)
        assert "00-2" not in result

    def test_lost_fumble_turnover_counts(self, starters):
        pbp = self._pbp([
            {"game_id": "g", "play_id": 1, "interception": 0, "fumble_lost": 1,
             "solo_tackle_1_player_id": "00-2"},
        ])
        result = chaos.find_offensive_tackles(pbp, starters)
        assert result["00-2"]["solo"] == 1

    def test_two_way_player_defensive_tackle_excluded(self, starters):
        # No turnover on the play -> a Sleeper-"offensive" player credited
        # with a tackle was actually on defense (Travis Hunter at CB).
        pbp = self._pbp([
            {"game_id": "g", "play_id": 1, "interception": 0, "fumble_lost": 0,
             "solo_tackle_1_player_id": "00-2"},
        ])
        result = chaos.find_offensive_tackles(pbp, starters)
        assert "00-2" not in result


# ---------------------------------------------------------------------------
# find_drops
# ---------------------------------------------------------------------------

class TestFindDrops:

    @pytest.fixture
    def starters(self):
        return {
            "00-2": {"position": "WR", "name": "Bob", "roster_id": 1},
            "00-4": {"position": "WR", "name": "Dave", "roster_id": 2},
        }

    @pytest.fixture
    def pbp(self):
        return pd.DataFrame([
            {"game_id": "g", "play_id": 1, "receiver_player_id": "00-2",
             "qtr": 1, "time": "1:00", "home_team": "A", "away_team": "B",
             "desc": "drop"},
            {"game_id": "g", "play_id": 2, "receiver_player_id": "00-4",
             "qtr": 1, "time": "2:00", "home_team": "A", "away_team": "B",
             "desc": "catch"},
            {"game_id": "g", "play_id": 3, "receiver_player_id": "00-777",
             "qtr": 1, "time": "3:00", "home_team": "A", "away_team": "B",
             "desc": "drop by non-starter"},
        ])

    @pytest.fixture
    def ftn(self):
        return pd.DataFrame([
            {"nflverse_game_id": "g", "nflverse_play_id": 1, "is_drop": True},
            {"nflverse_game_id": "g", "nflverse_play_id": 2, "is_drop": False},
            {"nflverse_game_id": "g", "nflverse_play_id": 3, "is_drop": True},
        ])

    def test_started_receiver_drop_counted(self, pbp, ftn, starters):
        result = chaos.find_drops(pbp, ftn, starters)
        assert result["00-2"]["count"] == 1

    def test_non_drop_not_counted(self, pbp, ftn, starters):
        result = chaos.find_drops(pbp, ftn, starters)
        assert "00-4" not in result

    def test_non_starter_drop_excluded(self, pbp, ftn, starters):
        result = chaos.find_drops(pbp, ftn, starters)
        assert "00-777" not in result


# ---------------------------------------------------------------------------
# assert_week_fully_charted
# ---------------------------------------------------------------------------

class TestAssertWeekFullyCharted:

    @pytest.fixture
    def schedule(self):
        return pd.DataFrame([
            {"game_id": "2025_02_A_B", "away_team": "A", "home_team": "B"},
            {"game_id": "2025_02_C_D", "away_team": "C", "home_team": "D"},
        ])

    @pytest.fixture
    def patched(self, monkeypatch, schedule):
        monkeypatch.setattr(
            chaos, "get_week_schedule", lambda season, week: schedule
        )

    def test_passes_when_all_games_charted(self, patched):
        ftn = pd.DataFrame(
            {"nflverse_game_id": ["2025_02_A_B", "2025_02_C_D"]}
        )
        # no exception, no return value
        assert chaos.assert_week_fully_charted(2025, 2, ftn) is None

    def test_exits_when_a_game_missing(self, patched):
        ftn = pd.DataFrame({"nflverse_game_id": ["2025_02_A_B"]})
        with pytest.raises(SystemExit) as exc:
            chaos.assert_week_fully_charted(2025, 2, ftn)
        assert exc.value.code == 1

    def test_warns_and_returns_when_no_schedule(self, monkeypatch):
        monkeypatch.setattr(
            chaos, "get_week_schedule",
            lambda season, week: pd.DataFrame(
                columns=["game_id", "away_team", "home_team"]
            ),
        )
        ftn = pd.DataFrame({"nflverse_game_id": []})
        assert chaos.assert_week_fully_charted(2025, 99, ftn) is None


# ---------------------------------------------------------------------------
# compute_touches / find_invalid_roster_spots
# ---------------------------------------------------------------------------

class TestComputeTouches:

    def _pbp(self, rows):
        cols = ["rusher_player_id", "receiver_player_id", "complete_pass"]
        return pd.DataFrame(rows, columns=cols)

    def test_rush_attempt_is_a_touch(self):
        pbp = self._pbp([
            {"rusher_player_id": "00-1", "receiver_player_id": None,
             "complete_pass": 0},
        ])
        assert chaos.compute_touches(pbp)["00-1"] == 1

    def test_completed_reception_is_a_touch(self):
        pbp = self._pbp([
            {"rusher_player_id": None, "receiver_player_id": "00-1",
             "complete_pass": 1},
        ])
        assert chaos.compute_touches(pbp)["00-1"] == 1

    def test_incomplete_pass_is_not_a_touch(self):
        pbp = self._pbp([
            {"rusher_player_id": None, "receiver_player_id": "00-1",
             "complete_pass": 0},
        ])
        assert chaos.compute_touches(pbp).get("00-1", 0) == 0


class TestFindInvalidRosterSpots:

    def _run(self, starters, snap_by_gsis, pbp_rows, exempt=None):
        # map each gsis to a synthetic pfr id and key snaps by that pfr id,
        # mirroring the real gsis -> pfr -> offense_pct join.
        gsis_to_pfr = {g: f"{g}-pfr" for g in starters}
        snap_share = {f"{g}-pfr": v for g, v in snap_by_gsis.items()}
        cols = ["rusher_player_id", "receiver_player_id", "complete_pass"]
        pbp = pd.DataFrame(pbp_rows, columns=cols)
        invalid, exempted = chaos.find_invalid_roster_spots(
            pbp, starters, snap_share, gsis_to_pfr, exempt=exempt
        )
        return invalid, exempted

    def test_low_snaps_no_touches_flagged(self):
        starters = {"00-1": {"position": "WR", "name": "A", "roster_id": 1}}
        invalid, _ = self._run(starters, {"00-1": 0.05}, [])
        assert "00-1" in invalid
        assert invalid["00-1"]["snap_pct"] == 0.05

    def test_enough_snaps_not_flagged(self):
        starters = {"00-1": {"position": "WR", "name": "A", "roster_id": 1}}
        invalid, _ = self._run(starters, {"00-1": 0.20}, [])
        assert "00-1" not in invalid

    def test_boundary_15pct_not_flagged(self):
        # threshold is strictly-less-than 15%.
        starters = {"00-1": {"position": "WR", "name": "A", "roster_id": 1}}
        invalid, _ = self._run(starters, {"00-1": 0.15}, [])
        assert "00-1" not in invalid

    def test_touch_exempts_low_snap_player(self):
        starters = {"00-1": {"position": "RB", "name": "A", "roster_id": 1}}
        invalid, _ = self._run(
            starters, {"00-1": 0.05},
            [{"rusher_player_id": "00-1", "receiver_player_id": None,
              "complete_pass": 0}],
        )
        assert "00-1" not in invalid

    def test_non_offensive_position_exempt(self):
        # a kicker has ~0 offensive snaps and never has touches, but must
        # NOT be flagged.
        starters = {"00-1": {"position": "K", "name": "A", "roster_id": 1}}
        invalid, _ = self._run(starters, {"00-1": 0.0}, [])
        assert "00-1" not in invalid

    def test_missing_snap_record_treated_as_zero(self):
        # inactive / bye player has no snap row -> 0% -> invalid.
        starters = {"00-1": {"position": "WR", "name": "A", "roster_id": 1}}
        invalid, _ = self._run(starters, {}, [])
        assert "00-1" in invalid
        assert invalid["00-1"]["snap_pct"] == 0.0

    def test_exempt_player_not_penalized(self):
        # a would-be-invalid player that is exempt (injured/benched) is moved
        # out of `invalid` and into `exempted`, never penalized.
        starters = {"00-1": {"position": "WR", "name": "A", "roster_id": 1}}
        invalid, exempted = self._run(
            starters, {"00-1": 0.0}, [], exempt={"00-1": "injury: ruled Out"}
        )
        assert "00-1" not in invalid
        assert "00-1" in exempted
        assert exempted["00-1"]["reason"] == "injury: ruled Out"

    def test_exempt_only_applies_to_would_be_invalid(self):
        # a player who played enough is neither invalid nor exempted, even if
        # present in the exempt map.
        starters = {"00-1": {"position": "WR", "name": "A", "roster_id": 1}}
        invalid, exempted = self._run(
            starters, {"00-1": 0.50}, [], exempt={"00-1": "injury: ruled Out"}
        )
        assert "00-1" not in invalid
        assert "00-1" not in exempted


# ---------------------------------------------------------------------------
# find_redzone_turnovers
# ---------------------------------------------------------------------------

class TestRedzoneLosText:

    def test_uses_yrdln_string(self):
        assert chaos.redzone_los_text({"yrdln": "KC 18"}) == "KC 18"

    def test_falls_back_to_yardline_100(self):
        assert chaos.redzone_los_text(
            {"yrdln": None, "yardline_100": 12}
        ) == "opponent's 12-yard line"

    def test_generic_when_nothing_available(self):
        assert chaos.redzone_los_text({}) == "the red zone"


class TestFindRedzoneTurnovers:

    def _pbp(self, rows):
        cols = [
            "yardline_100", "yrdln", "interception", "fumble_lost",
            "passer_player_id", "fumbled_1_player_id",
            "interception_player_id", "forced_fumble_player_1_player_id",
            "fumble_recovery_1_player_id",
            "game_id", "play_id", "qtr", "time", "away_team", "home_team", "desc",
        ]
        base = {
            "yardline_100": 10, "yrdln": "KC 10", "interception": 0,
            "fumble_lost": 0,
            "passer_player_id": None, "fumbled_1_player_id": None,
            "interception_player_id": None,
            "forced_fumble_player_1_player_id": None,
            "fumble_recovery_1_player_id": None,
            "game_id": "g", "play_id": 1, "qtr": 2, "time": "1:00",
            "away_team": "B", "home_team": "A", "desc": "turnover",
        }
        return pd.DataFrame([{**base, **r} for r in rows], columns=cols)

    def test_interception_committer_awarded(self):
        starters = {"00-qb": {"name": "QB", "position": "QB", "roster_id": 1}}
        pbp = self._pbp([
            {"yardline_100": 5, "yrdln": "KC 5", "interception": 1,
             "passer_player_id": "00-qb"},
        ])
        result = chaos.find_redzone_turnovers(pbp, starters)
        assert result["00-qb"]["count"] == 1
        assert result["00-qb"]["plays"][0]["yrdln"] == "KC 5"

    def test_interceptor_idp_awarded(self):
        starters = {"00-db": {"name": "DB", "position": "CB", "roster_id": 2}}
        pbp = self._pbp([
            {"yardline_100": 5, "interception": 1,
             "interception_player_id": "00-db"},
        ])
        result = chaos.find_redzone_turnovers(pbp, starters)
        assert result["00-db"]["count"] == 1

    def test_fumble_forcer_and_recoverer_awarded(self):
        starters = {
            "00-f": {"name": "Forcer", "position": "LB", "roster_id": 2},
            "00-r": {"name": "Recoverer", "position": "S", "roster_id": 3},
        }
        pbp = self._pbp([
            {"yardline_100": 8, "fumble_lost": 1,
             "forced_fumble_player_1_player_id": "00-f",
             "fumble_recovery_1_player_id": "00-r"},
        ])
        result = chaos.find_redzone_turnovers(pbp, starters)
        assert result["00-f"]["count"] == 1
        assert result["00-r"]["count"] == 1

    def test_turnover_outside_redzone_excluded(self):
        starters = {"00-qb": {"name": "QB", "position": "QB", "roster_id": 1}}
        pbp = self._pbp([
            {"yardline_100": 45, "interception": 1,
             "passer_player_id": "00-qb"},
        ])
        assert chaos.find_redzone_turnovers(pbp, starters) == {}

    def test_non_turnover_in_redzone_excluded(self):
        starters = {"00-qb": {"name": "QB", "position": "QB", "roster_id": 1}}
        pbp = self._pbp([
            {"yardline_100": 5, "interception": 0, "fumble_lost": 0,
             "passer_player_id": "00-qb"},
        ])
        assert chaos.find_redzone_turnovers(pbp, starters) == {}

    def test_non_starter_not_awarded(self):
        starters = {"00-qb": {"name": "QB", "position": "QB", "roster_id": 1}}
        pbp = self._pbp([
            {"yardline_100": 5, "interception": 1,
             "passer_player_id": "00-other"},
        ])
        assert chaos.find_redzone_turnovers(pbp, starters) == {}

    def test_dual_role_scores_once_per_play(self):
        # same defender forces AND recovers -> +5 once, both roles noted.
        starters = {"00-d": {"name": "D", "position": "LB", "roster_id": 2}}
        pbp = self._pbp([
            {"yardline_100": 3, "fumble_lost": 1,
             "forced_fumble_player_1_player_id": "00-d",
             "fumble_recovery_1_player_id": "00-d"},
        ])
        result = chaos.find_redzone_turnovers(pbp, starters)
        assert result["00-d"]["count"] == 1
        assert "forced fumble + fumble recovery" in result["00-d"]["plays"][0]["role"]


# ---------------------------------------------------------------------------
# print_report (scoring math + commissioner totals)
# ---------------------------------------------------------------------------

class TestPrintReport:

    @pytest.fixture
    def starters(self):
        return {
            "00-2": {
                "position": "WR", "name": "Bob Wide", "roster_id": 1,
            },
        }

    @pytest.fixture
    def team_names(self):
        return {1: "Team One"}

    @pytest.fixture
    def tackles(self):
        play = {
            "game_id": "g", "play_id": 1, "qtr": 1, "time": "1:00",
            "away_team": "B", "home_team": "A", "desc": "tackle",
        }
        return {
            "00-2": {
                "solo": 1,
                "assists": 1,
                "plays": [
                    {**play, "type": "solo"},
                    {**play, "play_id": 2, "type": "assist"},
                ],
            }
        }

    @pytest.fixture
    def drops(self):
        return {
            "00-2": {
                "count": 1,
                "plays": [{
                    "game_id": "g", "play_id": 3, "qtr": 2, "time": "2:00",
                    "away_team": "B", "home_team": "A", "desc": "drop",
                }],
            }
        }

    def test_total_includes_assists_by_default(
        self, capsys, starters, team_names, tackles, drops
    ):
        # (1 solo + 1 assist) * 15 + 1 drop * 5 = 35
        chaos.print_report(2, starters, team_names, tackles, drops,
                            count_assists=True)
        out = capsys.readouterr().out
        assert "TOTAL ADJUSTMENT: +35" in out

    def test_commissioner_total_matches_player_total(
        self, capsys, starters, team_names, tackles, drops
    ):
        chaos.print_report(2, starters, team_names, tackles, drops,
                            count_assists=True)
        out = capsys.readouterr().out
        assert "Team One" in out
        assert "TOTAL CHAOS ADJUSTMENTS" in out
        assert "+35" in out.split("COMMISSIONER")[1]

    def test_solo_only_excludes_assist_points(
        self, capsys, starters, team_names, tackles, drops
    ):
        # 1 solo * 15 + 1 drop * 5 = 20 (assist ignored)
        chaos.print_report(2, starters, team_names, tackles, drops,
                            count_assists=False)
        out = capsys.readouterr().out
        assert "TOTAL ADJUSTMENT: +20" in out

    def test_no_bonuses_message(self, capsys, team_names):
        chaos.print_report(2, {}, team_names, {}, {}, count_assists=True)
        out = capsys.readouterr().out
        assert (
            "No offensive tackle, drop, or "
            "invalid-roster-spot adjustments found." in out
        )
    def test_invalid_spot_penalty(self, capsys, starters, team_names):
        invalid = {"00-2": {"snap_pct": 0.05, "touches": 0}}
        chaos.print_report(2, starters, team_names, {}, {},
                           invalid=invalid, count_assists=True)
        out = capsys.readouterr().out
        assert "INVALID ROSTER SPOT" in out
        assert "TOTAL ADJUSTMENT: -15" in out

    def test_redzone_turnover_bonus(self, capsys, starters, team_names):
        redzone = {
            "00-2": {
                "count": 1,
                "plays": [{
                    "role": "committed turnover (interception thrown)",
                    "yrdln": "KC 18", "yardline_100": 18,
                    "game_id": "g", "play_id": 1, "qtr": 2, "time": "1:00",
                    "away_team": "B", "home_team": "A", "desc": "INT",
                }],
            }
        }
        chaos.print_report(2, starters, team_names, {}, {},
                           redzone=redzone, count_assists=True)
        out = capsys.readouterr().out
        assert "RED ZONE TURNOVER" in out
        assert "Line of scrimmage: KC 18 (red zone)" in out
        assert "TOTAL ADJUSTMENT: +5" in out

    def test_invalid_offsets_tackle_but_still_shown(
        self, capsys, starters, team_names
    ):
        tackles = {
            "00-2": {
                "solo": 1, "assists": 0,
                "plays": [{
                    "type": "solo", "game_id": "g", "play_id": 1,
                    "qtr": 1, "time": "1:00", "away_team": "B",
                    "home_team": "A", "desc": "t",
                }],
            }
        }
        invalid = {"00-2": {"snap_pct": 0.0, "touches": 0}}
        # +15 tackle - 15 invalid = 0, but the player is still reported.
        chaos.print_report(2, starters, team_names, tackles, {},
                           invalid=invalid, count_assists=True)
        out = capsys.readouterr().out
        assert "TOTAL ADJUSTMENT: +0" in out
        assert "INVALID ROSTER SPOT" in out



# ---------------------------------------------------------------------------
# find_ejection_candidates
# ---------------------------------------------------------------------------

class TestFindEjectionCandidates:

    @pytest.fixture
    def starters(self):
        return {
            "00-1": {"name": "Bob Runner", "position": "RB", "roster_id": 1},
        }

    def _pbp(self, desc):
        return pd.DataFrame([{
            "desc": desc, "game_id": "g", "play_id": 1, "qtr": 2,
            "time": "1:00", "away_team": "B", "home_team": "A",
        }])

    def test_ejection_with_started_player_name_flagged(self, starters):
        pbp = self._pbp("Penalty on A-55-B.Runner, was ejected.")
        result = chaos.find_ejection_candidates(pbp, starters)
        assert len(result) == 1
        assert result[0]["name"] == "Bob Runner"

    def test_no_ejection_keyword_not_flagged(self, starters):
        pbp = self._pbp("B.Runner rushes for 5 yards.")
        result = chaos.find_ejection_candidates(pbp, starters)
        assert result == []

    def test_ejection_without_started_player_not_flagged(self, starters):
        pbp = self._pbp("Penalty on A-99-J.Smith, was disqualified.")
        result = chaos.find_ejection_candidates(pbp, starters)
        assert result == []


# ---------------------------------------------------------------------------
# find_goal_line_fumble_candidates
# ---------------------------------------------------------------------------

class TestFindGoalLineFumbleCandidates:

    @pytest.fixture
    def starters(self):
        return {
            "00-1": {"name": "Bob Runner", "position": "RB", "roster_id": 1},
        }

    def _pbp(self, rows):
        cols = [
            "fumble_not_forced", "fumbled_1_player_id", "yardline_100",
            "yards_gained", "touchback", "fumble_out_of_bounds", "touchdown",
            "game_id", "play_id", "qtr", "time",
            "away_team", "home_team", "desc",
        ]
        base = {
            "game_id": "g", "play_id": 1, "qtr": 4, "time": "0:30",
            "away_team": "B", "home_team": "A", "desc": "fumble",
            "yards_gained": 0, "touchback": 0, "fumble_out_of_bounds": 0,
        }
        return pd.DataFrame([{**base, **r} for r in rows], columns=cols)

    def test_short_yardage_goal_line_fumble_flagged(self, starters):
        pbp = self._pbp([
            {"fumble_not_forced": 1, "fumbled_1_player_id": "00-1",
             "yardline_100": 2, "yards_gained": 0, "touchdown": 0},
        ])
        result = chaos.find_goal_line_fumble_candidates(pbp, starters)
        assert len(result) == 1
        assert result[0]["name"] == "Bob Runner"

    def test_long_td_fumble_uses_fumble_spot_not_snap(self, starters):
        # DeMercado-style: snap at own 28 (yardline_100=72), 71-yard run,
        # fumble at the 1. Snap-based logic would miss it; fumble-spot catches.
        pbp = self._pbp([
            {"fumble_not_forced": 1, "fumbled_1_player_id": "00-1",
             "yardline_100": 72, "yards_gained": 71, "touchdown": 0},
        ])
        result = chaos.find_goal_line_fumble_candidates(pbp, starters)
        assert len(result) == 1
        assert result[0]["fumble_spot"] == 1

    def test_fumble_through_end_zone_touchback_flagged(self, starters):
        # touchback branch: flagged even if snap was far from goal.
        pbp = self._pbp([
            {"fumble_not_forced": 1, "fumbled_1_player_id": "00-1",
             "yardline_100": 72, "yards_gained": 71, "touchback": 1,
             "fumble_out_of_bounds": 1, "touchdown": 0},
        ])
        result = chaos.find_goal_line_fumble_candidates(pbp, starters)
        assert len(result) == 1
        assert result[0]["through_end_zone"] is True

    def test_forced_fumble_excluded(self, starters):
        pbp = self._pbp([
            {"fumble_not_forced": 0, "fumbled_1_player_id": "00-1",
             "yardline_100": 2, "yards_gained": 0, "touchdown": 0},
        ])
        assert chaos.find_goal_line_fumble_candidates(pbp, starters) == []

    def test_non_starter_fumbler_excluded(self, starters):
        pbp = self._pbp([
            {"fumble_not_forced": 1, "fumbled_1_player_id": "00-999",
             "yardline_100": 2, "yards_gained": 0, "touchdown": 0},
        ])
        assert chaos.find_goal_line_fumble_candidates(pbp, starters) == []

    def test_midfield_fumble_excluded(self, starters):
        # fumble spot = 50 - 5 = 45, no touchback -> not flagged.
        pbp = self._pbp([
            {"fumble_not_forced": 1, "fumbled_1_player_id": "00-1",
             "yardline_100": 50, "yards_gained": 5, "touchdown": 0},
        ])
        assert chaos.find_goal_line_fumble_candidates(pbp, starters) == []


# ---------------------------------------------------------------------------
# print_candidates
# ---------------------------------------------------------------------------

class TestPrintCandidates:

    def test_empty_message(self, capsys):
        chaos.print_candidates([], [], {1: "Team One"})
        out = capsys.readouterr().out
        assert "NOT SCORED" in out
        assert "No ejection or goal-line-celebration" in out

    def test_fumble_candidate_printed_but_not_scored(self, capsys):
        fumbles = [{
            "name": "Bob Runner", "position": "RB", "roster_id": 1,
            "yardline_100": 2, "touchdown": 0, "play_id": 1, "qtr": 4,
            "time": "0:30", "away_team": "B", "home_team": "A",
            "desc": "FUMBLES at the 2",
        }]
        chaos.print_candidates([], fumbles, {1: "Team One"})
        out = capsys.readouterr().out
        assert "PREMATURE-CELEBRATION FUMBLES" in out
        assert "Bob Runner" in out
        # never contributes to a scored total
        assert "TOTAL CHAOS ADJUSTMENTS" not in out


# ---------------------------------------------------------------------------
# load_injured_out
# ---------------------------------------------------------------------------

class TestLoadInjuredOut:

    def test_out_and_doubtful_included_questionable_excluded(self, monkeypatch):
        df = pd.DataFrame([
            {"week": 2, "gsis_id": " 00-1 ", "report_status": "Out",
             "report_primary_injury": "Knee"},
            {"week": 2, "gsis_id": "00-2", "report_status": "Questionable",
             "report_primary_injury": "Ankle"},
            {"week": 2, "gsis_id": "00-4", "report_status": "Doubtful",
             "report_primary_injury": "Hamstring"},
            {"week": 3, "gsis_id": "00-3", "report_status": "Out",
             "report_primary_injury": "Groin"},
        ])
        monkeypatch.setattr(chaos.nfl, "import_injuries", lambda years: df)

        result = chaos.load_injured_out(2025, 2)

        assert result == {
            "00-1": "ruled Out (Knee)",
            "00-4": "ruled Doubtful (Hamstring)",
        }


# ---------------------------------------------------------------------------
# find_ingame_injuries
# ---------------------------------------------------------------------------

class TestFindIngameInjuries:

    @pytest.fixture
    def starters(self):
        return {
            "00-1": {"name": "Bob Runner", "position": "RB", "roster_id": 1},
        }

    def test_injury_language_with_name_flagged(self, starters):
        pbp = pd.DataFrame([{"desc": "5-B.Runner was injured on the play."}])
        result = chaos.find_ingame_injuries(pbp, starters)
        assert "00-1" in result

    def test_no_injury_language_not_flagged(self, starters):
        pbp = pd.DataFrame([{"desc": "5-B.Runner rushes for 4 yards."}])
        result = chaos.find_ingame_injuries(pbp, starters)
        assert result == {}


# ---------------------------------------------------------------------------
# resolve_exempt_players
# ---------------------------------------------------------------------------

class TestResolveExemptPlayers:

    @pytest.fixture
    def starters(self):
        return {
            "00-1": {"name": "Bob Runner", "position": "RB", "roster_id": 1},
        }

    def test_resolve_by_gsis(self, starters):
        result = chaos.resolve_exempt_players(["00-1"], starters)
        assert "00-1" in result

    def test_resolve_by_full_name_case_insensitive(self, starters):
        result = chaos.resolve_exempt_players(["bob runner"], starters)
        assert "00-1" in result

    def test_unmatched_token_ignored(self, starters, capsys):
        result = chaos.resolve_exempt_players(["Nobody Here"], starters)
        assert result == {}
        assert "did not match" in capsys.readouterr().out

    def test_empty_tokens(self, starters):
        assert chaos.resolve_exempt_players([], starters) == {}


# ---------------------------------------------------------------------------
# print_report exempt display
# ---------------------------------------------------------------------------

class TestPrintReportExempt:

    def test_exempt_player_shown_but_not_scored(self, capsys):
        starters = {
            "00-2": {"position": "WR", "name": "Bob Wide", "roster_id": 1},
        }
        exempt = {
            "00-2": {"snap_pct": 0.0, "touches": 0,
                     "reason": "injury: ruled Out (Knee)"},
        }
        chaos.print_report(2, starters, {1: "Team One"}, {}, {},
                           invalid={}, exempt=exempt, count_assists=True)
        out = capsys.readouterr().out
        assert "EXEMPT FROM INVALID ROSTER PENALTY" in out
        assert "injury: ruled Out (Knee)" in out
        # no scored adjustments -> no commissioner totals section
        assert "TOTAL CHAOS ADJUSTMENTS" not in out
