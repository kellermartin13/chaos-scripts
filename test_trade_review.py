"""
Tests for trade_review.py.

Focus: correctness of the trade review.
  - The full dynasty chain is walked newest -> oldest (and loops are safe).
  - Every completed trade across seasons/weeks is collected and deduped.
  - Traded draft picks resolve to the players they became; future picks
    remain unresolved placeholders.
  - Production is scored with the league's own scoring settings (dot product).
  - "Since the trade" counts from the filed week inclusive, across seasons.
  - Assets are grouped onto the side that received them and a winner verdict
    is computed.

All Sleeper I/O is injected/monkeypatched; no network access.
"""

import pytest

import trade_review as tr


# ---------------------------------------------------------------------------
# format_team_label
# ---------------------------------------------------------------------------

class TestFormatTeamLabel:

    def test_team_name_and_username_combined(self):
        user = {"username": "macflyguy",
                "metadata": {"team_name": "RB FACTORY"}}

        assert tr.format_team_label(user, 5) == "RB FACTORY (macflyguy)"

    def test_falls_back_to_display_name_as_handle(self):
        # Sleeper often leaves username null; display_name is the handle.
        user = {"username": None, "display_name": "tcwahl",
                "metadata": {"team_name": "Dad Bod Squad"}}

        assert tr.format_team_label(user, 5) == "Dad Bod Squad (tcwahl)"

    def test_handle_only_when_no_team_name(self):
        user = {"display_name": "macflyguy", "metadata": {}}

        assert tr.format_team_label(user, 5) == "macflyguy"

    def test_team_only_when_no_handle(self):
        user = {"metadata": {"team_name": "Orphan Team"}}

        assert tr.format_team_label(user, 5) == "Orphan Team"

    def test_roster_fallback_when_owner_unknown(self):
        assert tr.format_team_label({}, 9) == "Roster 9"

    def test_none_user_is_roster_fallback(self):
        assert tr.format_team_label(None, 3) == "Roster 3"


# ---------------------------------------------------------------------------
# build_league_chain
# ---------------------------------------------------------------------------

class TestBuildLeagueChain:

    @pytest.fixture
    def leagues(self):
        return {
            "2026": {
                "league_id": "2026",
                "season": "2026",
                "previous_league_id": "2025",
            },
            "2025": {
                "league_id": "2025",
                "season": "2025",
                "previous_league_id": None,
            },
        }

    def test_walks_newest_to_oldest(self, leagues):
        chain = tr.build_league_chain("2026", fetch=lambda lid: leagues[lid])

        assert [l["season"] for l in chain] == ["2026", "2025"]

    def test_single_season_chain(self, leagues):
        chain = tr.build_league_chain("2025", fetch=lambda lid: leagues[lid])

        assert [l["season"] for l in chain] == ["2025"]

    def test_loop_is_broken(self):
        looped = {
            "A": {"league_id": "A", "season": "1", "previous_league_id": "B"},
            "B": {"league_id": "B", "season": "0", "previous_league_id": "A"},
        }

        chain = tr.build_league_chain("A", fetch=lambda lid: looped[lid])

        # Two distinct leagues, then the loop back to A is refused.
        assert len(chain) == 2


# ---------------------------------------------------------------------------
# collect_trades
# ---------------------------------------------------------------------------

class TestCollectTrades:

    @pytest.fixture
    def chain(self):
        return [
            {"league_id": "2026", "season": "2026"},
            {"league_id": "2025", "season": "2025"},
        ]

    def _fetch(self, transactions_by_league_week):
        def fetch(league_id, week):
            return transactions_by_league_week.get((league_id, week), [])
        return fetch

    def test_only_completed_trades_kept(self, chain):
        data = {
            ("2025", 1): [
                {"type": "trade", "status": "complete",
                 "transaction_id": "t1", "status_updated": 10},
                {"type": "trade", "status": "failed",
                 "transaction_id": "t2", "status_updated": 20},
                {"type": "waiver", "status": "complete",
                 "transaction_id": "w1", "status_updated": 30},
            ],
        }

        trades = tr.collect_trades(
            chain, fetch=self._fetch(data), weeks=range(1, 3)
        )

        assert [t["transaction_id"] for t in trades] == ["t1"]

    def test_attaches_season_and_week(self, chain):
        data = {
            ("2025", 8): [
                {"type": "trade", "status": "complete",
                 "transaction_id": "t1", "status_updated": 10},
            ],
        }

        trades = tr.collect_trades(
            chain, fetch=self._fetch(data), weeks=range(1, 10)
        )

        assert trades[0]["season"] == "2025"
        assert trades[0]["week"] == 8

    def test_sorted_oldest_to_newest(self, chain):
        data = {
            ("2026", 1): [
                {"type": "trade", "status": "complete",
                 "transaction_id": "new", "status_updated": 999},
            ],
            ("2025", 1): [
                {"type": "trade", "status": "complete",
                 "transaction_id": "old", "status_updated": 1},
            ],
        }

        trades = tr.collect_trades(
            chain, fetch=self._fetch(data), weeks=range(1, 2)
        )

        assert [t["transaction_id"] for t in trades] == ["old", "new"]

    def test_dedups_by_transaction_id(self, chain):
        # Same transaction_id surfaced twice must appear once.
        data = {
            ("2025", 1): [
                {"type": "trade", "status": "complete",
                 "transaction_id": "dup", "status_updated": 5},
            ],
            ("2025", 2): [
                {"type": "trade", "status": "complete",
                 "transaction_id": "dup", "status_updated": 5},
            ],
        }

        trades = tr.collect_trades(
            chain, fetch=self._fetch(data), weeks=range(1, 3)
        )

        assert len(trades) == 1


# ---------------------------------------------------------------------------
# build_pick_index / resolve_pick
# ---------------------------------------------------------------------------

