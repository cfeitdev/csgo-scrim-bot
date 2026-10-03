"""Elo maths. Pure functions -- persistence lives in :mod:`scrimbot.store`."""

from __future__ import annotations

from typing import Sequence

DEFAULT_K = 32


def expected_score(rating: float, opponent_rating: float) -> float:
    return 1 / (1 + 10 ** ((opponent_rating - rating) / 400))


def team_deltas(
    winner_ratings: Sequence[int],
    loser_ratings: Sequence[int],
    *,
    k: int = DEFAULT_K,
    tie: bool = False,
) -> tuple[int, int]:
    """Elo change for each member of the winning / losing team.

    Uses the team averages so a stacked team gains less for beating a weak one.
    Returns ``(winner_delta, loser_delta)``.
    """
    if not winner_ratings or not loser_ratings:
        return 0, 0
    win_avg = sum(winner_ratings) / len(winner_ratings)
    lose_avg = sum(loser_ratings) / len(loser_ratings)
    expected = expected_score(win_avg, lose_avg)
    score = 0.5 if tie else 1.0
    winner_delta = round(k * (score - expected))
    loser_delta = round(k * ((1 - score) - (1 - expected)))
    return winner_delta, loser_delta


def balance_teams(players: Sequence, key=lambda p: p.elo) -> tuple[list, list]:
    """Snake-draft the roster by rating into two even, Elo-balanced teams."""
    ordered = sorted(players, key=key, reverse=True)
    teams: tuple[list, list] = ([], [])
    for index, player in enumerate(ordered):
        # A, B, B, A, A, B, B, A ... keeps the running totals close
        teams[0 if index % 4 in (0, 3) else 1].append(player)
    # Correct any imbalance caused by an odd roster size.
    while len(teams[0]) > len(teams[1]) + 1:
        teams[1].append(teams[0].pop())
    while len(teams[1]) > len(teams[0]) + 1:
        teams[0].append(teams[1].pop())
    return teams[0], teams[1]
