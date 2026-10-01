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

    def transactions(self, kind, count):
        return []

    def teams(self):
        return {}


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


# --------------------------------------------------------- manual import

from harambot.history.manual_import import (  # noqa: E402
    parse_history_csv,
    save_history,
)


def manual_csv(consolation="Mgr7"):
    lines = ["season,manager,team,finish,draft_pick,consolation_winner"]
    for i in range(1, 13):
        lines.append("2025-26,Mgr{0},Team{0},{0},{1},{2}".format(
            i, 13 - i, "yes" if "Mgr{}".format(i) == consolation else ""))
    lines.append("2024,Mgr3,Old3,1,,")
    for i in (1, 2, 4):
        lines.append("2024,Mgr{0},Old{0},{1},,".format(i, i + 1))
    # Upcoming season: Mgr12 leaves, Newbie takes over his team
    for i in range(1, 12):
        lines.append("2026,Mgr{0},New{0},,,".format(i))
    lines.append("2026,Newbie,Rookie,,,")
    return "\ufeff" + "\r\n".join(lines) + "\r\n"


def with_took_over(text):
    return text.replace(
        "season,manager,team,finish,draft_pick,consolation_winner",
        "season,manager,team,finish,draft_pick,consolation_winner,"
        "took_over_from").replace("2026,Newbie,Rookie,,,",
                                  "2026,Newbie,Rookie,,,,mgr12")


def test_manual_parse_ok():
    seasons, errors = parse_history_csv(with_took_over(manual_csv()))
    assert errors == []
    assert sorted(seasons) == [2024, 2025, 2026]
    assert len(seasons[2025]) == 12


def test_manual_parse_reports_problems():
    bad = ("season,manager,finish,draft_pick\n"
           "2025,Josh,1,1\n2025,josh,2,1\n2025,Sam,x,\n")
    _, errors = parse_history_csv(bad)
    text = " | ".join(errors)
    assert "listed twice" in text
    assert "draft_pick 1" in text
    assert "whole number" in text
    _, errors = parse_history_csv("team,finish\nA,1\n")
    assert "Missing column" in errors[0]


def test_manual_import_trophies_and_lottery():
    seasons, _ = parse_history_csv(with_took_over(manual_csv()))
    saved, skipped = save_history(GUILD, seasons)
    assert saved == [2024, 2025, 2026] and skipped == []
    s25 = Season.get(Season.season == 2025)
    assert s25.is_finished and s25.champion_guid == "manual:mgr1"
    assert s25.consolation_guid == "manual:mgr7"
    assert not Season.get(Season.season == 2026).is_finished

    entrants, previous, problems = build_lottery_entrants(GUILD)
    assert problems == [] and previous.season == 2025
    by = {e["manager_name"]: e for e in entrants}
    assert by["Mgr1"]["balls"] == 21        # picked 12th
    assert by["Mgr7"]["balls"] == 16        # picked 6th + consolation
    assert by["Newbie"]["balls"] == 10      # inherits Mgr12's 1st pick
    assert len(entrants) == 12


def test_manual_import_without_next_season():
    # Only last season entered: the lottery uses last season's managers.
    text = "\n".join(manual_csv().splitlines()[:13])
    seasons, errors = parse_history_csv(text)
    assert errors == []
    save_history(GUILD, seasons)
    entrants, previous, problems = build_lottery_entrants(GUILD)
    assert problems == [] and len(entrants) == 12


def test_manual_reimport_replaces_and_yahoo_wins(two_seasons):
    seasons, _ = parse_history_csv(with_took_over(manual_csv()))
    save_history(GUILD, seasons)
    save_history(GUILD, seasons)  # re-import is fine
    assert ManagerSeason.select().where(
        ManagerSeason.season == 2025).count() == 12
    # Yahoo sync never touches finished seasons from the files
    summaries = sync_league_history(GUILD, two_seasons)
    assert summaries == [{"season": 2025, "skipped": True,
                          "from_files": True}]
    s24 = Season.get(Season.season == 2024)
    assert s24.league_key == "manual" and s24.champion_guid == "manual:mgr3"
    assert ManagerSeason.select().where(
        ManagerSeason.season == 2025).count() == 12