class TestPickResolution:

    @pytest.fixture
    def chain(self):
        return [
            {"league_id": "2026", "season": "2026", "draft_id": "d2026"},
        ]

    @pytest.fixture
    def draft_meta(self):
        # slot 3 originally belongs to roster 4; roster 11 acquired and drafted
        # the pick, so the selection is stamped roster_id=11 but draft_slot=3.
        meta = {
            "d2026": {"slot_to_roster_id": {"3": 4}},
        }
        return lambda draft_id: meta[draft_id]

    @pytest.fixture
    def picks_fetch(self):
        picks = {
            "d2026": [
                {
                    "round": 2,
                    "draft_slot": 3,
                    "roster_id": 11,          # drafter, NOT original owner
                    "player_id": "9001",
                    "metadata": {
                        "first_name": "Rookie",
                        "last_name": "Back",
                        "position": "RB",
                    },
                },
            ],
        }
        return lambda draft_id: picks[draft_id]

    @pytest.fixture
    def index(self, chain, picks_fetch, draft_meta):
        return tr.build_pick_index(
            chain, picks_fetch=picks_fetch, draft_fetch=draft_meta
        )

    def test_index_keyed_by_original_owner_via_slot(self, index):
        # draft_slot 3 -> original owner roster 4 (not the drafting roster 11).
        assert ("2026", 2, 4) in index
        assert ("2026", 2, 11) not in index

    def test_resolves_traded_pick_to_drafted_player(self, index):
        # The traded pick carries roster_id = ORIGINAL owner (4); owner_id is
        # the new owner who received it in the trade.
        pick = {"season": "2026", "round": 2, "roster_id": 4, "owner_id": 11}

        asset = tr.resolve_pick(pick, index)

        assert asset["resolved"] is True
        assert asset["player_id"] == "9001"
        assert "Rookie Back" in asset["label"]

    def test_future_pick_is_unresolved_placeholder(self, index):
        pick = {"season": "2027", "round": 1, "roster_id": 4, "owner_id": 11}

        asset = tr.resolve_pick(pick, index)

        assert asset["resolved"] is False
        assert asset["player_id"] is None
        assert "pending" in asset["label"]

    def test_player_map_overrides_name_and_position(self, index):
        players = {
            "9001": {"full_name": "Rookie Back Jr.", "position": "WR"},
        }
        pick = {"season": "2026", "round": 2, "roster_id": 4, "owner_id": 11}

        asset = tr.resolve_pick(pick, index, players=players)

        assert asset["name"] == "Rookie Back Jr."
        assert asset["position"] == "WR"

    def test_falls_back_to_roster_id_without_slot_map(self, chain):
        # No slot_to_roster_id -> key by the drafting roster_id as a best effort.
        picks = {
            "d2026": [
                {"round": 1, "draft_slot": 5, "roster_id": 7,
                 "player_id": "p", "metadata": {"first_name": "A",
                                                "last_name": "B",
                                                "position": "QB"}},
            ],
        }
        index = tr.build_pick_index(
            chain,
            picks_fetch=lambda d: picks[d],
            draft_fetch=lambda d: {},
        )

        assert ("2026", 1, 7) in index


# ---------------------------------------------------------------------------
# score_stat_line
# ---------------------------------------------------------------------------

class TestScoreStatLine:

    def test_dot_product_of_stats_and_scoring(self):
        stat_line = {"rec": 5, "rec_yd": 80, "rec_td": 1}
        scoring = {"rec": 0.5, "rec_yd": 0.1, "rec_td": 6.0}

        # 5*0.5 + 80*0.1 + 1*6 = 2.5 + 8 + 6 = 16.5
        assert tr.score_stat_line(stat_line, scoring) == pytest.approx(16.5)

    def test_te_premium_is_honored(self):
        stat_line = {"rec": 4, "bonus_rec_te": 4}
        scoring = {"rec": 0.5, "bonus_rec_te": 1.0}

        # 4*0.5 + 4*1 = 2 + 4 = 6
        assert tr.score_stat_line(stat_line, scoring) == pytest.approx(6.0)

    def test_unscored_stats_ignored(self):
        stat_line = {"rec": 2, "made_up_stat": 999}
        scoring = {"rec": 1.0}

        assert tr.score_stat_line(stat_line, scoring) == pytest.approx(2.0)

    def test_non_numeric_values_ignored(self):
        stat_line = {"rec": 3, "team": "BUF"}
        scoring = {"rec": 1.0, "team": 5.0}

        assert tr.score_stat_line(stat_line, scoring) == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# compute_production_since
# ---------------------------------------------------------------------------

class TestComputeProductionSince:

    @pytest.fixture
    def chain_index(self):
        return {
            "seasons": ["2025", "2026"],
            "scoring": {
                "2025": {"rec": 1.0},
                "2026": {"rec": 1.0},
            },
        }

    @pytest.fixture
    def stats(self):
        # (season, week) -> {player_id: stat_line}
        return {
            ("2025", 7): {"p": {"rec": 5, "gp": 1}},
            ("2025", 8): {"p": {"rec": 10, "gp": 1}},
            ("2025", 9): {"p": {"rec": 3, "gp": 1}},
            ("2026", 1): {"p": {"rec": 4, "gp": 1}},
        }

    @pytest.fixture
    def cache(self, stats):
        return tr.WeeklyStatsCache(
            fetch=lambda season, week: stats.get((str(season), week), {})
        )

    def test_counts_from_filed_week_inclusive(
        self, chain_index, cache
    ):
        # Trade filed 2025 week 8 -> include wk8, wk9 (2025) + wk1 (2026),
        # exclude wk7.
        result = tr.compute_production_since(
            "p", "2025", 8, chain_index, cache, weeks=range(1, 19)
        )

        assert result["total"] == pytest.approx(17.0)  # 10 + 3 + 4

    def test_counts_games_played(self, chain_index, cache):
        result = tr.compute_production_since(
            "p", "2025", 8, chain_index, cache, weeks=range(1, 19)
        )

        assert result["games"] == 3

    def test_later_seasons_counted_in_full(self, chain_index, cache):
        # Filed 2026 week 1 -> only 2026 wk1 (2025 excluded entirely).
        result = tr.compute_production_since(
            "p", "2026", 1, chain_index, cache, weeks=range(1, 19)
        )

        assert result["total"] == pytest.approx(4.0)

    def test_uses_per_season_scoring(self, cache):
        chain_index = {
            "seasons": ["2025", "2026"],
            "scoring": {
                "2025": {"rec": 2.0},   # 2025 double-PPR
                "2026": {"rec": 1.0},
            },
        }

        result = tr.compute_production_since(
            "p", "2025", 8, chain_index, cache, weeks=range(1, 19)
        )

        # 2025: (10+3)*2 = 26 ; 2026: 4*1 = 4
        assert result["total"] == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# received_assets
