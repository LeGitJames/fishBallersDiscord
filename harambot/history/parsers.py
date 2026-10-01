"""Pure functions that turn raw Yahoo Fantasy JSON into flat rows.

No network or database access here, so these are easy to unit test against
saved API responses.
"""

# Yahoo stat ids for NBA head-to-head category leagues.
NBA_STATS = {
    "5": "FG%",
    "8": "FT%",
    "10": "3PTM",
    "12": "PTS",
    "15": "REB",
    "16": "AST",
    "17": "STL",
    "18": "BLK",
    "19": "TO",
}
# For these a lower number is the better week.
LOWER_IS_BETTER = {"19"}


def renew_to_league_key(renew):
    """Yahoo stores the previous season as ``"388_27081"``."""
    if not renew or "_" not in str(renew):
        return None
    game_id, league_id = str(renew).split("_", 1)
    return "{}.l.{}".format(game_id, league_id)


def _merge_team_meta(team_meta):
    """A team's first element is a list of single-key dicts (and empty
    lists). Merge them into one dict."""
    merged = {}
    for item in team_meta:
        if isinstance(item, dict):
            merged.update(item)
    return merged


def _manager(meta):
    managers = meta.get("managers") or []
    for m in managers:
        mgr = m.get("manager", {})
        if mgr.get("guid"):
            return mgr.get("guid"), mgr.get("nickname") or "Unknown"
    # Hidden/departed managers have no guid; fall back to the team slot.
    return "team:" + str(meta.get("team_id")), meta.get("name", "Unknown")


def _numbered(container):
    """Yield the values of Yahoo's {"0": ..., "1": ..., "count": n} dicts
    in order."""
    if isinstance(container, list):
        for c in container:
            yield c
        return
    i = 0
    while str(i) in container:
        yield container[str(i)]
        i += 1


def parse_standings(raw):
    """Return one dict per team from a raw ``/standings`` response."""
    league = raw["fantasy_content"]["league"]
    standings = league[1]["standings"]
    teams_container = standings[0]["teams"] if isinstance(
        standings, list) else standings["0"]["teams"]
    rows = []
    for entry in _numbered(teams_container):
        team = entry["team"]
        meta = _merge_team_meta(team[0])
        extra = {}
        for part in team[1:]:
            if isinstance(part, dict):
                extra.update(part)
        ts = extra.get("team_standings", {})
        outcomes = ts.get("outcome_totals", {})
        guid, name = _manager(meta)
        rank = ts.get("rank")
        rows.append(
            {
                "team_key": meta["team_key"],
                "team_id": str(meta.get("team_id")),
                "team_name": meta.get("name", ""),
                "manager_guid": guid,
                "manager_name": name,
                "final_rank": int(rank) if rank not in (None, "") else None,
                "wins": int(outcomes.get("wins") or 0),
                "losses": int(outcomes.get("losses") or 0),
                "ties": int(outcomes.get("ties") or 0),
            }
        )
    return rows


def _to_float(value):
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_scoreboard(raw):
    """Return one dict per team per stat from a raw ``/scoreboard``
    response. Only completed matchups ("postevent") are included."""
    league = raw["fantasy_content"]["league"]
    scoreboard = league[1]["scoreboard"]
    matchups = scoreboard["0"]["matchups"]
    rows = []
    for m in _numbered(matchups):
        matchup = m["matchup"]
        if matchup.get("status") != "postevent":
            continue
        week = int(matchup["week"])
        is_playoffs = str(matchup.get("is_playoffs", "0")) == "1"
        for t in _numbered(matchup["0"]["teams"]):
            team = t["team"]
            meta = _merge_team_meta(team[0])
            guid, mgr_name = _manager(meta)
            stats = team[1].get("team_stats", {}).get("stats", [])
            for s in stats:
                stat = s.get("stat", {})
                value = _to_float(stat.get("value"))
                if value is None:  # skips "61/128" style FGM/FGA rows
                    continue
                rows.append(
                    {
                        "week": week,
                        "is_playoffs": is_playoffs,
                        "team_key": meta["team_key"],
                        "team_name": meta.get("name", ""),
                        "manager_guid": guid,
                        "manager_name": mgr_name,
                        "stat_id": str(stat.get("stat_id")),
                        "value": value,
                    }
                )
    return rows