def test_manual_note_and_champion_only_seasons():
    text = ("season,manager,team,finish,note\n"
            "2019-20,Liam,Tacko,1,COVID year\n2020,Mitchell,Gasol,1,\n")
    seasons, errors = parse_history_csv(text)
    assert errors == []
    save_history(GUILD, seasons)
    s19 = Season.get(Season.season == 2019)
    assert s19.note == "COVID year" and s19.is_finished
    assert s19.champion_guid == "manual:liam"
    assert Season.get(Season.season == 2020).note is None


def test_old_database_gets_note_column():
    from harambot.database.history_models import create_history_tables
    database.execute_sql('ALTER TABLE "season" DROP COLUMN "note"')
    assert "note" not in {c.name for c in database.get_columns("season")}
    create_history_tables()
    assert "note" in {c.name for c in database.get_columns("season")}


def test_reveal_script_order_and_drama():
    entrants = [{"manager_name": "M{}".format(i), "team_name": "T{}".format(i),
                 "balls": lottery.balls_for(i)} for i in range(1, 13)]
    order = lottery.draw(entrants, rng=random.Random(5))
    steps = lottery.reveal_script(order, 8)
    text = [m for _, m in steps]
    # last pick first, #1 last
    assert "Pick **#12**" in text[0]
    assert "#1 pick" in text[-2] and text[-1].startswith("🎉 🥇")
    left = next(m for m in text if "3 teams left" in m)
    top3 = sorted((e for e in order if e["pick"] <= 3),
                  key=lambda e: e["manager_name"])
    assert left.index(top3[0]["manager_name"]) < left.index(
        top3[1]["manager_name"]) < left.index(top3[2]["manager_name"])
    # names revealed after a longer pause
    assert steps[-1][0] == 12
    # every manager revealed exactly once
    for e in entrants:
        assert sum("**{}**".format(e["manager_name"]) in m
                   for m in text) == 1


def test_reveal_script_small_league():
    order = [{"pick": 1, "manager_name": "A", "team_name": "a", "balls": 10},
             {"pick": 2, "manager_name": "B", "team_name": "b", "balls": 11}]
    text = [m for _, m in lottery.reveal_script(order, 2)]
    assert "2 teams left" in text[0] and "A and B" in text[0]
    assert text[-1].startswith("🎉 🥇 **A**")


def test_discord_links_tag_managers_in_reveal():
    from harambot.database.history_models import DiscordLink
    seasons, _ = parse_history_csv(manual_csv())
    save_history(GUILD, seasons)
    DiscordLink.create(guild_id=GUILD, manager_guid="manual:mgr1",
                       discord_user_id="111")
    entrants, _, _ = build_lottery_entrants(GUILD)
    by = {e["manager_name"]: e for e in entrants}
    assert by["Mgr1"]["discord_id"] == "111"
    assert by["Mgr2"]["discord_id"] is None
    order = lottery.draw(entrants, rng=random.Random(1))
    text = "\n".join(m for _, m in lottery.reveal_script(order, 2))
    assert text.count("<@111>") in (1, 2)  # reveal (+ "teams left" line)
    assert "**Mgr1**" in text
    # re-importing the CSV keeps links (same manager ids)
    save_history(GUILD, seasons)
    entrants, _, _ = build_lottery_entrants(GUILD)
    assert {e["manager_name"]: e for e in entrants}["Mgr1"]["discord_id"]


def test_manual_badge():
    text = "season,manager,finish,badge\n2016,Josh,1,🪠\n2018,Josh,1,\n"
    seasons, errors = parse_history_csv(text)
    assert errors == []
    save_history(GUILD, seasons)
    assert Season.get(Season.season == 2016).badge == "🪠"
    assert Season.get(Season.season == 2018).badge is None