# ---------------------------------------------------------------------------

class TestReceivedAssets:

    @pytest.fixture
    def players(self):
        return {
            "pl_a": {"full_name": "Alpha Player", "position": "WR"},
            "pl_b": {"full_name": "Beta Player", "position": "RB"},
        }

    def test_players_grouped_to_receiving_roster(self, players):
        trade = {
            "roster_ids": [1, 2],
            "adds": {"pl_a": 1, "pl_b": 2},
            "drops": {"pl_a": 2, "pl_b": 1},
            "draft_picks": [],
            "waiver_budget": [],
        }

        sides = tr.received_assets(trade, players, {})

        assert sides[1]["assets"][0]["player_id"] == "pl_a"
        assert sides[2]["assets"][0]["player_id"] == "pl_b"

    def test_pick_grouped_to_owner_id(self, players):
        trade = {
            "roster_ids": [1, 2],
            "adds": None,
            "draft_picks": [
                {"season": "2027", "round": 1, "roster_id": 1, "owner_id": 2},
            ],
            "waiver_budget": [],
        }

        sides = tr.received_assets(trade, players, {})

        assert sides[2]["assets"][0]["kind"] == "pick"
        assert 1 in sides  # sending roster still present

    def test_faab_added_to_receiver(self, players):
        trade = {
            "roster_ids": [1, 2],
            "adds": None,
            "draft_picks": [],
            "waiver_budget": [{"sender": 1, "receiver": 2, "amount": 15}],
        }

        sides = tr.received_assets(trade, players, {})

        assert sides[2]["faab_in"] == 15


# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------

class TestVerdict:

    def test_higher_total_wins_with_margin(self):
        winner, margin = tr.verdict({1: 100.0, 2: 60.0})

        assert winner == 1
        assert margin == pytest.approx(40.0)

    def test_tie_is_even(self):
        winner, margin = tr.verdict({1: 50.0, 2: 50.0})

        assert winner is None
        assert margin == 0.0

    def test_no_production_is_even(self):
        winner, margin = tr.verdict({1: 0.0, 2: 0.0})

        assert winner is None


# ---------------------------------------------------------------------------
# assess_lopsidedness
# ---------------------------------------------------------------------------

class TestAssessLopsidedness:

    def test_heist_at_three_x(self):
        # 210 vs 60 -> margin 150, ratio 3.5x
        assert tr.assess_lopsidedness({1: 210.0, 2: 60.0}, 1, 150.0) == "heist"

    def test_lopsided_at_two_x(self):
        # 130 vs 60 -> margin 70, ratio ~2.17x
        assert tr.assess_lopsidedness({1: 130.0, 2: 60.0}, 1, 70.0) == "lopsided"

    def test_close_trade_not_flagged(self):
        # 234 vs 132 -> big margin but only 1.77x
        assert tr.assess_lopsidedness({1: 234.0, 2: 132.0}, 1, 102.0) is None

    def test_small_margin_not_flagged_despite_ratio(self):
        # 3x ratio but margin below the absolute gate.
        assert tr.assess_lopsidedness({1: 3.0, 2: 1.0}, 1, 2.0) is None

    def test_shutout_is_heist(self):
        # Runner-up produced nothing, winner cleared the margin gate.
        assert tr.assess_lopsidedness({1: 120.0, 2: 0.0}, 1, 120.0) == "heist"

    def test_no_winner_is_not_lopsided(self):
        assert tr.assess_lopsidedness({1: 50.0, 2: 50.0}, None, 0.0) is None


# ---------------------------------------------------------------------------
# runner_up_of
# ---------------------------------------------------------------------------

class TestRunnerUpOf:

    def test_returns_highest_non_winner(self):
        roster, pts = tr.runner_up_of({1: 200.0, 2: 90.0, 3: 120.0}, 1)

        assert roster == 3
        assert pts == pytest.approx(120.0)

    def test_no_other_side_returns_none(self):
        roster, pts = tr.runner_up_of({1: 200.0}, 1)

        assert roster is None
        assert pts == 0.0


# ---------------------------------------------------------------------------
# print_lopsided_summary
# ---------------------------------------------------------------------------

