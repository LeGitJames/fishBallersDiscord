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