def test_hall_of_shame_winless_and_sweeps():
    from harambot.history.matchups_import import (
        looks_like_matchups, parse_matchups_csv, save_matchups)
    from harambot.history.queries import hall_of_shame
    hist = ("season,manager,team,finish,wins,losses,ties\n"
            "2022,Jerry,TERMINATOR,2,0,20,0\n2022,Josh,Surfer,1,16,4,0\n"
            "2021,Jack,Flint,1,1,18,1\n2021,Tom,World,2,10,10,0\n")
    s, e = parse_history_csv(hist)
    assert e == []
    save_history(GUILD, s)
    m = ("season,week,manager1,team1,score1,manager2,team2,score2\n"
         "2022-23,5,Josh,Surfer,9,Jerry,TERMINATOR,0\n"
         "2022,6,Jerry,TERMINATOR,0,Josh,Surfer,9\n"
         "2022,7,Jerry,TERMINATOR,0,Josh,Surfer,8\n"   # 8-0-1: not a sweep
         "2021,3,Tom,World,4,Jack,Flint,5\n")
    assert looks_like_matchups(m) and not looks_like_matchups(hist)
    ms, me = parse_matchups_csv(m)
    assert me == []
    assert save_matchups(GUILD, ms) == 4
    assert save_matchups(GUILD, ms) == 4  # re-import replaces
    data = hall_of_shame(GUILD)
    assert [r.manager_name for r in data["winless"]] == ["Jerry"]
    assert data["worst"][0].manager_name == "Jack"
    assert [(s[0], s[1], s[2]) for s in data["sweeps"]] == [
        (2022, 6, "Jerry"), (2022, 5, "Jerry")]
    _, bad = parse_matchups_csv("season,week,manager1,score1,manager2,"
                                "score2\n2022,x,A,9,B,0\n")
    assert "numbers" in bad[0]


def test_most_drafted():
    from harambot.history.drafts_import import (
        looks_like_drafts, parse_drafts_csv, save_drafts)
    from harambot.history.queries import most_drafted
    text = ("season,round,pick,manager,team,player\n"
            "2023,1,4,Josh,Surfer,Jokic\n2024,2,8,Josh,Surfer,Jokic\n"
            "2025,1,1,Josh,Surfer,Jokic\n2025,3,1,Josh,Surfer,Curry\n"
            "2024,1,1,Liam,Tacko,Curry\n2024,1,2,Liam,Tacko,Jokic\n")
    assert looks_like_drafts(text)
    seasons, errors = parse_drafts_csv(text)
    assert errors == []
    assert save_drafts(GUILD, seasons) == 6
    josh = most_drafted(GUILD, "manual:josh")
    assert josh[0]["player"] == "Jokic" and josh[0]["times"] == 3
    assert josh[0]["seasons"] == [2023, 2024, 2025]
    assert josh[0]["best_round"] == 1
    league = most_drafted(GUILD)
    assert league[0]["player"] == "Jokic" and league[0]["times"] == 4
    assert league[0]["managers"] == ["Josh", "Liam"]
    assert league[1]["player"] == "Curry"
    _, bad = parse_drafts_csv("season,round,pick,manager,player\n"
                              "2024,one,1,Josh,Jokic\n")
    assert "numbers" in bad[0]


def test_loyal_pairs():
    from harambot.history.drafts_import import parse_drafts_csv, save_drafts
    from harambot.history.queries import loyal_pairs
    text = ("season,round,pick,manager,player\n"
            "2023,1,4,Josh,Jokic\n2024,2,8,Josh,Jokic\n2025,1,1,Josh,Jokic\n"
            "2024,1,1,Liam,Jokic\n2023,3,1,Liam,Curry\n2025,2,1,Liam,Curry\n"
            "2025,5,1,Tom,Lopez\n")
    save_drafts(GUILD, parse_drafts_csv(text)[0])
    pairs = loyal_pairs(GUILD)
    assert [(p["manager"], p["player"], p["times"]) for p in pairs] == [
        ("Josh", "Jokic", 3), ("Liam", "Curry", 2)]


def test_rip_marker_for_former_managers():
    from harambot.history.queries import name_marker
    text = ("season,manager,team,finish\n"
            "2024,Mitchell,Gasol,1\n2024,Josh,Surfer,2\n"
            "2025,Josh,Surfer,1\n2025,Liam,Tacko,2\n")
    save_history(GUILD, parse_history_csv(text)[0])
    tag = name_marker(GUILD)
    assert tag("manual:mitchell", "Mitchell") == "Mitchell 🪦"
    assert tag("manual:josh", "Josh") == "Josh"


def test_record_book_import():
    from harambot.database.history_models import RecordBookEntry
    from harambot.history.records_import import (
        looks_like_records, parse_records_csv, save_records)
    with open(os.path.join(root_path, "..", "league_records.csv"),
              encoding="utf-8") as f:
        text = f.read()
    assert looks_like_records(text)
    parsed, errors = parse_records_csv(text)
    assert errors == []
    assert save_records(GUILD, parsed) == 29
    assert save_records(GUILD, parsed) == 29  # re-import replaces
    fewest_to = RecordBookEntry.select().where(
        (RecordBookEntry.stat_id == "19") & (RecordBookEntry.kind == "best")
        & (RecordBookEntry.scope == "week"))
    assert sorted(e.manager_name for e in fewest_to) == ["Jack", "Justin"]
    pts = RecordBookEntry.get((RecordBookEntry.stat_id == "12")
                              & (RecordBookEntry.scope == "week"))
    assert (pts.manager_guid, pts.season, pts.week, pts.value) == (
        "manual:liam", 2024, 7, 1220)
    _, bad = parse_records_csv("stat_id,scope,kind,season,manager,value\n"
                               "5,month,best,2024,Tom,.5\n")
    assert "scope" in bad[0]