class TestPrintLopsidedSummary:

    def _review(self, lopsided, totals, winner, margin, season="2025", week=4):
        return {
            "season": season, "week": week, "winner_roster": winner,
            "totals": totals, "margin": margin, "lopsided": lopsided,
        }

    def test_nothing_printed_when_no_lopsided_trades(self, capsys):
        reviews = [self._review(None, {1: 60.0, 2: 50.0}, 1, 10.0)]

        tr.print_lopsided_summary(reviews, {"2025": {1: "A", 2: "B"}})

        assert capsys.readouterr().out == ""

    def test_flagged_trade_appears_in_summary(self, capsys):
        reviews = [
            self._review(None, {1: 60.0, 2: 50.0}, 1, 10.0),
            self._review("lopsided", {3: 130.0, 4: 60.0}, 3, 70.0),
        ]
        names = {"2025": {3: "Winner Team (winuser)", 4: "Loser Team"}}

        tr.print_lopsided_summary(reviews, names)

        out = capsys.readouterr().out
        assert "LOPSIDED TRADES" in out
        assert "Winner Team (winuser)" in out
        assert "Loser Team" in out
        assert "2.2x more" in out

    def test_heist_labeled_and_ordered_first(self, capsys):
        reviews = [
            self._review("lopsided", {1: 130.0, 2: 60.0}, 1, 70.0, week=1),
            self._review("heist", {3: 210.0, 4: 60.0}, 3, 150.0, week=2),
        ]
        names = {"2025": {1: "A", 2: "B", 3: "C", 4: "D"}}

        tr.print_lopsided_summary(reviews, names)

        out = capsys.readouterr().out
        # Heist (3.5x) is more lopsided than lopsided (2.2x), so it lists first.
        assert out.index("HEIST") < out.index("LOPSIDED *** [2025 Week 1]")

    def test_shutout_runner_up_renders(self, capsys):
        reviews = [self._review("heist", {1: 120.0, 2: 0.0}, 1, 120.0)]

        tr.print_lopsided_summary(reviews, {"2025": {1: "A", 2: "B"}})

        assert "a shutout" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# compute_manager_overview / manager_rankings
# ---------------------------------------------------------------------------

class TestManagerOverview:

    @pytest.fixture
    def owner_map(self):
        # Two seasons; roster ids reused but owners are stable users.
        return {
            ("2025", 1): "userA",
            ("2025", 2): "userB",
            ("2026", 1): "userB",   # rosters swapped between seasons
            ("2026", 2): "userA",
        }

    @pytest.fixture
    def reviews(self):
        return [
            # 2025: roster 1 (A) 100 vs roster 2 (B) 40 -> A wins
            {"season": "2025", "totals": {1: 100.0, 2: 40.0},
             "winner_roster": 1},
            # 2026: roster 1 (B) 30 vs roster 2 (A) 90 -> A wins again
            {"season": "2026", "totals": {1: 30.0, 2: 90.0},
             "winner_roster": 2},
        ]

    def test_net_aggregates_across_seasons(self, reviews, owner_map):
        overview = tr.compute_manager_overview(reviews, owner_map)

        # A: (100-40) + (90-30) = 60 + 60 = 120
        assert overview["userA"]["net"] == pytest.approx(120.0)
        # B: (40-100) + (30-90) = -60 + -60 = -120
        assert overview["userB"]["net"] == pytest.approx(-120.0)

    def test_received_and_record(self, reviews, owner_map):
        overview = tr.compute_manager_overview(reviews, owner_map)

        assert overview["userA"]["received"] == pytest.approx(190.0)
        assert overview["userA"]["wins"] == 2
        assert overview["userB"]["losses"] == 2

    def test_tie_counted(self, owner_map):
        reviews = [
            {"season": "2025", "totals": {1: 50.0, 2: 50.0},
             "winner_roster": None},
        ]

        overview = tr.compute_manager_overview(reviews, owner_map)

        assert overview["userA"]["ties"] == 1
        assert overview["userB"]["ties"] == 1

    def test_rankings_pick_best_worst_and_active(self, reviews, owner_map):
        overview = tr.compute_manager_overview(reviews, owner_map)
        rankings = tr.manager_rankings(overview)

        assert rankings["best"] == "userA"
        assert rankings["worst"] == "userB"

    def test_rankings_empty_overview(self):
        rankings = tr.manager_rankings({})

        assert rankings == {
            "best": None, "worst": None,
            "most_active": None, "most_passive": None,
        }

    def test_non_trader_seeded_and_most_passive(self):
        # userC owns a roster but never appears in a trade.
        owner_map = {
            ("2025", 1): "userA",
            ("2025", 2): "userB",
            ("2025", 3): "userC",
        }
        reviews = [
            {"season": "2025", "totals": {1: 100.0, 2: 40.0},
             "winner_roster": 1},
        ]

        overview = tr.compute_manager_overview(reviews, owner_map)

        assert overview["userC"]["trades"] == 0
        rankings = tr.manager_rankings(overview)
        assert rankings["most_passive"] == "userC"
        assert rankings["most_active"] in ("userA", "userB")

    def test_print_overview_shows_headlines(self, reviews, owner_map, capsys):
        overview = tr.compute_manager_overview(reviews, owner_map)
        names = {"userA": "Team A (alpha)", "userB": "Team B (bravo)"}

        tr.print_manager_overview(overview, names)

        out = capsys.readouterr().out
        assert "MANAGER OVERVIEW" in out
        assert "Best trader:" in out and "Team A (alpha)" in out
        assert "Worst trader:" in out and "Team B (bravo)" in out
        assert "Most aggressive:" in out
        assert "Most passive:" in out

    def test_print_overview_empty_is_silent(self, capsys):
        tr.print_manager_overview({}, {})

        assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# build_trade_review (integration of the pure pieces)
# ---------------------------------------------------------------------------

