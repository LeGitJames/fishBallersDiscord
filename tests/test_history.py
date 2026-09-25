import copy
import json
import os
import random

import pytest

from harambot.database.history_models import (
    DraftLottery,
    HISTORY_TABLES,
    ManagerSeason,
    Season,
    WeeklyTeamStat,
)
from harambot.database.models import database
from harambot.history import lottery, parsers
from harambot.history.queries import build_lottery_entrants
from harambot.history.sync import sync_league_history

root_path = os.path.dirname(os.path.realpath(__file__))
GUILD = "123"


def load(name):
    with open(os.path.join(root_path, name)) as f:
        return json.load(f)


@pytest.fixture(autouse=True)
def db():
    database.create_tables(HISTORY_TABLES)
    yield
    database.drop_tables(HISTORY_TABLES)


# ---------------------------------------------------------------- lottery


def test_balls_follow_league_rule():
    assert lottery.balls_for(1) == 10
    assert lottery.balls_for(2) == 11
    assert lottery.balls_for(12) == 21
    assert lottery.balls_for(4, is_consolation_winner=True) == 14


def test_draw_places_every_team_once():
    entrants = [{"id": i, "balls": lottery.balls_for(i)} for i in range(1, 13)]
    order = lottery.draw(entrants, rng=random.Random(7))
    assert [e["pick"] for e in order] == list(range(1, 13))
    assert sorted(e["id"] for e in order) == list(range(1, 13))


def test_draw_is_weighted():
    entrants = [{"id": "few", "balls": 10}, {"id": "many", "balls": 21}]
    rng = random.Random(1)
    firsts = sum(
        lottery.draw(entrants, rng)[0]["id"] == "many" for _ in range(20000)
    )
    assert abs(firsts / 20000 - 21 / 31) < 0.02


def test_first_pick_odds_sum_to_one():
    entrants = [{"balls": lottery.balls_for(i)} for i in range(1, 13)]
    odds = lottery.first_pick_odds(entrants)
    assert abs(sum(p for _, p in odds) - 1) < 1e-9
    assert odds[0][1] == pytest.approx(10 / 186)


# ---------------------------------------------------------------- parsers


def test_renew_to_league_key():
    assert parsers.renew_to_league_key("388_27081") == "388.l.27081"
    assert parsers.renew_to_league_key("") is None
    assert parsers.renew_to_league_key(None) is None


def completed_scoreboard():
    raw = load("test-matchups-category.json")
    matchups = raw["fantasy_content"]["league"][1]["scoreboard"]["0"][
        "matchups"
    ]
    i = 0
    while str(i) in matchups:
        matchups[str(i)]["matchup"]["status"] = "postevent"
        i += 1
    return raw


def test_parse_scoreboard_skips_unfinished_weeks():
    assert parsers.parse_scoreboard(load("test-matchups-category.json")) == []


def test_parse_scoreboard_nba_categories():
    rows = parsers.parse_scoreboard(completed_scoreboard())
    reb = [r for r in rows if r["stat_id"] == "15" and r["team_name"] ==
           "LOS DIOSES"]
    assert reb[0]["value"] == 87
    assert reb[0]["week"] == 9
    assert reb[0]["manager_guid"] == "ONVHWMWVEBRZHGYCHWPDPIVZFY"
    fg = [r for r in rows if r["stat_id"] == "5"][0]
    assert fg["value"] == pytest.approx(0.477)
    # "61/128" FGM/FGA rows are not numbers and are skipped
    assert not any(r["stat_id"] == "9004003" for r in rows)
    assert set(parsers.NBA_STATS) <= {r["stat_id"] for r in rows}


def standings_raw(season_teams):
    """Build a minimal raw standings response.
    season_teams: list of (team_id, name, guid, nickname, rank, w, l, t)"""
    teams = {}
    for i, (tid, name, guid, nick, rank, w, l_, t) in enumerate(season_teams):
        teams[str(i)] = {
            "team": [
                [
                    {"team_key": "k.t.{}".format(tid)},
                    {"team_id": str(tid)},
                    {"name": name},
                    [],
                    {"managers": [{"manager": {"guid": guid,
                                               "nickname": nick}}]},
                ],
                {"team_stats": {}},
                {"team_standings": {
                    "rank": rank,
                    "outcome_totals": {"wins": str(w), "losses": str(l_),
                                       "ties": str(t)},
                }},
            ]
        }
    teams["count"] = len(season_teams)
    return {"fantasy_content": {"league": [{}, {"standings": [
        {"teams": teams}]}]}}


def test_parse_standings_real_sample():
    rows = parsers.parse_standings(load("test-yahoo-standings-raw.json"))
    assert rows[0]["final_rank"] == 1
    assert rows[0]["team_name"] == "Lumber Kings"
    assert rows[0]["wins"] == 144
    assert all(r["manager_guid"] for r in rows)


def test_first_round_order():
    picks = [
        {"pick": 1, "round": 1, "team_key": "a"},
        {"pick": 2, "round": 1, "team_key": "b"},
        {"pick": 3, "round": 2, "team_key": "b"},
    ]
    assert parsers.first_round_order(picks, {"a": "A", "b": "B"}) == {
        "A": 1, "B": 2}


# ------------------------------------------------------------------ sync