def test_matchup_streaks():
    from harambot.history.matchups_import import (
        parse_matchups_csv, save_matchups)
    from harambot.history.queries import matchup_streaks
    rows = ["season,week,manager1,team1,score1,manager2,team2,score2"]
    for w in range(1, 6):   # Josh beats Jerry weeks 1-5
        rows.append("2022,{},Josh,S,6,Jerry,T,3".format(w))
    rows.append("2022,6,Josh,S,4,Jerry,T,4")   # tie ends both streaks
    rows.append("2022,7,Josh,S,3,Jerry,T,6")
    save_matchups(GUILD, parse_matchups_csv("\n".join(rows))[0])
    s = matchup_streaks(GUILD)
    assert (s["W"][0]["manager"], s["W"][0]["length"],
            s["W"][0]["start"], s["W"][0]["end"]) == ("Josh", 5, 1, 5)
    assert (s["L"][0]["manager"], s["L"][0]["length"]) == ("Jerry", 5)
    assert {(x["manager"], x["length"]) for x in s["W"]} == {
        ("Josh", 5), ("Jerry", 1)}


def test_draft_board_filters():
    from harambot.history.drafts_import import parse_drafts_csv, save_drafts
    from harambot.history.queries import draft_board, drafted_players
    text = ("season,round,pick,manager,team,player\n"
            "2016,1,2,Josh,S,Curry\n2016,1,1,Liam,T,Westbrook\n"
            "2016,2,1,Josh,S,Lillard\n2019,1,1,Liam,T,Harden\n"
            "2019,2,5,Liam,T,Curry\n")
    save_drafts(GUILD, parse_drafts_csv(text)[0])
    assert [p.player for p in draft_board(GUILD, 2016, 1)] == [
        "Westbrook", "Curry"]
    assert [p.player for p in draft_board(GUILD, 2016, 2)] == [
        "Westbrook", "Curry", "Lillard"]
    assert [p.player for p in draft_board(GUILD, 2019, None,
                                          "manual:liam")] == [
        "Harden", "Curry"]
    assert [(p.season, p.manager_name) for p in draft_board(
        GUILD, player="Curry")] == [(2016, "Josh"), (2019, "Liam")]
    assert drafted_players(GUILD, "Cur") == ["Curry"]


def _stats_league():
    from harambot.history.matchups_import import (
        parse_matchups_csv, save_matchups)
    hist = ("season,manager,team,finish,draft_pick,wins,losses,ties\n"
            "2024,Josh,Surfer,1,3,2,1,0\n2024,Liam,Tacko,2,1,1,2,0\n"
            "2024,Tom,World,3,2,1,1,0\n"
            "2025,Josh,Surfer,2,1,3,0,0\n2025,Liam,Tacko,1,2,0,3,0\n"
            "2025,Tom,World,3,3,1,1,0\n")
    save_history(GUILD, parse_history_csv(hist)[0])
    m = ("season,week,manager1,team1,score1,manager2,team2,score2,"
         "is_playoffs\n"
         "2024,1,Josh,Surfer,9,Liam,Tacko,0,\n"
         "2024,2,Liam,Tacko,5,Josh,Surfer,4,\n"
         "2024,3,Josh,Surfer,6,Tom,World,3,\n"
         "2024,4,Josh,Surfer,5,Liam,Tacko,4,1\n"
         "2025,1,Josh,Surfer,6,Liam,Tacko,2,\n"
         "2025,2,Josh,Surfer,7,Liam,Tacko,1,\n"
         "2025,3,Tom,World,5,Liam,Tacko,4,\n")
    save_matchups(GUILD, parse_matchups_csv(m)[0])