class TestBuildTradeReview:

    @pytest.fixture
    def players(self):
        return {
            "star": {"full_name": "Star Player", "position": "WR"},
            "bust": {"full_name": "Bust Player", "position": "RB"},
        }

    @pytest.fixture
    def chain_index(self):
        return {
            "seasons": ["2025"],
            "scoring": {"2025": {"rec": 1.0}},
        }

    @pytest.fixture
    def stats_cache(self):
        stats = {
            ("2025", 8): {
                "star": {"rec": 20, "gp": 1},
                "bust": {"rec": 2, "gp": 1},
            },
        }
        return tr.WeeklyStatsCache(
            fetch=lambda s, w: stats.get((str(s), w), {})
        )

    @pytest.fixture
    def trade(self):
        return {
            "transaction_id": "t1",
            "season": "2025",
            "week": 8,
            "status_updated": 100,
            "roster_ids": [1, 2],
            "adds": {"star": 1, "bust": 2},
            "draft_picks": [],
            "waiver_budget": [],
        }

    def test_winner_is_side_with_more_production(
        self, trade, players, chain_index, stats_cache
    ):
        review = tr.build_trade_review(
            trade, players, {}, chain_index, stats_cache
        )

        assert review["winner_roster"] == 1

    def test_totals_reflect_dot_product(
        self, trade, players, chain_index, stats_cache
    ):
        review = tr.build_trade_review(
            trade, players, {}, chain_index, stats_cache
        )

        assert review["totals"][1] == pytest.approx(20.0)
        assert review["totals"][2] == pytest.approx(2.0)

    def test_lopsided_field_present(
        self, trade, players, chain_index, stats_cache
    ):
        review = tr.build_trade_review(
            trade, players, {}, chain_index, stats_cache
        )

        # 20 vs 2 is a big ratio but margin (18) is below the gate.
        assert review["lopsided"] is None


# ---------------------------------------------------------------------------
# build_all_reviews (full pipeline with injected I/O)
# ---------------------------------------------------------------------------

class TestBuildAllReviews:

    @pytest.fixture
    def chain(self):
        return [
            {"league_id": "2026", "season": "2026", "draft_id": "d2026",
             "scoring_settings": {"rec": 1.0}},
            {"league_id": "2025", "season": "2025", "draft_id": "d2025",
             "scoring_settings": {"rec": 1.0}},
        ]

    @pytest.fixture
    def players(self):
        return {"pl": {"full_name": "Traded Guy", "position": "WR"}}

    def test_pipeline_scores_a_pick_that_became_a_player(self, chain, players):
        # A 2025 week-8 trade sends roster 1's 2026 R1 pick to roster 2; that
        # pick became "rook", who scores in 2026.
        transactions = {
            ("2025", 8): [
                {
                    "type": "trade", "status": "complete",
                    "transaction_id": "t1", "status_updated": 100,
                    "roster_ids": [1, 2],
                    "adds": {"pl": 1},
                    "draft_picks": [
                        {"season": "2026", "round": 1,
                         "roster_id": 1, "owner_id": 2},
                    ],
                    "waiver_budget": [],
                },
            ],
        }
        drafts = {
            "d2026": [
                {"round": 1, "draft_slot": 1, "roster_id": 1,
                 "player_id": "rook",
                 "metadata": {"first_name": "Rook", "last_name": "Ie",
                              "position": "RB"}},
            ],
            "d2025": [],
        }
        draft_meta = {
            "d2026": {"slot_to_roster_id": {"1": 1}},
            "d2025": {"slot_to_roster_id": {}},
        }
        stats = {
            ("2026", 1): {"rook": {"rec": 12, "gp": 1}},
            ("2025", 8): {"pl": {"rec": 5, "gp": 1}},
        }

        reviews = tr.build_all_reviews(
            chain,
            players,
            transactions_fetch=lambda lid, wk: transactions.get((lid, wk), []),
            draft_picks_fetch=lambda did: drafts.get(did, []),
            draft_meta_fetch=lambda did: draft_meta.get(did, {}),
            stats_fetch=lambda s, w: stats.get((str(s), w), {}),
        )

        assert len(reviews) == 1

        review = reviews[0]

        # Roster 2 received the pick -> "rook" (12 pts in 2026).
        assert review["totals"][2] == pytest.approx(12.0)
        # Roster 1 received the player "pl" (5 pts from wk8 2025).
        assert review["totals"][1] == pytest.approx(5.0)
        assert review["winner_roster"] == 2

    def test_season_filter(self, chain, players):
        transactions = {
            ("2025", 1): [
                {"type": "trade", "status": "complete",
                 "transaction_id": "t2025", "status_updated": 1,
                 "roster_ids": [1, 2], "adds": {"pl": 1},
                 "draft_picks": [], "waiver_budget": []},
            ],
            ("2026", 1): [
                {"type": "trade", "status": "complete",
                 "transaction_id": "t2026", "status_updated": 2,
                 "roster_ids": [1, 2], "adds": {"pl": 1},
                 "draft_picks": [], "waiver_budget": []},
            ],
        }

        reviews = tr.build_all_reviews(
            chain,
            players,
            season_filter="2026",
            transactions_fetch=lambda lid, wk: transactions.get((lid, wk), []),
            draft_picks_fetch=lambda did: [],
            draft_meta_fetch=lambda did: {},
            stats_fetch=lambda s, w: {},
        )

        assert [r["transaction_id"] for r in reviews] == ["t2026"]


# ---------------------------------------------------------------------------
# build_replacement_ranks (WAR-style: leaguewide starter demand per position)
# ---------------------------------------------------------------------------

