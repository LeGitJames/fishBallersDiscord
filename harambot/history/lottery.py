"""Weighted draft lottery.

League rule: last season's pick #1 gets 10 balls, #2 gets 11, ... #12 gets
21. The consolation-bracket champion gets one bonus ball. Picks are drawn
one at a time; once a team is drawn, all its balls leave the hopper.
"""

import secrets

BASE_BALLS = 10
CONSOLATION_BONUS = 1


def balls_for(previous_pick, is_consolation_winner=False):
    """``previous_pick`` is 1-based. ``None`` means the manager had no pick
    last year (e.g. a new manager); callers should set one explicitly."""
    if previous_pick is None:
        raise ValueError("previous_pick is required")
    balls = BASE_BALLS + (int(previous_pick) - 1)
    if is_consolation_winner:
        balls += CONSOLATION_BONUS
    return balls


def first_pick_odds(entrants):
    """entrants: list of dicts with a "balls" key. Returns list of
    (entrant, probability of landing pick #1)."""
    total = sum(e["balls"] for e in entrants)
    return [(e, e["balls"] / total) for e in entrants]


def draw(entrants, rng=None):
    """Run the lottery. Returns the entrants in draft order, each with a
    "pick" key added. Uses a cryptographically secure RNG by default so
    nobody can argue it was rigged."""
    rng = rng or secrets.SystemRandom()
    hopper = [dict(e) for e in entrants]
    order = []
    while hopper:
        total = sum(e["balls"] for e in hopper)
        ticket = rng.randrange(total)
        running = 0
        for i, e in enumerate(hopper):
            running += e["balls"]
            if ticket < running:
                chosen = hopper.pop(i)
                chosen["pick"] = len(order) + 1
                order.append(chosen)
                break
    return order


def reveal_script(order, delay):
    """Messages for revealing a finished draw, last pick first.

    Returns a list of (seconds_to_wait_before, message). The top three
    picks get a drumroll teaser and a longer pause before the name.
    """
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    long_pause = delay * 1.5
    by_pick = sorted(order, key=lambda e: e["pick"], reverse=True)

    def who(e):
        text = "**{}** ({})".format(e["manager_name"], e["team_name"])
        if e.get("discord_id"):
            text += " <@{}>".format(e["discord_id"])
        return text

    steps = []
    for e in by_pick:
        p = e["pick"]
        if p > 3:
            steps.append((delay, "🔹 Pick **#{}**: {} · had {} balls".format(
                p, who(e), e["balls"])))
            continue
        if p == min(3, len(order)) and len(order) > 1:
            # Alphabetical, so the list doesn't give away the order
            left = sorted(
                (x for x in order if x["pick"] <= p),
                key=lambda x: x["manager_name"].lower(),
            )
            names = [
                "<@{}>".format(x["discord_id"]) if x.get("discord_id")
                else x["manager_name"]
                for x in left
            ]
            names_text = (", ".join(names[:-1]) + " and " + names[-1]
                          if len(names) > 1 else names[0])
            steps.append((delay, "😬 **{} teams left:** {}. One of you is "
                          "getting the #1 pick…".format(len(left),
                                                        names_text)))
        if p == 1 and len(order) > 1:
            teaser = "🥁 …which means the **#1 pick** goes to…"
        else:
            teaser = "🥁 Pick **#{}** goes to…".format(p)
        steps.append((delay, teaser))
        reveal = "{} {}!".format(medals[p], who(e))
        if p == 1:
            reveal = "🎉 " + reveal + " 🎉"
        steps.append((long_pause, reveal))
    return steps