def test_head_to_head_and_nemesis():
    from harambot.history import stats
    _stats_league()
    h = stats.head_to_head(GUILD, "manual:josh", "manual:liam")
    assert (h["overall"]["w"], h["overall"]["l"]) == (4, 1)
    assert (h["regular"]["w"], h["regular"]["l"]) == (3, 1)
    assert [(g["season"], g["week"]) for g in h["playoff_meetings"]] == [
        (2024, 4)]
    assert h["biggest_win"]["mine"] == 9 and h["biggest_loss"]["week"] == 2
    assert h["streak"] == ("W", 3)
    assert stats.head_to_head(GUILD, "manual:tom", "manual:nobody") is None
    o = stats.opponents(GUILD, "manual:liam", min_games=1)
    assert o["nemesis"]["name"] == "Josh"
    assert o["punching_bag"]["name"] == "Josh"  # only win came vs Josh


def test_profile_chokers_draft_luck_recap():
    from harambot.history import stats
    _stats_league()
    p = stats.profile(GUILD, "manual:josh")
    assert p["titles"] == [2024] and p["finals"] == [2025]
    assert p["sweeps_given"] == 1 and p["sweeps_taken"] == 0
    assert (p["playoff_record"]["w"], p["playoff_record"]["l"]) == (1, 0)
    c = stats.chokers(GUILD)
    assert c["no_title"][0].manager_name == "Josh"   # 3-0, finished 2nd
    d = stats.draft_luck(GUILD)
    assert [(r.season, r.draft_position) for r in d["champions"]] == [
        (2025, 2), (2024, 3)]
    assert d["avg_by_slot"][1] == 2.0   # Liam 2nd, Josh 2nd
    r = stats.season_recap(GUILD, 2024)
    assert [m.manager_name for m in r["standings"]] == ["Josh", "Liam",
                                                        "Tom"]
    assert len(r["sweeps"]) == 1 and r["first_pick"].manager_name == "Liam"
    assert stats.season_recap(GUILD, 1999) is None


def test_playoff_round_names():
    from harambot.history import stats
    from harambot.history.matchups_import import (
        parse_matchups_csv, save_matchups)
    hist = ("season,manager,finish\n2024,A,1\n2024,B,2\n2024,C,3\n"
            "2024,D,4\n2024,E,5\n2024,F,6\n")
    save_history(GUILD, parse_history_csv(hist)[0])
    m = ("season,week,manager1,score1,manager2,score2,is_playoffs\n"
         "2024,18,A,5,B,4,\n"
         "2024,19,C,5,F,4,1\n2024,19,D,5,E,4,1\n"
         "2024,20,A,5,D,4,1\n2024,20,B,5,C,4,1\n2024,20,E,5,F,4,1\n"
         "2024,21,A,5,B,4,1\n2024,21,C,5,D,4,1\n")
    save_matchups(GUILD, parse_matchups_csv(m)[0])

    def rnd(week, x, y):
        return stats.playoff_round(GUILD, 2024, week,
                                   ("manual:" + x, "manual:" + y))
    assert rnd(19, "c", "f") == "Quarter-final"
    assert rnd(20, "a", "d") == "Semi-final"
    assert rnd(20, "e", "f") == "5th-place game"
    assert rnd(21, "a", "b") == "Grand Final"
    assert rnd(21, "c", "d") == "3rd-place game"
    o = stats.opponents(GUILD, "manual:a")
    assert o["rows"] == [] and o["hidden"] == 2 and o["nemesis"] is None


def test_lottery_breakdown():
    entrants = [{"manager_name": "M{}".format(i), "team_name": "T",
                 "balls": lottery.balls_for(i)} for i in range(1, 13)]
    order = lottery.draw(entrants, rng=random.Random(9))
    assert all(1 <= e["ball"] <= e["hopper"] for e in order)
    assert order[0]["hopper"] == 186 and order[-1]["hopper"] == \
        order[-1]["balls"]
    b = lottery.breakdown(order, sims=3000, rng=random.Random(1))
    steps = b["steps"]
    assert steps[-1]["chance"] == 1.0
    assert steps[0]["left"] == 186
    assert abs(sum(s["expected"] for s in steps) - 78) < 1e-6  # 1+..+12
    assert 0 < b["exact_odds"] < 1