class TestBuildReplacementRanks:

    @pytest.fixture
    def league(self):
        return {
            "total_rosters": 10,
            "roster_positions": [
                "QB", "RB", "RB", "WR", "WR", "WR", "TE",
                "FLEX", "K", "DEF", "BN", "BN",
            ],
        }

    def test_dedicated_slots_scaled_by_teams(self, league):
        ranks = tr.build_replacement_ranks(league)

        assert ranks["QB"] == pytest.approx(10.0)  # 1 slot * 10 teams

    def test_flex_demand_spread_across_eligible_positions(self, league):
        ranks = tr.build_replacement_ranks(league)

        # RB: (2 dedicated + 1/3 flex) * 10 = 23.333...
        assert ranks["RB"] == pytest.approx(23.3333, abs=1e-3)

    def test_bench_slots_create_no_demand(self, league):
        ranks = tr.build_replacement_ranks(league)

        # WR: (3 dedicated + 1/3 flex) * 10 = 33.333...; BN never counted.
        assert ranks["WR"] == pytest.approx(33.3333, abs=1e-3)

    def test_super_flex_adds_qb_share(self):
        league = {
            "total_rosters": 12,
            "roster_positions": ["QB", "SUPER_FLEX"],
        }

        ranks = tr.build_replacement_ranks(league)

        # QB: (1 dedicated + 1/4 superflex) * 12 = 15.0
        assert ranks["QB"] == pytest.approx(15.0)

    def test_missing_team_count_yields_zero(self):
        ranks = tr.build_replacement_ranks({"roster_positions": ["QB"]})

        assert ranks["QB"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# ReplacementBaselineCache
# ---------------------------------------------------------------------------

class TestReplacementBaselineCache:

    @pytest.fixture
    def chain_index(self):
        return {"scoring": {"2025": {"rec": 1.0}}}

    @pytest.fixture
    def players(self):
        return {
            "wr1": {"position": "WR"},
            "wr2": {"position": "WR"},
            "wr3": {"position": "WR"},
            "wr4": {"position": "WR"},
            "te1": {"position": "TE"},
        }

    @pytest.fixture
    def stats(self):
        return {
            ("2025", 1): {
                "wr1": {"rec": 10},
                "wr2": {"rec": 8},
                "wr3": {"rec": 6},
                "wr4": {"rec": 4},
                "te1": {"rec": 9},
            },
        }

    @pytest.fixture
    def cache_factory(self, chain_index, players, stats):
        def make(ranks, band=tr.DEFAULT_REPLACEMENT_BAND):
            stats_cache = tr.WeeklyStatsCache(
                fetch=lambda season, week: stats.get((str(season), week), {})
            )
            return tr.ReplacementBaselineCache(
                chain_index, players, stats_cache, ranks, band=band
            )
        return make

    def test_single_rank_band_reads_replacement_player(self, cache_factory):
        # 2 WR starters leaguewide -> replacement is rank index 2 (0-based) = 6.
        cache = cache_factory({"2025": {"WR": 2}}, band=1)

        assert cache.get("2025", 1)["WR"] == pytest.approx(6.0)

    def test_band_averages_multiple_ranks(self, cache_factory):
        # 2 starters, band 2 -> mean of ranks [2,3] = (6 + 4) / 2 = 5.
        cache = cache_factory({"2025": {"WR": 2}}, band=2)

        assert cache.get("2025", 1)["WR"] == pytest.approx(5.0)

    def test_zero_demand_position_has_zero_baseline(self, cache_factory):
        # TE not started (rank 0) -> baseline 0.0 even though a TE scored.
        cache = cache_factory({"2025": {"WR": 2, "TE": 0}}, band=1)

        assert cache.get("2025", 1)["TE"] == pytest.approx(0.0)

    def test_rank_beyond_slate_depth_is_zero(self, cache_factory):
        # Only 4 WRs exist; rank 5 has no one -> replacement is effectively 0.
        cache = cache_factory({"2025": {"WR": 5}}, band=1)

        assert cache.get("2025", 1)["WR"] == pytest.approx(0.0)

    def test_result_is_memoized(self, cache_factory):
        cache = cache_factory({"2025": {"WR": 2}}, band=1)

        assert cache.get("2025", 1) is cache.get("2025", 1)


# ---------------------------------------------------------------------------
# compute_par_since
# ---------------------------------------------------------------------------

class StubBaselines:
    """Minimal baseline_cache: (season, week) -> {position: baseline_pts}."""

    def __init__(self, table):
        self._table = table

    def get(self, season, week):
        return self._table.get((str(season), week), {})


class TestComputeParSince:

    @pytest.fixture
    def chain_index(self):
        return {
            "seasons": ["2025", "2026"],
            "scoring": {"2025": {"rec": 1.0}, "2026": {"rec": 1.0}},
        }

    @pytest.fixture
    def stats(self):
        return {
            ("2025", 7): {"p": {"rec": 5, "gp": 1}},
            ("2025", 8): {"p": {"rec": 10, "gp": 1}},
            ("2025", 9): {"p": {"rec": 3, "gp": 1}},
            ("2026", 1): {"p": {"rec": 4, "gp": 1}},
        }

    @pytest.fixture
    def cache(self, stats):
        return tr.WeeklyStatsCache(
            fetch=lambda season, week: stats.get((str(season), week), {})
        )

    def test_par_subtracts_baseline_over_window(self, chain_index, cache):
        # Filed 2025 wk8 -> wk8, wk9 (2025) + wk1 (2026); baseline 2/wk.
        baselines = StubBaselines(
            {
                ("2025", 8): {"WR": 2.0},
                ("2025", 9): {"WR": 2.0},
                ("2026", 1): {"WR": 2.0},
            }
        )

        result = tr.compute_par_since(
            "p", "WR", "2025", 8, chain_index, cache, baselines,
            weeks=range(1, 19),
        )

        # points 10+3+4 = 17; baseline 2*3 = 6; PAR = 11.
        assert result["par_total"] == pytest.approx(11.0)

    def test_points_total_matches_raw_production(self, chain_index, cache):
        baselines = StubBaselines({})

        result = tr.compute_par_since(
            "p", "WR", "2025", 8, chain_index, cache, baselines,
            weeks=range(1, 19),
        )

        assert result["points_total"] == pytest.approx(17.0)

    def test_missing_baseline_defaults_to_zero(self, chain_index, cache):
        # No baseline entries -> PAR equals raw points.
        baselines = StubBaselines({})

        result = tr.compute_par_since(
            "p", "WR", "2025", 8, chain_index, cache, baselines,
            weeks=range(1, 19),
        )

        assert result["par_total"] == pytest.approx(17.0)

    def test_none_position_passes_points_through(self, chain_index, cache):
        # Unknown position can't match a baseline -> PAR == points.
        baselines = StubBaselines({("2025", 8): {"WR": 2.0}})

        result = tr.compute_par_since(
            "p", None, "2025", 8, chain_index, cache, baselines,
            weeks=range(1, 19),
        )

        assert result["par_total"] == pytest.approx(17.0)

    def test_counts_games_played(self, chain_index, cache):
        result = tr.compute_par_since(
            "p", "WR", "2025", 8, chain_index, cache, StubBaselines({}),
            weeks=range(1, 19),
        )

        assert result["games"] == 3

    def test_end_bound_excludes_weeks_at_or_after(self, chain_index, cache):
        # Cap at 2026 wk1 -> only 2025 wk8, wk9 count (2026 wk1 excluded).
        result = tr.compute_par_since(
            "p", "WR", "2025", 8, chain_index, cache, StubBaselines({}),
            weeks=range(1, 19), end_season="2026", end_week=1,
        )

        assert result["points_total"] == pytest.approx(13.0)  # 10 + 3

    def test_floor_clamps_below_replacement_weeks(self, chain_index, cache):
        # Baseline above the wk9 (3 pts) line -> that week floors at 0.
        baselines = StubBaselines(
            {
                ("2025", 8): {"WR": 2.0},   # 10 -> +8
                ("2025", 9): {"WR": 5.0},   # 3  -> -2 -> floored 0
                ("2026", 1): {"WR": 2.0},   # 4  -> +2
            }
        )

        result = tr.compute_par_since(
            "p", "WR", "2025", 8, chain_index, cache, baselines,
            weeks=range(1, 19), floor_weekly=True,
        )

        # 8 + 0 + 2 = 10 (vs 8 - 2 + 2 = 8 unfloored).
        assert result["par_total"] == pytest.approx(10.0)

    def test_par_per_game_divides_by_games(self, chain_index, cache):
        baselines = StubBaselines({})

        result = tr.compute_par_since(
            "p", "WR", "2025", 8, chain_index, cache, baselines,
            weeks=range(1, 19),
        )

        # par_total 17 over 3 games, rounded to 2 dp.
        assert result["par_per_game"] == pytest.approx(5.67)


# ---------------------------------------------------------------------------
# score_asset_par
# ---------------------------------------------------------------------------

class TestScoreAssetPar:

    @pytest.fixture
    def chain_index(self):
        return {"seasons": ["2025"], "scoring": {"2025": {"rec": 1.0}}}

    @pytest.fixture
    def cache(self):
        stats = {("2025", 5): {"p": {"rec": 12, "gp": 1}}}
        return tr.WeeklyStatsCache(
            fetch=lambda season, week: stats.get((str(season), week), {})
        )

    def test_resolved_asset_gets_par_dict(self, chain_index, cache):
        asset = {"resolved": True, "player_id": "p", "position": "WR"}
        baselines = StubBaselines({("2025", 5): {"WR": 2.0}})

        scored = tr.score_asset_par(
            asset, {"season": "2025", "week": 5}, chain_index, cache, baselines
        )

        assert scored["par"]["par_total"] == pytest.approx(10.0)

    def test_unresolved_asset_carries_no_par(self, chain_index, cache):
        asset = {"resolved": False, "player_id": None, "position": None}

        scored = tr.score_asset_par(
            asset, {"season": "2025", "week": 5}, chain_index, cache,
            StubBaselines({}),
        )

        assert scored["par"] is None


# ---------------------------------------------------------------------------
# compute_production_since — end bound
# ---------------------------------------------------------------------------

class TestProductionEndBound:

    @pytest.fixture
    def chain_index(self):
        return {
            "seasons": ["2025", "2026"],
            "scoring": {"2025": {"rec": 1.0}, "2026": {"rec": 1.0}},
        }

    @pytest.fixture
    def cache(self):
        stats = {
            ("2025", 8): {"p": {"rec": 10, "gp": 1}},
            ("2025", 9): {"p": {"rec": 3, "gp": 1}},
            ("2026", 1): {"p": {"rec": 4, "gp": 1}},
        }
        return tr.WeeklyStatsCache(
            fetch=lambda season, week: stats.get((str(season), week), {})
        )

    def test_end_bound_stops_counting(self, chain_index, cache):
        # Held only through 2025 wk9 (exclusive) -> just wk8.
        result = tr.compute_production_since(
            "p", "2025", 8, chain_index, cache,
            weeks=range(1, 19), end_season="2025", end_week=9,
        )

        assert result["total"] == pytest.approx(10.0)

    def test_no_end_bound_runs_to_present(self, chain_index, cache):
        result = tr.compute_production_since(
            "p", "2025", 8, chain_index, cache, weeks=range(1, 19)
        )

        assert result["total"] == pytest.approx(17.0)  # 10 + 3 + 4


# ---------------------------------------------------------------------------
# build_ownership_timeline / hold_window_end (dynasty: assets keep moving)
# ---------------------------------------------------------------------------

class TestOwnershipTimeline:

    @pytest.fixture
    def chain(self):
        return [
            {"league_id": "L2026", "season": "2026"},
            {"league_id": "L2025", "season": "2025"},
        ]

    @pytest.fixture
    def txns(self):
        # (league_id, week) -> [transactions]
        return {
            ("L2025", 1): [
                {
                    "status": "complete",
                    "status_updated": 100,
                    "adds": {"diggs": 1},
                    "drops": {"filler": 2},
                }
            ],
            ("L2025", 5): [
                {  # re-trade: roster 1 ships Diggs to roster 2
                    "status": "complete",
                    "status_updated": 200,
                    "adds": {"diggs": 2},
                    "drops": {"diggs": 1},
                }
            ],
            ("L2026", 3): [
                {  # ignored: not complete
                    "status": "pending",
                    "status_updated": 300,
                    "adds": {"diggs": 3},
                    "drops": {},
                }
            ],
        }

    @pytest.fixture
    def timeline(self, chain, txns):
        return tr.build_ownership_timeline(
            chain,
            fetch=lambda lid, wk: txns.get((lid, wk), []),
            weeks=range(1, 6),
        )

    def test_events_are_chronologically_ordered(self, timeline):
        keys = [(e["season"], e["week"], e["event"]) for e in timeline["diggs"]]

        assert keys[0] == ("2025", 1, "add")

    def test_incomplete_transactions_ignored(self, timeline):
        # The pending L2026 add must not appear.
        assert all(e["season"] != "2026" for e in timeline["diggs"])

    def test_retrade_ends_hold_window(self, timeline):
        # Roster 1 acquired Diggs 2025 wk1, shipped him wk5.
        end = tr.hold_window_end(timeline, "diggs", 1, "2025", 1)

        assert end == ("2025", 5)

    def test_new_owner_still_holds(self, timeline):
        # Roster 2 received Diggs wk5 and never moved him.
        end = tr.hold_window_end(timeline, "diggs", 2, "2025", 5)

        assert end is None

    def test_drop_ends_hold_window(self):
        chain = [{"league_id": "L", "season": "2025"}]
        txns = {
            ("L", 1): [
                {"status": "complete", "status_updated": 1,
                 "adds": {"x": 1}, "drops": {}}
            ],
            ("L", 4): [
                {"status": "complete", "status_updated": 2,
                 "adds": {}, "drops": {"x": 1}}
            ],
        }
        timeline = tr.build_ownership_timeline(
            chain, fetch=lambda lid, wk: txns.get((lid, wk), []),
            weeks=range(1, 6),
        )

        assert tr.hold_window_end(timeline, "x", 1, "2025", 1) == ("2025", 4)

    def test_no_events_means_still_held(self, timeline):
        assert tr.hold_window_end(timeline, "unknown", 1, "2025", 1) is None


# ---------------------------------------------------------------------------
# attach_lineage (link re-traded assets without summing value)
# ---------------------------------------------------------------------------

class TestAttachLineage:

    @pytest.fixture
    def scenario(self):
        # T1 (2020): roster 1 acquires Diggs (and ships a pick).
        # T2 (2022): roster 1 flips Diggs for Chase + a pick-player.
        # Scrub was dropped, not traded, so its lineage ends.
        trades = [
            {"season": "2020", "week": 1, "drops": {"sent_pick": 1}},
            {"season": "2022", "week": 1, "drops": {"diggs": 1}},
        ]
        reviews = [
            {
                "season": "2020", "week": 1,
                "sides": {1: {"assets": [
                    {"name": "Diggs", "player_id": "diggs",
                     "end": ("2022", 1), "became": None},
                    {"name": "Scrub", "player_id": "scrub",
                     "end": ("2021", 5), "became": None},
                ]}},
            },
            {
                "season": "2022", "week": 1,
                "sides": {1: {"assets": [
                    {"name": "Ja'Marr Chase", "player_id": "chase",
                     "end": None, "became": None},
                    {"name": "2023 1st -> Bijan", "player_id": "bijan",
                     "end": None, "became": None},
                ]}},
            },
        ]
        tr.attach_lineage(reviews, trades)
        return reviews

    def test_assigns_sequential_trade_numbers(self, scenario):
        assert [r["trade_no"] for r in scenario] == [1, 2]

    def test_retraded_asset_links_to_return_package(self, scenario):
        diggs = scenario[0]["sides"][1]["assets"][0]

        assert diggs["became"] == {
            "trade_no": 2,
            "assets": ["Ja'Marr Chase", "2023 1st -> Bijan"],
        }

    def test_dropped_asset_ends_lineage(self, scenario):
        scrub = scenario[0]["sides"][1]["assets"][1]

        assert scrub["became"] == {"dropped": True}

    def test_still_held_asset_has_no_lineage(self, scenario):
        chase = scenario[1]["sides"][1]["assets"][0]

        assert chase["became"] is None

    def test_lineage_line_renders_reference(self, scenario):
        diggs = scenario[0]["sides"][1]["assets"][0]

        assert "see T2" in tr._par_lineage_line(diggs)

    def test_dropped_line_renders_terminal(self, scenario):
        scrub = scenario[0]["sides"][1]["assets"][1]

        assert "lineage ends" in tr._par_lineage_line(scrub)


# ---------------------------------------------------------------------------
# wrap_report_html / _report_title (GitHub Pages output)
# ---------------------------------------------------------------------------

class TestWrapReportHtml:

    def test_escapes_report_text(self):
        out = tr.wrap_report_html(
            "winner > loser & <b>", "L", generated="2026-01-01 00:00 UTC"
        )

        assert "winner &gt; loser &amp; &lt;b&gt;" in out

    def test_includes_title_and_lang(self):
        out = tr.wrap_report_html("x", "My League", generated="t")

        assert "<title>My League</title>" in out and 'lang="en"' in out

    def test_wraps_body_in_pre(self):
        out = tr.wrap_report_html("body-text", "L", generated="t")

        assert "<pre>body-text</pre>" in out

    def test_escapes_title(self):
        out = tr.wrap_report_html("x", "A & B <x>", generated="t")

        assert "<title>A &amp; B &lt;x&gt;</title>" in out


class TestReportTitle:

    def test_includes_season_when_given(self):
        assert tr._report_title("Chaos", "2025") == "Chaos — Trade Review (PAR) · 2025"

    def test_omits_season_when_absent(self):
        assert tr._report_title("Chaos") == "Chaos — Trade Review (PAR)"

    def test_falls_back_to_dynasty_without_name(self):
        assert tr._report_title(None) == "Dynasty — Trade Review (PAR)"