def first_round_order(draft_results, team_key_to_guid):
    """Map manager GUID -> first-round pick number from
    ``League.draft_results()`` output."""
    order = {}
    for pick in draft_results:
        if int(pick.get("round", 0)) != 1:
            continue
        guid = team_key_to_guid.get(pick.get("team_key"))
        if guid and guid not in order:
            order[guid] = int(pick["pick"])
    return order


def parse_matchup_results(raw):
    """One dict per completed matchup in a raw ``/scoreboard`` response:
    week, is_playoffs (playoff or consolation game), the two team keys and
    names, and how many categories each team won."""
    league = raw["fantasy_content"]["league"]
    matchups = league[1]["scoreboard"]["0"]["matchups"]
    out = []
    for m in _numbered(matchups):
        matchup = m["matchup"]
        if matchup.get("status") != "postevent":
            continue
        teams = []
        for t in _numbered(matchup["0"]["teams"]):
            meta = _merge_team_meta(t["team"][0])
            teams.append((meta["team_key"], meta.get("name", "")))
        if len(teams) != 2:
            continue
        wins = {key: 0 for key, _ in teams}
        for w in matchup.get("stat_winners") or []:
            winner = (w.get("stat_winner") or {}).get("winner_team_key")
            if winner in wins:
                wins[winner] += 1
        out.append({
            "week": int(matchup["week"]),
            "is_playoffs": str(matchup.get("is_playoffs", "0")) == "1"
            or str(matchup.get("is_consolation", "0")) == "1",
            "teams": teams,
            "wins": wins,
        })
    return out


def _transaction_data(player_entry):
    """The transaction_data dict from one player in a transaction."""
    parts = player_entry if isinstance(player_entry, list) else [player_entry]
    for part in parts:
        if isinstance(part, dict) and "transaction_data" in part:
            data = part["transaction_data"]
            if isinstance(data, list):
                data = data[0] if data else {}
            return data
    return {}


def _player_name(player_entry):
    meta = player_entry[0] if isinstance(player_entry, list) else []
    if isinstance(meta, dict):
        meta = [meta]
    for part in meta:
        if isinstance(part, dict) and "name" in part:
            name = part["name"]
            return name.get("full") if isinstance(name, dict) else name
    return None


def parse_trade(t):
    """Normalise one trade from ``League.transactions("trade", "")``.

    Returns {key, timestamp, status, teams: [(team_key, team_name), ...],
    gets: {team_key: [player names]}} or None if it isn't a trade."""
    if t.get("type") not in (None, "trade"):
        return None
    gets, names = {}, {}
    players = t.get("players") or {}
    for p in _numbered(players) if isinstance(players, dict) else players:
        entry = p.get("player") if isinstance(p, dict) else None
        if entry is None:
            continue
        data = _transaction_data(entry)
        dest = data.get("destination_team_key")
        name = _player_name(entry)
        if not dest or not name:
            continue
        names[dest] = data.get("destination_team_name") or names.get(dest)
        src = data.get("source_team_key")
        if src:
            names.setdefault(src, data.get("source_team_name"))
        gets.setdefault(dest, []).append(name)
    trader = t.get("trader_team_key")
    tradee = t.get("tradee_team_key")
    if trader:
        names.setdefault(trader, t.get("trader_team_name"))
    if tradee:
        names.setdefault(tradee, t.get("tradee_team_name"))
    keys = [k for k in (trader, tradee) if k] or list(names)
    if len(keys) < 2:
        return None
    return {
        "key": t.get("transaction_key"),
        "timestamp": int(t.get("timestamp") or 0),
        "status": t.get("status", "successful"),
        "teams": [(k, names.get(k) or "") for k in keys[:2]],
        "gets": {k: gets.get(k, []) for k in keys[:2]},
    }