def test_trades_import_and_queries():
    from harambot.history.matchups_import import looks_like_matchups
    from harambot.history.drafts_import import looks_like_drafts
    from harambot.history.trades_import import (
        looks_like_trades, parse_trades_csv, save_trades)
    from harambot.history.queries import (
        trade_summary, traded_players, trades)
    text = ("season,date,status,manager1,team1,manager1_gets,manager2,"
            "team2,manager2_gets\n"
            '2018-19,"Mar 10, 6:34 pm",accepted,Josh,Surfer,KD + Capela,'
            "Liam,808s,Russell\n"
            '2018-19,"Nov 7, 9:37 pm",accepted,Michael,Honey,Tatum,'
            "Josh,Surfer,Draymond\n"
            '2018-19,"Nov 7, 5:50 am",accepted,Michael,Honey,Draymond,'
            "Josh,Surfer,Tatum\n"
            '2021-22,"Nov 18, 8:42 pm",vetoed,Josh,Surfer,Bam,'
            "Liam,Tacko,Barnes\n")
    assert looks_like_trades(text)
    assert not looks_like_matchups(text) and not looks_like_drafts(text)
    seasons, errors = parse_trades_csv(text)
    assert errors == []
    # Sorted by date within the season: Nov 7 am, Nov 7 pm, then March.
    assert [r["manager1_gets"] for r in seasons[2018]] == [
        "Draymond", "Tatum", "KD + Capela"]
    assert save_trades(GUILD, seasons) == 4
    assert [t.seq for t in trades(GUILD, season=2018)] == [1, 2, 3]
    assert len(trades(GUILD, player="capela")) == 1
    assert len(trades(GUILD, manager_guid="manual:liam")) == 2
    assert len(trades(GUILD, manager_guid="manual:josh",
                      partner_guid="manual:michael")) == 2
    assert "KD" in traded_players(GUILD, "k")
    made, buddy = trade_summary(GUILD, "manual:josh")
    assert made == 3 and buddy[1] == "Michael" and buddy[2] == 2
    _, bad = parse_trades_csv(
        "season,manager1,manager1_gets,manager2,manager2_gets,status\n"
        "2020,Josh,KD,Liam,,accepted\n2020,Josh,KD,Liam,Bam,maybe\n")
    assert len(bad) == 2


def test_lottery_result_counts_in_draft_luck():
    import datetime
    import json as _json
    from harambot.database.history_models import DraftLottery
    from harambot.history import stats
    from harambot.history.queries import lottery_picks
    _stats_league()
    assert stats.draft_luck(GUILD)["luckiest"] == []   # only 2 drafts each

    def run(order, when):
        DraftLottery.create(
            guild_id=str(GUILD), season=2026, run_by="1",
            run_at=datetime.datetime(2026, 9, when),
            results=_json.dumps([
                {"pick": i, "manager_guid": "manual:" + n.lower(),
                 "manager_name": n} for i, n in enumerate(order, 1)]))
    run(["Josh", "Liam", "Tom"], 1)    # older run, replaced by the next
    run(["Tom", "Josh", "Liam"], 2)
    assert lottery_picks(GUILD)[2026] == {
        "manual:tom": 1, "manual:josh": 2, "manual:liam": 3}
    luck = stats.draft_luck(GUILD)["luckiest"]
    assert [(d["name"], d["seasons"], d["top3"]) for d in luck][0] == (
        "Josh", 3, 3)
    assert {d["name"]: d["picks"] for d in luck}["Tom"] == [2, 3, 1]


def _manual_2024_only():
    lines = ["season,manager,team,finish,draft_pick"]
    for i in range(1, 13):
        lines.append("2024,Mgr{0},Team{0},{0},{1}".format(i, 13 - i))
    save_history(GUILD, parse_history_csv("\n".join(lines) + "\n")[0])


def test_sync_keeps_file_history_and_maps_managers(two_seasons):
    from harambot.database.history_models import ManagerAlias
    from harambot.history.managers_import import (
        looks_like_managers, parse_managers_csv, save_managers)
    _manual_2024_only()
    summaries = sync_league_history(GUILD, two_seasons, full=True)
    assert summaries[1] == {"season": 2024, "skipped": True,
                            "from_files": True}
    assert summaries[0]["unmatched"] == ["Newbie (Rookie)"]
    # 2024 untouched
    s24 = Season.get(Season.season == 2024)
    assert s24.league_key == "manual" and s24.champion_guid == "manual:mgr1"
    assert WeeklyTeamStat.select().where(
        WeeklyTeamStat.season == 2024).count() == 0
    # 2025 from Yahoo, under the managers' existing ids
    guids = {r.manager_guid for r in ManagerSeason.select().where(
        ManagerSeason.season == 2025)}
    assert "manual:mgr5" in guids and "G5" not in guids
    assert "manual:newbie" in guids
    assert ManagerAlias.get(ManagerAlias.yahoo_guid == "G5").manager_guid \
        == "manual:mgr5"
    # Tell it who Newbie is, sync again
    text = "yahoo_name,manager\nNewbie,Mgr12\n"
    assert looks_like_managers(text)
    parsed, errors = parse_managers_csv(text)
    assert errors == [] and save_managers(GUILD, parsed) == 1
    summaries = sync_league_history(GUILD, two_seasons)
    assert summaries[0]["unmatched"] == []
    assert ManagerSeason.get(
        (ManagerSeason.season == 2025) & (ManagerSeason.team_name == "Rookie")
    ).manager_guid == "manual:mgr12"