MANAGERS = [("G{}".format(i), "Mgr{}".format(i)) for i in range(1, 13)]


class FakeHandler:
    def __init__(self, seasons):
        self.seasons = seasons

    def get_standings_raw(self, key):
        return self.seasons[key]["standings"]

    def get_scoreboard_raw(self, key, week=None):
        raw = copy.deepcopy(completed_scoreboard())
        m = raw["fantasy_content"]["league"][1]["scoreboard"]["0"][
            "matchups"]
        i = 0
        while str(i) in m:
            m[str(i)]["matchup"]["week"] = str(week)
            i += 1
        return raw


class FakeLeague:
    def __init__(self, key, handler):
        self.league_id = key
        self.yhandler = handler
        self.sc = None

    def settings(self):
        return self.yhandler.seasons[self.league_id]["settings"]

    def draft_results(self):
        return self.yhandler.seasons[self.league_id]["draft"]


@pytest.fixture
def two_seasons(monkeypatch):
    """2024 finished (Mgr1 champ, Mgr7 consolation), 2025 predraft with
    Mgr12 replaced by a new manager on the same team slot."""
    finished = [
        (i, "Team{}".format(i), g, n, i, 100 - i, 50 + i, 3)
        for i, (g, n) in enumerate(MANAGERS, start=1)
    ]
    current = [
        (i, "New{}".format(i), g, n, i, 0, 0, 0)
        for i, (g, n) in enumerate(MANAGERS, start=1)
    ]
    current[11] = (12, "Rookie", "GNEW", "Newbie", 12, 0, 0, 0)
    # last year's draft: team i picked 13 - i
    draft = [{"pick": 13 - i, "round": 1, "team_key": "k.t.{}".format(i)}
             for i in range(1, 13)]
    seasons = {
        "2.l.1": {
            "settings": {"season": "2025", "renew": "1_1", "is_finished": 0,
                         "draft_status": "predraft", "name": "Hoops",
                         "current_week": "1", "start_week": "1"},
            "standings": standings_raw(current),
            "draft": [],
        },
        "1.l.1": {
            "settings": {"season": "2024", "renew": "", "is_finished": 1,
                         "draft_status": "postdraft", "name": "Hoops",
                         "start_week": "1", "end_week": "3",
                         "num_playoff_teams": "6",
                         "has_playoff_consolation_games": True},
            "standings": standings_raw(finished),
            "draft": draft,
        },
    }
    handler = FakeHandler(seasons)
    import yahoo_fantasy_api
    monkeypatch.setattr(
        yahoo_fantasy_api, "League",
        lambda sc, key, handler=None: FakeLeague(key, handler),
        raising=False,
    )
    return FakeLeague("2.l.1", handler)


def test_sync_walks_renew_chain(two_seasons):
    summaries = sync_league_history(GUILD, two_seasons)
    assert [s["season"] for s in summaries] == [2025, 2024]
    s24 = Season.get(Season.season == 2024)
    assert s24.is_finished and s24.champion_guid == "G1"
    assert s24.consolation_guid == "G7"  # rank num_playoff_teams + 1
    assert Season.get(Season.season == 2025).champion_guid is None
    weeks = {w.week for w in WeeklyTeamStat.select().where(
        WeeklyTeamStat.season == 2024)}
    assert weeks == {1, 2, 3}
    assert ManagerSeason.get(
        (ManagerSeason.season == 2024) & (ManagerSeason.manager_guid == "G3")
    ).draft_position == 10


def test_sync_skips_finished_and_keeps_overrides(two_seasons):
    sync_league_history(GUILD, two_seasons)
    s = Season.get(Season.season == 2024)
    s.consolation_guid, s.consolation_overridden = "G8", True
    s.save()
    summaries = sync_league_history(GUILD, two_seasons)
    assert summaries[1] == {"season": 2024, "skipped": True}
    sync_league_history(GUILD, two_seasons, full=True)
    assert Season.get(Season.season == 2024).consolation_guid == "G8"


def test_lottery_entrants(two_seasons):
    sync_league_history(GUILD, two_seasons)
    entrants, previous, problems = build_lottery_entrants(GUILD)
    assert problems == [] and previous.season == 2024
    assert len(entrants) == 12
    by = {e["manager_guid"]: e for e in entrants}
    # Mgr1 picked 12th -> 21 balls; Mgr11 picked 2nd -> 11 balls
    assert by["G1"]["balls"] == 21
    assert by["G11"]["balls"] == 11
    # Mgr7 won consolation and picked 6th -> 15 + 1
    assert by["G7"]["balls"] == 16 and by["G7"]["consolation_winner"]
    # New manager inherits team 12's pick (1st -> 10 balls)
    assert by["GNEW"]["balls"] == 10
    assert sum(e["balls"] for e in entrants) == sum(range(10, 22)) + 1


def test_lottery_draw_saved_shape(two_seasons):
    sync_league_history(GUILD, two_seasons)
    entrants, _, _ = build_lottery_entrants(GUILD)
    order = lottery.draw(entrants, rng=random.Random(3))
    DraftLottery.create(guild_id=GUILD, season=2025, run_by="1",
                        results=json.dumps(order))
    saved = json.loads(DraftLottery.get().results)
    assert [e["pick"] for e in saved] == list(range(1, 13))