def test_sync_adds_matchups_and_draft_for_new_seasons(two_seasons):
    from harambot.database.history_models import DraftPick, WeeklyMatchup
    _manual_2024_only()
    cur = two_seasons.yhandler.seasons["2.l.1"]
    cur["settings"].update(draft_status="postdraft", current_week="3")
    cur["draft"] = [
        {"pick": 1, "round": 1, "team_key": "k.t.3", "player_id": 101},
        {"pick": 13, "round": 2, "team_key": "k.t.3", "player_id": 102},
    ]
    two_seasons.player_details = lambda ids: [
        {"player_id": str(i), "name": {"full": "Player {}".format(i)}}
        for i in ids]
    handler = two_seasons.yhandler
    plain = handler.get_scoreboard_raw

    def scoreboard(key, week=None):
        raw = json.loads(json.dumps(plain(key, week)).replace(
            "418.l.15944.t.", "k.t."))
        return raw
    handler.get_scoreboard_raw = scoreboard
    sync_league_history(GUILD, two_seasons)
    games = list(WeeklyMatchup.select().where(WeeklyMatchup.season == 2025))
    assert {g.week for g in games} == {1, 2}
    assert all(g.manager1_guid.startswith("manual:") for g in games)
    assert all(0 <= g.score1 + g.score2 <= 9 for g in games)
    picks = list(DraftPick.select().where(DraftPick.season == 2025)
                 .order_by(DraftPick.round))
    assert [(p.round, p.pick, p.player, p.manager_guid) for p in picks] == [
        (1, 1, "Player 101", "manual:mgr3"),
        (2, 1, "Player 102", "manual:mgr3")]
    assert ManagerSeason.get(
        (ManagerSeason.season == 2025)
        & (ManagerSeason.manager_guid == "manual:mgr3")).draft_position == 1


def _yahoo_trade(ts, a, b, a_gives, b_gives, status="successful"):
    players, i = {}, 0
    for src, dst, names in ((a, b, a_gives), (b, a, b_gives)):
        for n in names:
            players[str(i)] = {"player": [
                [{"player_key": "p.{}".format(i)}, {"name": {"full": n}}],
                {"transaction_data": [{
                    "type": "trade", "source_team_key": src[0],
                    "source_team_name": src[1],
                    "destination_team_key": dst[0],
                    "destination_team_name": dst[1]}]}]}
            i += 1
    players["count"] = i
    return {"transaction_key": "t{}".format(ts), "type": "trade",
            "status": status, "timestamp": str(ts),
            "trader_team_key": a[0], "trader_team_name": a[1],
            "tradee_team_key": b[0], "tradee_team_name": b[1],
            "players": players}


def test_parse_trade_and_trade_rows():
    from harambot.history.sync import trade_rows
    t = _yahoo_trade(1700000000, ("k.t.1", "Surfer"), ("k.t.2", "Tacko"),
                     ["Kyrie Irving"], ["Nikola Vucevic", "Bam Adebayo"])
    p = parsers.parse_trade(t)
    assert p["teams"] == [("k.t.1", "Surfer"), ("k.t.2", "Tacko")]
    assert p["gets"] == {"k.t.1": ["Nikola Vucevic", "Bam Adebayo"],
                         "k.t.2": ["Kyrie Irving"]}
    rows = trade_rows(
        [t, _yahoo_trade(1600000000, ("k.t.3", "X"), ("k.t.1", "Surfer"),
                         ["A"], ["B"]),
         _yahoo_trade(1650000000, ("k.t.3", "X"), ("k.t.2", "Tacko"),
                      ["C"], ["D"], status="pending")],
        lambda key, name: ({"Surfer": "Josh", "Tacko": "Liam"}.get(
            name, "Tom"), name))
    assert [r["manager1"] for r in rows] == ["Tom", "Josh"]   # oldest first
    assert rows[1]["manager1_gets"] == "Nikola Vucevic + Bam Adebayo"
    assert rows[1]["status"] == "accepted"


def test_refresh_current_trades_leaves_file_seasons(two_seasons):
    from harambot.history.queries import trades
    from harambot.history.sync import refresh_current_trades
    _manual_2024_only()
    sync_league_history(GUILD, two_seasons)   # 2025 from Yahoo
    two_seasons.transactions = lambda kind, count: [_yahoo_trade(
        1700000000, ("k.t.1", "New1"), ("k.t.2", "New2"), ["X"], ["Y"])]
    assert refresh_current_trades(GUILD, two_seasons) == 2025
    rows = trades(GUILD, season=2025)
    assert [(r.manager1_guid, r.manager1_gets) for r in rows] == [
        ("manual:mgr1", "Y")]
    # a locked season is never touched
    two_seasons.yhandler.seasons["2.l.1"]["settings"]["season"] = "2024"
    assert refresh_current_trades(GUILD, two_seasons) is None


def test_team_name_history():
    from harambot.history.queries import team_name_history
    _stats_league()
    hist = ("season,manager,team,finish,draft_pick\n"
            "2022,Josh,Old Name,1,1\n2023,Josh,Surfer,1,1\n")
    save_history(GUILD, parse_history_csv(hist)[0])
    assert team_name_history(GUILD, "manual:josh") == [
        ("Old Name", 2022, 2022), ("Surfer", 2023, 2025)]


def test_draft_feed_picks_and_notes(two_seasons):
    from harambot.database.history_models import DraftPick
    from harambot.history import draft_feed
    _manual_2024_only()
    DraftPick.create(guild_id=str(GUILD), season=2023, round=2, pick=1,
                     manager_guid="manual:mgr3", manager_name="Mgr3",
                     player="Paul George")
    DraftPick.create(guild_id=str(GUILD), season=2024, round=1, pick=5,
                     manager_guid="manual:mgr3", manager_name="Mgr3",
                     player="Paul George")
    DraftPick.create(guild_id=str(GUILD), season=2024, round=4, pick=1,
                     manager_guid="manual:mgr5", manager_name="Mgr5",
                     player="Jokic")
    league = two_seasons
    league.yhandler.seasons["2.l.1"]["settings"]["draft_status"] = "inprogress"
    league.draft_results = lambda: [
        {"pick": 1, "round": 1, "team_key": "k.t.3", "player_id": 7},
        {"pick": 2, "round": 1, "team_key": "k.t.1", "player_id": 8},
        {"pick": 3, "round": 1, "team_key": "k.t.2"},   # on the clock
    ]
    league.teams = lambda: {}
    league.player_details = lambda ids: [
        {"player_id": "7", "name": {"full": "Paul George"},
         "editorial_team_abbr": "PHI", "display_position": "SF"},
        {"player_id": "8", "name": {"full": "Jokic"},
         "editorial_team_abbr": "DEN", "display_position": "C"}]
    sync_league_history(GUILD, league)   # teams for 2025
    season, picks, finished = draft_feed.new_picks(GUILD, league, 0)
    assert season == 2025 and not finished
    assert [(p["overall"], p["manager_guid"]) for p in picks] == [
        (1, "manual:mgr3"), (2, "manual:mgr1")]
    _, again, _ = draft_feed.new_picks(GUILD, league, 2)
    assert again == []
    notes = draft_feed.pick_notes(GUILD, 2025, picks[0])
    assert notes[0].startswith("❤️ Mgr3 has drafted Paul George 2 times")
    notes = draft_feed.pick_notes(GUILD, 2025, picks[1])
    assert notes == ["📜 Last year: Mgr5's round 4 pick, 3 rounds earlier "
                     "this time"]
    text = draft_feed.format_pick(picks[0], ["x"], "<@1>")
    assert text.splitlines()[0] == "**━━ Round 1 ━━**"
    assert "**#1** Mgr1" not in text and "takes **Paul George**" in text
    draft_feed.save_pick(GUILD, 2025, picks[0])
    assert DraftPick.select().where(DraftPick.season == 2025).count() == 1
